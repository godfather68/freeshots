"""Surge Watch signals (shots.evaluation.surge catalogue + rule, shots.predict.surge per fixture): the committed table
is the report's numbers, the rule, route applicability, season-end reduction, which fixtures are assessed, and no
leakage (a fixture's signals use only matches dated before it and never move when later results arrive)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from shots.evaluation import surge as sv
from shots.features import candidates as C
from shots.predict import board as board_mod
from shots.predict import surge as sg
from tests.test_features_recent_form import _season

TABLE = sv.load_table()


# ----------------------------------------------------------------------------- catalogue

def test_committed_table_is_the_report_json():
    """Regenerating the table from the report JSON (with its stored cut points) reproduces it exactly: the stats
    are the research's own, deterministic, and the file is in sync with the report."""
    report = json.loads(sv.REPORT_JSON.read_text(encoding="utf-8"))
    assert sv.build_table(report, TABLE["cuts"]) == TABLE
    assert [s["id"] for s in TABLE["signals"]] == [s.id for s in sv.SIGNALS]
    seg = report["odds"]["segments"]["dead rubber (both teams nothing to play for)"]
    dr = TABLE["signals"][0]["routes"]["odds"]
    assert (dr["n"], dr["over_rate"], dr["stated_rate"]) == (seg["n"], round(seg["over5"]["rate"], 4),
                                                            round(seg["over5"]["stated"], 4))


def test_applies_needs_a_resolved_interval_and_enough_seasons():
    for s in TABLE["signals"]:
        for route, st in s["routes"].items():
            lo, hi = st["excess_ci"]
            ok = (lo > 0 if s["direction"] == "boost" else hi < 0) and st["seasons_held"] >= sv.MIN_SEASONS_HELD
            assert st["applies"] is (ok and s["id"] not in sv.HELD_BACK), (s["id"], route)
    by = {s["id"]: s["routes"] for s in TABLE["signals"]}
    # held back: passes the research bar on the no-odds route but is never applied
    assert not by["beating_ratings"]["noodds"]["applies"] and "beating_ratings" in sv.HELD_BACK
    assert by["dead_rubber"]["odds"]["applies"] and not by["dead_rubber"]["noodds"]["applies"]
    assert by["home_mismatch"]["noodds"]["applies"] and not by["home_mismatch"]["odds"]["applies"]
    assert by["winter"]["odds"]["applies"] and by["winter"]["noodds"]["applies"]


def test_pooled_winter_combines_both_months():
    report = json.loads(sv.REPORT_JSON.read_text(encoding="utf-8"))
    dec, jan = (report["noodds"]["segments"][k] for k in ("month: 12", "month: 01"))
    w = next(s for s in TABLE["signals"] if s["id"] == "winter")["routes"]["noodds"]
    assert w["n"] == dec["n"] + jan["n"]
    rate = (dec["n"] * dec["over5"]["rate"] + jan["n"] * jan["over5"]["rate"]) / w["n"]
    assert w["over_rate"] == round(rate, 4) and w["excess_ci"][1] < 0


def test_table_version_is_a_content_hash():
    other = json.loads(json.dumps(TABLE))
    assert sv.table_version(other) == sv.table_version(TABLE)
    other["signals"][0]["routes"]["odds"]["applies"] = False
    assert sv.table_version(other) != sv.table_version(TABLE)


# ----------------------------------------------------------------------------- the rule

def frame(**cols) -> pd.DataFrame:
    base = {k: np.nan for k in sv.INPUTS}
    base["month"] = 10
    n = max(len(v) for v in cols.values())
    return pd.DataFrame({k: cols.get(k, [base[k]] * n) for k in sv.INPUTS}, index=[f"m{i}" for i in range(n)])


def test_flags_follow_the_research_definitions():
    cuts = TABLE["cuts"]
    x = frame(dead_n=[2, 1, 0, np.nan], games_left_min=[3, 4, np.nan, 0], h2h_gap=[4, 3.99, 120, np.nan],
              goals_l5=[4.0, 4.2, 4.199999999, np.nan], res_l3=[cuts["res_l3_q80"], 0, cuts["res_l3_q20"] - 0.01, np.nan],
              rating_gap=[cuts["rating_gap_q90"], 0, np.nan, 1], month=[12, 1, 2, 11])
    f = sv.flags(x, cuts)
    assert f["dead_rubber"].tolist() == [True, False, False, False]
    assert f["dead_team"].tolist() == [False, True, False, False]
    assert f["last_three"].tolist() == [True, False, False, True]
    assert f["h2h_above"].tolist() == [True, False, False, False]
    # strictly below 4.2 goals, exact at 6 decimals (goal averages are multiples of 0.2)
    assert f["low_goal_run"].tolist() == [True, False, False, False]
    assert f["beating_ratings"].tolist() == [True, False, False, False]
    assert f["below_ratings"].tolist() == [False, False, True, False]
    assert f["home_mismatch"].tolist() == [True, False, False, True]
    assert f["winter"].tolist() == [True, True, False, False]


