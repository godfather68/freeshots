"""Structural model candidates (2026-10-15). NOT production: production is ``negbin_pit_comp``.
Evidence and the adoption decision: reports/2026-10-15_model_candidates.md.

Every candidate is a *base* distributional model (raw total CDF + per-side CDFs) wrapped in exactly
the production calibration (``NegBinPitComp``: per-competition PIT recalibration on the latest two
training seasons, scored by a base fitted before them, then refitted on all rows). Only the base
changes, so differences in the leaderboard are differences of structure.

Bases (all use the production design ``negbin._Design`` per side, so the inputs are the same):

``FlexNB`` -- the production two-sided NB, re-estimated with ``nbmle.fit_nb`` so that it can carry
  * a dispersion submodel per side: log alpha_s = gamma' z, z = competition dummies (ridge) and/or
    log of the side mean (NB-P: Var = mu + a mu^(2 + g));
  * a total dispersion submodel (``total="alpha_z"``): T ~ NB(mu_h + mu_a, alpha_T(z)) with z from
    {competition (ridge), log total mean, closeness |log(mu_h / mu_a)|, P(over 2.5) (centred,
    missing -> 0 + flag), early-season}; fitted by MLE on the total with the mean held fixed. This
    replaces the global home/away correlation of production (``total="moment"``) by a dispersion
    that varies with the type of match. ``total="rho_z"``: keep the moment construction but let the
    correlation vary, rho = tanh(d' [1, closeness, logmu]).
  * recency weights (half-life in days) for every coefficient and dispersion;
  * competition intercepts (ridge) in the mean (with recency weights: per-competition level drift).
  With the defaults (no submodel, no weights, ``total="moment"``) it is the production NB up to the
  optimiser tolerance.

``JointShare`` -- total and split modelled jointly: the total is the ``FlexNB`` total, the home share
  of a match with T shots is BetaBinomial(T, p, phi), logit p = d' [1, log(mu_h / mu_a), comp ...];
  per-team curves are mixtures over T (their dependence is that of the total, not independence).

``GBMNB`` -- LightGBM means per side (Poisson objective), optionally boosted from the GLM log mean
  (``offset=True``); early stopping on the latest season carved from the rows it is given; the total
  dispersion submodel is then fitted on that held-out season with out-of-sample means (in-sample GBM
  means would understate the dispersion); finally the boosters are refitted on all rows with the
  chosen number of rounds. Hyper-parameters are a small fixed grid picked on validation seasons.

``Pool`` -- linear pool (``kind="linear"``) or quantile averaging (``kind="quantile"``) of two bases
  with a fixed weight chosen on validation seasons; the pooled CDF is then PIT-recalibrated.

None of these bases has ``sides_``, so ``explain`` reports them as unsupported (it explains NB GLMs only)
rather than produce a wrong decomposition; the cross-border wrapper needs an NB as well. Both would need
work before a candidate could replace production.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import betaln, gammaln

from shots.models.calibrated import NegBinPitComp
from shots.models.common import FEATURES, SIDES, carve_calibration, clean_train
from shots.models.negbin import MAX_SIDE, MAX_TOTAL, _Design
from shots.models.nbmle import fit_nb, recency_weights

log = logging.getLogger(__name__)

TOTAL_Z = ("comp", "logmu", "close", "o25", "early")

# ---------------------------------------------------------------------- candidate feature families (phase 2)
# Columns of data/processed/features_candidates.parquet (shots.features.candidates), merged on match_id.
# Rule 1 (coordinator, 2026-10-15): never ``*_sched_days_to_next`` nor the Champions League ``*_sched_ucl_*``.
FORBIDDEN = ("sched_days_to_next", "sched_ucl_")
FAMILIES = ("A", "E", "F", "G", "D", "C", "B")


def _num(rows: pd.DataFrame, col: str) -> pd.Series:
    if any(f in col for f in FORBIDDEN):
        raise ValueError(f"{col} is not allowed as a pre-match feature (rule 1)")
    if col not in rows:
        raise KeyError(f"candidate feature {col} missing: merge data/processed/features_candidates.parquet")
    return rows[col].astype(float)


def family_columns(rows: pd.DataFrame, side: str, fam: str) -> dict[str, pd.Series]:
    """Raw (unfilled) design columns of one family for the model of ``side`` (s = side, o = opponent)."""
    o = "away" if side == "home" else "home"
    out: dict[str, pd.Series] = {}
    if fam == "A":  # opponent/venue-adjusted ratings (EKF)
        out["A_log_exp"] = np.log(_num(rows, f"adj_exp_{side}_shots").clip(lower=1.0))
        out["A_att_s"] = _num(rows, f"{side}_adj_att")
        out["A_def_o"] = _num(rows, f"{o}_adj_def")
        out["A_sot_att_s"] = _num(rows, f"{side}_adj_sot_att")
        out["A_sot_def_o"] = _num(rows, f"{o}_adj_sot_def")
        out["A_sd"] = _num(rows, f"{side}_adj_att_sd") + _num(rows, f"{o}_adj_def_sd")
    elif fam == "E":  # head to head, relative to the rating expectation
        tot = _num(rows, "h2h_total_shrunk")
        out["E_log_h2h"] = np.log(tot.clip(lower=1.0)) - np.log(_num(rows, "adj_exp_total_shots").clip(lower=1.0))
        out["E_dev"] = _num(rows, "h2h_dev_shrunk")
    elif fam == "F":  # referee (England / Scotland only)
        out["F_ref_dev"] = _num(rows, "ref_shots_dev")
    elif fam == "G":  # season context
        out["G_dead_n"] = _num(rows, "ctx_dead_n")
        out["G_progress"] = _num(rows, "ctx_progress")
        out["G_gap_safety_s"] = _num(rows, f"{side}_ctx_gap_safety").clip(-30, 30)
        # season end (team's last three league matches; the season's fixture count is fixed in advance)
        out["G_last3_s"] = (_num(rows, f"{side}_ctx_games_left") <= 3).astype(float).where(
            rows[f"{side}_ctx_games_left"].notna())
        out["G_last3_o"] = (_num(rows, f"{o}_ctx_games_left") <= 3).astype(float).where(
            rows[f"{o}_ctx_games_left"].notna())
    elif fam == "D":  # congestion
        out["D_n14_s"] = _num(rows, f"{side}_sched_n14")
        out["D_n14_o"] = _num(rows, f"{o}_sched_n14")
        out["D_sunmon_s"] = _num(rows, f"{side}_sched_sunmon_share_l8")
    elif fam == "C":  # market structure beyond 1X2 / O2.5
        out["C_total_goals"] = _num(rows, "mktx_total_goals")
        out["C_abs_gd"] = _num(rows, "mktx_abs_gd")
    elif fam == "B":  # style volume proxies
        out["B_corners_s"] = np.log1p(_num(rows, f"{side}_style_corners_for_ewm"))
        out["B_corners_o"] = np.log1p(_num(rows, f"{o}_style_corners_against_ewm"))
    else:
        raise ValueError(f"unknown family {fam!r}")
    return out


def family_inputs(families) -> list[str]:
    """Columns of ``features_candidates`` read by ``family_columns`` for these families (both sides)."""
    cols: list[str] = []
    for fam in families:
        if fam == "A":
            cols += [f"adj_exp_{s}_shots" for s in SIDES]
            cols += [f"{s}_adj_{k}" for s in SIDES for k in ("att", "def", "sot_att", "sot_def", "att_sd", "def_sd")]
        elif fam == "E":
            cols += ["h2h_total_shrunk", "adj_exp_total_shots", "h2h_dev_shrunk"]
        elif fam == "G":
            cols += ["ctx_dead_n", "ctx_progress"] + [f"{s}_ctx_{k}" for s in SIDES for k in ("gap_safety", "games_left")]
        elif fam == "F":
            cols += ["ref_shots_dev"]
        elif fam == "D":
            cols += [f"{s}_sched_{k}" for s in SIDES for k in ("n14", "sunmon_share_l8")]
        elif fam == "C":
            cols += ["mktx_total_goals", "mktx_abs_gd"]
        elif fam == "B":
            cols += [f"{s}_style_corners_{k}_ewm" for s in SIDES for k in ("for", "against")]
    return list(dict.fromkeys(cols))


# Candidate inputs that are NaN by design for some rows (not a sign of a missing / stale build): games left is
# undefined in playoff formats (shots.features.candidates.SEASON_FORMATS).
NAN_BY_DESIGN = ("home_ctx_games_left", "away_ctx_games_left")


def missing_candidate_inputs(rows: pd.DataFrame, families) -> pd.Series:
    """Per row: True when an input of ``families`` is absent or NaN (other than ``NAN_BY_DESIGN``). Such a row
    was not covered by the candidate feature build (stale or failed build, a fixture added after it): scoring
    it would silently median-fill the gap (biased; reports/2026-10-16_fin_c_verification.md, section 5)."""
    cols = [c for c in family_inputs(families) if c not in NAN_BY_DESIGN]
    if any(c not in rows.columns for c in cols):
        return pd.Series(True, index=rows.index)
    return rows[cols].isna().any(axis=1)


class FamilyBlock:
    """Learns fills (training median), drops constant columns and adds one missing flag per family."""

    def __init__(self, families, side):
        self.families = tuple(families)
        self.side = side

    def _raw(self, rows):
        out = {}
        for fam in self.families:
            cols = family_columns(rows, self.side, fam)
            first = next(iter(cols.values()))
            out[f"{fam}_missing"] = first.isna().astype(float)
            out.update(cols)
        return out

    def fit(self, rows) -> "FamilyBlock":
        raw = self._raw(rows)
        self.fill_ = {k: (float(v.median()) if v.notna().any() else 0.0) for k, v in raw.items()}
        self.cols_ = [k for k, v in raw.items() if v.fillna(self.fill_[k]).std() > 0]
        return self

    def __call__(self, rows) -> np.ndarray:
        raw = self._raw(rows)
        if not self.cols_:
            return np.zeros((len(rows), 0))
        return np.column_stack([raw[k].fillna(self.fill_[k]).to_numpy(float) for k in self.cols_])


def nb_pmf_fast(mu, alpha, kmax: int) -> np.ndarray:
    """NB2 pmf on 0..kmax from log-gamma (rows = matches; per-row alpha)."""
    mu = np.maximum(np.asarray(mu, float), 1e-9)[:, None]
    a = np.maximum(np.broadcast_to(np.asarray(alpha, float), (mu.shape[0],))[:, None], 1e-8)
    k = np.arange(kmax + 1, dtype=float)[None, :]
    r = 1.0 / a
    return np.exp(gammaln(k + r) - gammaln(r) - gammaln(k + 1.0) - r * np.log1p(a * mu)
                  + k * (np.log(a * mu) - np.log1p(a * mu)))


def _keep_columns(X: pd.DataFrame) -> list[str]:
    """Same rule as ``negbin._fit_nb``: drop constant and exactly duplicated columns."""
    keep: list[str] = []
    for c in X.columns:
        v = X[c].to_numpy()
        if c != "const" and v.std() == 0:
            continue
        if any(np.array_equal(v, X[k].to_numpy()) for k in keep):
            continue
        keep.append(c)
    return keep


def _dummies(comp: pd.Series, levels: list[str]) -> np.ndarray:
    c = pd.Categorical(comp.astype(str), categories=levels)
    out = np.zeros((len(comp), len(levels)))
    codes = np.asarray(c.codes)
    ok = codes >= 0
    out[np.nonzero(ok)[0], codes[ok]] = 1.0   # unseen competition -> all zero = pooled level
    return out


def _early(rows: pd.DataFrame) -> np.ndarray:
    h = rows["home_n_prior_season"].astype(float).fillna(0).clip(0, 10) / 10
    a = rows["away_n_prior_season"].astype(float).fillna(0).clip(0, 10) / 10
    return np.minimum(h, a).to_numpy()


class _DispersionZ:
    """Builds the dispersion design z (and its ridge mask) from match rows and side means."""

    def __init__(self, terms, levels: list[str]):
        self.terms = tuple(terms)
        self.levels = levels

    def fit(self, rows, mh, ma) -> "_DispersionZ":
        self.logmu0_ = float(np.mean(np.log(mh + ma)))
        o25 = rows["mkt_p_over25"].astype(float) if "mkt_p_over25" in rows else pd.Series(np.nan, index=rows.index)
        self.o25_mean_ = float(o25.mean()) if o25.notna().any() else 0.0
        self.has_o25_ = bool(o25.notna().any())
        if "style" in self.terms:
            v = np.log(_num(rows, "home_style_total_sd10") + _num(rows, "away_style_total_sd10"))
            self.style0_ = float(v.mean())
        return self

    def __call__(self, rows, mh, ma, side_mu=None):
        cols, pen = [np.ones(len(rows))], [False]
        if "comp" in self.terms:
            d = _dummies(rows["competition"], self.levels)
            cols += list(d.T)
            pen += [True] * d.shape[1]
        if "logmu" in self.terms:
            m = side_mu if side_mu is not None else mh + ma
            cols.append(np.log(m) - (self.logmu0_ if side_mu is None else self.logmu0_ - np.log(2.0)))
            pen.append(False)
        if "close" in self.terms:
            cols.append(np.abs(np.log(mh / ma)))
            pen.append(False)
        if "o25" in self.terms and self.has_o25_:
            o = rows["mkt_p_over25"].astype(float)
            cols.append((o - self.o25_mean_).fillna(0.0).to_numpy())
            cols.append(o.isna().astype(float).to_numpy())
            pen += [False, False]
        if "early" in self.terms:
            cols.append(_early(rows))
            pen.append(False)
        if "style" in self.terms:  # volatility of both teams' recent match totals (candidate family B)
            v = np.log(_num(rows, "home_style_total_sd10") + _num(rows, "away_style_total_sd10"))
            cols.append((v - self.style0_).fillna(0.0).to_numpy())
            pen.append(False)
        return np.column_stack(cols), np.asarray(pen)


class FlexNB:
    """Two-sided NB with optional dispersion / correlation submodels, recency weights and
    competition intercepts (see module docstring). Base model for ``NegBinPitComp``."""

    def __init__(self, side_z=(), total: str = "moment", total_z=TOTAL_Z, half_life_days: float | None = None,
                 comp_intercept: bool = False, lam: float = 50.0, lam_z: float = 50.0, rho_tol: float = 0.03,
                 families=(), shrink: bool = False):
        assert total in ("moment", "alpha_z", "rho_z")
        self.side_z = tuple(side_z)
        self.total = total
        self.total_z = tuple(total_z)
        self.half_life_days = half_life_days
        self.comp_intercept = comp_intercept
        self.lam = lam
        self.lam_z = lam_z
        self.rho_tol = rho_tol
        self.families = tuple(families)
        self.shrink = shrink

    def input_columns(self) -> list[str]:
        from shots.models.persist import _negbin_inputs

        return sorted(_negbin_inputs()) + ["competition"] + family_inputs(getattr(self, "families", ()))

    # ------------------------------------------------------------------ design
    def _X(self, rows: pd.DataFrame, side: str) -> tuple[np.ndarray, np.ndarray]:
        s = self.side_fits_[side]
        X = s["design"].transform(rows)[s["cols"]].to_numpy(float)
        pen = np.zeros(X.shape[1], bool)
        if self.comp_intercept:
            d = _dummies(rows["competition"], self.levels_)
            X = np.column_stack([X, d])
            pen = np.concatenate([pen, np.ones(d.shape[1], bool)])
        if getattr(self, "families", ()):  # models pickled before families existed have none
            F = s["block"](rows)
            X = np.column_stack([X, F])
            pen = np.concatenate([pen, np.zeros(F.shape[1], bool)])
        return X, pen

    def _Zside(self, rows, side, mu):
        cols, pen = [np.ones(len(rows))], [False]
        if "comp" in self.side_z:
            d = _dummies(rows["competition"], self.levels_)
            cols += list(d.T)
            pen += [True] * d.shape[1]
        if "logmu" in self.side_z:
            cols.append(np.log(mu) - self.side_fits_[side]["logmu0"])
            pen.append(False)
        return np.column_stack(cols), np.asarray(pen)

    # ------------------------------------------------------------------ fit
    def fit(self, train: pd.DataFrame) -> "FlexNB":
        tr = clean_train(train)
        self.levels_ = sorted(tr["competition"].astype(str).unique())
        w = recency_weights(tr["date"].to_numpy(), self.half_life_days)
        self.side_fits_ = {}
        mus, pearson = {}, {}
        for side in SIDES:
            d = _Design(side).fit(tr)
            Xdf = d.transform(tr)
            cols = _keep_columns(Xdf)
            self.side_fits_[side] = dict(design=d, cols=cols)
            if self.families:
                self.side_fits_[side]["block"] = FamilyBlock(self.families, side).fit(tr)
            X, pen = self._X(tr, side)
            y = tr[f"{side}_shots"].to_numpy(float)
            mean_fit = fit_nb(y, X, None, w, pen_x=pen, lam=self.lam, x_const=0)
            mu = mean_fit.mu(X)
            self.side_fits_[side].update(beta=mean_fit.beta, gamma=mean_fit.gamma, logmu0=float(np.mean(np.log(mu))))
            if self.side_z:
                Z, pz = self._Zside(tr, side, mu)
                disp = fit_nb(y, np.zeros((len(y), 0)), Z, w, offset=np.log(mu), pen_z=pz, lam_z=self.lam_z,
                              x_const=None, z_const=0)
                self.side_fits_[side]["gamma"] = disp.gamma
            a = self._side_alpha(tr, side, mu)
            mus[side] = mu
            pearson[side] = (y - mu) / np.sqrt(mu + a * mu ** 2)
        mh, ma = mus["home"], mus["away"]
        self.rho_ = float(np.corrcoef(pearson["home"], pearson["away"])[0, 1])
        self.mode_ = "moment" if abs(self.rho_) >= self.rho_tol else "independent"
        yt = tr["total_shots"].to_numpy(float)
        if self.total == "alpha_z":
            self.zt_ = _DispersionZ(self.total_z, self.levels_).fit(tr, mh, ma)
            Z, pz = self.zt_(tr, mh, ma)
            self.total_fit_ = fit_nb(yt, np.zeros((len(yt), 0)), Z, w, offset=np.log(mh + ma), pen_z=pz,
                                     lam_z=self.lam_z, x_const=None, z_const=0)
        elif self.total == "rho_z":
            self._fit_rho(tr, mh, ma, yt, w)
        self.prior_fill_ = float(tr["league_prior_shots"].mean())
        self.shrink_ = None
        if self.shrink:
            self._fit_shrink(tr)
        return self

    def _level(self, rows) -> np.ndarray:
        """League level of the match total: 2 x the season-to-date league prior (per team per match)."""
        return 2 * rows["league_prior_shots"].astype(float).fillna(self.prior_fill_).clip(lower=1.0).to_numpy()

    def _fit_shrink(self, tr):
        """Out-of-sample shrinkage of the predicted total toward the league level:
        log mu' = log L + c + b (log mu - log L), (c, b) by NB maximum likelihood on the latest season
        carved from the training rows, scored by a model fitted on the rows before it (dispersion of
        that model held fixed). b < 1 pulls extreme predictions back toward the league (the
        within-season slope of actual on predicted is below 1 out of sample)."""
        from scipy import optimize

        from shots.models.nbmle import nb_loglik_terms

        inner, held = carve_calibration(tr, 3000)
        if held.empty or inner.empty:
            return
        kw = {k: getattr(self, k) for k in ("side_z", "total", "total_z", "half_life_days", "comp_intercept",
                                           "lam", "lam_z", "rho_tol", "families")}
        m = FlexNB(**kw).fit(inner)
        mu, alpha = m.total_params(held)
        L = m._level(held)
        x = np.log(mu) - np.log(L)
        y = held["total_shots"].to_numpy(float)

        def nll(th):
            return -nb_loglik_terms(y, np.exp(np.log(L) + th[0] + th[1] * x), alpha).mean()

        th = optimize.minimize(nll, np.array([0.0, 1.0]), method="Nelder-Mead",
                               options=dict(xatol=1e-6, fatol=1e-10, maxiter=400)).x
        self.shrink_ = (float(th[0]), float(th[1]))
        log.info("total shrink toward league level: c=%.4f b=%.3f (n=%d)", th[0], th[1], len(held))

    def _ratio(self, rows, mh, ma) -> np.ndarray | float:
        if not getattr(self, "shrink_", None):
            return 1.0
        c, b = self.shrink_
        L = self._level(rows)
        lm = np.log(mh + ma)
        return np.exp(np.log(L) + c + b * (lm - np.log(L)) - lm)

    def _fit_rho(self, tr, mh, ma, yt, w):
        from scipy import optimize

        from shots.models.nbmle import nb_loglik_terms

        vh = mh + self._side_alpha(tr, "home", mh) * mh ** 2
        va = ma + self._side_alpha(tr, "away", ma) * ma ** 2
        R = self._rho_design(mh, ma)
        ww = np.ones(len(yt)) if w is None else w / w.mean()

        def nll(d):
            rho = np.tanh(R @ d)
            mu = mh + ma
            alpha = np.clip((vh + va + 2 * rho * np.sqrt(vh * va) - mu) / mu ** 2, 1e-6, None)
            return -np.dot(ww, nb_loglik_terms(yt, mu, alpha)) / len(yt)

        d0 = np.zeros(R.shape[1])
        d0[0] = np.arctanh(np.clip(self.rho_, -0.9, 0.9))
        self.rho_d_ = optimize.minimize(nll, d0, method="Nelder-Mead",
                                        options=dict(maxiter=600, xatol=1e-5, fatol=1e-9)).x

    def _rho_design(self, mh, ma):
        if not hasattr(self, "rho_logmu0_"):
            self.rho_logmu0_ = float(np.mean(np.log(mh + ma)))
        return np.column_stack([np.ones(len(mh)), np.abs(np.log(mh / ma)), np.log(mh + ma) - self.rho_logmu0_])

    # ------------------------------------------------------------------ predict
    def _raw_mu(self, rows: pd.DataFrame, side: str) -> np.ndarray:
        X, _ = self._X(rows, side)
        return np.exp(np.clip(X @ self.side_fits_[side]["beta"], -20, 20))

    def side_mu(self, rows: pd.DataFrame, side: str) -> np.ndarray:
        if not getattr(self, "shrink_", None):
            return self._raw_mu(rows, side)
        rh, ra = self._raw_mu(rows, "home"), self._raw_mu(rows, "away")
        return (rh if side == "home" else ra) * self._ratio(rows, rh, ra)

    def _side_alpha(self, rows, side, mu) -> np.ndarray:
        g = self.side_fits_[side]["gamma"]
        if not self.side_z:
            return np.full(len(rows), float(np.exp(g[0])))
        Z, _ = self._Zside(rows, side, mu)
        return np.exp(np.clip(Z @ g, -12, 3))

    def side_alpha(self, rows, side) -> np.ndarray:
        return self._side_alpha(rows, side, self.side_mu(rows, side))

    def total_params(self, rows: pd.DataFrame, mh=None, ma=None) -> tuple[np.ndarray, np.ndarray]:
        ratio = 1.0
        if mh is None and ma is None and getattr(self, "shrink_", None):
            rh, ra = self._raw_mu(rows, "home"), self._raw_mu(rows, "away")
            ratio = self._ratio(rows, rh, ra)
            if self.total == "alpha_z":  # dispersion design on the raw means it was fitted with
                Z, _ = self.zt_(rows, rh, ra)
                return (rh + ra) * ratio, np.exp(np.clip(Z @ self.total_fit_.gamma, -12, 3))
            mh, ma = rh * ratio, ra * ratio
        mh = self.side_mu(rows, "home") if mh is None else mh
        ma = self.side_mu(rows, "away") if ma is None else ma
        mu = mh + ma
        if self.total == "alpha_z":
            Z, _ = self.zt_(rows, mh, ma)
            return mu, np.exp(np.clip(Z @ self.total_fit_.gamma, -12, 3))
        ah, aa = self._side_alpha(rows, "home", mh), self._side_alpha(rows, "away", ma)
        vh, va = mh + ah * mh ** 2, ma + aa * ma ** 2
        rho = np.tanh(self._rho_design(mh, ma) @ self.rho_d_) if self.total == "rho_z" else (
            self.rho_ if self.mode_ == "moment" else 0.0)
        return mu, np.clip((vh + va + 2 * rho * np.sqrt(vh * va) - mu) / mu ** 2, 1e-8, None)

    def total_pmf(self, rows: pd.DataFrame) -> np.ndarray:
        mu, alpha = self.total_params(rows)
        return nb_pmf_fast(mu, alpha, MAX_TOTAL)

    def total_cdf(self, rows: pd.DataFrame) -> np.ndarray:
        return np.minimum(np.cumsum(self.total_pmf(rows), axis=1), 1.0)

    def side_cdf(self, rows: pd.DataFrame, side: str) -> np.ndarray:
        mu = self.side_mu(rows, side)
        return np.minimum(np.cumsum(nb_pmf_fast(mu, self._side_alpha(rows, side, mu), MAX_SIDE), axis=1), 1.0)


# ---------------------------------------------------------------------- joint: total + share

def _bb_logpmf(h, t, a, b):
    return (gammaln(t + 1) - gammaln(h + 1) - gammaln(t - h + 1) + betaln(h + a, t - h + b) - betaln(a, b))


class JointShare:
    """Total from a ``FlexNB`` (its total distribution), home share | total ~ BetaBinomial."""

    def __init__(self, share_comp: bool = True, phi_comp: bool = False, **flex):
        self.share_comp = share_comp
        self.phi_comp = phi_comp
        self.flex = {"total": "alpha_z", **flex}

    def _S(self, rows, mh, ma):
        cols = [np.ones(len(rows)), np.log(mh / ma)]
        if self.share_comp:
            cols += list(_dummies(rows["competition"], self.levels_).T)
        return np.column_stack(cols)

    def fit(self, train: pd.DataFrame) -> "JointShare":
        from scipy import optimize

        tr = clean_train(train)
        self.levels_ = sorted(tr["competition"].astype(str).unique())
        self.nb_ = FlexNB(**self.flex).fit(tr)
        mh, ma = self.nb_.side_mu(tr, "home"), self.nb_.side_mu(tr, "away")
        S = self._S(tr, mh, ma)
        h, t = tr["home_shots"].to_numpy(float), tr["total_shots"].to_numpy(float)
        ns = S.shape[1]
        lam = 50.0

        def nll(theta):
            d, lphi = theta[:ns], theta[ns]
            p = 1.0 / (1.0 + np.exp(-(S @ d)))
            phi = np.exp(lphi)
            a, b = p * phi, (1 - p) * phi
            return -np.mean(_bb_logpmf(h, t, a, b)) + 0.5 * lam * np.sum(d[2:] ** 2) / len(h)

        x0 = np.zeros(ns + 1)
        x0[1], x0[ns] = 1.0, np.log(50.0)
        res = optimize.minimize(nll, x0, method="L-BFGS-B")
        self.share_d_, self.share_phi_ = res.x[:ns], float(np.exp(res.x[ns]))
        log.info("joint share: phi=%.1f slope=%.3f", self.share_phi_, self.share_d_[1])
        return self

    def side_mu(self, rows, side):
        return self.nb_.side_mu(rows, side)

    def input_columns(self) -> list[str]:
        return self.nb_.input_columns()

    def total_cdf(self, rows):
        return self.nb_.total_cdf(rows)

    def side_cdf(self, rows: pd.DataFrame, side: str, chunk: int = 400) -> np.ndarray:
        mh, ma = self.nb_.side_mu(rows, "home"), self.nb_.side_mu(rows, "away")
        mu, alpha = self.nb_.total_params(rows, mh, ma)
        p = 1.0 / (1.0 + np.exp(-(self._S(rows, mh, ma) @ self.share_d_)))
        if side == "away":
            p = 1.0 - p
        tmax = 120
        pt = nb_pmf_fast(mu, alpha, tmax)                          # (n, T)
        t = np.arange(tmax + 1, dtype=float)
        hh = np.arange(MAX_SIDE + 1, dtype=float)
        out = np.empty((len(rows), MAX_SIDE + 1))
        H, T = np.meshgrid(hh, t, indexing="ij")                    # (H, T)
        valid = H <= T
        for i in range(0, len(rows), chunk):
            a = (p[i:i + chunk] * self.share_phi_)[:, None, None]
            b = ((1 - p[i:i + chunk]) * self.share_phi_)[:, None, None]
            lp = _bb_logpmf(H[None], T[None], a, b)
            cond = np.where(valid[None], np.exp(np.where(valid[None], lp, 0.0)), 0.0)  # (c, H, T)
            out[i:i + chunk] = np.einsum("cht,ct->ch", cond, pt[i:i + chunk])
        return np.minimum(np.cumsum(out, axis=1), 1.0)


# ---------------------------------------------------------------------- gradient boosting

GBM_PARAMS = dict(objective="poisson", learning_rate=0.05, num_leaves=15, min_data_in_leaf=300,
                  feature_fraction=0.7, bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0,
                  max_bin=255, verbose=-1, seed=11, deterministic=True, force_row_wise=True, num_threads=2)
GBM_MAX_ROUNDS = 3000
GBM_EARLY_STOP = 100


def cached_fit(model, train: pd.DataFrame):
    """``model.fit(train)``, memoised on disk when SHOTS_FIT_CACHE is set (model selection only: lets a
    pool reuse the member fits of the stand-alone runs). Key = class, constructor kwargs and a content
    hash of the training rows (ids, dates, targets, market columns), so a different window or the
    no-odds variant never hits the cache of another."""
    import hashlib
    import os

    root = os.environ.get("SHOTS_FIT_CACHE")
    if not root:
        return model.fit(train)
    import joblib

    cols = [c for c in ("match_id", "date", "home_shots", "away_shots", "mkt_p_home", "mkt_p_away",
                        "mkt_p_over25") if c in train]
    h = hashlib.sha1(repr((type(model).__name__, sorted(vars(model).items()))).encode())
    h.update(pd.util.hash_pandas_object(train[cols], index=False).to_numpy().tobytes())
    path = Path(root) / f"{type(model).__name__}_{h.hexdigest()[:20]}.joblib"
    if path.exists():
        return joblib.load(path)
    model.fit(train)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    joblib.dump(model, tmp)
    os.replace(tmp, path)
    return model


def _gbm_X(rows: pd.DataFrame, categories) -> pd.DataFrame:
    X = rows.reindex(columns=FEATURES).astype(float)
    X["competition"] = pd.Categorical(rows["competition"].astype(str), categories=categories)
    return X


class GBMNB:
    """LightGBM side means (+ GLM offset), NB total with a dispersion submodel fitted out of sample."""

    def __init__(self, offset: bool = True, total_z=TOTAL_Z, params: dict | None = None,
                 lam_z: float = 50.0, refit: bool = True, glm: dict | None = None):
        self.offset = offset
        self.total_z = tuple(total_z)
        self.params = {**GBM_PARAMS, **(params or {})}
        self.lam_z = lam_z
        self.refit = refit
        self.glm = dict(glm or {})

    def _init(self, glm, rows, side):
        return np.log(glm.side_mu(rows, side)) if self.offset else None

    def _train(self, df, side, glm, rounds=None, valid=None):
        import lightgbm as lgb

        X = _gbm_X(df, self.categories_)
        dtr = lgb.Dataset(X, df[f"{side}_shots"].to_numpy(float), init_score=self._init(glm, df, side),
                          free_raw_data=False)
        if rounds is not None:
            return lgb.train(self.params, dtr, num_boost_round=max(rounds, 1))
        vdf, vglm = valid
        dva = lgb.Dataset(_gbm_X(vdf, self.categories_), vdf[f"{side}_shots"].to_numpy(float),
                          init_score=self._init(vglm, vdf, side), reference=dtr)
        return lgb.train(self.params, dtr, num_boost_round=GBM_MAX_ROUNDS, valid_sets=[dva],
                         callbacks=[lgb.early_stopping(GBM_EARLY_STOP, verbose=False)])

    def _mu(self, rows, side, glm=None, booster=None):
        if glm is None and self.offset:
            glm = self.glm_
        booster = booster or self.boosters_[side]
        X = _gbm_X(rows, self.categories_)
        raw = booster.predict(X, raw_score=True, num_iteration=booster.best_iteration or None)
        base = np.log(glm.side_mu(rows, side)) if self.offset else 0.0
        return np.exp(np.clip(raw + base, -20, 20))

    def fit(self, train: pd.DataFrame) -> "GBMNB":
        tr = clean_train(train)
        self.categories_ = sorted(tr["competition"].astype(str).unique())
        inner, es = carve_calibration(tr, 3000)
        if es.empty or inner.empty:  # a single season: early-stop on a deterministic half instead
            from shots.models.common import hash_half

            m = hash_half(tr["match_id"])
            inner, es = tr[~m], tr[m]
        glm_in = FlexNB(**self.glm).fit(inner) if self.offset else None
        boosters, self.best_iter_ = {}, {}
        for side in SIDES:
            boosters[side] = self._train(inner, side, glm_in, valid=(es, glm_in))
            self.best_iter_[side] = int(boosters[side].best_iteration or boosters[side].current_iteration())
        mh = self._mu(es, "home", glm_in, boosters["home"])
        ma = self._mu(es, "away", glm_in, boosters["away"])
        # dispersion submodels on the held-out season, out-of-sample means
        levels = sorted(es["competition"].astype(str).unique())
        self.zt_ = _DispersionZ(self.total_z, levels).fit(es, mh, ma)
        Z, pz = self.zt_(es, mh, ma)
        yt = es["total_shots"].to_numpy(float)
        self.total_fit_ = fit_nb(yt, np.zeros((len(yt), 0)), Z, None, offset=np.log(mh + ma), pen_z=pz,
                                 lam_z=self.lam_z, x_const=None, z_const=0)
        self.side_alpha_ = {}
        for side, m in (("home", mh), ("away", ma)):
            y = es[f"{side}_shots"].to_numpy(float)
            self.side_alpha_[side] = float(np.exp(fit_nb(y, np.zeros((len(y), 0)), None, None, offset=np.log(m),
                                                         x_const=None, z_const=0).gamma[0]))
        log.info("gbm best_iter %s, side alpha %s", self.best_iter_, self.side_alpha_)
        if self.refit:
            self.glm_ = FlexNB(**self.glm).fit(tr) if self.offset else None
            scale = len(tr) / max(len(inner), 1)
            self.boosters_ = {s: self._train(tr, s, self.glm_, rounds=int(round(self.best_iter_[s] * scale)))
                              for s in SIDES}
        else:
            self.glm_, self.boosters_ = glm_in, boosters
        return self

    def side_mu(self, rows, side):
        return self._mu(rows, side)

    def input_columns(self) -> list[str]:
        cols = list(FEATURES) + ["competition"]
        return cols + (self.glm_.input_columns() if self.offset else [])

    def total_params(self, rows, mh=None, ma=None):
        mh = self.side_mu(rows, "home") if mh is None else mh
        ma = self.side_mu(rows, "away") if ma is None else ma
        Z, _ = self.zt_(rows, mh, ma)
        return mh + ma, np.exp(np.clip(Z @ self.total_fit_.gamma, -12, 3))

    def total_cdf(self, rows):
        mu, a = self.total_params(rows)
        return np.minimum(np.cumsum(nb_pmf_fast(mu, a, MAX_TOTAL), axis=1), 1.0)

    def side_cdf(self, rows, side):
        return np.minimum(np.cumsum(nb_pmf_fast(self.side_mu(rows, side), self.side_alpha_[side], MAX_SIDE),
                                    axis=1), 1.0)


# ---------------------------------------------------------------------- pooling

def _quantile_average(c1: np.ndarray, c2: np.ndarray, w: float, n_levels: int = 999) -> np.ndarray:
    """CDF of w Q1 + (1 - w) Q2 (Vincentization) on the integer support of c1 / c2."""
    from shots.models.calibration import continuous_quantile

    p = (np.arange(n_levels) + 0.5) / n_levels
    K = max(c1.shape[1], c2.shape[1])
    pad = lambda c: np.pad(c, ((0, 0), (0, K - c.shape[1])), constant_values=1.0)
    c1, c2 = pad(c1), pad(c2)
    q = np.stack([w * continuous_quantile(c1, pi) + (1 - w) * continuous_quantile(c2, pi) for pi in p], axis=1)
    # F(k) = share of levels with averaged quantile <= k (q is non-decreasing in p)
    k = np.arange(K)
    return np.stack([(q <= kk).mean(axis=1) for kk in k], axis=1)


class Pool:
    def __init__(self, members=("flex", "gbm"), weight: float = 0.5, kind: str = "linear",
                 flex: dict | None = None, gbm: dict | None = None):
        assert kind in ("linear", "quantile")
        self.members = tuple(members)
        self.weight = weight
        self.kind = kind
        self.flex = dict(flex or {})
        self.gbm = dict(gbm or {})

    def fit(self, train):
        tr = clean_train(train)
        self.m1_ = cached_fit(FlexNB(**self.flex), tr)
        self.m2_ = cached_fit(GBMNB(**self.gbm), tr)
        return self

    def input_columns(self) -> list[str]:
        return list(dict.fromkeys(self.m1_.input_columns() + self.m2_.input_columns()))

    def _combine(self, c1, c2):
        if self.kind == "linear":
            K = max(c1.shape[1], c2.shape[1])
            pad = lambda c: np.pad(c, ((0, 0), (0, K - c.shape[1])), constant_values=1.0)
            return self.weight * pad(c1) + (1 - self.weight) * pad(c2)
        return _quantile_average(c1, c2, self.weight)

    def side_mu(self, rows, side):
        return self.weight * self.m1_.side_mu(rows, side) + (1 - self.weight) * self.m2_.side_mu(rows, side)

    def total_cdf(self, rows):
        return self._combine(self.m1_.total_cdf(rows), self.m2_.total_cdf(rows))

    def side_cdf(self, rows, side):
        return self._combine(self.m1_.side_cdf(rows, side), self.m2_.side_cdf(rows, side))


# ---------------------------------------------------------------------- calibrated wrappers

class _Cached:
    """Thin proxy: ``fit`` goes through ``cached_fit`` and returns the fitted base itself."""

    def __init__(self, base):
        self.base = base

    def fit(self, train):
        return cached_fit(self.base, clean_train(train))


class CandidatePit(NegBinPitComp):
    """Production calibration (per-competition PIT, 2-season window) around a candidate base."""

    name = "cand_pit"
    base_cls = FlexNB
    base_kw: dict = {}

    def __init__(self, base_kw: dict | None = None, **kw):
        super().__init__(**kw)
        self.base_kw = {**type(self).base_kw, **(base_kw or {})}

    def _new_nb(self):
        return _Cached(self.base_cls(**self.base_kw))

    def predict(self, rows: pd.DataFrame, confidences=(0.7, 0.8, 0.9)) -> pd.DataFrame:
        """As production, except that the median is read off the recalibrated curve G(F(k)) (the
        published curve), not off the raw NB: min{k : G(F(k)) >= 0.5}."""
        from shots.models.calibrated import _cdf

        out = super().predict(rows, confidences)
        cdf = _cdf(self.nb_, rows)
        g = np.asarray(rows[self.group]) if self.group else None
        G = self.pit_map_(cdf.ravel(), np.repeat(g, cdf.shape[1]) if g is not None else None).reshape(cdf.shape)
        out["median"] = (G >= 0.5).argmax(axis=1).astype(float)
        return out


# ---------------------------------------------------------------------- fin_c (no-odds route candidate)

FIN_C_FAMILIES = ("A", "E", "G")
FIN_C_BASE = dict(families=FIN_C_FAMILIES, shrink=True, side_z=("comp",))


class FinC(CandidatePit):
    """``fin_c``: the production two-sided NB (same design, moment-matched total, per-competition PIT calibration
    on two seasons) + candidate families A (opponent-adjusted EKF ratings), E (head-to-head), G (season context)
    + out-of-sample shrinkage of the total toward the league level + per-competition side dispersion.
    Evidence: reports/2026-10-15_model_candidates.md, reports/2026-10-16_fin_c_verification.md,
    reports/2026-10-16_fin_c_adoption.md. Production use: the no-odds route only, behind
    ``shots.config.NOODDS_MODEL`` (rows need the ``shots.features.candidates`` columns of ``family_inputs``).

    The median follows ``shots.config.median_from_curve()`` like production (``NegBinCal.predict``), so switching
    the model alone does not also change how ``expected`` is read."""

    name = "fin_c"
    base_cls = FlexNB
    base_kw = FIN_C_BASE
    families = FIN_C_FAMILIES

    def predict(self, rows: pd.DataFrame, confidences=(0.7, 0.8, 0.9)) -> pd.DataFrame:
        from shots.models.calibrated import NegBinCal

        return NegBinCal.predict(self, rows, confidences)

    @classmethod
    def missing_inputs(cls, rows: pd.DataFrame) -> pd.Series:
        return missing_candidate_inputs(rows, cls.families)


def _cand(name: str, base_cls, **base_kw) -> type[CandidatePit]:
    return type(f"Cand_{name}", (CandidatePit,), {"name": name, "base_cls": base_cls, "base_kw": base_kw,
                                                    "__module__": __name__})


CANDIDATES: dict[str, type[CandidatePit]] = {
    # sanity: the production NB re-estimated with nbmle (should equal negbin_pit_comp up to tolerance)
    "cand_nbmle": _cand("cand_nbmle", FlexNB),
    # 1. dispersion
    "cand_disp_side": _cand("cand_disp_side", FlexNB, side_z=("comp", "logmu")),
    "cand_disp_total": _cand("cand_disp_total", FlexNB, total="alpha_z"),
    "cand_disp_rho": _cand("cand_disp_rho", FlexNB, total="rho_z"),
    "cand_disp_both": _cand("cand_disp_both", FlexNB, side_z=("comp", "logmu"), total="alpha_z"),
    # 2. joint total + share
    "cand_joint": _cand("cand_joint", JointShare),
    # 3. distributional GBM
    "cand_gbm": _cand("cand_gbm", GBMNB, offset=True),
    "cand_gbm_raw": _cand("cand_gbm_raw", GBMNB, offset=False),
    # 4. pooling
    "cand_pool": _cand("cand_pool", Pool, weight=0.5, kind="linear"),
    "cand_pool_q": _cand("cand_pool_q", Pool, weight=0.5, kind="quantile"),
    # 5. time handling
    "cand_recency": _cand("cand_recency", FlexNB, half_life_days=1460),
    "cand_comp_drift": _cand("cand_comp_drift", FlexNB, comp_intercept=True, half_life_days=1460),
}

# Candidates with a production route (behind a switch in shots.config); registered in shots.models.MODELS.
PRODUCTION_CANDIDATES: dict[str, type[CandidatePit]] = {"fin_c": FinC}

# Explicit globals so pickles (joblib) of these dynamically built classes resolve.
for _cls in CANDIDATES.values():
    globals()[_cls.__name__] = _cls

__all__ = ["FlexNB", "JointShare", "GBMNB", "Pool", "CandidatePit", "CANDIDATES", "nb_pmf_fast", "FinC",
           "PRODUCTION_CANDIDATES", "family_inputs", "missing_candidate_inputs", "FIN_C_BASE"]
