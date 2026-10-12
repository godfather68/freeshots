"""Metrics for lower-bound predictions of total match shots.

A lower bound ``lb_q`` at confidence q is the (1 - q) quantile of the predictive distribution, so:

- coverage_q    = share of rows with actual >= lb_q            (target: q)
- calib_q       = coverage_q - q                                (target: 0; > 0 = too conservative)
- pinball_q     = mean pinball loss at tau = 1 - q              (lower is better)
                  rho_tau(u) = max(tau * u, (tau - 1) * u), u = actual - lb_q
- sharp_q       = mean lb_q                                     (higher is better at equal coverage)
- mae / bias    = mean |median - actual|, mean (median - actual)

``pinball_mean`` averages pinball_q over the confidences (single headline number).
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

from shots.models.base import CONFIDENCES, bound_column

ACTUAL = "actual"
COLD_START_N = 5


def pinball(actual, lb, q: float) -> float:
    """Mean pinball loss of lower bound ``lb`` (quantile tau = 1 - q) against ``actual``."""
    y = np.asarray(actual, dtype=float)
    p = np.asarray(lb, dtype=float)
    tau = 1.0 - q
    u = y - p
    return float(np.mean(np.maximum(tau * u, (tau - 1.0) * u)))


def score(df: pd.DataFrame, confidences: Iterable[float] = CONFIDENCES) -> dict[str, float]:
    """Metrics for one slice. ``df`` needs ``actual``, ``median`` and the ``lb_*`` columns.

    Rows without an actual value are ignored.
    """
    df = df[df[ACTUAL].notna()]
    y = df[ACTUAL].to_numpy(dtype=float)
    out: dict[str, float] = {"n": float(len(df))}
    if len(df) == 0:
        return out
    pins = []
    for q in confidences:
        lb = df[bound_column(q)].to_numpy(dtype=float)
        k = round(q * 100)
        cov = float(np.mean(y >= lb))
        out[f"cov_{k}"] = cov
        out[f"calib_{k}"] = cov - q
        out[f"pinball_{k}"] = pinball(y, lb, q)
        out[f"sharp_{k}"] = float(np.mean(lb))
        pins.append(out[f"pinball_{k}"])
    out["pinball_mean"] = float(np.mean(pins))
    med = df["median"].to_numpy(dtype=float)
    out["mae"] = float(np.mean(np.abs(med - y)))
    out["bias"] = float(np.mean(med - y))
    return out


def breakdown(df: pd.DataFrame, by: str | list[str] | None,
              confidences: Iterable[float] = CONFIDENCES) -> pd.DataFrame:
    """One row of ``score`` per group of ``by`` (``None`` = overall, single row named 'all')."""
    confidences = tuple(confidences)
    if by is None:
        return pd.DataFrame([score(df, confidences)], index=pd.Index(["all"], name="slice"))
    rows = {key: score(g, confidences) for key, g in df.groupby(by, sort=True, observed=True)}
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.name = by if isinstance(by, str) else "/".join(by)
    return out


def add_slices(pred: pd.DataFrame) -> pd.DataFrame:
    """Add the ``cold_start`` slice column (either team has < 5 prior matches)."""
    pred = pred.copy()
    pred["cold_start"] = pred["n_prior_min"] < COLD_START_N
    return pred


SLICES = {
    "overall": None,
    "season": "season",
    "competition": "competition",
    "tier": "tier",
    "cold_start": "cold_start",
}


def all_breakdowns(pred: pd.DataFrame, confidences: Iterable[float] = CONFIDENCES) -> dict[str, pd.DataFrame]:
    """All standard breakdowns for ONE model's predictions."""
    pred = add_slices(pred) if "cold_start" not in pred else pred
    return {name: breakdown(pred, by, confidences) for name, by in SLICES.items()}
