"""Repository: the SQL board filters return exactly what the pandas reference filters return, and
entitlements are enforced in the queries."""

import itertools
import random
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from shots.db import load
from shots.db.access import FULL_ACCESS, Entitlements
from shots.db.engine import make_engine
from shots.db.repo import Repository
from shots.web import filters as flt
from tests.test_web import META, TODAY, make_board, row
from tests.test_web_team import with_team

COMPS = [("Spain La Liga", "Spain", 1), ("Italy Serie B", "Italy", 2), ("England Premier League", "England", 1),
         ("England Championship", "England", 2), ("Germany Bundesliga", "Germany", 1)]
TEAMS = ["Atlético Madrid", "Köln", "Bari", "Cesena", "Fulham", "Brentford", "Hull", "Leeds", "Málaga", "Bayern",
         "Real Sociedad", "Saint-Étienne", "Union Berlin", "Sampdoria", "Pisa", "Derby", "Celta", "Girona"]


def random_board(seed=0, n=80):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        comp, country, tier = rng.choice(COMPS)
        day = TODAY + timedelta(days=rng.randint(-3, 32))
        home, away = rng.sample(TEAMS, 2)
        r = row(f"r{i}", str(day), rng.choice(["", "12:30", "15:00", "15:00", "20:45"]), comp, country, tier,
                home + f" {i}", away, round(rng.uniform(14, 36)), odds=rng.random() < .3, cold=rng.random() < .15)
        r["above_league"] = round(r["expected_shots"] - rng.uniform(22, 30), 1)
        if rng.random() < .1:
            r["p_24"] = r["p_25"]  # ties in p
        rows.append(r)
    return with_team(pd.DataFrame(rows))


@pytest.fixture
def repo(migrated_db):
    engine = make_engine(migrated_db)
    load.import_board(engine, random_board(), META)
    yield Repository(engine)
    engine.dispose()


FILTERS = [
    flt.Filters(),
    flt.Filters(n=15), flt.Filters(n=40, pmin=10),
    flt.Filters(leagues=("Spain La Liga",)), flt.Filters(groups=("top5",)), flt.Filters(groups=("second",)),
    flt.Filters(leagues=("Italy Serie B",), groups=("top5",)),
    flt.Filters(smin=20), flt.Filters(smax=25.5), flt.Filters(smin=22, smax=30),
    flt.Filters(pmin=50), flt.Filters(n=30, pmin=35),
    *(flt.Filters(when=w) for w in flt.WHEN),
    flt.Filters(odds=True), flt.Filters(nocold=True), flt.Filters(odds=True, nocold=True),
    flt.Filters(q="atletico"), flt.Filters(q="KOLN"), flt.Filters(q="saint étienne"), flt.Filters(q="1 cel"),
    flt.Filters(q="nobody"), flt.Filters(q="a | b"),
    *(flt.Filters(sort=s) for s in flt.SORTS),
    *(flt.Filters(side=s, tn=tn, tp=tp) for s, tn, tp in itertools.product(flt.SIDES, (5, 12, 20), (0, 50))),
    flt.Filters(groups=("second",), when="7d", pmin=30, sort="prob", side="either", tn=10, tp=40),
]


def reference(board, f):
    return flt.apply(flt.prepare(board), f, TODAY)


@pytest.mark.parametrize("f", FILTERS, ids=lambda f: f.qs() or "default")
def test_sql_filters_equal_pandas_reference(repo, f):
    board = random_board()
    want = reference(board, f)
    got = repo.latest_board(f, TODAY)
    assert [r["match_id"] for r in got.rows] == want["match_id"].tolist()
    assert [r["p"] for r in got.rows] == want["p"].tolist()
    if f.side:
        assert [r["tp"] for r in got.rows] == want["tp"].tolist()
        assert [r["tside"] for r in got.rows] == want["tside"].tolist()
    assert got.total == int((flt.prepare(board)["_day"] >= TODAY).sum())


def test_rows_carry_board_columns(repo):
    board = random_board().set_index("match_id")
    got = {r["match_id"]: r for r in repo.latest_board(flt.Filters(), TODAY).rows}
    for mid, r in got.items():
        b = board.loc[mid]
        assert r["date"] == b["date"].date() and r["kickoff"] == b["kickoff"]
        for k in ("competition", "country", "tier", "round", "home_team", "away_team", "expected_shots", "lb_70",
                  "lb_80", "lb_90", "league_avg_shots", "above_league", "odds", "cold_start", "model",
                  "home_mean", "away_mean", "home_lb_80", "away_median"):
            assert r[k] == b[k], k
        assert r["mkt_p_over25"] == (None if np.isnan(b["mkt_p_over25"]) else b["mkt_p_over25"])


