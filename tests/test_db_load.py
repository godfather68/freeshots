"""Loaders: idempotent upserts of results / fixtures, board / picks / backfill runs, outcome views."""

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from shots.db import load
from shots.db.engine import make_engine
from shots.db.models import Competition, Match, Prediction, PredictionRun, Team
from shots.ingest.schema import make_match_id
from shots.predict import track
from tests.test_web import META, make_board
from tests.test_web_team import with_team


def played(rows):
    """rows: (competition, country, tier, date, home, away, home_shots, away_shots) -> canonical frame."""
    df = pd.DataFrame(rows, columns=["competition", "country", "tier", "date", "home_team", "away_team",
                                     "home_shots", "away_shots"])
    df["date"] = pd.to_datetime(df["date"])
    df["match_id"] = [make_match_id(c, d, h, a) for c, d, h, a in zip(df.competition, df.date, df.home_team, df.away_team)]
    df["source"], df["season"] = "football-data", "2026/2027"
    for c in ("home_shots", "away_shots"):
        df[c] = df[c].astype("Int64")
    df["odds_over25"] = 1.9
    return df


def fixtures(rows):
    df = played([(*r[:6], None, None) for r in rows])
    df["kickoff"] = [r[6] for r in rows]
    df["round"] = "Regular Season - 8"
    df["odds_over25"] = np.nan
    return df


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


def count(engine, model):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(model)).scalar_one()


def match(engine, mid):
    with engine.connect() as c:
        return c.execute(select(Match).where(Match.match_id == mid)).mappings().one()


# ----------------------------------------------------------------------------- results and fixtures

def test_upsert_matches_is_idempotent_and_updates_changes(engine):
    df = played([("Spain La Liga", "Spain", 1, "2026-10-03", "Betis", "Atlético Madrid", 12, 10),
                 ("Spain La Liga", "Spain", 1, "2026-10-04", "Getafe", "Betis", 8, 9)])
    assert load.upsert_matches(engine, df) == {"rows": 2, "written": 2}
    assert load.upsert_matches(engine, df) == {"rows": 2, "written": 0}  # nothing changed, nothing written
    assert (count(engine, Match), count(engine, Team), count(engine, Competition)) == (2, 3, 1)
    m = match(engine, df.match_id[0])
    assert (m["status"], m["home_team_id"], m["search_key"]) == ("played", "spain:betis", "betis | atletico madrid")
    df.loc[0, "home_shots"] = 13  # upstream correction
    assert load.upsert_matches(engine, df)["written"] == 1
    assert match(engine, df.match_id[0])["home_shots"] == 13
    assert load.upsert_matches(engine, df, since=date(2026, 10, 4))["rows"] == 1


def test_fixture_then_result_lifecycle(engine):
    fx = fixtures([("Spain La Liga", "Spain", 1, "2026-10-10", "Betis", "Getafe", "21:00")])
    fx["kickoff_utc"] = pd.Timestamp("2026-10-10 19:00")
    assert load.upsert_fixtures(engine, fx, today=date(2026, 10, 8))["written"] == 1
    assert load.upsert_fixtures(engine, fx, today=date(2026, 10, 8))["written"] == 0
    m = match(engine, fx.match_id[0])
    assert (m["status"], m["kickoff_local"], m["kickoff_utc"], m["home_shots"]) == (
        "scheduled", "21:00", datetime(2026, 10, 10, 19, 0), None)
    fx2 = fx.assign(odds_over25=1.8)  # odds published later
    load.upsert_fixtures(engine, fx2, today=date(2026, 10, 8))
    assert match(engine, fx.match_id[0])["odds_over25"] == 1.8
    load.upsert_fixtures(engine, fx, today=date(2026, 10, 8))  # a later file without odds keeps them
    assert match(engine, fx.match_id[0])["odds_over25"] == 1.8
    # result arrives: played, kick-off kept
    res = played([("Spain La Liga", "Spain", 1, "2026-10-10", "Betis", "Getafe", 15, 9)])
    load.upsert_matches(engine, res)
    m = match(engine, fx.match_id[0])
    assert (m["status"], m["home_shots"], m["kickoff_local"]) == ("played", 15, "21:00")
    # a stale fixture file never reverts a played match
    assert load.upsert_fixtures(engine, fx, today=date(2026, 10, 8))["written"] == 0
    assert match(engine, fx.match_id[0])["status"] == "played"


