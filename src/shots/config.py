"""Production switches. Each one is a single line; the defaults are production as validated before 2026-10-16.

Evidence and the exact user-visible changes: reports/2026-10-16_fin_c_adoption.md.

``NOODDS_MODEL``
    Model of the NO-ODDS route (domestic fixtures without published odds; most of the board).
    ``"negbin_pit_comp"`` = production. ``"fin_c"`` = the candidate (production NB + opponent-adjusted
    ratings, head-to-head, season context, shrinkage toward the league level; ``shots.models.candidates.FinC``).
    With ``"fin_c"``, the update builds the candidate features once per run, and every fixture whose
    candidate features are missing is scored by the production no-odds model instead (logged; the step fails
    when more than ``FALLBACK_MAX_SHARE`` of the non-cold-start no-odds fixtures fall back). The odds route and
    the cross-border (UEFA) route keep production in both settings.
``MEDIAN_FROM_CURVE``
    ``False`` = production: ``expected`` (the integer median) is read off the raw NB before recalibration.
    ``True`` = read it off the published (recalibrated) curve at its 0.5 level, as ``team_predict`` already does,
    so that ``expected`` and ``P(total >= n)`` always agree. Applies to every route.

``ODDS_MODEL``
    Model of the ODDS route (domestic fixtures with published odds). ``"negbin_pit_comp"`` = production.
    ``"cand_odds_oos"`` = opt-in candidate, NOT adopted (reports/2026-10-26_overshoot_patterns.md): the production
    NB + an out-of-sample correction of the total (season end, head to head, rating disagreement, recent form,
    goal form, league drift; ``shots.models.candidates.OOS_TERMS``). With it, the update builds the candidate
    features (families A/E/G + recent form) once per run, odds fixtures whose candidate rows are missing fall back
    to the production odds model (same ``FALLBACK_MAX_SHARE`` rule, reported as ``odds_route`` in board.meta.json).

Environment overrides (dry runs, tests): ``SHOTS_NOODDS_MODEL``, ``SHOTS_ODDS_MODEL`` and ``SHOTS_MEDIAN_FROM_CURVE``
(``1`` / ``0``).
The scheduled pipeline sets neither, so the lines below decide what is published.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

NOODDS_MODEL = "fin_c"            # adopted 2026-10-20 (user approval); was "negbin_pit_comp"
MEDIAN_FROM_CURVE = True           # adopted 2026-10-20 (user approval)

FALLBACK_MAX_SHARE = 0.02          # fin_c route: share of non-cold-start no-odds fixtures allowed to fall back
CANDIDATE_NOODDS_MODELS = ("fin_c",)
ODDS_MODEL = "negbin_pit_comp"     # production; opt-in candidate "cand_odds_oos" (2026-10-26), not adopted
CANDIDATE_ODDS_MODELS = ("cand_odds_oos",)


def noodds_model() -> str:
    name = os.environ.get("SHOTS_NOODDS_MODEL") or NOODDS_MODEL
    if name not in ("negbin_pit_comp", *CANDIDATE_NOODDS_MODELS):
        raise ValueError(f"unknown no-odds model {name!r} (config.NOODDS_MODEL / SHOTS_NOODDS_MODEL)")
    return name


def candidate_noodds() -> bool:
    """True when the no-odds route uses a candidate model that needs ``shots.features.candidates``."""
    return noodds_model() in CANDIDATE_NOODDS_MODELS


def odds_model() -> str:
    name = os.environ.get("SHOTS_ODDS_MODEL") or ODDS_MODEL
    if name not in ("negbin_pit_comp", *CANDIDATE_ODDS_MODELS):
        raise ValueError(f"unknown odds model {name!r} (config.ODDS_MODEL / SHOTS_ODDS_MODEL)")
    return name


def candidate_odds() -> bool:
    """True when the odds route uses a candidate model that needs ``shots.features.candidates``."""
    return odds_model() in CANDIDATE_ODDS_MODELS


def median_from_curve() -> bool:
    raw = os.environ.get("SHOTS_MEDIAN_FROM_CURVE")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(MEDIAN_FROM_CURVE)


__all__ = ["NOODDS_MODEL", "MEDIAN_FROM_CURVE", "FALLBACK_MAX_SHARE", "ODDS_MODEL", "noodds_model",
           "candidate_noodds", "odds_model", "candidate_odds", "median_from_curve"]