def test_league_groups_and_leagues(repo):
    board = flt.prepare(random_board())
    up = board[board["_day"] >= TODAY]
    view = repo.latest_board(flt.Filters(), TODAY)
    counts = {l["name"]: l["count"] for g in view.league_groups for l in g["leagues"]}
    assert counts == up.groupby("competition").size().to_dict()
    assert [g["country"] for g in view.league_groups] == sorted(up["country"].unique())
    assert set(repo.board_leagues()) == set(board["competition"])


def test_match_detail_curves(repo):
    board = random_board().set_index("match_id")
    d = repo.match_detail("r3", TODAY)
    assert d["curve_ns"] == list(range(10, 46)) and d["curve"] == [board.loc["r3", f"p_{n}"] for n in range(10, 46)]
    assert d["team_ns"] == list(range(3, 26)) and d["away_curve"] == [board.loc["r3", f"pa_{n}"] for n in range(3, 26)]
    assert repo.match_detail("nope", TODAY) is None


def test_old_board_without_team_columns(migrated_db):
    engine = make_engine(migrated_db)
    load.import_board(engine, make_board(), META)
    repo = Repository(engine)
    view = repo.latest_board(flt.Filters(side="either", tn=12, tp=80), TODAY)
    assert not view.has_team and [r["match_id"] for r in view.rows] == ["a", "b", "c", "d"]
    assert repo.match_detail("a", TODAY)["team_ns"] is None
    engine.dispose()


# ----------------------------------------------------------------------------- entitlements

FREE = Entitlements(can_view_all_leagues=False, allowed_leagues=frozenset(flt.TOP5), max_days_ahead=3,
                    team_predictions=False, export=False, track_detail=False)


def test_entitlements_restrict_leagues_dates_and_team_data(repo):
    board = flt.prepare(random_board())
    view = repo.latest_board(flt.Filters(), TODAY, FREE)
    ok = board[(board["_day"] >= TODAY) & (board["_day"] <= TODAY + timedelta(days=3))
               & board["competition"].isin(flt.TOP5)]
    assert sorted(r["match_id"] for r in view.rows) == sorted(ok["match_id"])
    assert view.total == len(ok) and not view.has_team
    assert all("home_mean" not in r and "tp" not in r for r in view.rows)
    # a team filter or an explicitly requested other league cannot widen access
    sneaky = repo.latest_board(flt.Filters(leagues=("Italy Serie B",), side="home", tn=5, tp=90), TODAY, FREE)
    assert {r["competition"] for r in sneaky.rows} <= set(flt.TOP5)
    assert set(repo.board_leagues(FREE)) <= set(flt.TOP5)
    hidden = board[~board["competition"].isin(flt.TOP5)]["match_id"].iloc[0]
    assert repo.match_detail(hidden, TODAY, FREE) is None
    far = board[(board["_day"] > TODAY + timedelta(days=3)) & board["competition"].isin(flt.TOP5)]["match_id"].iloc[0]
    assert repo.match_detail(far, TODAY, FREE) is None
    near = ok["match_id"].iloc[0]
    d = repo.match_detail(near, TODAY, FREE)
    assert d is not None and d["home_curve"] is None and "home_mean" not in d
    assert repo.match_detail(near, TODAY, FULL_ACCESS)["home_curve"] is not None


def test_track_detail_entitlement(migrated_db):
    from tests.test_web import back_frame, seed_backfill

    seed_backfill(migrated_db, back_frame())
    repo = Repository(make_engine(migrated_db))
    full = repo.track_summary("backfill", 24, .7)
    assert full["has_data"] and full["total"] == 600 and "reliability" in full["metrics"]
    limited = repo.track_summary("backfill", 24, .7, FREE)  # Spain La Liga is in the plan, detail is not
    assert limited["metrics"]["selected"] == full["metrics"]["selected"] and "reliability" not in limited["metrics"]
    other = replace(FREE, allowed_leagues=frozenset(["Italy Serie A"]))
    assert repo.track_summary("backfill", 24, .7, other)["has_data"] is False
    repo.engine.dispose()


def test_data_status_and_missing_db(repo, tmp_path):
    s = repo.data_status(TODAY)
    assert s["revision"] and s["runs"]["board"]["count"] == 1 and s["latest_board_run"] == 1
    assert s["predictions"] == 80 and s["last_job"] is None
    missing = Repository(tmp_path / "absent.db")
    assert missing.state() == "missing" and missing.board_meta() is None
    assert missing.latest_board(flt.Filters(), TODAY).rows == []
    assert not (tmp_path / "absent.db").exists()


def test_shared_helpers_are_single_definitions():
    from shots.db import repo as r
    from shots.db import text

    assert flt.fold is text.fold and flt.weekend is r.weekend and flt.TOP5 is r.TOP5
    assert flt.fold("  Atlético KÖLN ") == "atletico koln"