def test_fixture_no_longer_listed_becomes_unlisted(engine):
    a = fixtures([("Spain La Liga", "Spain", 1, "2026-10-10", "Betis", "Getafe", "21:00"),
                  ("Spain La Liga", "Spain", 1, "2026-10-11", "Sevilla", "Celta", "18:00")])
    load.upsert_fixtures(engine, a, today=date(2026, 10, 8))
    assert load.upsert_fixtures(engine, a.iloc[[0]], today=date(2026, 10, 8))["unlisted"] == 1
    assert match(engine, a.match_id[1])["status"] == "unlisted"
    load.upsert_fixtures(engine, a, today=date(2026, 10, 8))  # listed again
    assert match(engine, a.match_id[1])["status"] == "scheduled"


def test_read_fixtures_adds_utc_and_merges_football_data_odds(tmp_path):
    cal = fixtures([("Germany Bundesliga", "Germany", 1, "2026-10-10", "Dortmund", "Werder Bremen", "20:30"),
                    ("England Premier League", "England", 1, "2026-10-11", "Fulham", "Brentford", "14:00"),
                    ("Germany Bundesliga", "Germany", 1, "2026-10-01", "Old", "Match", "15:30")])
    cal["status"] = "To be played"
    cal.to_parquet(tmp_path / "fixtures.parquet", index=False)
    fd_dir = tmp_path / "fx"
    fd_dir.mkdir()
    (fd_dir / "fixtures_2026-10-08.csv").write_text(
        "Div,Date,Time,HomeTeam,AwayTeam,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5\n"
        "D1,10/10/2026,19:30,Dortmund,Werder Bremen,1.5,4.5,6.0,1.6,2.3\n"
        "I1,12/10/2026,20:45,Napoli,Lecce,1.3,5.0,9.0,1.9,1.9\n", encoding="utf-8")
    out = load.read_fixtures(date(2026, 10, 8), tmp_path / "fixtures.parquet", fd_dir).set_index("home_team")
    assert "Old" not in out.index
    assert out.loc["Dortmund", "kickoff_utc"] == pd.Timestamp("2026-10-10 18:30")  # CEST = UTC+2
    assert out.loc["Fulham", "kickoff_utc"] == pd.Timestamp("2026-10-11 13:00")  # BST = UTC+1
    assert out.loc["Dortmund", "odds_over25"] == 1.6 and out.loc["Dortmund", "kickoff"] == "20:30"
    # football-data only: its Time is UK time (BST) -> UTC -> Rome local
    assert out.loc["Napoli", "competition"] == "Italy Serie A"
    assert out.loc["Napoli", "kickoff_utc"] == pd.Timestamp("2026-10-12 19:45")
    assert out.loc["Napoli", "kickoff"] == "21:45"


# ----------------------------------------------------------------------------- board runs

def runs(engine, kind=None):
    with engine.connect() as c:
        q = select(PredictionRun).order_by(PredictionRun.id)
        if kind:
            q = q.where(PredictionRun.kind == kind)
        return [dict(r) for r in c.execute(q).mappings()]


def test_import_board_roundtrip_and_idempotence(engine):
    board = with_team(make_board())
    res = load.import_board(engine, board, META)
    assert res["status"] == "inserted" and res["predictions"] == 5
    assert load.import_board(engine, board, META)["status"] == "exists"
    [run] = runs(engine, "board")
    assert (run["curve_n_min"], run["curve_n_max"], run["team_n_min"], run["team_n_max"]) == (10, 45, 3, 25)
    assert run["meta"]["built_at"] == META["built_at"] and run["models"] == META["models"]
    with engine.connect() as c:
        p = c.execute(select(Prediction).where(Prediction.match_id == "a")).mappings().one()
    a = board.set_index("match_id").loc["a"]
    assert p["total_curve"] == [a[f"p_{n}"] for n in range(10, 46)]
    assert p["home_curve"] == [a[f"ph_{n}"] for n in range(3, 26)]
    assert (p["median"], p["lb_80"], p["odds_available"], p["cold_start"], p["home_lb_90"]) == (
        30.0, 26, True, False, a["home_lb_90"])
    m = match(engine, "a")  # board-only fixture: stub match created from the board row
    assert (m["status"], m["home_team_id"], m["kickoff_local"], m["round"]) == (
        "scheduled", "spain:atletico-madrid", "20:00", "Regular Season - 8")


