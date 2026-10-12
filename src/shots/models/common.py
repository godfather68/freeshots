"""Shared helpers for the shot models: feature lists, calibration split, integer bounds.

Integer lower bound convention (see ``base.py``): the bound at confidence q is the largest
integer N with P(total >= N) >= q. Because shots are integers, ``total >= x`` is equivalent to
``total >= ceil(x)`` for any real x, so a real-valued threshold that is valid at level q can be
turned into an integer bound with ``ceil`` without losing coverage.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shots.models.base import CONFIDENCES, TARGET, bound_column

TARGETS = ["home_shots", "away_shots", "total_shots"]
IDENTIFIERS = ["match_id", "date", "season", "competition", "home_team", "away_team",
               "home_team_id", "away_team_id"]

SIDES = ("home", "away")
# Per-side numeric pre-match features (prefixed home_/away_ in features.parquet).
SIDE_FEATURES = [
    *(f"{s}_{d}_{w}" for s in ("shots", "sot") for d in ("for", "against")
      for w in ("l5", "l10", "std", "ewm", "venue_l5")),
    "rest_days", "n_prior_matches", "n_prior_season", "tier_change",
    "shots_for_shrunk", "shots_against_shrunk",
]
MATCH_FEATURES = [
    "tier", "league_prior_shots", "league_prior_n",
    "mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25",
]
# All numeric pre-match features. Identifiers and targets are never in this list.
FEATURES = [f"{side}_{f}" for side in SIDES for f in SIDE_FEATURES] + MATCH_FEATURES
assert not set(FEATURES) & set(TARGETS + IDENTIFIERS)

EPS = 1e-9


def clean_train(train: pd.DataFrame, cols=TARGETS) -> pd.DataFrame:
    """Rows usable for fitting: all target columns present."""
    cols = [c for c in cols if c in train.columns]
    return train.dropna(subset=cols)


def season_order(seasons: pd.Series) -> list[str]:
    """Season labels ('2015/2016') sorted chronologically (lexical order works for YYYY/YYYY)."""
    return sorted(pd.unique(seasons.astype(str)))


def carve_calibration(train: pd.DataFrame, min_rows: int = 3000) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split ``train`` into (fit, calibration) by time: calibration = latest season(s).

    Takes the latest season label; if it has fewer than ``min_rows`` rows (e.g. a season in
    progress) earlier seasons are added until it does. Never touches prediction rows.
    """
    seasons = season_order(train["season"])
    cal_seasons: list[str] = []
    n = 0
    for s in reversed(seasons):
        if len(cal_seasons) == len(seasons) - 1:
            break  # always keep at least one season to fit on
        cal_seasons.append(s)
        n += int((train["season"] == s).sum())
        if n >= min_rows:
            break
    is_cal = train["season"].isin(cal_seasons)
    return train[~is_cal], train[is_cal]


def hash_half(ids: pd.Series) -> np.ndarray:
    """Deterministic ~50/50 boolean split keyed on match_id (stable across runs)."""
    h = pd.util.hash_pandas_object(ids.astype(str), index=False).to_numpy()
    return (h % 2).astype(bool)


def threshold_for_coverage(scores: np.ndarray, q: float, finite_sample: bool = True) -> float:
    """Smallest t with share(scores <= t) >= q (conformal (n+1) correction if finite_sample)."""
    s = np.sort(np.asarray(scores, dtype=float))
    n = len(s)
    k = int(np.ceil((n + 1) * q)) if finite_sample else int(np.ceil(n * q))
    k = min(max(k, 1), n)
    return float(s[k - 1])


def lb_from_cdf(cdf: np.ndarray, q: float) -> np.ndarray:
    """Integer bounds from per-row CDF arrays ``cdf[i, k] = P(T_i <= k)``.

    N = max{n : P(T >= n) >= q} = min{k : F(k) > 1 - q}.
    """
    above = cdf > (1.0 - q) + EPS
    idx = above.argmax(axis=1)
    idx[~above.any(axis=1)] = cdf.shape[1]  # tail beyond the support grid (practically never)
    return idx.astype(int)


def ceil_int(x) -> np.ndarray:
    return np.ceil(np.asarray(x, dtype=float) - 1e-7).astype(int)


def finalize(index: pd.Index, median, bounds: dict[float, np.ndarray]) -> pd.DataFrame:
    """Assemble the output frame: monotone (higher q -> lower or equal bound), non-negative ints."""
    out = pd.DataFrame(index=index)
    out["median"] = np.asarray(median, dtype=float)
    prev = None
    for q in sorted(bounds):
        b = np.maximum(np.asarray(bounds[q]).astype(int), 0)
        if prev is not None:
            b = np.minimum(b, prev)
        out[bound_column(q)] = b.astype(int)
        prev = b
    return out


def pinball(y: np.ndarray, pred: np.ndarray, tau: float) -> float:
    d = np.asarray(y, float) - np.asarray(pred, float)
    return float(np.mean(np.maximum(tau * d, (tau - 1) * d)))


__all__ = [
    "CONFIDENCES", "TARGET", "TARGETS", "IDENTIFIERS", "FEATURES", "bound_column",
    "clean_train", "carve_calibration", "hash_half", "threshold_for_coverage", "lb_from_cdf",
    "ceil_int", "finalize", "pinball", "season_order",
]
