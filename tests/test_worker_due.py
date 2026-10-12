"""Skip-if-fresh and due-work rules (shots.worker.due), evaluated against a real migrated DB."""

import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import update

from shots.db import jobs, load
from shots.db.engine import make_engine, session_scope
from shots.db.models import JobRun
from shots.worker import due
from tests.test_db_load import fixtures
from tests.test_web import META, row

UTC = ZoneInfo("UTC")
WED = datetime(2026, 10, 14, 15, 0)  # a Wednesday: no odds publication that day


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


@pytest.fixture
def cal_dir(tmp_path):
    d = tmp_path / "calendars"
    d.mkdir()
    old = d / "laliga_2026.ics"
    old.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n", encoding="utf-8")
    os.utime(old, (time.mktime((2026, 1, 1, 0, 0, 0, 0, 0, -1)),) * 2)
    return d


def ok_update(engine, started_at, status="ok", job="update"):
    jid = jobs.start(engine, job)
    jobs.finish(engine, jid, status, {})
    with session_scope(engine) as s:
        s.execute(update(JobRun).where(JobRun.id == jid).values(started_at=started_at,
                                                                finished_at=started_at + timedelta(minutes=10)))
    return jid


def with_board(engine):
    load.import_board(engine, pd.DataFrame([row("far", "2030-05-01", "20:00", "Spain La Liga", "Spain", 1,
                                                "Betis", "Getafe", 27)]), META)


def add_fixture(engine, day, kickoff_utc=None, odds=None, home="Sevilla", away="Celta"):
    fx = fixtures([("Spain La Liga", "Spain", 1, day, home, away, "21:00")])
    fx["kickoff_utc"] = pd.Timestamp(kickoff_utc) if kickoff_utc else pd.NaT
    if odds is not None:
        fx["odds_over25"] = odds
    load.upsert_fixtures(engine, fx, today=date(2026, 10, 1))


def decide(engine, now, cal_dir, since=None, **kw):
    if since is not None:
        ok_update(engine, since)
    return due.decide(engine, now=now, tz=UTC, cal_dir=cal_dir, **kw)


def codes(d):
    return [r.code for r in d.reasons]


def test_never_updated_runs(engine, cal_dir):
    d = decide(engine, WED, cal_dir)
    assert d.run and codes(d) == ["never"]


def test_force_always_runs(engine, cal_dir):
    with_board(engine)
    d = decide(engine, WED, cal_dir, since=WED - timedelta(minutes=5), force=True)
    assert d.run and codes(d) == ["forced"]


def test_older_than_min_interval_runs(engine, cal_dir):
    with_board(engine)
    d = decide(engine, WED, cal_dir, since=WED - timedelta(hours=2, minutes=1))
    assert d.run and codes(d) == ["interval"]
    d = due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir, min_interval=timedelta(hours=4))
    assert not d.run  # same history, longer configured interval: fresh


def test_fresh_and_nothing_due_skips(engine, cal_dir):
    with_board(engine)
    add_fixture(engine, "2026-10-18", "2026-10-18 19:00")
    d = decide(engine, WED, cal_dir, since=WED - timedelta(minutes=30))
    assert not d.run and codes(d) == ["fresh"] and d.summary().startswith("skip")


def test_only_ok_updates_count_as_fresh(engine, cal_dir):
    with_board(engine)
    ok_update(engine, WED - timedelta(hours=5))
    ok_update(engine, WED - timedelta(minutes=10), status="partial")
    ok_update(engine, WED - timedelta(minutes=5), status="ok", job="worker")
    d = due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir)
    assert d.run and codes(d) == ["interval"]


def test_no_board_is_due(engine, cal_dir):
    d = decide(engine, WED, cal_dir, since=WED - timedelta(minutes=30))
    assert d.run and codes(d) == ["no-board"]


def test_match_finished_since_last_run_is_due(engine, cal_dir):
    with_board(engine)
    add_fixture(engine, "2026-10-14", "2026-10-14 12:45")  # ends ~14:45 UTC, after the 14:30 run
    d = decide(engine, WED, cal_dir, since=WED - timedelta(minutes=30))
    assert d.run and codes(d) == ["results"] and "1 match" in d.reasons[0].detail


