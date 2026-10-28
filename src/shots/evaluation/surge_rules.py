"""Pure per-fixture rules for Surge Watch's league-table signals and signal stacking (2026-10-28).

Research and numbers: ``reports/2026-10-28_surge_table_signals.md`` (``python -m shots.evaluation.surge_table``).
Everything here is a pure function of pre-match inputs (rule 1): the season-context features of
``shots.features.candidates.family_context`` (the league table built from results dated strictly before the match)
and the promotion flag of ``shots.features.recent_form`` (``form_promo``). No model, no I/O.

Season phase (per competition-season, format-aware, from the table of both teams):
- ``progress`` = share of the scheduled games already played, mean of the two teams (``ctx_progress``; for playoff
  formats, Belgium and Greece, the share of the regular season, capped at 1 in the playoff rounds);
- ``games_left`` = fewer of the two teams' remaining league games (``ctx_games_left``; NaN for playoff formats,
  where a team's season end is not known in advance);
- phase: ``run_in`` if games_left <= 6, else ``late`` if progress >= 2/3 (last third; playoff rounds of playoff
  formats are ``late``), else ``middle`` if progress >= 0.25, else ``early``. NaN progress -> ``None``.

Table inputs (``TABLE_INPUTS``, match level; NaN = unknown, the condition is then not met):
``home_rank_frac`` / ``away_rank_frac`` ((rank - 1) / (n - 1), 0 = leader), ``home_gap_title`` /
``away_gap_title`` (leader points - own), ``home_gap_safety`` / ``away_gap_safety`` (own points - best team in the
drop zone; <= 0 in the zone), ``home_games_left`` / ``away_games_left``, ``home_promo`` / ``away_promo`` (+1 promoted
this season), ``progress``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PHASES = ("early", "middle", "late", "run_in")
EARLY_MAX = 0.25
LATE_MIN = 2.0 / 3.0
RUN_IN_LEFT = 6

TABLE_INPUTS = ("home_rank_frac", "away_rank_frac", "home_gap_title", "away_gap_title", "home_gap_safety",
                "away_gap_safety", "home_games_left", "away_games_left", "home_promo", "away_promo", "progress")


def _num(x, n: int | None = None) -> np.ndarray:
    a = pd.to_numeric(pd.Series(x), errors="coerce").to_numpy(float) if not isinstance(x, np.ndarray) else x.astype(float)
    return a if n is None else np.broadcast_to(a, (n,)).astype(float)


def season_phase(progress, games_left_min) -> np.ndarray:
    """Phase label per match (object array of ``PHASES`` or None), see the module docstring."""
    p = _num(progress)
    gl = _num(games_left_min, len(p))
    out = np.full(len(p), None, dtype=object)
    with np.errstate(invalid="ignore"):
        out[p < EARLY_MAX] = "early"
        out[(p >= EARLY_MAX) & (p < LATE_MIN)] = "middle"
        out[p >= LATE_MIN] = "late"
        out[(gl <= RUN_IN_LEFT) & ~np.isnan(p)] = "run_in"
    return out


def table_inputs(feats: pd.DataFrame) -> pd.DataFrame:
    """``TABLE_INPUTS`` from a candidate (+ form) feature frame (columns ``{side}_ctx_rank_frac``,
    ``{side}_ctx_gap_title``, ``{side}_ctx_gap_safety``, ``{side}_ctx_games_left``, ``ctx_progress`` and
    ``{side}_form_promo``; any missing column gives NaN). Index preserved."""
    def col(name):
        return pd.to_numeric(feats[name], errors="coerce") if name in feats else pd.Series(np.nan, index=feats.index)

    out = {}
    for side in ("home", "away"):
        for k in ("rank_frac", "gap_title", "gap_safety", "games_left"):
            out[f"{side}_{k}"] = col(f"{side}_ctx_{k}")
        out[f"{side}_promo"] = col(f"{side}_form_promo")
    out["progress"] = col("ctx_progress")
    return pd.DataFrame(out, index=feats.index)[list(TABLE_INPUTS)]


def _get(x: pd.DataFrame, c: str) -> np.ndarray:
    return pd.to_numeric(x[c], errors="coerce").to_numpy(float) if c in x else np.full(len(x), np.nan)


def phase_of(x: pd.DataFrame) -> np.ndarray:
    return season_phase(_get(x, "progress"), np.fmin(_get(x, "home_games_left"), _get(x, "away_games_left")))


def _in(ph: np.ndarray, phases) -> np.ndarray:
    return np.isin(ph.astype(str), list(phases))


# --------------------------------------------------------------------------- parametrised families (research grid)

def title_race(x: pd.DataFrame, pts: float = 3, phases=("late", "run_in")) -> np.ndarray:
    """Both teams within ``pts`` points of first place, in one of ``phases``."""
    with np.errstate(invalid="ignore"):
        return (_get(x, "home_gap_title") <= pts) & (_get(x, "away_gap_title") <= pts) & _in(phase_of(x), phases)


def six_pointer(x: pd.DataFrame, pts: float = 3, phases=("late", "run_in")) -> np.ndarray:
    """Relegation six-pointer: both teams within ``pts`` points of the safety line (either side of it)."""
    with np.errstate(invalid="ignore"):
        return ((np.abs(_get(x, "home_gap_safety")) <= pts) & (np.abs(_get(x, "away_gap_safety")) <= pts)
                & _in(phase_of(x), phases))


def top_bottom(x: pd.DataFrame, gap: float = 0.6, phases=("middle", "late", "run_in"), home_top: bool | None = None) -> np.ndarray:
    """Table mismatch: rank-fraction gap >= ``gap`` (0 = leader, 1 = last); ``home_top`` True/False restricts to the
    home / away side being the higher one."""
    d = _get(x, "away_rank_frac") - _get(x, "home_rank_frac")  # > 0: home higher in the table
    with np.errstate(invalid="ignore"):
        if home_top is None:
            ok = np.abs(d) >= gap
        else:
            ok = (d >= gap) if home_top else (-d >= gap)
        return ok & _in(phase_of(x), phases)


def mid_table(x: pd.DataFrame, k: float = 1.5, lo: float = 0.2, hi: float = 0.8, phases=("late", "run_in")) -> np.ndarray:
    """Neither team can realistically move: both in the middle ranks [lo, hi] and both more than ``k`` x games left
    points from the title and from the drop line (3 x games left = mathematically out)."""
    out = np.ones(len(x), bool)
    with np.errstate(invalid="ignore"):
        for s in ("home", "away"):
            gl = _get(x, f"{s}_games_left")
            rf = _get(x, f"{s}_rank_frac")
            out &= (rf >= lo) & (rf <= hi) & (_get(x, f"{s}_gap_title") > k * gl) & (_get(x, f"{s}_gap_safety") > k * gl)
        return out & _in(phase_of(x), phases)


def needs_win(x: pd.DataFrame, pts: float = 3, both: bool = False, phases=("run_in",)) -> np.ndarray:
    """A team near the drop line (|gap to safety| <= ``pts``) in ``phases``; ``both`` = both teams."""
    with np.errstate(invalid="ignore"):
        h = np.abs(_get(x, "home_gap_safety")) <= pts
        a = np.abs(_get(x, "away_gap_safety")) <= pts
        return ((h & a) if both else (h | a)) & _in(phase_of(x), phases)


def promoted(x: pd.DataFrame, where: str = "any", phases=("middle", "late", "run_in")) -> np.ndarray:
    """A promoted team (``promo`` = +1) placed ``where``: "top" (rank_frac <= 0.25), "bottom" (>= 0.75),
    "any"; in ``phases``."""
    out = np.zeros(len(x), bool)
    with np.errstate(invalid="ignore"):
        for s in ("home", "away"):
            rf = _get(x, f"{s}_rank_frac")
            pos = {"top": rf <= 0.25, "bottom": rf >= 0.75, "any": ~np.isnan(rf)}[where]
            out |= (_get(x, f"{s}_promo") == 1) & pos
        return out & _in(phase_of(x), phases)


FAMILIES = {"title_race": title_race, "six_pointer": six_pointer, "top_bottom": top_bottom, "mid_table": mid_table,
            "needs_win": needs_win, "promoted": promoted}


# --------------------------------------------------------------------------- the adopted definitions (live rule)

# signal id -> (family, keyword arguments). top_vs_bottom and promoted_struggling: the definitions chosen on the
# discovery seasons 2019/20-2022/23 only (largest |z| of the excess on the no-odds route, discovery n >= 150;
# ``surge_table.select`` re-derives and checks them). title_race: did not reach the discovery minimum n (103 < 150)
# and is chosen with a relaxed n >= 100 after the fact, so the catalogue holds it back (numbers shown, never applied).
# Report: reports/2026-10-28_surge_table_signals.md.
SELECTED: dict[str, tuple[str, dict]] = {
    "top_vs_bottom": ("top_bottom", {"gap": 0.6, "phases": ("late", "run_in")}),
    "promoted_struggling": ("promoted", {"where": "bottom", "phases": ("late", "run_in")}),
    "title_race": ("title_race", {"pts": 6, "phases": ("late", "run_in")}),
}


def segment_name(family: str, kwargs: dict) -> str:
    """The research segment name of a definition (report JSON key)."""
    parts = [f"{k}={'+'.join(v) if isinstance(v, (tuple, list)) else v}" for k, v in kwargs.items()]
    return f"table {family}({', '.join(parts)})"


def table_flags(x: pd.DataFrame, selected: dict[str, tuple[str, dict]] | None = None) -> pd.DataFrame:
    """Boolean column per signal id of ``selected`` (default ``SELECTED``) from ``TABLE_INPUTS`` (missing columns or
    NaN values = condition not met)."""
    sel = SELECTED if selected is None else selected
    return pd.DataFrame({sid: np.asarray(FAMILIES[fam](x, **kw), bool) for sid, (fam, kw) in sel.items()},
                        index=x.index)


# --------------------------------------------------------------------------- stacking

STACK_GROUPS = ("0", "1", "2", "3+")
STRONG_MIN_BOOSTS = 2


def stack_group(n_boosts) -> np.ndarray:
    """"0" / "1" / "2" / "3+" from the number of applying boost signals of a fixture."""
    n = np.asarray(n_boosts, int)
    return np.array([STACK_GROUPS[min(int(v), 3)] for v in n], dtype=object)


def strong(n_boosts, min_boosts: int = STRONG_MIN_BOOSTS) -> np.ndarray:
    """"Strong surge candidate": at least ``min_boosts`` applying boost signals (after the season-end rule)."""
    return np.asarray(n_boosts, int) >= min_boosts


__all__ = ["strong", "segment_name", "PHASES", "TABLE_INPUTS", "season_phase", "table_inputs", "phase_of", "table_flags", "stack_group",
           "title_race", "six_pointer", "top_bottom", "mid_table", "needs_win", "promoted", "FAMILIES", "SELECTED"]