def test_assign_keeps_route_signals_and_one_season_end():
    x = frame(dead_n=[2, 2], games_left_min=[1, 1], h2h_gap=[5, 5], rating_gap=[9, 9], month=[1, 1])
    fl = sv.flags(x, TABLE["cuts"])
    got = sv.assign(fl, pd.Series(["odds", "noodds"], index=fl.index), TABLE)
    # odds: dead rubber wins the season-end group; home_mismatch does not apply there
    assert got["m0"] == {"signals": ["dead_rubber", "h2h_above"], "against": ["winter"]}
    # no odds: dead rubber does not apply (fin_c absorbs it), so the next season-end member that does
    assert got["m1"] == {"signals": ["last_three", "h2h_above", "home_mismatch"], "against": ["winter"]}


def board_rows(ids, dates, comp="Spain La Liga", odds=False, model=None, expected=25, **extra):
    n = len(ids)
    return pd.DataFrame({"match_id": ids, "date": pd.to_datetime(dates), "competition": [comp] * n,
                         "odds": [odds] * n, "expected_shots": [float(expected)] * n,
                         "model": [model or ("negbin_pit_comp_2026-10-05" if odds else "fin_c-noodds_2026-10-14")] * n,
                         **extra})


def test_only_production_domestic_fixtures_of_researched_leagues_are_assessed():
    b = pd.concat([
        board_rows(["a"], ["2026-11-01"]),
        board_rows(["b"], ["2026-11-01"], odds=True),
        board_rows(["c"], ["2026-11-01"], model="negbin_pit_comp-noodds_2026-10-05"),       # fallback model
        board_rows(["d"], ["2026-11-01"], odds=True, model="cand_odds_oos-odds_2026-10-26"),  # candidate
        board_rows(["e"], ["2026-11-01"], comp="England National League"),                    # not researched
        board_rows(["f"], ["2026-11-01"], comp="UEFA Champions League",
                   model="negbin_pit_comp-noodds_2026-10-05-ucl-exp"),
    ], ignore_index=True)
    b["experimental"] = b["competition"].eq("UEFA Champions League")
    assert sg.assessed(b, TABLE).tolist() == [True, True, False, False, False, False]


def test_surge_scored_odds_fixtures_are_never_labelled_excluded_ones_are():
    """No double counting (since Surge's adoption, 2026-10-27): with odds, only negbin_pit_comp-scored fixtures (the
    competitions excluded from Surge, or a Surge fallback) are assessed; no-odds fixtures as before."""
    b = pd.concat([
        board_rows(["s"], ["2026-11-01"], odds=True, model="cand_odds_surge-odds_2026-10-26"),            # Surge
        board_rows(["x"], ["2026-11-01"], odds=True, comp="Scotland Championship",
                   model="negbin_pit_comp_2026-10-05"),                                                  # excluded
        board_rows(["n"], ["2026-11-01"], comp="Scotland Championship"),                                  # no odds
    ], ignore_index=True)
    assert sg.assessed(b, TABLE).tolist() == [False, True, True]


# ----------------------------------------------------------------------------- per fixture, causal

FAM = ("adj", "h2h", "ctx")


def research_table(**over) -> dict:
    """The committed table for the synthetic league "L1", with every signal applying on both routes and cut points
    set so that synthetic data produce signals."""
    t = json.loads(json.dumps(TABLE))
    t["competitions"] = ["L1"]
    for s in t["signals"]:
        for r in s["routes"].values():
            r["applies"] = True
    t["cuts"].update(over)
    return t


