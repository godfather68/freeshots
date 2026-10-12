"""Migration 0003 (2026-10-09 DB verification): REPLACE / UPSERT cannot rewrite a prediction, runs cannot
be deleted or re-timed, replays and late inserts never count as official live picks, and the file
track record drops post-kick-off scans like the DB. Bypass attempts use a plain sqlite3 connection with
foreign_keys OFF (what the evaluator used), not the app's engine."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select, text

from shots.db import load, migrate
from shots.db.engine import make_engine
from shots.db.models import Prediction, PredictionRun
from shots.db.repo import Repository
from shots.ingest.schema import make_match_id
from shots.predict import track
from tests.test_db_integrity import DAY, DAY_S, KICKOFF, MID, board, local, meta_at, scheduled
from tests.test_db_load import played, write_picks


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


@pytest.fixture
def raw(migrated_db):
    """A plain sqlite3 client: no pragmas, foreign keys OFF."""
    c = sqlite3.connect(migrated_db, timeout=10, isolation_level=None)
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    yield c
    c.close()


def settled_board(engine):
    """A board run whose match has since been played (locked + settled)."""
    scheduled(engine)
    load.import_board(engine, board(), meta_at(KICKOFF - timedelta(hours=5)))
    load.upsert_matches(engine, played([("Spain La Liga", "Spain", 1, DAY_S, "Betis", "Getafe", 14, 12)]))
    with engine.connect() as c:
        return c.execute(select(Prediction.id, Prediction.run_id, Prediction.median)).one()


def db_now(raw) -> str:
    return raw.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]


# ----------------------------------------------------------------------------- 2a: REPLACE / UPSERT

def test_insert_or_replace_cannot_rewrite_a_settled_prediction(engine, raw):
    pid, run_id, median = settled_board(engine)
    now = db_now(raw)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("INSERT OR REPLACE INTO predictions (id, run_id, match_id, median, created_at, pre_match) "
                    "VALUES (?, ?, ?, 99, ?, 0)", (pid, run_id, MID, now))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):  # same (run, match), new id
        raw.execute("REPLACE INTO predictions (run_id, match_id, median, created_at, pre_match) "
                    "VALUES (?, ?, 99, ?, 0)", (run_id, MID, now))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("INSERT INTO predictions (id, run_id, match_id, median, created_at, pre_match) "
                    "VALUES (?, ?, ?, 99, ?, 0) ON CONFLICT(id) DO UPDATE SET median = excluded.median",
                    (pid, run_id, MID, now))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("INSERT OR IGNORE INTO predictions (run_id, match_id, median, created_at, pre_match) "
                    "VALUES (?, ?, 99, ?, 0)", (run_id, MID, now))
    assert raw.execute("SELECT id, median FROM predictions").fetchall() == [(pid, median)]


# ----------------------------------------------------------------------------- 2b: runs

def test_run_with_predictions_cannot_be_deleted_even_without_foreign_keys(engine, raw):
    _, run_id, _ = settled_board(engine)
    with pytest.raises(sqlite3.DatabaseError, match="cannot be deleted"):
        raw.execute("DELETE FROM prediction_runs WHERE id = ?", (run_id,))
    assert raw.execute("SELECT count(*) FROM prediction_runs").fetchone()[0] == 1
    assert raw.execute("SELECT count(*) FROM predictions WHERE run_id = ?", (run_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("assignment", [
    "params = json_set(params, '$.scan_ts', '2000-01-01T00:00:00')",
    "built_at = '2000-01-01 00:00:00.000000'",
    "kind = 'backfill'",
    "source_key = 'board:other'",
    "models = '[\"other\"]'",
    "content_hash = 'x'",
    "created_at = '2000-01-01 00:00:00.000000'",
    "id = 999",
])
def test_run_identity_and_timing_are_immutable(engine, raw, assignment):
    _, run_id, _ = settled_board(engine)
    before = raw.execute("SELECT * FROM prediction_runs").fetchall()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute(f"UPDATE prediction_runs SET {assignment} WHERE id = ?", (run_id,))
    assert raw.execute("SELECT * FROM prediction_runs").fetchall() == before


def test_run_bookkeeping_columns_stay_writable(engine, raw):
    """import_board bumps confirmed_at and meta on an identical rebuild."""
    _, run_id, _ = settled_board(engine)
    raw.execute("UPDATE prediction_runs SET confirmed_at = '2030-01-01 00:00:00.000000', meta = '{}', "
                "status = 'complete', n_predictions = n_predictions WHERE id = ?", (run_id,))
    assert raw.execute("SELECT confirmed_at FROM prediction_runs").fetchone()[0].startswith("2030")


def test_identical_board_rebuild_still_bumps_confirmed_at(engine):
    scheduled(engine)
    first = load.import_board(engine, board(), meta_at(KICKOFF - timedelta(hours=6)))
    again = load.import_board(engine, board(), meta_at(KICKOFF - timedelta(hours=5)))
    assert again == {"status": "unchanged", "run_id": first["run_id"], "predictions": 1}


def test_delete_run_still_purges_a_broken_import_before_kickoff(engine):
    scheduled(engine)
    run_id = load.import_board(engine, board(), meta_at(KICKOFF - timedelta(hours=6)))["run_id"]
    assert load.delete_run(engine, run_id) == 1
    with engine.connect() as c:
        assert c.execute(select(PredictionRun.id)).all() == []


# ----------------------------------------------------------------------------- 2c: insert time, replays

def test_created_at_must_be_the_insert_time(engine, raw):
    scheduled(engine)
    run_id = load.import_board(engine, board(), meta_at(KICKOFF - timedelta(hours=6)))["run_id"]
    other = make_match_id("Spain La Liga", pd.Timestamp(DAY_S), "Sevilla", "Celta")
    load.import_board(engine, board(other, home="Sevilla", away="Celta"), meta_at(KICKOFF - timedelta(hours=5)))
    for created in ("2026-01-01 00:00:00.000000", None):
        with pytest.raises(sqlite3.DatabaseError, match="insert time"):
            raw.execute("INSERT INTO predictions (run_id, match_id, median, created_at, pre_match) "
                        "VALUES (?, ?, 25, ?, 0)", (run_id, other, created))


def test_pre_match_claims_the_db_can_disprove_are_rejected(engine, raw):
    pid, run_id, _ = settled_board(engine)  # MID is played
    with pytest.raises(sqlite3.DatabaseError, match="pre_match"):
        raw.execute("INSERT INTO predictions (run_id, match_id, median, created_at, pre_match) "
                    "VALUES (?, ?, 25, ?, 1)", (run_id + 1000, MID, db_now(raw)))
    # a scheduled match whose kick-off has passed: a late insert cannot claim pre_match
    late = make_match_id("Spain La Liga", pd.Timestamp(datetime.now().date()), "Girona", "Osasuna")
    raw.execute("INSERT INTO matches (match_id, source, competition_id, date, kickoff_utc, status, home_team_id, "
                "away_team_id, home_team, away_team, search_key, created_at, updated_at) "
                "SELECT ?, 't', competition_id, date('now'), datetime('now', '-1 hour'), 'scheduled', home_team_id, "
                "away_team_id, 'Girona', 'Osasuna', 'girona | osasuna', created_at, updated_at FROM matches LIMIT 1",
                (late,))
    with pytest.raises(sqlite3.DatabaseError, match="pre_match"):
        raw.execute("INSERT INTO predictions (run_id, match_id, median, created_at, pre_match) "
                    "VALUES (?, ?, 25, ?, 1)", (run_id, late, db_now(raw)))
    raw.execute("INSERT INTO predictions (run_id, match_id, median, created_at, pre_match) "
                "VALUES (?, ?, 25, ?, 0)", (run_id, late, db_now(raw)))  # honest flag: accepted


def test_backdated_live_scan_inserted_after_kickoff_is_not_official(engine, tmp_path):
    """A live file whose name claims a pre-kick-off scan, imported after kick-off, is not official."""
    from tests.test_db_load import fixtures

    today = datetime.now().date()
    fx = fixtures([("Spain La Liga", "Spain", 1, str(today), "Girona", "Osasuna", "00:00")])
    fx["kickoff_utc"] = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1))
    load.upsert_fixtures(engine, fx, today=today)
    mid = fx.loc[0, "match_id"]
    d = tmp_path / "picks"
    d.mkdir()
    scan = local(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3))  # "before kick-off"
    write_picks(d / f"{scan:%Y-%m-%d_%H%M}_at_least_24.csv",
                [(str(today), "", "Spain La Liga", "Girona", "Osasuna", 0.9, 27.0, mid)])
    load.import_picks_dir(engine, d)
    with engine.connect() as c:
        assert c.execute(select(Prediction.pre_match)).scalar() is False
        assert c.execute(text("SELECT count(*) FROM v_official_picks")).scalar() == 0


def test_replays_are_never_official_but_stay_queryable(engine, tmp_path):
    scheduled(engine)
    d = tmp_path / "picks"
    d.mkdir()
    write_picks(d / f"replay_{DAY - timedelta(days=1)}_at_least_24.csv",
                [(DAY_S, "16:00", "Spain La Liga", "Betis", "Getafe", 0.8, 25.0, MID)], replay=True)
    write_picks(d / f"{local(KICKOFF - timedelta(hours=30)):%Y-%m-%d_%H%M}_at_least_24.csv",
                [(DAY_S, "16:00", "Spain La Liga", "Betis", "Getafe", 0.7, 25.0, MID)])
    load.import_picks_dir(engine, d)
    load.upsert_matches(engine, played([("Spain La Liga", "Spain", 1, DAY_S, "Betis", "Getafe", 14, 12)]))
    repo = Repository(engine)
    live, replay = repo.track_records("live"), repo.track_records("replay")
    assert live[["match_id", "p", "source", "status"]].values.tolist() == [[MID, 0.7, "live", "settled"]]
    assert replay[["match_id", "p", "source", "status"]].values.tolist() == [[MID, 0.8, "replay", "settled"]]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM v_official_picks WHERE source = 'replay'")).scalar() == 0


def test_rows_inserted_late_before_0003_drop_out_of_the_official_record(tmp_path):
    """A 0002-era row flagged pre_match from its claimed scan time but inserted days after the match
    (the replay case of the verification) is re-checked by the view against its insert time."""
    path = tmp_path / "old.db"
    migrate.upgrade(path, "0002_pre_match")
    engine = make_engine(path)
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO competitions (id, name, country, tier) VALUES (1, 'Spain La Liga', 'Spain', 1)"))
            c.execute(text("INSERT INTO teams (team_id, name, country) VALUES ('spain:a', 'A', 'Spain'), "
                           "('spain:b', 'B', 'Spain')"))
            c.execute(text("INSERT INTO matches (match_id, source, competition_id, date, kickoff_utc, status, "
                           "home_shots, away_shots, home_team_id, away_team_id, home_team, away_team, search_key, "
                           "created_at, updated_at) VALUES ('m', 't', 1, '2026-10-03', '2026-10-03 14:00:00.000000', "
                           "'played', 14, 12, 'spain:a', 'spain:b', 'A', 'B', 'a | b', '2026-10-01', '2026-10-01')"))
            for rid, source, created in ((1, "live", "2026-10-03 08:00:00.000000"),
                                         (2, "live", "2026-10-09 10:43:58.000000")):
                params = json.dumps({"n": 24, "source": source, "scan_ts": "2026-10-03T09:00:0" + str(rid)})
                c.execute(text("INSERT INTO prediction_runs (id, kind, source_key, status, built_at, models, params, "
                               "meta, n_predictions, created_at) VALUES (:i, 'picks', :k, 'complete', "
                               "'2026-10-03 07:00:00.000000', '[]', :p, '{}', 1, :c)"),
                          {"i": rid, "k": f"picks:{rid}", "p": params, "c": created})
                c.execute(text("INSERT INTO predictions (run_id, match_id, points, created_at, pre_match) "
                               "VALUES (:r, 'm', :pts, :c, 1)"), {"r": rid, "pts": json.dumps({"24": 0.5 + rid / 10}), "c": created})
            # under 0002 the late row (later scan_ts) wins the official record
            assert c.execute(text("SELECT run_id FROM v_official_picks")).scalar() == 2
        assert migrate.upgrade(engine) == migrate.head()
        with engine.connect() as c:
            assert c.execute(text("SELECT run_id, pre_match FROM v_prediction_outcomes ORDER BY run_id")).all() == \
                [(1, 1), (2, 0)]
            assert c.execute(text("SELECT run_id FROM v_official_picks")).scalars().all() == [1]
        assert migrate.downgrade(engine, "0002_pre_match") == "0002_pre_match"
        with engine.connect() as c:
            names = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')")}
        assert "trg_predictions_no_replace" not in names and "v_replay_picks" not in names
        assert "v_official_picks" in names and "trg_predictions_no_update" in names
        assert migrate.upgrade(engine) == migrate.head()
    finally:
        engine.dispose()


# ----------------------------------------------------------------------------- 1c: file record = DB rule

def test_file_record_drops_post_kickoff_scans_like_the_db(engine, tmp_path):
    """A scan taken after kick-off on match day used to win the file record (dates compared only)."""
    scheduled(engine, kickoff_utc=None)  # DB knows no kick-off time: both sides use the row's "16:00"
    lock = datetime.combine(DAY, datetime.min.time().replace(hour=16), ZoneInfo("Europe/Madrid")) \
        .astimezone(timezone.utc).replace(tzinfo=None)
    d = tmp_path / "picks"
    d.mkdir()
    for when, p in ((lock - timedelta(hours=3), 0.707), (lock + timedelta(hours=2), 0.999)):
        write_picks(d / f"{local(when):%Y-%m-%d_%H%M}_at_least_24.csv",
                    [(DAY_S, "16:00", "Spain La Liga", "Betis", "Getafe", p, 25.0, MID)])
    res = played([("Spain La Liga", "Spain", 1, DAY_S, "Betis", "Getafe", 14, 12)])
    mpath = tmp_path / "m.parquet"
    res.to_parquet(mpath, index=False)
    load.import_picks_dir(engine, d)
    load.upsert_matches(engine, res)
    file_rec = track.settle(d, mpath, out_path=None)
    db_rec = Repository(engine).track_records("live")
    assert file_rec["p"].tolist() == db_rec["p"].tolist() == [0.707]
