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

**Table context (2026-10-28).** For the same assessed fixtures, ``table_context`` records each team's league
position before the match (rank 1..n by points, then goal difference; the candidate build's season-context family
``ctx_*``, from results dated strictly before the fixture), points, games played / left, gap to the leader and to
safety, and the competition's season phase (``surge_rules.phase_of``: the same definition the table signals use).
Stored next to the labels (``"context"`` in the
same board-meta block), never in a prediction row; published as the fixture's ``context`` (docs/static-api.md).

**League-table signals and stacking (2026-10-28, reports/2026-10-28_surge_table_signals.md).** ``inputs`` adds the
table inputs (``surge_rules.table_inputs``: rank, gaps, games left, promotion, progress) so ``top_vs_bottom`` and
``promoted_struggling`` can fire. A fixture with ``surge_rules.STRONG_MIN_BOOSTS``+ applying boosts is a "strong
surge candidate" (``"strong": true`` in its labels) only on a route whose ``stacking.routes.<route>.strong_applies``
is true in the catalogue (today: odds, i.e. fixtures scored by ``negbin_pit_comp``; never no-odds).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from shots.evaluation import surge as sv
from shots.evaluation import surge_rules as sr

log = logging.getLogger(__name__)

FORM_COLS = ("home_form_goals_l5", "away_form_goals_l5", "home_form_res_l3", "away_form_res_l3",
             "home_form_promo", "away_form_promo")

# Season phase: one definition everywhere, the table signals' own (shots.evaluation.surge_rules.season_phase):
# run_in (either team <= 6 league games left), else late (>= 2/3 of the scheduled games played), middle (>= 25%),
# early; playoff formats (Belgium, Greece) by the regular-season share, playoff rounds = late, never run_in.
PHASES = sr.PHASES
CTX_KEYS = ("pos", "of", "pts", "gp", "left", "to_top", "to_safety")


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
    }, index=expected.index).join(sr.table_inputs(f))  # league-table signals (top_vs_bottom, promoted_struggling)


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
        out = {mid: with_strong(got[mid], route[mid], table) for mid in sorted(got.index)}
    return {"version": sv.table_version(table), "fixtures": out}


def season_phase(progress: float, games_left_min: float | None) -> str | None:
    """One of ``PHASES`` or None (unknown progress): ``surge_rules.season_phase`` for one fixture."""
    return sr.season_phase([progress], [np.nan if games_left_min is None else games_left_min])[0]


def _league_sizes() -> dict:
    """The season calendars' league sizes: the same ones the candidate build used (shots.predict.scan)."""
    from shots.features import candidates as fc
    from shots.predict import scan as sc

    return fc.calendar_league_sizes(pd.read_parquet(sc.CALENDAR_PATH)) if sc.CALENDAR_PATH.exists() else {}


def table_context(rows: pd.DataFrame, cand: pd.DataFrame, ids, league_sizes: dict | None = None) -> dict:
    """{match_id: {"phase", "home": {pos, of, pts, gp, left, to_top, to_safety}, "away": {...}}} for the fixtures
    ``ids``, from the season-context columns of ``cand`` (the candidate build of ``rows`` = played matches +
    upcoming fixtures, built with ``league_sizes``). Pre-match by construction: the build's table only holds
    results dated strictly before each fixture.

    ``of`` = league size (the calendar's, else the teams seen in the competition-season, as in the build);
    ``pos`` = 1 + rank by points then goal difference (None before a team's first game: no table yet);
    ``to_top`` = leader's points minus own; ``to_safety`` = own points minus those of the best team in the drop
    zone (bottom round(15% n) places; <= 0 = in it); ``left`` None for playoff formats; ``phase`` =
    ``surge_rules.phase_of`` of the fixture's table inputs (the signals' own definition). A fixture
    whose stored rank does not map onto a whole position (inconsistent league size) is left out. Deterministic:
    sorted by match_id."""
    from shots.features import candidates as fc

    sizes = league_sizes or {}
    r = rows.drop_duplicates("match_id").set_index("match_id")
    teams: dict[tuple[str, str], int] = {}
    for (comp, season), g in rows.groupby(["competition", "season"], sort=False):
        teams[(str(comp), str(season))] = len(pd.unique(pd.concat([g["home_team_id"], g["away_team_id"]])))
    phases = pd.Series(sr.phase_of(sr.table_inputs(cand)), index=cand.index)
    out: dict = {}
    for mid in sorted(set(ids)):
        if mid not in r.index or mid not in cand.index:
            continue
        comp, season = str(r.at[mid, "competition"]), str(r.at[mid, "season"])
        nt = max(int(sizes.get((comp, season), 0)), teams.get((comp, season), 0))
        if nt < 2:
            continue
        _, end_known = fc.scheduled_games(comp, season, nt)
        f = cand.loc[mid]
        sides, ok = {}, True
        for side in ("home", "away"):
            v = {k: f.get(f"{side}_ctx_{k}") for k in ("gp", "pts", "rank_frac", "gap_title", "gap_safety", "games_left")}
            if any(v[k] is None or not np.isfinite(v[k]) for k in ("gp", "pts", "rank_frac", "gap_title", "gap_safety")):
                ok = False
                break
            rank = float(v["rank_frac"]) * (nt - 1)
            if abs(rank - round(rank)) > 1e-6 or not 0 <= round(rank) < nt:
                log.warning("table context: %s rank %.4f does not fit a league of %d; left out", mid, v["rank_frac"], nt)
                ok = False
                break
            left = v["games_left"]
            known = end_known and left is not None and np.isfinite(left)
            sides[side] = {"pos": int(round(rank)) + 1 if v["gp"] > 0 else None, "of": nt, "pts": int(v["pts"]),
                           "gp": int(v["gp"]), "left": int(left) if known else None,
                           "to_top": int(v["gap_title"]), "to_safety": int(v["gap_safety"])}
        if not ok:
            continue
        ph = phases.get(mid)
        phase = ph if isinstance(ph, str) else None
        out[mid] = {"phase": phase, "home": sides["home"], "away": sides["away"]}
    return out


def strong_applies(table: dict, route: str) -> bool:
    """Whether the catalogue allows the "strong surge candidate" label on a route (``stacking.routes.<route>``)."""
    return bool(((table.get("stacking") or {}).get("routes") or {}).get(route, {}).get("strong_applies", False))


def with_strong(labels: dict, route: str, table: dict) -> dict:
    """``labels`` plus ``"strong": True`` when it has ``surge_rules.STRONG_MIN_BOOSTS``+ boosts and the route allows
    the strong label; unchanged (no key) otherwise."""
    n = len(labels["signals"])
    if strong_applies(table, route) and bool(sr.strong([n])[0]):
        return {**labels, "strong": True}
    return labels


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
    try:  # an add-on of the add-on: a failure costs the context, never the labels
        out["context"] = table_context(rows, cand, out["fixtures"], _league_sizes())
    except Exception:  # noqa: BLE001
        log.exception("table context failed; the surge labels are published without it")
    n = len(out["fixtures"])
    flagged = sum(1 for v in out["fixtures"].values() if v["signals"])
    log.info("surge signals: %d fixtures assessed, %d with at least one boost", n, flagged)
    return out


__all__ = ["board_surge", "fixture_signals", "inputs", "with_form", "assessed", "table_context", "season_phase",
           "PHASES", "strong_applies", "with_strong"]
