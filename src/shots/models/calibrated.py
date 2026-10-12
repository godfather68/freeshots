"""Conformally calibrated models (TASKS 3b.1-3b.3). Methods: ``calibration.py``; evidence:
reports/2026-10-08_models_3b.md.

``negbin_cal`` (3b.1) -- the negbin model + split-conformal correction of the CDF *level*.
  1. ``carve_calibration`` holds out the latest training season (>= 3000 rows), as qgbm does.
  2. A NegBinModel is fitted on the earlier rows; its CDF on the held-out season gives
     out-of-sample scores e = 1 - F(y). The conformal q-quantile Q of e is the level shift:
     bound = min{k : F(k) > 1 - Q}, coverage >= q (finite-sample, exchangeability) and ~= q.
     A level shift (not an additive one in shots) keeps each match's predictive spread; both
     were backtested and are equivalent (pinball 1.4122 vs 1.4121), level is the natural one
     for a distributional model.
  3. ``refit_full=True``: the NB is then refitted on all training rows and the calibrated level is
     applied to the refitted CDF. This is approximately valid: the refit only appends one season to
     an expanding 4-10 season window, so the coefficients/dispersion barely move, and what the
     shift corrects is a property of the NB family rather than of one fit. Backtest: refit vs no
     refit identical to 3 decimals (1.4124 vs 1.4122); refit kept so the NB sees the latest data
     when refitted monthly.

``negbin_cal_comp`` (3b.3) -- same, Mondrian per competition, calibration window = latest 2
  seasons (scored by an NB fitted before both), global fallback for groups < 300 rows.

``negbin_pit_comp`` (3b.3, recommended) -- PIT recalibration per competition, 2-season window.
  negbin's over-coverage is integer granularity, not bias: its non-randomized PIT is uniform
  (9.8 / 19.9 / 30.0 / 50.6% below .1/.2/.3/.5) and its model-implied coverage of the integer
  bounds is 82.6% at q=0.8 vs 82.9% observed. The pinball-optimal integer bound of a calibrated
  discrete distribution covers with rate >= q (by ~half the pmf at the bound, ~2.6pp here).
  Forcing coverage to exactly q costs pinball (1.404 -> 1.412). So this variant only corrects the
  CDF where it is miscalibrated (per competition: Scottish leagues -4pp, Segunda +2.6pp), keeping
  pinball-optimal integer bounds: level t_q per group with mean_i u_i(t_q) = 1 - q, u_i the
  randomized-PIT CDF of calibration row i (``pit_level``).

Per-team shots (all NegBinCal variants, reports/2026-10-09_team_shots.md): ``team_predict`` and
  ``team_prob_at_least`` recalibrate each side's NB with the same scheme as the total (same carved
  window, target, per-competition groups and fallback), one calibrator + PIT map per side, fitted
  inside ``fit``. Total-shots outputs are unaffected. Models pickled before this addition raise a
  RuntimeError from the team_* methods asking for a retrain.

``blend`` (3b.2) -- average of the negbin and qgbm *uncalibrated* lower quantiles + one CQR shift.
  1. Same carve-out; the held-out season is split 50/50 by match_id hash as in qgbm; one half is
     used for LightGBM early stopping. The NB and the boosters are fitted on the earlier rows
     so calibration scores are out-of-sample for both members.
  2. b_q(x) = w nb_q(x) + (1 - w) qgbm_q(x), nb_q = interpolated NB quantile at level 1 - q
     (``continuous_quantile``), w = 0.5 fixed (members have near-equal pinball; no tuning).
  3. One-sided CQR on the whole held-out season (``cal_half=False``: the early-stopping half only
     chose an iteration count; using it doubles n and backtested better, 1.4124 vs 1.4139):
     bound = ceil(b_q - Q_q). Median = average of the two members' medians.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from shots.models.base import CONFIDENCES, TARGET, ShotModel
from shots.models.calibration import (ConformalShift, PitLevel, PitMap, cdf_at, continuous_quantile,
                                      lb_at_level)
from shots.models.common import (SIDES, carve_calibration, ceil_int, clean_train, finalize,
                                 hash_half, lb_from_cdf)
from shots.models.negbin import MAX_SIDE, NegBinModel, nb_pmf
from shots.models.qgbm import QuantileGBM

log = logging.getLogger(__name__)


def _cdf(nb: NegBinModel, rows: pd.DataFrame) -> np.ndarray:
    return np.cumsum(nb.total_pmf(rows), axis=1)


def _side_cdf(nb: NegBinModel, rows: pd.DataFrame, side: str) -> np.ndarray:
    """CDF of one team's shots (raw NB of that side) on 0..MAX_SIDE, rows = matches."""
    return np.cumsum(nb_pmf(nb.side_mu(rows, side), nb.sides_[side]["alpha"], MAX_SIDE), axis=1)


