"""Naive baselines (TASKS 3.1).

``league_avg``: per-competition mean total shots over the competition's latest ``n_seasons``
training seasons; bound = empirical integer bound of that competition's total-shots
distribution over the same window. Unknown competitions fall back to the global (all
competitions, same window logic on the latest seasons) distribution.

``team_avg``: point estimate from the shrunk team form features,
    median = (home_for_shrunk + away_against_shrunk)/2 + (away_for_shrunk + home_against_shrunk)/2
bounds = ceil(median + t_q), t_q = residual threshold with share(resid >= t_q) >= q on training.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shots.models.base import CONFIDENCES, TARGET, ShotModel
from shots.models.common import ceil_int, clean_train, finalize, season_order


def empirical_bound(y: np.ndarray, q: float) -> int:
    """Largest integer N with share(y >= N) >= q."""
    s = np.sort(np.asarray(y, dtype=float))
    k = int(np.floor((1 - q) * len(s) + 1e-9))
    return int(s[min(k, len(s) - 1)])


def residual_threshold(r: np.ndarray, q: float) -> float:
    """Largest t with share(r >= t) >= q."""
    s = np.sort(np.asarray(r, dtype=float))
    k = int(np.floor((1 - q) * len(s) + 1e-9))
    return float(s[min(k, len(s) - 1)])


class LeagueAverage(ShotModel):
    name = "league_avg"

    def __init__(self, n_seasons: int = 3, min_rows: int = 200):
        self.n_seasons = n_seasons
        self.min_rows = min_rows

    def fit(self, train: pd.DataFrame) -> "LeagueAverage":
        tr = clean_train(train, [TARGET])
        self.samples_: dict[str, np.ndarray] = {}
        for comp, g in tr.groupby("competition", sort=False):
            keep = season_order(g["season"])[-self.n_seasons:]
            y = g.loc[g["season"].isin(keep), TARGET].to_numpy()
            if len(y) >= self.min_rows:
                self.samples_[comp] = y
        keep = season_order(tr["season"])[-self.n_seasons:]
        self.global_ = tr.loc[tr["season"].isin(keep), TARGET].to_numpy()
        return self

    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        stats = {}
        for comp in rows["competition"].unique():
            y = self.samples_.get(comp, self.global_)
            stats[comp] = (y.mean(), {q: empirical_bound(y, q) for q in confidences})
        comp = rows["competition"]
        median = comp.map(lambda c: stats[c][0]).to_numpy(float)
        bounds = {q: comp.map(lambda c, q=q: stats[c][1][q]).to_numpy(int) for q in confidences}
        return finalize(rows.index, median, bounds)


def team_avg_point(df: pd.DataFrame) -> pd.Series:
    h = (df["home_shots_for_shrunk"] + df["away_shots_against_shrunk"]) / 2
    a = (df["away_shots_for_shrunk"] + df["home_shots_against_shrunk"]) / 2
    pt = h + a
    # shrunk features are always present in features.parquet; guard anyway with the league prior
    return pt.fillna(2 * df["league_prior_shots"]).fillna(23.5)


class TeamAverage(ShotModel):
    name = "team_avg"

    def fit(self, train: pd.DataFrame) -> "TeamAverage":
        tr = clean_train(train, [TARGET])
        self.resid_ = (tr[TARGET] - team_avg_point(tr)).dropna().to_numpy()
        return self

    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        m = team_avg_point(rows).to_numpy(float)
        med = m + float(np.median(self.resid_))
        bounds = {q: ceil_int(m + residual_threshold(self.resid_, q)) for q in confidences}
        return finalize(rows.index, med, bounds)
