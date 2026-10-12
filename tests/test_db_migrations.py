"""Schema: migrations from empty, models in sync with migrations, pragmas, seeded plans/entitlements."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from shots.db import access, migrate
from shots.db.engine import db_path, make_engine, session_scope
from shots.db.models import Base

TABLES = {"competitions", "teams", "matches", "prediction_runs", "predictions", "job_runs", "plans",
          "plan_entitlements", "users", "subscriptions", "sessions", "login_attempts"}
VIEWS = {"v_prediction_outcomes", "v_prediction_points", "v_official_picks", "v_replay_picks"}


def test_upgrade_from_empty_creates_schema(tmp_path):
    path = tmp_path / "new.db"
    assert migrate.current(path) is None
    assert migrate.upgrade(path) == migrate.head()
    assert migrate.is_current(path)
    engine = make_engine(path)
    insp = inspect(engine)
    assert TABLES <= set(insp.get_table_names())
    assert VIEWS <= set(insp.get_view_names())
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 5000
        triggers = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"trg_predictions_no_update", "trg_predictions_no_delete_after_kickoff"} <= triggers
    engine.dispose()


def test_upgrade_is_idempotent_and_downgrade_roundtrips(migrated_db):
    assert migrate.upgrade(migrated_db) == migrate.head()
    assert migrate.downgrade(migrated_db) is None
    engine = make_engine(migrated_db)
    assert not (TABLES & set(inspect(engine).get_table_names()))
    engine.dispose()
    assert migrate.upgrade(migrated_db) == migrate.head()


def test_models_match_migrations(migrated_db):
    """Autogenerate finds nothing to change: the ORM models and the migration agree."""
    engine = make_engine(migrated_db)
    with engine.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    assert diff == []


def test_default_path_and_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SHOTS_DB", str(tmp_path / "x.db"))
    assert db_path() == tmp_path / "x.db"
    monkeypatch.delenv("SHOTS_DB")
    assert db_path().name == "shots.db" and db_path().parent.name == "data"


def test_seeded_plans_map_to_entitlements(migrated_db):
    engine = make_engine(migrated_db)
    with session_scope(engine) as s:
        assert access.default_plan_key(s) == "free"
        free, pro, premium = (access.for_plan(s, k) for k in ("free", "pro", "premium"))
        nothing = access.for_plan(s, "no-such-plan")
        keys = {k for (k,) in s.execute(text("SELECT DISTINCT key FROM plan_entitlements"))}
    engine.dispose()
    assert keys == set(access.KEYS)
    assert not free.can_view_all_leagues and free.allowed_leagues == frozenset(
        ["England Premier League", "Spain La Liga", "Italy Serie A", "Germany Bundesliga", "France Ligue 1"])
    assert (free.max_days_ahead, free.team_predictions, free.export, free.track_detail) == (3, False, False, False)
    assert pro.can_view_all_leagues and (pro.max_days_ahead, pro.team_predictions, pro.export, pro.track_detail) == (
        30, True, False, False)
    assert premium.can_view_all_leagues and premium.max_days_ahead is None
    assert premium.team_predictions and premium.export and premium.track_detail
    # unknown plan: deny by default
    assert not nothing.can_view_all_leagues and not nothing.allowed_leagues and nothing.max_days_ahead == 0
    assert not (nothing.team_predictions or nothing.export or nothing.track_detail)
    assert free.league_allowed("Spain La Liga") and not free.league_allowed("Italy Serie B")


def test_account_tables_start_empty(migrated_db):
    engine = make_engine(migrated_db)
    with engine.connect() as c:
        for t in ("users", "subscriptions", "sessions", "login_attempts"):
            assert c.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0
    engine.dispose()


def test_0002_pre_match_backfills_existing_rows_and_roundtrips(tmp_path):
    """Upgrade a 0001 DB holding predictions: pre_match is back-filled from built_at vs the lock moment
    (kickoff_utc, else 00:00 UTC of the match date), the append-only trigger is back, and downgrade
    returns to the 0001 schema with the data intact."""
    path = tmp_path / "old.db"
    assert migrate.upgrade(path, "0001_initial") == "0001_initial"
    engine = make_engine(path)
    with engine.begin() as c:
        c.execute(text("INSERT INTO competitions (id, name, country, tier) VALUES (1, 'Spain La Liga', 'Spain', 1)"))
        c.execute(text("INSERT INTO teams (team_id, name, country) VALUES ('spain:a', 'A', 'Spain'), ('spain:b', 'B', 'Spain')"))
        for mid, day, ko in (("known", "2026-10-10", "2026-10-10 19:00:00.000000"), ("unknown", "2026-10-10", None)):
            c.execute(text("INSERT INTO matches (match_id, source, competition_id, date, kickoff_utc, status, home_team_id, "
                           "away_team_id, home_team, away_team, search_key, created_at, updated_at) VALUES "
                           "(:m, 't', 1, :d, :k, 'scheduled', 'spain:a', 'spain:b', 'A', 'B', 'a | b', :d, :d)"),
                      {"m": mid, "d": day, "k": ko})
        for rid, built in ((1, "2026-10-09 20:00:00.000000"), (2, "2026-10-10 12:00:00.000000")):
            c.execute(text("INSERT INTO prediction_runs (id, kind, source_key, status, built_at, models, params, meta, "
                           "n_predictions, created_at) VALUES (:i, 'board', :k, 'complete', :b, '[]', '{}', '{}', 2, :b)"),
                      {"i": rid, "k": f"board:{rid}", "b": built})
            for mid in ("known", "unknown"):
                c.execute(text("INSERT INTO predictions (run_id, match_id, median, created_at) VALUES (:r, :m, 25, :b)"),
                          {"r": rid, "m": mid, "b": built})
    assert migrate.upgrade(engine, "0002_pre_match") == "0002_pre_match"
    with engine.connect() as c:
        got = c.execute(text("SELECT run_id, match_id, pre_match FROM predictions ORDER BY run_id, match_id")).all()
        view = c.execute(text("SELECT run_id, match_id, pre_match FROM v_prediction_outcomes ORDER BY run_id, match_id")).all()
    # run 1 (evening before): both pre-match; run 2 (12:00 on match day): before the 19:00 kick-off,
    # but after 00:00 UTC for the match without a known kick-off
    assert got == [(1, "known", 1), (1, "unknown", 1), (2, "known", 1), (2, "unknown", 0)] and view == got
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as c:
            c.execute(text("UPDATE predictions SET median = 1"))
    assert migrate.downgrade(engine, "0001_initial") == "0001_initial"
    with engine.connect() as c:
        cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(predictions)")}
        assert "pre_match" not in cols
        assert c.execute(text("SELECT count(*) FROM v_prediction_outcomes")).scalar() == 4
        assert c.execute(text("SELECT count(*) FROM v_official_picks")).scalar() == 0
        triggers = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"trg_predictions_no_update", "trg_predictions_no_delete_after_kickoff"} <= triggers
    assert migrate.upgrade(engine, "0002_pre_match") == "0002_pre_match"
    engine.dispose()
