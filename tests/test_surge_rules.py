"""League-table surge signals, season phase and stacking (shots.evaluation.surge_rules, 2026-10-28)."""

import json

import numpy as np
import pandas as pd
import pytest

from shots.evaluation import surge as sv
from shots.evaluation import surge_rules as sr

TABLE = sv.load_table()


def tx(**cols) -> pd.DataFrame:
    """One-row TABLE_INPUTS frame (NaN unless given)."""
    base = {c: np.nan for c in sr.TABLE_INPUTS}
    base.update(cols)
    return pd.DataFrame([base])


# ----------------------------------------------------------------------------- phase

@pytest.mark.parametrize("progress,left,want", [
    (0.0, 30, "early"), (0.249, 28, "early"), (0.25, 25, "middle"), (0.66, 13, "middle"), (2 / 3, 12, "late"),
    (0.8, 7, "late"), (0.85, 6, "run_in"), (0.4, 6, "run_in"), (1.0, 0, "run_in"),
    (1.0, np.nan, "late"),    # playoff format: playoff rounds count as late, never run-in
    (0.1, np.nan, "early"), (np.nan, 3, None), (np.nan, np.nan, None)])
def test_season_phase(progress, left, want):
    assert sr.season_phase(np.array([progress]), np.array([left]))[0] == want


def test_phase_of_uses_the_fewer_games_left():
    x = tx(progress=0.7, home_games_left=10, away_games_left=5)
    assert sr.phase_of(x)[0] == "run_in"


# ----------------------------------------------------------------------------- families

def late(**cols):
    return tx(progress=0.8, home_games_left=8, away_games_left=8, **cols)


def test_top_vs_bottom_needs_the_gap_and_the_last_third():
    kw = sr.SELECTED["top_vs_bottom"][1]
    assert sr.top_bottom(late(home_rank_frac=0.05, away_rank_frac=0.7), **kw)[0]
    assert sr.top_bottom(late(home_rank_frac=0.9, away_rank_frac=0.1), **kw)[0]       # either side on top
    assert not sr.top_bottom(late(home_rank_frac=0.2, away_rank_frac=0.7), **kw)[0]   # gap 0.5
    mid = tx(progress=0.5, home_games_left=19, away_games_left=19, home_rank_frac=0.0, away_rank_frac=1.0)
    assert not sr.top_bottom(mid, **kw)[0]


def test_promoted_struggling():
    kw = sr.SELECTED["promoted_struggling"][1]
    assert sr.promoted(late(away_promo=1, away_rank_frac=0.8, home_rank_frac=0.3), **kw)[0]
    assert not sr.promoted(late(away_promo=1, away_rank_frac=0.5), **kw)[0]
    assert not sr.promoted(late(away_promo=0, away_rank_frac=0.9), **kw)[0]
    assert not sr.promoted(late(away_promo=-1, away_rank_frac=0.9), **kw)[0]       # relegated, not promoted
    assert not sr.promoted(late(away_promo=np.nan, away_rank_frac=0.9), **kw)[0]   # newcomer: unknown


def test_title_race():
    kw = sr.SELECTED["title_race"][1]
    assert sr.title_race(late(home_gap_title=0, away_gap_title=6), **kw)[0]
    assert not sr.title_race(late(home_gap_title=0, away_gap_title=7), **kw)[0]


def test_other_families_on_synthetic_rows():
    assert sr.six_pointer(late(home_gap_safety=-2, away_gap_safety=3))[0]
    assert not sr.six_pointer(late(home_gap_safety=-2, away_gap_safety=4))[0]
    assert sr.needs_win(tx(progress=0.9, home_games_left=4, away_games_left=4, home_gap_safety=1))[0]
    ok = late(home_rank_frac=0.5, away_rank_frac=0.4, home_gap_title=20, away_gap_title=25, home_gap_safety=15,
              away_gap_safety=12)
    assert sr.mid_table(ok, k=1.0)[0] and not sr.mid_table(ok, k=2.0)[0]


def test_missing_inputs_never_flag():
    fl = sr.table_flags(pd.DataFrame(index=[0, 1]))
    assert list(fl.columns) == list(sr.SELECTED) and not fl.to_numpy().any()


def test_table_inputs_from_candidate_columns():
    feats = pd.DataFrame({"home_ctx_rank_frac": [0.1], "away_ctx_rank_frac": [0.9], "ctx_progress": [0.8],
                          "home_ctx_games_left": [7], "away_ctx_games_left": [7], "away_form_promo": [1]},
                         index=["m1"])
    x = sr.table_inputs(feats)
    assert list(x.columns) == list(sr.TABLE_INPUTS) and x.index.tolist() == ["m1"]
    assert np.isnan(x.at["m1", "home_gap_title"]) and x.at["m1", "away_promo"] == 1
    fl = sr.table_flags(x)
    assert fl.at["m1", "top_vs_bottom"] and fl.at["m1", "promoted_struggling"] and not fl.at["m1", "title_race"]


# ----------------------------------------------------------------------------- catalogue integration

def base_inputs(n=1, **cols):
    d = {k: [np.nan] * n for k in sv.INPUTS}
    d["month"] = [10] * n
    d.update({k: v if isinstance(v, list) else [v] * n for k, v in cols.items()})
    return pd.DataFrame(d)


def test_flags_without_table_inputs_are_the_old_flags_plus_false_columns():
    x = base_inputs(dead_n=2.0, games_left_min=2.0)
    fl = sv.flags(x, TABLE["cuts"])
    assert set(sv.TABLE_SIGNALS) <= set(fl.columns)
    assert not fl[list(sv.TABLE_SIGNALS)].to_numpy().any()
    assert fl.at[0, "dead_rubber"] and fl.at[0, "last_three"]


