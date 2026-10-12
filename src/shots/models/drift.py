"""League-drift candidate: ``negbin_pit_comp`` + league-wide level-shift terms (improvement #2).

Motivation (reports/2026-10-08_ship_decision.md, reports/2026-10-09_league_drift_features.md): the
production model lags league-wide level shifts early in a season (2026/27: median about 1 shot
too low in August, 0.5 in September) because its level comes from the season-to-date league prior
and team rolling form, which need weeks to absorb a new level.

``NegBinDriftModel`` is ``NegBinModel`` with ``DriftDesign``: the production design plus, per side,
  * one relative drift term per entry of ``drift_terms``: log1p(clip(drift / prev, -0.5, 1)),
    prev = ``league_prev_level_shots`` (the competition's previous-season level). With the log
    link a coefficient of 1 means "the drift passes fully into the rate". Terms:
    ``global`` -> ``league_global_drift28``, ``recent28`` -> ``league_recent28_drift``,
    ``level`` -> ``league_level_drift``, ``recent100`` -> ``league_recent100_drift``;
  * optionally (``early_days``) each term again multiplied by an early-season weight
    w = clip(1 - days_since_season_start / early_days, 0, 1), so the drift can matter more before the
    rolling features catch up;
  * ``base="prev"``: where the season-to-date prior is a fallback (``league_prior_n`` < 20, i.e.
    the first round or so), the league base level and the cold-start fills use the previous-season
    level instead of the competition's all-time mean.
All inputs are strictly pre-match features from ``features.parquet``. Missing drift values (cold
start) are 0 (no drift); missing prev falls back to the league prior.

``NegBinPitCompDrift`` (registry ``negbin_pit_comp_drift``) is ``negbin_pit_comp`` with this NB as its
base model; calibration (per-competition PIT map on the latest 2 seasons), the team methods and
the no-odds variant (market features blanked) work unchanged. Variant selection and backtest:
reports/2026-10-09_drift_model.md. Status: candidate only, NOT adopted (no measurable gain over
negbin_pit_comp on the hold-out seasons).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shots.models.calibrated import NegBinPitComp
from shots.models.negbin import NegBinModel, _Design

PRIOR_MIN_OBS = 20  # features/team_form.py: season-to-date prior needs >= 20 team-observations
DRIFT_SOURCES = {
    "global": "league_global_drift28",
    "recent28": "league_recent28_drift",
    "level": "league_level_drift",
    "recent100": "league_recent100_drift",
}
PREV_COL = "league_prev_level_shots"
DAYS_COL = "days_since_season_start"


def drift_inputs(drift_terms, base: str, early_days) -> list[str]:
    cols = [PREV_COL, *(DRIFT_SOURCES[t] for t in drift_terms)]
    if early_days:
        cols.append(DAYS_COL)
    if base == "prev":
        cols.append("league_prior_n")
    return cols


class DriftDesign(_Design):
    def __init__(self, side: str, drift_terms=("global",), base: str = "prior",
                 early_days: float | None = None):
        super().__init__(side)
        unknown = set(drift_terms) - set(DRIFT_SOURCES)
        if unknown:
            raise ValueError(f"unknown drift terms {sorted(unknown)}; choose from {sorted(DRIFT_SOURCES)}")
        if base not in ("prior", "prev"):
            raise ValueError(f"base must be 'prior' or 'prev', got {base!r}")
        self.drift_terms = tuple(drift_terms)
        self.base = base
        self.early_days = early_days

    def _require(self, df: pd.DataFrame) -> None:
        missing = [c for c in drift_inputs(self.drift_terms, self.base, self.early_days) if c not in df]
        if missing:
            raise KeyError(f"drift model needs feature columns {missing}; rebuild features.parquet "
                           "(features/team_form.py league-drift block)")

    def _rebase(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.base != "prev":
            return df
        prior = df["league_prior_shots"].astype(float)
        fallback = df["league_prior_n"].fillna(0).to_numpy() < PRIOR_MIN_OBS
        prev = df[PREV_COL].astype(float)
        out = df.copy()
        out["league_prior_shots"] = np.where(fallback & prev.notna().to_numpy(), prev, prior)
        return out

    def fit(self, df: pd.DataFrame) -> "DriftDesign":
        self._require(df)
        return super().fit(self._rebase(df))

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        self._require(df)
        df = self._rebase(df)
        X = super().transform(df)
        prior = df["league_prior_shots"].astype(float).fillna(self.prior_mean_).clip(lower=1.0)
        prev = df[PREV_COL].astype(float).fillna(prior).clip(lower=1.0)
        w = None
        if self.early_days:
            days = df[DAYS_COL].astype(float).fillna(0.0)
            w = (1.0 - days / float(self.early_days)).clip(0.0, 1.0)
        for t in self.drift_terms:
            d = df[DRIFT_SOURCES[t]].astype(float).fillna(0.0)
            X[f"drift_{t}"] = np.log1p((d / prev).clip(-0.5, 1.0))
            if w is not None:
                X[f"drift_{t}_early"] = X[f"drift_{t}"] * w
        return X


class NegBinDriftModel(NegBinModel):
    name = "negbin_drift"

    def __init__(self, drift_terms=("global",), base: str = "prior", early_days: float | None = None,
                 **kw):
        super().__init__(**kw)
        self.drift_terms = tuple(drift_terms)
        self.base = base
        self.early_days = early_days

    def _design(self, side: str) -> DriftDesign:
        return DriftDesign(side, self.drift_terms, self.base, self.early_days)

    def extra_inputs(self) -> list[str]:
        """Feature columns read on top of the production negbin inputs (for persist metadata)."""
        return drift_inputs(self.drift_terms, self.base, self.early_days)


class NegBinPitCompDrift(NegBinPitComp):
    """``negbin_pit_comp`` whose base NB carries league-drift terms (see module docstring).
    Defaults = the variant chosen on validation (reports/2026-10-09_drift_model.md)."""

    name = "negbin_pit_comp_drift"

    def __init__(self, drift_terms=("global", "level"), base: str = "prior",
                 early_days: float | None = None, **kw):
        super().__init__(**kw)
        self.drift_terms = tuple(drift_terms)
        self.base = base
        self.early_days = early_days

    def _new_nb(self) -> NegBinDriftModel:
        return NegBinDriftModel(self.drift_terms, self.base, self.early_days)


__all__ = ["DriftDesign", "NegBinDriftModel", "NegBinPitCompDrift", "DRIFT_SOURCES", "drift_inputs"]
