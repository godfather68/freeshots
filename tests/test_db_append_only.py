"""Predictions are append-only: never updated; never deleted once the match has kicked off (DB triggers,
so the rule holds for any client, not only our loaders)."""

from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from shots.db import load
from shots.db.engine import make_engine, session_scope
from shots.db.models import Match, Prediction, PredictionRun
from tests.test_web import META, row


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


def one_row_board():
    return pd.DataFrame([row("m", "2026-10-10", "20:00", "Spain La Liga", "Spain", 1, "Betis", "Getafe", 27)])


def setup(engine, kickoff_utc: datetime, status: str = "scheduled") -> int:
    """Import a one-row board, then move the match's kick-off; returns the prediction id."""
    load.import_board(engine, one_row_board(), META)
    with session_scope(engine) as s:
        s.execute(update(Match).where(Match.match_id == "m").values(kickoff_utc=kickoff_utc, status=status,
                                                                     date=kickoff_utc.date()))
        return s.scalar(select(Prediction.id).where(Prediction.match_id == "m"))


PAST = datetime(2020, 1, 1, 15, 0)
FUTURE = datetime.now() + timedelta(days=3650)


@pytest.mark.parametrize("kickoff", [PAST, FUTURE])
def test_updates_are_always_rejected(engine, kickoff):
    pid = setup(engine, kickoff)
    with pytest.raises(DBAPIError, match="append-only"):
        with session_scope(engine) as s:
            s.execute(update(Prediction).where(Prediction.id == pid).values(median=99.0))
    with pytest.raises(DBAPIError, match="append-only"):  # through the ORM as well
        with session_scope(engine) as s:
            s.get(Prediction, pid).lb_80 = 1
    with engine.connect() as c:
        assert c.execute(select(Prediction.median).where(Prediction.id == pid)).scalar() == 27.0


def test_delete_after_kickoff_is_rejected(engine):
    pid = setup(engine, PAST)
    with pytest.raises(DBAPIError, match="after kick-off"):
        with session_scope(engine) as s:
            s.execute(delete(Prediction).where(Prediction.id == pid))
    with pytest.raises(DBAPIError, match="after kick-off"):
        load.delete_run(engine, 1)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM predictions")).scalar() == 1


def test_delete_of_played_match_is_rejected_even_with_future_kickoff(engine):
    setup(engine, FUTURE, status="played")
    with pytest.raises(DBAPIError, match="after kick-off"):
        load.delete_run(engine, 1)


def test_unknown_kickoff_locks_at_start_of_match_date(engine):
    load.import_board(engine, one_row_board(), META)
    with session_scope(engine) as s:
        s.execute(update(Match).where(Match.match_id == "m").values(kickoff_utc=None, date=(datetime.now() - timedelta(days=1)).date()))
    with pytest.raises(DBAPIError, match="after kick-off"):
        load.delete_run(engine, 1)


def test_delete_before_kickoff_is_allowed(engine):
    """Purging a broken run is fine while nothing has kicked off."""
    setup(engine, FUTURE)
    assert load.delete_run(engine, 1) == 1
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM predictions")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM prediction_runs")).scalar() == 0


def test_runs_with_predictions_cannot_be_deleted_directly(engine):
    setup(engine, FUTURE)
    with pytest.raises(DBAPIError):  # foreign key: predictions reference the run
        with session_scope(engine) as s:
            s.execute(delete(PredictionRun).where(PredictionRun.id == 1))


def test_new_runs_append_and_keep_history(engine):
    setup(engine, PAST)  # first prediction is locked
    changed = one_row_board().assign(expected_shots=31.0)
    res = load.import_board(engine, changed, dict(META, built_at="2026-10-09T09:00:00"))
    assert res["status"] == "inserted"
    with engine.connect() as c:
        assert [m for (m,) in c.execute(select(Prediction.median).order_by(Prediction.id))] == [27.0, 31.0]
