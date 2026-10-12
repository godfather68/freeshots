"""Post-hoc calibration of lower bounds (TASKS 3b.1-3b.3).

Two conformal score types, both one-sided (we only care that ``total >= bound`` holds with
rate q), used by ``ConformalShift`` (target: integer-bound coverage ~= q exactly):

* ``additive`` (CQR, Romano et al. 2019): the base model gives a continuous lower quantile
  ``qhat``; score ``E = qhat - y``. Bound ``ceil(qhat - Q)`` covers ``y`` iff ``E <= Q``
  (y integer), so taking ``Q`` as the conformal q-quantile of E on calibration rows gives
  coverage >= q under exchangeability. The correction is a constant number of shots.

* ``level`` (probability-level / PIT recalibration) for models with a full predictive CDF ``F``:
  score ``e = 1 - F(y)``. With ``lb_t = min{k : F(k) > t}`` we have ``y >= lb_t`` iff
  ``F(y) > t`` iff ``e < 1 - t``, so ``t = 1 - Q - 1e-9`` with ``Q`` the conformal q-quantile of
  ``e`` gives the same finite-sample guarantee. The correction moves the *probability level*
  used to read the quantile, so it scales with each match's predictive spread.

PIT recalibration (``PitLevel``, target: a calibrated predictive CDF, not exact coverage):
choose the level t_q at which the CDF is read so that the non-randomized PIT of the calibration
rows has mass exactly 1 - q below t_q (``pit_level``). The bound min{k : F(k) > t_q} is then the
pinball-optimal integer of the recalibrated CDF; with integer outcomes its coverage is >= q,
exceeding q by up to the pmf at the bound (granularity), which is the price of integer bounds
that do not waste pinball.

Group-conditional (Mondrian) variant: thresholds per group (competition or tier) computed on that
group's calibration rows only, with fallback to the global threshold for groups with fewer than
``min_group`` rows (and for groups unseen in calibration). With ``shrink > 0`` the group threshold
is pulled towards the global one, ``w = n_g / (n_g + shrink)`` (empirical-Bayes flavour; trades
the per-group guarantee for lower variance).

Optional recency weighting (``half_life_days``): weighted conformal quantile (Tibshirani et al.
2019, Barber et al. 2023 "non-exchangeable conformal"): calibration row i gets weight
``0.5 ** (age_i / half_life)`` with age measured from the latest calibration date; the test
point gets weight 1.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

GLOBAL = "__global__"


def conformal_quantile(scores, q: float, weights=None) -> float:
    """Smallest t with (weighted) share(scores <= t) >= q, conformal finite-sample version.

    Unweighted: the ceil((n+1) q)-th smallest score (capped at the max).
    Weighted: normalise weights with an extra unit mass for the test point (placed at +inf)
    and take the smallest score whose cumulative mass reaches q.
    """
    s = np.asarray(scores, dtype=float)
    if len(s) == 0:
        raise ValueError("no calibration scores")
    order = np.argsort(s, kind="stable")
    s = s[order]
    if weights is None:
        n = len(s)
        k = min(max(int(np.ceil((n + 1) * q - 1e-9)), 1), n)
        return float(s[k - 1])
    w = np.asarray(weights, dtype=float)[order]
    cum = np.cumsum(w) / (w.sum() + 1.0)
    k = int(np.searchsorted(cum, q - 1e-12, side="left"))
    return float(s[min(k, len(s) - 1)])


def recency_weights(dates: pd.Series, half_life_days: float | None) -> np.ndarray | None:
    if not half_life_days:
        return None
    d = pd.to_datetime(dates)
    age = (d.max() - d).dt.days.to_numpy(float)
    return 0.5 ** (age / float(half_life_days))


def pit_level(Fm1, F, p: float, weights=None) -> float:
    """Level t with (weighted) mean_i u_i(t) = p, where u_i(t) = clip((t - F_i(y_i - 1)) /
    (F_i(y_i) - F_i(y_i - 1)), 0, 1) is the CDF of the randomized PIT of row i (Czado, Gneiting &
    Held 2009, non-randomized PIT). Deterministic; solved by bisection on [0, 1].

    If the model is calibrated, t = p. ``min{k : F(k) > t}`` then covers with rate >= 1 - p.
    """
    Fm1, F = np.asarray(Fm1, float), np.asarray(F, float)
    w = np.ones(len(F)) if weights is None else np.asarray(weights, float)
    den = np.maximum(F - Fm1, 1e-12)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        t = 0.5 * (lo + hi)
        u = np.average(np.clip((t - Fm1) / den, 0.0, 1.0), weights=w)
        lo, hi = (t, hi) if u < p else (lo, t)
    return 0.5 * (lo + hi)


class GroupedThreshold:
    """Per-confidence (and optionally per-group) thresholds computed by ``_stat``.

    ``fit(data, groups, dates)`` with ``data = {q: array or tuple of arrays}`` (row-aligned with
    the calibration rows); ``threshold(q, groups, n_rows)`` -> per-row thresholds for new rows.
    Groups with fewer than ``min_group`` calibration rows, and unseen groups, use the global value.
    """

    def __init__(self, group_col: str | None = None, min_group: int = 300, shrink: float = 0.0,
                 half_life_days: float | None = None):
        self.group_col = group_col
        self.min_group = min_group
        self.shrink = shrink
        self.half_life_days = half_life_days

    def _stat(self, data, q: float, w) -> float:  # pragma: no cover - abstract
        raise NotImplementedError

    @staticmethod
    def _subset(data, m):
        return tuple(a[m] for a in data) if isinstance(data, tuple) else data[m]

    def fit(self, data: dict, groups: pd.Series | None = None,
            dates: pd.Series | None = None) -> "GroupedThreshold":
        w = recency_weights(dates, self.half_life_days) if dates is not None else None
        self.Q_: dict[float, dict] = {}
        self.group_n_: dict = {}
        for q, d in data.items():
            d = tuple(np.asarray(a, float) for a in d) if isinstance(d, tuple) else np.asarray(d, float)
            table = {GLOBAL: self._stat(d, q, w)}
            if self.group_col is not None:
                g = np.asarray(groups)
                for key in pd.unique(g):
                    m = g == key
                    n = int(m.sum())
                    self.group_n_[key] = n
                    if n < self.min_group:
                        continue
                    Qg = self._stat(self._subset(d, m), q, None if w is None else w[m])
                    if self.shrink > 0:
                        lam = n / (n + self.shrink)
                        Qg = lam * Qg + (1 - lam) * table[GLOBAL]
                    table[key] = Qg
            self.Q_[q] = table
        return self

    def threshold(self, q: float, groups: pd.Series | None, n_rows: int) -> np.ndarray:
        table = self.Q_[q]
        if self.group_col is None or groups is None:
            return np.full(n_rows, table[GLOBAL])
        return pd.Series(np.asarray(groups)).map(table).fillna(table[GLOBAL]).to_numpy(float)

    def summary(self) -> dict:
        return {str(q): {str(k): round(float(v), 4) for k, v in t.items()} for q, t in self.Q_.items()}


class ConformalShift(GroupedThreshold):
    """Conformal q-quantile of conformity scores (coverage iff score <= threshold)."""

    def _stat(self, data, q, w):
        return conformal_quantile(data, q, w)


class PitLevel(GroupedThreshold):
    """Recalibrated CDF level t_q = ``pit_level(Fm1, F, 1 - q)``; data = (F(y - 1), F(y))."""

    def _stat(self, data, q, w):
        Fm1, F = data
        return pit_level(Fm1, F, 1.0 - q, w)


class PitMap:
    """Recalibration map G(t) = mean_i u_i(t) (non-randomized PIT CDF of the calibration rows),
    per group with global fallback (< ``min_group`` rows or unseen group). If the model's CDF is
    F, the recalibrated CDF is G(F(k)), so a calibrated P(T >= n) = 1 - G(F(n - 1)).
    Stored on a grid of ``n_grid`` levels (G is piecewise linear and monotone)."""

    def __init__(self, group_col: str | None = None, min_group: int = 300, n_grid: int = 1001):
        self.group_col = group_col
        self.min_group = min_group
        self.n_grid = n_grid

    @staticmethod
    def _curve(Fm1, F, grid) -> np.ndarray:
        den = np.maximum(F - Fm1, 1e-12)
        out = np.empty(len(grid))
        for i in range(0, len(grid), 64):
            g = grid[i:i + 64, None]
            out[i:i + 64] = np.clip((g - Fm1[None, :]) / den[None, :], 0.0, 1.0).mean(axis=1)
        return out

    def fit(self, Fm1, F, groups=None) -> "PitMap":
        Fm1, F = np.asarray(Fm1, float), np.asarray(F, float)
        self.grid_ = np.linspace(0.0, 1.0, self.n_grid)
        self.curves_ = {GLOBAL: self._curve(Fm1, F, self.grid_)}
        if self.group_col is not None and groups is not None:
            g = np.asarray(groups)
            for key in pd.unique(g):
                m = g == key
                if m.sum() >= self.min_group:
                    self.curves_[key] = self._curve(Fm1[m], F[m], self.grid_)
        return self

    def __call__(self, t, groups=None) -> np.ndarray:
        t = np.asarray(t, float)
        out = np.interp(t, self.grid_, self.curves_[GLOBAL])
        if self.group_col is not None and groups is not None:
            g = np.asarray(groups)
            for key, curve in self.curves_.items():
                if key == GLOBAL:
                    continue
                m = g == key
                if m.any():
                    out[m] = np.interp(t[m], self.grid_, curve)
        return out


# --------------------------------------------------------------------------- CDF helpers

def cdf_at(cdf: np.ndarray, y: np.ndarray) -> np.ndarray:
    """F_i(y_i) from per-row CDF arrays (y beyond the grid -> 1)."""
    y = np.asarray(y).astype(int)
    k = np.clip(y, 0, cdf.shape[1] - 1)
    out = cdf[np.arange(len(y)), k].astype(float)
    out[y >= cdf.shape[1]] = 1.0
    out[y < 0] = 0.0
    return out


def lb_at_level(cdf: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Per-row integer bound min{k : F(k) > t_i} for per-row levels t_i."""
    t = np.asarray(t, float)
    above = cdf > t[:, None]
    idx = above.argmax(axis=1)
    idx[~above.any(axis=1)] = cdf.shape[1]
    return idx.astype(int)


def continuous_quantile(cdf: np.ndarray, p: float) -> np.ndarray:
    """Linearly interpolated quantile of a discrete CDF at level p.

    For k = min{k : F(k) > p}: x = k - 1 + (p - F(k-1)) / (F(k) - F(k-1)) in (k-1, k], so
    ``ceil(x) == k`` = the integer bound ``lb_from_cdf(cdf, 1 - p)``; x is a continuous score that
    moves smoothly with the distribution (needed for additive conformal shifts and blending).
    """
    k = lb_at_level(cdf, np.full(cdf.shape[0], p))
    k = np.minimum(k, cdf.shape[1] - 1)
    rows = np.arange(cdf.shape[0])
    Fk = cdf[rows, k].astype(float)
    Fkm1 = np.where(k > 0, cdf[rows, np.maximum(k - 1, 0)], 0.0).astype(float)
    frac = (p - Fkm1) / np.maximum(Fk - Fkm1, 1e-12)
    return (k - 1 + np.clip(frac, 0.0, 1.0)).astype(float)


__all__ = ["GroupedThreshold", "ConformalShift", "PitLevel", "PitMap", "pit_level", "conformal_quantile", "recency_weights", "cdf_at", "lb_at_level",
           "continuous_quantile", "GLOBAL"]
