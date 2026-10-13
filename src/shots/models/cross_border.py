"""EXPERIMENTAL model for cross-border competitions (UEFA Champions League): the production no-odds
model applied to each club's domestic form. Evidence and limits: reports/2026-10-13_champions_league.md.

There is no shot history for these competitions, so nothing here is fitted on them. ``CrossBorderModel``
wraps a fitted ``NegBinCal`` (the ``negbin_pit_comp`` no-odds model) and changes only what an unseen,
mixed-league competition requires:

1. Inputs: rows from ``shots.features.cross_border`` (each club's domestic features, the league level =
   mean of the two domestic league priors, ``tier_change`` = 0, no market features).
2. Clubs without data (``{side}_known`` False): the predictive distribution is the average over
   ``n_profiles`` real team profiles (that side's rate features as ratios to the league prior, sampled
   once from recent top-tier matches), i.e. the model's distribution marginalised over "which kind
   of team is this". The mean stays near a league-average opponent and the spread widens by the
   between-team variation. Both unknown: independent profiles on both sides.
3. Unseen competition: the level of an unseen competition is uncertain. The mean is multiplied by
   exp(level_sd * z - level_sd^2 / 2), z ~ N(0, 1) (Gauss-Hermite, both teams together), which widens
   the distribution without moving its mean. ``level_sd`` comes from the leave-one-league-out
   backtest on the validation period (never the test period).
4. ``split_sd``: the same for the home / away split (home * exp(s u), away * exp(-s u)): how a
   club's domestic rates translate against a club of another league is unknown. It mostly widens the
   per-team curves; the total is close to unaffected.
5. Calibration: the base model's GLOBAL PIT levels / map (the competition is not in its
   per-competition tables). This is a fallback, not evidence of calibration on the competition.

Outputs follow the production contract (``predict``, ``prob_at_least``, ``team_predict``,
``team_prob_at_least``), so the board and scan code treat it like any routed model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shots.models.base import CONFIDENCES, ShotModel
from shots.models.calibration import cdf_at, lb_at_level
from shots.models.common import SIDES, finalize, lb_from_cdf
from scipy.special import gammaln

from shots.models.negbin import MAX_SIDE, MAX_TOTAL, RATE_COLS, SOT_COLS

# Validation-period estimates (reports/2026-10-13_champions_league.md); do not tune on test data.
LEVEL_SD = 0.023
SPLIT_SD = 0.134
N_PROFILES = 100
PROFILE_SEED = 20261013
PROFILE_SEASONS = 3          # latest season labels the profiles are drawn from
RATIO_COLS = sorted({name for _, name in RATE_COLS + SOT_COLS}
                    | {name.replace("_for_", "_against_") for _, name in RATE_COLS + SOT_COLS}
                    | {name.replace("_against_", "_for_") for _, name in RATE_COLS + SOT_COLS})
RAW_COLS = ["rest_days", "n_prior_season"]
GH_POINTS = 3
MIXTURE_CHUNK = 20       # fixtures per batch when unknown clubs are expanded into profiles


def _gauss_hermite(sd: float, points: int = GH_POINTS) -> tuple[np.ndarray, np.ndarray]:
    """(nodes, weights) of N(0, sd^2); a single node 0 when sd == 0."""
    if not sd:
        return np.zeros(1), np.ones(1)
    x, w = np.polynomial.hermite_e.hermegauss(points)
    return sd * x, w / w.sum()


def fast_nb_pmf(mu: np.ndarray, alpha, kmax: int) -> np.ndarray:
    """NB2 pmf on 0..kmax (rows = scenarios), same parametrisation as ``negbin.nb_pmf`` (alpha -> 0 gives
    Poisson) but computed directly from log-gamma: about ten times faster, equal to ~1e-13. The mixtures
    here evaluate it on (fixtures x profiles x spread nodes) rows."""
    mu = np.maximum(np.asarray(mu, float), 1e-12)[:, None]
    alpha = np.broadcast_to(np.asarray(alpha, float), (mu.shape[0],))[:, None]
    k = np.arange(kmax + 1, dtype=float)[None, :]
    pois = alpha < 1e-8
    n = 1.0 / np.where(pois, 1.0, alpha)
    out = np.exp(gammaln(k + n) - gammaln(n) - gammaln(k + 1.0) + n * np.log(n / (n + mu)) + k * np.log(mu / (n + mu)))
    if pois.any():
        out = np.where(pois, np.exp(k * np.log(mu) - mu - gammaln(k + 1.0)), out)
    return out


def total_pmf(nb, mh: np.ndarray, ma: np.ndarray) -> np.ndarray:
    """``nb.pmf_from_mu`` (the fitted model's own total distribution), fast when it is a single NB."""
    if nb.mode_ != "moment":
        return nb.pmf_from_mu(mh, ma)
    mu, alpha_t = nb.moment_params(mh, ma)
    return fast_nb_pmf(mu, alpha_t, MAX_TOTAL)


def make_profiles(played: pd.DataFrame, k: int = N_PROFILES, seed: int = PROFILE_SEED,
                  seasons: int = PROFILE_SEASONS) -> dict[str, pd.DataFrame]:
    """{side: k rows of real team profiles}: that side's rate features divided by the match's league
    prior (``RATIO_COLS``) plus ``RAW_COLS``. Drawn (fixed seed, sorted by match_id, so the result is
    deterministic) from top-tier matches of the latest ``seasons`` season labels with complete features."""
    labels = sorted(played["season"].astype(str).unique())[-seasons:]
    pool = played[played["season"].astype(str).isin(labels)]
    if "tier" in pool and (pool["tier"] == 1).any():
        pool = pool[pool["tier"] == 1]
    out = {}
    for j, side in enumerate(SIDES):
        cols = [f"{side}_{c}" for c in RATIO_COLS + RAW_COLS]
        ok = pool.dropna(subset=[*cols, "league_prior_shots"]).sort_values("match_id")
        if ok.empty:
            raise ValueError("no complete rows to draw team profiles from")
        take = np.random.default_rng(seed + j).choice(len(ok), size=k, replace=len(ok) < k)
        sub = ok.iloc[np.sort(take)]
        prof = pd.DataFrame({c: sub[f"{side}_{c}"].to_numpy(float) / sub["league_prior_shots"].to_numpy(float)
                             for c in RATIO_COLS})
        for c in RAW_COLS:
            prof[c] = sub[f"{side}_{c}"].to_numpy(float)
        out[side] = prof.reset_index(drop=True)
    return out


class CrossBorderModel(ShotModel):
    name = "cross_border"

    def __init__(self, level_sd: float = LEVEL_SD, split_sd: float = SPLIT_SD, n_profiles: int = N_PROFILES,
                 seed: int = PROFILE_SEED, team_curves: bool = True):
        self.level_sd = level_sd
        self.split_sd = split_sd
        self.n_profiles = n_profiles
        self.seed = seed
        self.team_curves = team_curves

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_base(cls, base, played: pd.DataFrame, **kw) -> "CrossBorderModel":
        """Wrap an already fitted NegBinCal; profiles are drawn from ``played`` (feature rows)."""
        model = cls(**kw)
        model.base_ = base
        return model.fit(played)

    def fit(self, train: pd.DataFrame) -> "CrossBorderModel":
        """Draws the team profiles only; the wrapped model (``base_``) must be fitted already."""
        if not hasattr(self, "base_"):
            raise RuntimeError("CrossBorderModel wraps a fitted model: use CrossBorderModel.from_base")
        self.profiles_ = make_profiles(train, self.n_profiles, self.seed)
        self.confidences = tuple(self.base_.confidences)
        return self

    def __getstate__(self):
        state = dict(self.__dict__)
        state.pop("_memo_", None)
        return state

    @property
    def group(self):
        return self.base_.group

    @property
    def has_team_calibration(self) -> bool:
        return self.team_curves and getattr(self.base_, "has_team_calibration", False)

    # ------------------------------------------------------------------ distributions

    @staticmethod
    def _known(rows: pd.DataFrame, side: str) -> np.ndarray:
        if f"{side}_known" in rows:
            return rows[f"{side}_known"].fillna(False).to_numpy(bool)
        return rows[f"{side}_shots_for_ewm"].notna().to_numpy()

    def _expand(self, rows: pd.DataFrame, unknown: tuple[bool, bool]) -> tuple[pd.DataFrame, int]:
        """Scenario rows for fixtures sharing the same unknown sides: S = 1 (both known) or
        n_profiles copies of each row with the unknown side(s) replaced by profile i (the away
        side uses a fixed shuffle of the profiles, so two unknown sides are paired independently)."""
        if not any(unknown):
            return rows.reset_index(drop=True), 1
        k = self.n_profiles
        out = rows.iloc[np.repeat(np.arange(len(rows)), k)].reset_index(drop=True)
        prior = out["league_prior_shots"].to_numpy(float)
        order = {"home": np.arange(k), "away": np.random.default_rng(self.seed + 7).permutation(k)}
        for side, miss in zip(SIDES, unknown):
            if not miss:
                continue
            prof = self.profiles_[side].iloc[order[side]]
            for c in RATIO_COLS:
                out[f"{side}_{c}"] = np.tile(prof[c].to_numpy(float), len(rows)) * prior
            for c in RAW_COLS:
                out[f"{side}_{c}"] = np.tile(prof[c].to_numpy(float), len(rows))
            out[f"{side}_tier_change"] = 0.0
        return out, k

    def _cdfs(self, rows: pd.DataFrame) -> dict[str, np.ndarray]:
        """{"total" | "home" | "away": CDF array per row}, memoised for the last rows object."""
        kh, ka = self._known(rows, "home"), self._known(rows, "away")
        used = [c for c in rows.columns if c == "league_prior_shots" or c.startswith(("home_", "away_"))
                and pd.api.types.is_numeric_dtype(rows[c])]
        key = (len(rows), kh.tobytes(), ka.tobytes(),
               int(pd.util.hash_pandas_object(rows[used], index=False).to_numpy().sum(dtype=np.uint64)))
        memo = getattr(self, "_memo_", None)
        if memo is not None and memo[0] == key:
            return memo[1]
        nb = self.base_.nb_
        zl, wl = _gauss_hermite(self.level_sd)
        zs, ws = _gauss_hermite(self.split_sd)
        # mean-preserving multipliers: exp(z - sd^2 / 2)
        cl = np.exp(zl - self.level_sd ** 2 / 2)
        ch, ca = np.exp(zs - self.split_sd ** 2 / 2), np.exp(-zs - self.split_sd ** 2 / 2)
        mult_h, mult_a = np.outer(cl, ch).ravel(), np.outer(cl, ca).ravel()
        weight = np.outer(wl, ws).ravel()
        kmax = {"total": total_pmf(nb, np.ones(1), np.ones(1)).shape[1], "home": MAX_SIDE + 1, "away": MAX_SIDE + 1}
        pmf = {name: np.zeros((len(rows), n)) for name, n in kmax.items()}
        pos = np.arange(len(rows))
        for unknown in ((False, False), (True, False), (False, True), (True, True)):
            mask = (~kh == unknown[0]) & (~ka == unknown[1])
            if not mask.any():
                continue
            idx = pos[mask]
            step = len(idx) if not any(unknown) else MIXTURE_CHUNK  # bound the memory of the scenario rows
            for start in range(0, len(idx), step):
                part = idx[start:start + step]
                sub, s = self._expand(rows.iloc[part], unknown)
                mh, ma = nb.side_mu(sub, "home"), nb.side_mu(sub, "away")
                acc = {name: np.zeros((len(sub), n)) for name, n in kmax.items()}
                for fh, fa, w in zip(mult_h, mult_a, weight):
                    acc["total"] += w * total_pmf(nb, mh * fh, ma * fa)
                    acc["home"] += w * fast_nb_pmf(mh * fh, nb.sides_["home"]["alpha"], MAX_SIDE)
                    acc["away"] += w * fast_nb_pmf(ma * fa, nb.sides_["away"]["alpha"], MAX_SIDE)
                for name in pmf:
                    pmf[name][part] = acc[name].reshape(len(part), s, -1).mean(axis=1)
        out = {name: np.minimum(np.cumsum(p, axis=1), 1.0) for name, p in pmf.items()}
        self._memo_ = (key, out)
        return out

    def _groups(self, rows: pd.DataFrame):
        return rows[self.group] if self.group else None

    # ------------------------------------------------------------------ total shots

    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        cdf = self._cdfs(rows)["total"]
        median = lb_from_cdf(cdf, 0.5).astype(float)
        bounds = {q: lb_at_level(cdf, self.base_._levels(rows, q)) for q in confidences}
        return finalize(rows.index, median, bounds)

    def prob_at_least(self, rows: pd.DataFrame, n) -> np.ndarray:
        cdf = self._cdfs(rows)["total"]
        n = np.broadcast_to(np.asarray(n), (len(rows),)).astype(int)
        return 1.0 - self.base_.pit_map_(cdf_at(cdf, n - 1), self._groups(rows))

    def mean(self, rows: pd.DataFrame) -> np.ndarray:
        """Expected total shots of the (uncalibrated) mixture distribution."""
        cdf = self._cdfs(rows)["total"]
        return (1.0 - cdf).sum(axis=1)

    # ------------------------------------------------------------------ per-team shots

    def team_predict(self, rows: pd.DataFrame, side: str, confidences=CONFIDENCES) -> pd.DataFrame:
        base = self.base_
        base._require_team()
        cdf = self._cdfs(rows)[side]
        median = lb_at_level(cdf, base._team_levels(rows, side, 0.5)).astype(float)
        bounds = {q: lb_at_level(cdf, base._team_levels(rows, side, q)) for q in confidences}
        out = finalize(rows.index, median, bounds)
        g = np.repeat(np.asarray(rows[self.group]), MAX_SIDE) if self.group else None
        G = base.team_pit_map_[side](cdf[:, :MAX_SIDE].ravel(), g).reshape(len(rows), MAX_SIDE)
        out["mean"] = (1.0 - G).sum(axis=1)
        return out

    def team_prob_at_least(self, rows: pd.DataFrame, side: str, n) -> np.ndarray:
        base = self.base_
        base._require_team()
        cdf = self._cdfs(rows)[side]
        n = np.broadcast_to(np.asarray(n), (len(rows),)).astype(int)
        return 1.0 - base.team_pit_map_[side](cdf_at(cdf, n - 1), self._groups(rows))


__all__ = ["CrossBorderModel", "make_profiles", "LEVEL_SD", "SPLIT_SD", "N_PROFILES"]
