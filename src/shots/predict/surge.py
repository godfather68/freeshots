"""Surge signals of the upcoming board fixtures (Surge Watch). Catalogue, cut points and the rule:
``shots.evaluation.surge`` (from ``reports/2026-10-26_overshoot_patterns.md``).

Called by ``shots.predict.board.build`` after the board is scored; the result goes into ``board.meta.json`` under
``"surge"`` (and from there into the board run's ``meta`` in the database), never into a prediction row. **It never
changes a prediction**: it only reads the published expected total (the integer median) and pre-match features.

Inputs, all pre-match and causal (rule 1):
- season context (games left, nothing to play for), head-to-head and opponent-adjusted ratings: the candidate
  feature build (``shots.features.candidates`` families A/E/G, the one ``fin_c`` reads; within ``python -m shots
  update`` it is the build the no-odds route already made);
- recent form (goals in the last 5, last-3 match total minus the rating expectation):
  ``shots.features.recent_form.family_form`` on the same rows.
Each feature of a fixture uses only matches dated strictly before its date; upcoming fixtures never enter a window,
so later results cannot change a fixture's signals.

Assessed fixtures: domestic, of a competition the research covered, scored by the model the research studied for
their route (``ROUTE_MODELS``: odds -> ``negbin_pit_comp``, no odds -> ``fin_c``). Fallback, candidate and
experimental (cross-border) predictions are not assessed: the research did not study them.

**No double counting with the Surge model (since 2026-10-27).** The odds route is scored by "Surge"
(``cand_odds_surge``), which already corrects the total for the same over-shoot patterns. An odds fixture is
therefore assessed ONLY when it was scored by ``negbin_pit_comp``: today the competitions of
``shots.config.SURGE_EXCLUDED_COMPETITIONS`` (Scotland Championship), or a Surge fallback. A Surge-scored odds
fixture never gets Surge Watch labels (the catalogue's odds-route excesses describe production's residuals, part of
which Surge removes). No-odds fixtures are assessed as before (scored by ``fin_c``).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from shots.evaluation import surge as sv

log = logging.getLogger(__name__)

FORM_COLS = ("home_form_goals_l5", "away_form_goals_l5", "home_form_res_l3", "away_form_res_l3")


def with_form(rows: pd.DataFrame, cand: pd.DataFrame) -> pd.DataFrame:
    """``cand`` (indexed by match_id, with ``adj_exp_total_shots``) plus the recent-form columns of ``FORM_COLS``,
    computed on ``rows`` (played matches + upcoming fixtures, canonical columns) when the build lacks them."""
    if all(c in cand.columns for c in FORM_COLS):
        return cand
    from shots.features import candidates as fc
    from shots.features.recent_form import family_form

    m = fc._prepare(rows, None)
    exp_total = cand["adj_exp_total_shots"].reindex(m["match_id"]).to_numpy(float)
    form = pd.DataFrame(family_form(m, exp_total), index=pd.Index(m["match_id"], name="match_id"))
    form = form[~form.index.duplicated()]
    return cand.join(form[[c for c in FORM_COLS if c not in cand.columns]], how="left")


def inputs(feats: pd.DataFrame, expected: pd.Series, dates: pd.Series) -> pd.DataFrame:
    """The rule's match-level inputs (``shots.evaluation.surge.INPUTS``) for the fixtures of ``expected``
    (index = match_id; values = published integer median). ``feats``: candidate + form columns by match_id."""
    f = feats.reindex(expected.index)
    with np.errstate(invalid="ignore"):
        res = f[["home_form_res_l3", "away_form_res_l3"]].mean(axis=1)  # the study's mean (one side may be NaN)
    return pd.DataFrame({
        "dead_n": f["ctx_dead_n"],
        "games_left_min": np.fmin(f["home_ctx_games_left"], f["away_ctx_games_left"]),
        "h2h_gap": f["h2h_total_wmean"] - expected.astype(float),
        "goals_l5": f["home_form_goals_l5"] + f["away_form_goals_l5"],
        "res_l3": res,
        "rating_gap": (f["home_adj_att"] - f["home_adj_def"]) - (f["away_adj_att"] - f["away_adj_def"]),
        "month": pd.to_datetime(dates.reindex(expected.index)).dt.month,
    }, index=expected.index)


def assessed(board: pd.DataFrame, table: dict) -> pd.Series:
    """Fixtures the research applies to (bool, board index): the published ``model`` label must start with the
    route's research model (``table["route_models"]``: ``negbin_pit_comp_`` with odds, ``fin_c-noodds_`` without).
    A Surge-scored odds fixture (label ``cand_odds_surge-odds_...``) is never assessed (no double counting)."""
    route = np.where(board["odds"].astype(bool), "odds", "noodds")
    prefix = pd.Series(route, index=board.index).map(lambda r: table["route_models"][r] + "_")
    model = board["model"].fillna("").astype(str)
    ok = pd.Series([m.startswith(p) for m, p in zip(model, prefix)], index=board.index)
    exp = (board["experimental"].fillna(False).astype(bool) if "experimental" in board
           else pd.Series(False, index=board.index))
    return ok & ~exp & board["competition"].isin(table["competitions"])


def fixture_signals(board: pd.DataFrame, feats: pd.DataFrame, table: dict | None = None) -> dict:
    """{"version", "fixtures": {match_id: {"signals": [...], "against": [...]}}} for the assessed fixtures of
    ``board`` (columns match_id, date, competition, odds, model, expected_shots[, experimental]). Deterministic:
    fixtures sorted by match_id, ids in catalogue order."""
    table = table or sv.load_table()
    b = board[assessed(board, table)].drop_duplicates("match_id").set_index("match_id")
    out: dict = {}
    if len(b):
        x = inputs(feats, b["expected_shots"], b["date"])
        fl = sv.flags(x, table["cuts"])
        route = pd.Series(np.where(b["odds"].astype(bool), "odds", "noodds"), index=b.index)
        got = sv.assign(fl, route, table)
        out = {mid: got[mid] for mid in sorted(got.index)}
    return {"version": sv.table_version(table), "fixtures": out}


def board_surge(board: pd.DataFrame, matches: pd.DataFrame, fixtures: pd.DataFrame, table: dict | None = None) -> dict:
    """Surge signals of a freshly scored board (``shots.predict.board.build``). ``matches``: played matches;
    ``fixtures``: the upcoming fixtures of the build (cross-border ones are ignored)."""
    from shots.ingest import cross_border as xb
    from shots.predict import scan as sc

    table = table or sv.load_table()
    if not assessed(board, table).any():  # nothing the research applies to: no feature build needed
        return {"version": sv.table_version(table), "fixtures": {}}
    dom = fixtures[~fixtures["competition"].map(xb.is_cross_border).astype(bool)]
    cand = sc.candidate_features(matches, dom)  # the no-odds route's build when it ran in this update
    rows = pd.concat([matches, dom.drop(columns=["kickoff"], errors="ignore")], ignore_index=True)
    out = fixture_signals(board, with_form(rows, cand), table)
    n = len(out["fixtures"])
    flagged = sum(1 for v in out["fixtures"].values() if v["signals"])
    log.info("surge signals: %d fixtures assessed, %d with at least one boost", n, flagged)
    return out


__all__ = ["board_surge", "fixture_signals", "inputs", "with_form", "assessed"]