def test_identical_rebuild_only_confirms_latest_run(engine):
    board = make_board()
    load.import_board(engine, board, META)
    later = dict(META, built_at="2026-10-08T12:00:00")
    res = load.import_board(engine, board, later)
    assert res["status"] == "unchanged"
    [run] = runs(engine, "board")
    assert run["meta"]["built_at"] == "2026-10-08T12:00:00" and run["confirmed_at"] > run["built_at"]
    assert count(engine, Prediction) == 5
    changed = board.assign(expected_shots=board["expected_shots"] + 1)
    assert load.import_board(engine, changed, dict(META, built_at="2026-10-08T15:00:00"))["status"] == "inserted"
    assert len(runs(engine, "board")) == 2 and count(engine, Prediction) == 10


def test_board_without_team_columns(engine):
    load.import_board(engine, make_board(), META)
    [run] = runs(engine, "board")
    assert run["team_n_min"] is None
    with engine.connect() as c:
        assert c.execute(select(Prediction.home_curve).where(Prediction.match_id == "a")).scalar() is None


# ----------------------------------------------------------------------------- picks, backfill, outcomes

def write_picks(path, rows, n=24, replay=False):
    df = pd.DataFrame(rows, columns=["date", "kickoff", "competition", "home_team", "away_team", f"p_at_least_{n}",
                                     "expected_shots", "match_id"])
    df = df.assign(league_avg_shots=26.0, above_league=0.0, lb_80=20, odds="no", mkt_p_over25=np.nan,
                   cold_start=False, model="negbin_x", selected=df[f"p_at_least_{n}"] >= 0.7, min_prob=0.7)
    if replay:
        df = df.drop(columns=["odds"])
    df.to_csv(path, index=False)


# Picks only count as official when inserted (DB clock) before kick-off, so the scanned matches are
# always in the future; their results are loaded after the picks, as in real use.
D1, D2, DUP = (date.today() + timedelta(days=k) for k in (3, 4, 12))
SCAN = f"{D1:%Y-%m-%d}_0900"


@pytest.fixture
def picks_setup(engine, tmp_path):
    """Two matches that will be played (one reaches 24, one not), one upcoming; three scans incl. a
    replay and a scan taken after a match day (not official for that match). Returns (picks dir,
    results parquet, (m1, m2, up), results frame); results are NOT loaded into the DB."""
    res = played([("Spain La Liga", "Spain", 1, f"{D1}", "Betis", "Getafe", 14, 12),
                  ("Spain La Liga", "Spain", 1, f"{D2}", "Sevilla", "Celta", 10, 9)])
    m1, m2 = res.match_id
    up = make_match_id("Spain La Liga", pd.Timestamp(DUP), "Girona", "Osasuna")
    d = tmp_path / "picks"
    d.mkdir()
    write_picks(d / f"replay_{D1 - timedelta(days=1)}_at_least_24.csv",
                [(f"{D1}", "13:00", "Spain La Liga", "Betis", "Getafe", 0.61, 25.0, m1),
                 (f"{D2}", "16:00", "Spain La Liga", "Sevilla", "Celta", 0.72, 26.0, m2)], replay=True)
    write_picks(d / f"{SCAN}_at_least_24.csv",
                [(f"{D1}", "13:00", "Spain La Liga", "Betis", "Getafe", 0.66, 25.0, m1),
                 (f"{D2}", "16:00", "Spain La Liga", "Sevilla", "Celta", 0.75, 26.0, m2)])
    write_picks(d / f"{D2 + timedelta(days=1):%Y-%m-%d}_0900_at_least_24.csv",  # after both match days
                [(f"{D2}", "16:00", "Spain La Liga", "Sevilla", "Celta", 0.99, 26.0, m2),
                 (f"{DUP}", "18:00", "Spain La Liga", "Girona", "Osasuna", 0.81, 27.0, up)])
    mpath = tmp_path / "matches.parquet"
    res.to_parquet(mpath, index=False)
    return d, mpath, (m1, m2, up), res