def _check_side(side: str) -> str:
    if side not in SIDES:
        raise ValueError(f"side must be 'home' or 'away', got {side!r}")
    return side


def _coverage(y, lb) -> float:
    return float(np.mean(np.asarray(y, float) >= np.asarray(lb, float)))


def carve_window(train: pd.DataFrame, n_windows: int, min_rows: int = 3000):
    """``carve_calibration`` applied ``n_windows`` times: (fit rows, calibration rows).

    n_windows=1 -> latest season (>= min_rows); 2 -> latest two such blocks. Always leaves at
    least one season to fit on (``carve_calibration`` guarantees it at each step).
    """
    fit_df, cal_parts = train, []
    for _ in range(n_windows):
        if fit_df["season"].nunique() < 2:
            break
        fit_df, cal = carve_calibration(fit_df, min_rows)
        cal_parts.append(cal)
    return fit_df, pd.concat(cal_parts[::-1]) if cal_parts else fit_df.iloc[:0]


class NegBinCal(ShotModel):
    """NB + post-hoc calibration of the CDF level at which bounds are read.

    target="exact": split-conformal on e = 1 - F(y) -> integer-bound coverage ~= q exactly.
    target="pit":   recalibrate the (non-randomized) PIT so that the predictive CDF is calibrated;
                    bounds stay the pinball-optimal integers of the recalibrated CDF, whose
                    coverage is >= q by the integer granularity (see reports/2026-10-08_models_3b.md).
    """

    name = "negbin_cal"

    def __init__(self, target: str = "exact", window_seasons: int = 1, refit_full: bool = True,
                 group: str | None = None, min_group: int = 300, shrink: float = 0.0,
                 half_life_days: float | None = None, min_cal_rows: int = 3000,
                 confidences=CONFIDENCES):
        assert target in ("exact", "pit")
        self.target = target
        self.window_seasons = window_seasons
        self.refit_full = refit_full
        self.group = group
        self.min_group = min_group
        self.shrink = shrink
        self.half_life_days = half_life_days
        self.min_cal_rows = min_cal_rows
        self.confidences = tuple(confidences)

    def _new_nb(self) -> NegBinModel:
        """Base NB of this calibrated model (subclasses swap it, e.g. ``drift.NegBinPitCompDrift``)."""
        return NegBinModel()

    def _levels(self, rows: pd.DataFrame, q: float) -> np.ndarray:
        """Per-row CDF level t: bound = min{k : F(k) > t}."""
        Q = self.calibrator_.threshold(q, rows[self.group] if self.group else None, len(rows))
        return 1.0 - Q - 1e-9 if self.target == "exact" else Q

    def fit(self, train: pd.DataFrame) -> "NegBinCal":
        tr = clean_train(train)
        fit_df, cal_df = carve_window(tr, self.window_seasons, self.min_cal_rows)
        self.cal_seasons_ = sorted(cal_df["season"].astype(str).unique())
        base = self._new_nb().fit(fit_df)
        cdf_cal = _cdf(base, cal_df)
        y_cal = cal_df[TARGET].to_numpy(float)
        F, Fm1 = cdf_at(cdf_cal, y_cal), cdf_at(cdf_cal, y_cal - 1)
        groups = cal_df[self.group] if self.group else None
        if self.target == "exact":
            cal = ConformalShift(self.group, self.min_group, self.shrink, self.half_life_days)
            data = {q: 1.0 - F for q in self.confidences}
        else:
            cal = PitLevel(self.group, self.min_group, self.shrink, self.half_life_days)
            data = {q: (Fm1, F) for q in self.confidences}
        self.calibrator_ = cal.fit(data, groups, cal_df["date"])
        self.cal_report_ = {
            q: dict(raw_coverage=_coverage(y_cal, lb_at_level(cdf_cal, np.full(len(y_cal), 1 - q))),
                    cal_coverage=_coverage(y_cal, lb_at_level(cdf_cal, self._levels(cal_df, q))),
                    n=len(y_cal))
            for q in self.confidences
        }
        self.pit_map_ = PitMap(self.group, self.min_group).fit(Fm1, F, groups)
        self._fit_team_calibration(base, cal_df)
        self.nb_ = self._new_nb().fit(tr) if self.refit_full else base
        return self

    # ------------------------------------------------------------------ per-team shots
    # Each side's raw NB (``nb_.sides_[side]``) is recalibrated exactly like the total: same
    # calibration window (``cal_df``, scored by the NB fitted before it), same target ("pit" or
    # "exact"), same groups / min_group fallback / shrink / recency weighting, one calibrator and
    # one PIT map per side. Nothing here touches the total-shots state (calibrator_, pit_map_,
    # nb_), so total predictions are unchanged. The median is read at the recalibrated 0.5 level.

    def _team_levels_q(self) -> tuple[float, ...]:
        return tuple(sorted({0.5, *self.confidences}))

    def _fit_team_calibration(self, base: NegBinModel, cal_df: pd.DataFrame) -> None:
        groups = cal_df[self.group] if self.group else None
        self.team_calibrator_, self.team_pit_map_, self.team_cal_report_ = {}, {}, {}
        for side in SIDES:
            cdf = _side_cdf(base, cal_df, side)
            y = cal_df[f"{side}_shots"].to_numpy(float)
            F, Fm1 = cdf_at(cdf, y), cdf_at(cdf, y - 1)
            levels = self._team_levels_q()
            if self.target == "exact":
                cal = ConformalShift(self.group, self.min_group, self.shrink, self.half_life_days)
                data = {q: 1.0 - F for q in levels}
            else:
                cal = PitLevel(self.group, self.min_group, self.shrink, self.half_life_days)
                data = {q: (Fm1, F) for q in levels}
            self.team_calibrator_[side] = cal.fit(data, groups, cal_df["date"])
            self.team_pit_map_[side] = PitMap(self.group, self.min_group).fit(Fm1, F, groups)
            self.team_cal_report_[side] = {
                q: dict(raw_coverage=_coverage(y, lb_at_level(cdf, np.full(len(y), 1 - q))),
                        cal_coverage=_coverage(y, lb_at_level(cdf, self._team_levels(cal_df, side, q))),
                        n=len(y))
                for q in self.confidences
            }

    def _team_levels(self, rows: pd.DataFrame, side: str, q: float) -> np.ndarray:
        Q = self.team_calibrator_[side].threshold(q, rows[self.group] if self.group else None,
                                                  len(rows))
        return 1.0 - Q - 1e-9 if self.target == "exact" else Q

    @property
    def has_team_calibration(self) -> bool:
        return hasattr(self, "team_pit_map_")

    def _require_team(self) -> None:
        if not self.has_team_calibration:
            raise RuntimeError(
                f"this {self.name} model was fitted before per-team calibration existed "
                "(team_* methods need it). Retrain it: `python -m shots.models.train --model "
                f"{self.name}`, and delete cached '<model>-noodds_*' dirs in models_store so the "
                "no-odds variant is refitted. Total-shots predictions of this model still work.")

    def team_prob_at_least(self, rows: pd.DataFrame, side: str, n) -> np.ndarray:
        """Recalibrated P(shots of ``side`` >= n_i) per row (n scalar or per-row array):
        1 - G_side(F_side(n_i - 1)), G_side = that side's PIT map of the calibration window."""
        self._require_team()
        side = _check_side(side)
        cdf = _side_cdf(self.nb_, rows, side)
        n = np.broadcast_to(np.asarray(n), (len(rows),)).astype(int)
        t = cdf_at(cdf, n - 1)
        return 1.0 - self.team_pit_map_[side](t, rows[self.group] if self.group else None)

    def team_predict(self, rows: pd.DataFrame, side: str, confidences=CONFIDENCES) -> pd.DataFrame:
        """Per-team shots for ``side``, indexed like ``rows``: ``median`` (integer-valued float,
        recalibrated 0.5 level), ``lb_70``... (int, monotone in q, the side's calibrated lower
        bounds, same convention as the total) and ``mean`` (expected shots under the
        recalibrated distribution, float; use it for display such as "17 - 13")."""
        self._require_team()
        side = _check_side(side)
        missing = [q for q in confidences if q not in self.team_calibrator_[side].Q_]
        if missing:
            raise ValueError(f"{self.name} was not fitted for confidences {missing}")
        cdf = _side_cdf(self.nb_, rows, side)
        median = lb_at_level(cdf, self._team_levels(rows, side, 0.5)).astype(float)
        bounds = {q: lb_at_level(cdf, self._team_levels(rows, side, q)) for q in confidences}
        out = finalize(rows.index, median, bounds)
        # E[T] = sum_{k>=1} P(T >= k) with the recalibrated survival function
        g = np.repeat(np.asarray(rows[self.group]), MAX_SIDE) if self.group else None
        G = self.team_pit_map_[side](cdf[:, :MAX_SIDE].ravel(), g).reshape(len(rows), MAX_SIDE)
        out["mean"] = (1.0 - G).sum(axis=1)
        return out

    def team_bound_probabilities(self, rows: pd.DataFrame, side: str,
                                 confidences=CONFIDENCES) -> pd.DataFrame:
        """Columns ``p_70``... = recalibrated P(side shots >= lb_q) for the side's own bounds."""
        pred = self.team_predict(rows, side, confidences)
        out = pd.DataFrame(index=rows.index)
        for q in confidences:
            c = round(q * 100)
            out[f"p_{c}"] = self.team_prob_at_least(rows, side, pred[f"lb_{c}"].to_numpy())
        return out

    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        missing = [q for q in confidences if q not in self.calibrator_.Q_]
        if missing:
            raise ValueError(f"{self.name} was not fitted for confidences {missing}")
        cdf = _cdf(self.nb_, rows)
        median = lb_from_cdf(cdf, 0.5).astype(float)  # integer median, as negbin
        bounds = {q: lb_at_level(cdf, self._levels(rows, q)) for q in confidences}
        return finalize(rows.index, median, bounds)

    def prob_at_least(self, rows: pd.DataFrame, n) -> np.ndarray:
        """Recalibrated P(total >= n_i) per row: 1 - G(F(n_i - 1)), G = PIT map of the
        calibration window (per group with fallback). Use it to state the honest confidence of
        an integer bound (it is >= q for the bounds of ``target='pit'``)."""
        cdf = _cdf(self.nb_, rows)
        n = np.broadcast_to(np.asarray(n), (len(rows),)).astype(int)
        t = cdf_at(cdf, n - 1)
        return 1.0 - self.pit_map_(t, rows[self.group] if self.group else None)

    def bound_probabilities(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        """Columns ``p_70``... = recalibrated P(total >= lb_q) for this model's own bounds."""
        pred = self.predict(rows, confidences)
        out = pd.DataFrame(index=rows.index)
        for q in confidences:
            out[f"p_{round(q * 100)}"] = self.prob_at_least(rows, pred[f"lb_{round(q * 100)}"].to_numpy())
        return out


class NegBinCalComp(NegBinCal):
    """Exact-coverage Mondrian conformal per competition on the latest 2 seasons (fallback global
    below 300 rows)."""

    name = "negbin_cal_comp"

    def __init__(self, **kw):
        kw = {"group": "competition", "window_seasons": 2, **kw}
        super().__init__(**kw)


class NegBinPitComp(NegBinCal):
    """PIT recalibration per competition on the latest 2 seasons (fallback global below 300 rows).
    Recommended candidate (reports/2026-10-08_models_3b.md)."""

    name = "negbin_pit_comp"

    def __init__(self, **kw):
        kw = {"target": "pit", "group": "competition", "window_seasons": 2, **kw}
        super().__init__(**kw)


class Blend(ShotModel):
    name = "blend"

    def __init__(self, weight: float = 0.5, group: str | None = None, min_group: int = 300,
                 shrink: float = 0.0, half_life_days: float | None = None, cal_half: bool = False,
                 min_cal_rows: int = 3000, confidences=CONFIDENCES):
        self.weight = weight
        self.group = group
        self.min_group = min_group
        self.shrink = shrink
        self.half_life_days = half_life_days
        self.cal_half = cal_half
        self.min_cal_rows = min_cal_rows
        self.confidences = tuple(confidences)

    def _raw(self, rows: pd.DataFrame) -> tuple[dict[float, np.ndarray], np.ndarray]:
        cdf = _cdf(self.nb_, rows)
        g = self.qg_.raw_quantiles(rows)
        w = self.weight
        raw = {q: w * continuous_quantile(cdf, 1 - q) + (1 - w) * g[round(1 - q, 6)]
               for q in self.confidences}
        median = w * lb_from_cdf(cdf, 0.5) + (1 - w) * g[0.5]
        return raw, median

    def fit(self, train: pd.DataFrame) -> "Blend":
        tr = clean_train(train)
        fit_df, cal_df = carve_calibration(tr, self.min_cal_rows)
        self.cal_seasons_ = sorted(cal_df["season"].astype(str).unique())
        es_mask = hash_half(cal_df["match_id"])
        self.qg_ = QuantileGBM(self.confidences)
        self.qg_.categories_ = sorted(fit_df["competition"].unique())
        self.qg_.fit_boosters(fit_df, cal_df[es_mask])
        self.nb_ = NegBinModel().fit(fit_df)
        conf_df = cal_df[~es_mask] if self.cal_half else cal_df
        raw, _ = self._raw(conf_df)
        y = conf_df[TARGET].to_numpy(float)
        self.calibrator_ = ConformalShift(self.group, self.min_group, self.shrink, self.half_life_days).fit(
            {q: raw[q] - y for q in self.confidences},
            conf_df[self.group] if self.group else None, conf_df["date"])
        groups = conf_df[self.group] if self.group else None
        self.cal_report_ = {
            q: dict(raw_coverage=_coverage(y, ceil_int(raw[q])),
                    cal_coverage=_coverage(y, ceil_int(raw[q] - self.calibrator_.threshold(q, groups, len(y)))),
                    n=len(y))
            for q in self.confidences
        }
        return self

    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        missing = [q for q in confidences if q not in self.calibrator_.Q_]
        if missing:
            raise ValueError(f"{self.name} was not fitted for confidences {missing}")
        raw, median = self._raw(rows)
        groups = rows[self.group] if self.group else None
        bounds = {q: ceil_int(raw[q] - self.calibrator_.threshold(q, groups, len(rows))) for q in confidences}
        return finalize(rows.index, median, bounds)