def split(data: pd.DataFrame, cut) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(played before `cut`, upcoming from `cut` on with results removed)."""
    played = data[data["date"] < cut]
    up = data[data["date"] >= cut].copy()
    for c in ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"):
        up[c] = np.nan
    return played, up


def signals_for(rows: pd.DataFrame, board: pd.DataFrame, table: dict) -> tuple[dict, pd.DataFrame]:
    cand = C.build_candidates(rows, None, families=FAM).set_index("match_id")
    feats = sg.with_form(rows, cand)
    b = board.set_index("match_id")
    return sg.fixture_signals(board, feats, table), sg.inputs(feats, b["expected_shots"], b["date"])


@pytest.fixture(scope="module")
def league():
    data = _season(n_days=30, seed=4, comp="L1")
    days = sorted(data["date"].unique())
    return data, days


def test_inputs_match_a_hand_computation(league):
    data, days = league
    played, up = split(data, days[20])
    rows = pd.concat([played, up], ignore_index=True)
    board = board_rows(up["match_id"].tolist(), up["date"].tolist(), comp="L1", expected=24)
    _, x = signals_for(rows, board, research_table())
    mid = up["match_id"].iloc[0]
    fx = up.iloc[0]
    hist = played.sort_values(["date", "match_id"])
    goals = []
    for team in (fx["home_team_id"], fx["away_team_id"]):
        mine = hist[(hist.home_team_id == team) | (hist.away_team_id == team)].tail(5)
        goals.append((mine.home_goals + mine.away_goals).mean())
    assert x.loc[mid, "goals_l5"] == pytest.approx(sum(goals))
    assert x.loc[mid, "month"] == pd.Timestamp(fx["date"]).month
    assert np.isfinite(x.loc[mid, "res_l3"]) and np.isfinite(x.loc[mid, "rating_gap"])


@pytest.mark.parametrize("k", [0, 3, 6])
def test_no_leakage_later_results_never_change_a_fixture(league, k):
    """Results dated on or after day D (own match, same-day matches, everything later) are added: the inputs and
    signals of every fixture dated on or before D are unchanged."""
    data, days = league
    cut = days[20]
    played, up = split(data, cut)
    rows = pd.concat([played, up], ignore_index=True)
    board = board_rows(up["match_id"].tolist(), up["date"].tolist(), comp="L1", expected=24)
    # cut points inside the synthetic data's range, so that signals actually fire
    table = research_table(goals_l5_q20=5.0, res_l3_q80=0.0, res_l3_q20=-0.5, rating_gap_q90=0.0)
    before, xb = signals_for(rows, board, table)
    assert any(v["signals"] for v in before["fixtures"].values())

    d = days[20 + k]
    later = rows.copy()
    known = (later["date"] >= d).to_numpy()
    rng = np.random.default_rng(7 + k)
    for c in ("home_shots", "away_shots", "home_goals", "away_goals"):
        later.loc[known, c] = rng.integers(0, 40, known.sum())
    later.loc[known, "home_sot"] = later.loc[known, "home_shots"] // 3
    later.loc[known, "away_sot"] = later.loc[known, "away_shots"] // 3
    after, xa = signals_for(later, board, table)

    keep = up.loc[up["date"] <= d, "match_id"]
    pd.testing.assert_frame_equal(xb.loc[keep], xa.loc[keep])
    assert {m: before["fixtures"][m] for m in keep} == {m: after["fixtures"][m] for m in keep}


def test_earlier_results_do_change_the_inputs(league):
    """The other side of the leakage test: the inputs really read the results before the fixture."""
    data, days = league
    played, up = split(data, days[20])
    board = board_rows(up["match_id"].tolist(), up["date"].tolist(), comp="L1", expected=24)
    _, xb = signals_for(pd.concat([played, up], ignore_index=True), board, research_table())
    p2 = played.copy()
    last = (p2["date"] == p2["date"].max()).to_numpy()
    p2.loc[last, "home_goals"] = 9
    _, xa = signals_for(pd.concat([p2, up], ignore_index=True), board, research_table())
    assert not xb["goals_l5"].equals(xa["goals_l5"])


def test_output_is_deterministic_and_sorted(league):
    data, days = league
    played, up = split(data, days[20])
    rows = pd.concat([played, up], ignore_index=True)
    board = board_rows(up["match_id"].tolist(), up["date"].tolist(), comp="L1", expected=24)
    a, _ = signals_for(rows, board, research_table(goals_l5_q20=5.0))
    b, _ = signals_for(rows, board.iloc[::-1].reset_index(drop=True), research_table(goals_l5_q20=5.0))
    assert a == b and list(a["fixtures"]) == sorted(a["fixtures"])
    assert a["version"] == sv.table_version(research_table(goals_l5_q20=5.0))


def test_board_surge_uses_the_shared_candidate_build(league, monkeypatch):
    from shots.predict import scan

    data, days = league
    played, up = split(data, days[20])
    calls = []

    def fake(matches, fixtures):
        calls.append(len(fixtures))
        return C.build_candidates(pd.concat([matches, fixtures], ignore_index=True), None, families=FAM).set_index("match_id")

    monkeypatch.setattr(scan, "candidate_features", fake)
    board = board_rows(up["match_id"].tolist(), up["date"].tolist(), comp="L1", expected=24)
    out = sg.board_surge(board, played, up, research_table())
    assert calls == [len(up)] and set(out["fixtures"]) == set(up["match_id"])
    # nothing assessed: no feature build at all
    calls.clear()
    none = sg.board_surge(board.assign(competition="Elsewhere"), played, up, research_table())
    assert none["fixtures"] == {} and calls == []


def test_a_failure_never_costs_the_board(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("feature build failed")

    monkeypatch.setattr(sg, "board_surge", boom)
    assert board_mod._surge(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()) is None
    assert "surge signals failed" in caplog.text
