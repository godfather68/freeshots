"""Shot-count models implementing the ``ShotModel`` contract (see ``base.py``)."""

from shots.models.base import CONFIDENCES, TARGET, ShotModel, bound_column
from shots.models.baselines import LeagueAverage, TeamAverage
from shots.models.calibrated import Blend, NegBinCal, NegBinCalComp, NegBinPitComp
from shots.models.candidates import CANDIDATES
from shots.models.drift import NegBinPitCompDrift
from shots.models.negbin import NegBinModel
from shots.models.qgbm import QuantileGBM

MODELS: dict[str, type[ShotModel]] = {
    "league_avg": LeagueAverage,
    "team_avg": TeamAverage,
    "negbin": NegBinModel,
    "qgbm": QuantileGBM,
    "negbin_cal": NegBinCal,
    "negbin_cal_comp": NegBinCalComp,
    "negbin_pit_comp": NegBinPitComp,
    "negbin_pit_comp_drift": NegBinPitCompDrift,
    "blend": Blend,
    # structural candidates (2026-10-15), not production: reports/2026-10-15_model_candidates.md
    **CANDIDATES,
}

__all__ = ["CONFIDENCES", "TARGET", "ShotModel", "bound_column", "MODELS",
           "LeagueAverage", "TeamAverage", "NegBinModel", "QuantileGBM",
           "NegBinCal", "NegBinCalComp", "NegBinPitComp", "NegBinPitCompDrift", "Blend"]