def test_match_finished_before_last_run_is_not_due_again(engine, cal_dir):
    with_board(engine)
    add_fixture(engine, "2026-10-14", "2026-10-14 11:00")  # ended 13:00, already seen by the 14:30 run
    assert not decide(engine, WED, cal_dir, since=WED - timedelta(minutes=30)).run


def test_played_match_is_not_due(engine, cal_dir):
    with_board(engine)
    add_fixture(engine, "2026-10-14", "2026-10-14 12:45")
    load.upsert_matches(engine, fixtures([("Spain La Liga", "Spain", 1, "2026-10-14", "Sevilla", "Celta", "21:00")])
                        .assign(home_shots=10, away_shots=12).astype({"home_shots": "Int64", "away_shots": "Int64"}))
    assert not decide(engine, WED, cal_dir, since=WED - timedelta(minutes=30)).run


def test_unknown_kickoff_counts_as_finished_at_end_of_match_day(engine, cal_dir):
    with_board(engine)
    add_fixture(engine, "2026-10-13")  # no kick-off time: finished at 2026-10-14 00:00 UTC
    d = decide(engine, datetime(2026, 10, 14, 0, 10), cal_dir, since=datetime(2026, 10, 13, 23, 50))
    assert codes(d) == ["results", "new-day"]


def test_new_day_is_due(engine, cal_dir):
    with_board(engine)
    d = decide(engine, datetime(2026, 10, 14, 0, 15), cal_dir, since=datetime(2026, 10, 13, 23, 30))
    assert d.run and codes(d) == ["new-day"]


def test_new_day_uses_the_local_date(engine, cal_dir):
    with_board(engine)
    ok_update(engine, datetime(2026, 10, 13, 23, 30))  # 01:30 in Paris on the 14th
    d = due.decide(engine, now=datetime(2026, 10, 14, 0, 15), tz=ZoneInfo("Europe/Paris"), cal_dir=cal_dir)
    assert not d.run


def test_odds_publication_with_fixtures_lacking_odds_is_due(engine, cal_dir):
    with_board(engine)
    add_fixture(engine, "2026-10-17", "2026-10-17 19:00")  # no odds yet
    tue = datetime(2026, 10, 13, 9, 30)
    d = decide(engine, tue, cal_dir, since=tue - timedelta(minutes=45))  # last run 08:45, before 09:00
    assert d.run and codes(d) == ["odds"] and "1 fixture" in d.reasons[0].detail
    ok_update(engine, tue)  # a run after 09:00 already fetched what was published
    assert not due.decide(engine, now=tue + timedelta(minutes=30), tz=UTC, cal_dir=cal_dir).run


def test_odds_publication_when_all_fixtures_have_odds_is_not_due(engine, cal_dir):
    with_board(engine)
    add_fixture(engine, "2026-10-17", "2026-10-17 19:00", odds=1.85)
    add_fixture(engine, "2026-11-20", "2026-11-20 19:00", home="Getafe", away="Betis")  # beyond 7 days
    tue = datetime(2026, 10, 13, 9, 30)
    assert not decide(engine, tue, cal_dir, since=tue - timedelta(minutes=45)).run


def test_changed_calendar_is_due(engine, cal_dir):
    with_board(engine)
    ok_update(engine, WED - timedelta(minutes=30))
    assert not due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir).run
    new = cal_dir / "seriea_2026.ics"
    new.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n", encoding="utf-8")
    edited = (WED - timedelta(minutes=10)).replace(tzinfo=UTC).timestamp()
    os.utime(new, (edited, edited))
    d = due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir)
    assert d.run and codes(d) == ["calendars"]


def test_publication_moments():
    tue_morning = datetime(2026, 10, 13, 8, 0)
    assert due.last_odds_publication(tue_morning, UTC) == datetime(2026, 10, 9, 9, 0)  # previous Friday
    assert due.next_odds_publication(tue_morning, UTC) == datetime(2026, 10, 13, 9, 0)
    paris = ZoneInfo("Europe/Paris")  # 09:00 local = 07:00 UTC in summer time
    assert due.next_odds_publication(tue_morning - timedelta(hours=2), paris) == datetime(2026, 10, 13, 7, 0)


def test_next_match_end(engine):
    add_fixture(engine, "2026-10-17", "2026-10-17 19:00")
    assert due.next_match_end(engine, WED) == datetime(2026, 10, 17, 21, 0)