def test_flags_with_table_inputs():
    x = base_inputs(progress=0.8, home_games_left=8.0, away_games_left=8.0, home_rank_frac=0.0,
                    away_rank_frac=0.95, away_promo=1.0, home_gap_title=0.0, away_gap_title=40.0)
    fl = sv.flags(x, TABLE["cuts"])
    assert fl.at[0, "top_vs_bottom"] and fl.at[0, "promoted_struggling"] and not fl.at[0, "title_race"]


def test_table_signals_in_the_catalogue():
    by = {s["id"]: s for s in TABLE["signals"]}
    for sid in ("top_vs_bottom", "promoted_struggling"):
        assert by[sid]["direction"] == "boost" and by[sid]["group"] is None
        assert by[sid]["routes"]["noodds"]["applies"] and by[sid]["routes"]["odds"]["applies"]
        assert by[sid]["segments"] == [sr.segment_name(*sr.SELECTED[sid])]
    assert by["title_race"]["direction"] == "damper" and "title_race" in sv.HELD_BACK
    assert not any(r["applies"] for r in by["title_race"]["routes"].values())
    # the research numbers come from the table report, unchanged
    rep = sv.load_table_report()
    seg = rep["noodds"]["segments"][by["promoted_struggling"]["segments"][0]]
    assert by["promoted_struggling"]["routes"]["noodds"]["n"] == seg["n"]


def test_assign_counts_table_boosts_and_never_held_back():
    x = base_inputs(2, progress=0.8, home_games_left=8.0, away_games_left=8.0, home_rank_frac=[0.0, 0.0],
                    away_rank_frac=[0.95, 0.95], away_promo=[1.0, 1.0], home_gap_title=[0.0, 0.0],
                    away_gap_title=[3.0, 3.0])
    x["h2h_gap"] = [5.0, np.nan]
    fl = sv.flags(x, TABLE["cuts"])
    assert fl["title_race"].all()
    got = sv.assign(fl, pd.Series(["noodds", "noodds"]), TABLE)
    assert got[0]["signals"] == ["h2h_above", "top_vs_bottom", "promoted_struggling"]
    assert got[1]["signals"] == ["top_vs_bottom", "promoted_struggling"]
    assert all("title_race" not in g["against"] for g in got)
    assert sr.stack_group([len(g["signals"]) for g in got]).tolist() == ["3+", "2"]
    assert sr.strong([len(g["signals"]) for g in got]).all()


def test_stacking_table_is_additive_and_follows_its_rule():
    old_keys = {"report", "report_date", "margin", "matches", "period", "method", "min_seasons_held", "route_models",
                "routes", "competitions", "cuts", "signals"}
    assert old_keys | {"table_report", "stacking"} == set(TABLE)
    st = TABLE["stacking"]
    assert st["min_boosts"] == sr.STRONG_MIN_BOOSTS and st["groups"] == ["0", "1", "2", "3+", "2+"]
    rep = sv.load_table_report()
    for r in sv.ROUTES:
        v = st["routes"][r]
        by = v["by_count"]
        assert sum(by[g]["n"] for g in sr.STACK_GROUPS) == rep[r]["n"]
        assert by["2+"]["n"] == by["2"]["n"] + by["3+"]["n"]
        applying = {s["id"] for s in TABLE["signals"] if s["direction"] == "boost" and s["routes"][r]["applies"]}
        assert set(v["signals"]) == applying
        two = rep[r]["stacking"]["groups"]["2+"]
        oos = rep[r]["oos"]["stacking_confirmation"]["groups"]["2+"]
        want = (two["excess_ci"][0] > 0 and two["seasons_pos"] >= sv.MIN_SEASONS_HELD and two["conf"]["excess"] > 0
                and oos["excess"] > 0)
        assert v["strong_applies"] is bool(want)
    assert st["routes"]["odds"]["strong_applies"] and not st["routes"]["noodds"]["strong_applies"]


def test_table_version_changes_with_the_stacking_table():
    other = json.loads(json.dumps(TABLE))
    other["stacking"]["routes"]["noodds"]["strong_applies"] = True
    assert sv.table_version(other) != sv.table_version(TABLE)


def test_live_definitions_are_cells_of_the_research_grid():
    from shots.evaluation import surge_table as st

    g = st.grid()
    for sid, (fam, kw) in sr.SELECTED.items():
        assert g[sr.segment_name(fam, kw)] == (fam, kw), sid
    rep = sv.load_table_report()
    chosen = {**rep["selection"]["chosen"], **rep["selection"]["relaxed_chosen"]}
    for sid, (fam, kw) in sr.SELECTED.items():
        assert chosen[fam]["segment"] == sr.segment_name(fam, kw), sid


def test_research_applies_rule():
    from shots.evaluation import surge_table as st

    c = {"excess_ci": [0.001, 0.03], "seasons_pos": 5, "seasons_neg": 2, "conf": {"excess": 0.01}}
    assert st.applies(c, "boost")
    assert not st.applies({**c, "conf": {"excess": -0.01}}, "boost")
    assert not st.applies({**c, "seasons_pos": 4}, "boost")
    assert not st.applies(c, "damper")
    d = {"excess_ci": [-0.05, -0.01], "seasons_pos": 1, "seasons_neg": 6, "conf": {"excess": -0.02}}
    assert st.applies(d, "damper")