def test_import_picks_idempotent(engine, picks_setup):
    d, _, (m1, m2, up), _ = picks_setup
    assert load.import_picks_dir(engine, d) == {"files": 3, "inserted": 3, "predictions": 6}
    assert load.import_picks_dir(engine, d) == {"files": 3, "inserted": 0, "predictions": 0}
    pr = runs(engine, "picks")
    assert [r["params"]["source"] for r in pr] == ["live", "live", "replay"]
    assert pr[0]["params"]["scan_ts"] == f"{D1}T09:00:00" and pr[0]["params"]["n"] == 24
    assert match(engine, up)["status"] == "scheduled"  # stub for the upcoming fixture


def test_official_picks_view_matches_track_settle(engine, picks_setup):
    d, mpath, (m1, m2, up), res = picks_setup
    load.import_picks_dir(engine, d)
    load.upsert_matches(engine, res)  # results arrive after the scans
    from shots.db.repo import Repository

    got = Repository(engine).track_records("live").set_index(["match_id", "n"]).sort_index()
    want = track.settle(d, mpath, out_path=None).set_index(["match_id", "n"]).sort_index()
    assert list(got.index) == list(want.index)
    assert got["p"].tolist() == want["p"].tolist()
    assert got["status"].tolist() == want["status"].tolist()
    assert got["hit"].astype(str).tolist() == want["hit"].astype(str).tolist()
    assert got.loc[(m2, 24), "p"] == 0.75  # the post-match-day scan does not count
    assert got.loc[(up, 24), "status"] == "pending"


def test_import_backfill_idempotent_and_skips_unknown_matches(engine, picks_setup):
    _, _, (m1, m2, _), res = picks_setup
    load.upsert_matches(engine, res)
    back = pd.DataFrame({"match_id": [m1, m1, m2, m2, "ghost", "ghost"], "date": pd.Timestamp(D1),
                         "competition": "Spain La Liga", "N": [22, 24] * 3, "p": [.7, .6, .5, .4, .3, .2],
                         "actual": [26, 26, 19, 19, 20, 20]})
    res = load.import_backfill(engine, back)
    assert res["status"] == "inserted" and res["predictions"] == 2
    assert load.import_backfill(engine, back)["status"] == "exists"
    from shots.db.repo import Repository

    rec = Repository(engine).track_records("backfill").set_index(["match_id", "n"])
    assert rec.loc[(m1, 24), "p"] == 0.6 and bool(rec.loc[(m1, 24), "hit"]) is True
    assert bool(rec.loc[(m2, 22), "hit"]) is False and rec.loc[(m2, 22), "actual"] == 19


def test_sync_all_end_to_end(monkeypatch, tmp_path, picks_setup, migrated_db):
    d, mpath, _, _ = picks_setup
    board_path = tmp_path / "board.parquet"
    make_board().to_parquet(board_path, index=False)
    (tmp_path / "board.meta.json").write_text(json.dumps(META), encoding="utf-8")
    monkeypatch.setenv("SHOTS_BOARD", str(board_path))
    monkeypatch.setattr(load, "MATCHES_PATH", mpath)
    monkeypatch.setattr(load, "CALENDAR_PATH", tmp_path / "none.parquet")
    monkeypatch.setattr(load, "FD_FIXTURES_DIR", tmp_path / "none")
    monkeypatch.setattr(load, "PICKS_DIR", d)
    monkeypatch.setattr(load, "BACKFILL_PATH", tmp_path / "none.parquet")
    first = load.sync_all(migrated_db, today=date(2026, 10, 8))
    assert first["board"]["status"] == "inserted" and first["picks"]["inserted"] == 3
    # sync_all loads results before picks: a DB rebuilt from files cannot prove that scans of matches
    # already played were inserted before kick-off, so only the upcoming match's pick is official
    assert first["official_picks"] == {"pending": 1}
    again = load.sync_all(migrated_db, today=date(2026, 10, 8))
    assert again["matches"]["written"] == 0 and again["board"]["status"] == "exists"
    assert again["picks"]["inserted"] == 0
