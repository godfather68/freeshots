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


def test_missing_board_runs_whatever_the_age(engine, cal_dir):
    d = decide(engine, WED, cal_dir, since=WED - timedelta(hours=5), min_gap=timedelta(hours=9))
    assert d.run and codes(d) == ["no-board"]


def test_min_gap_defers_due_work_and_the_interval_but_not_force(engine, cal_dir):
    """The hourly GitHub schedule: nothing runs within 90 min of the previous attempt, whatever is due."""
    with_board(engine)
    add_fixture(engine, "2026-10-14", "2026-10-14 12:45")  # ends ~14:45 UTC: `results` is due at 15:00
    gap = timedelta(minutes=90)
    ok_update(engine, WED - timedelta(minutes=58))  # the previous hourly slot ran
    d = due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir, min_gap=gap)
    assert not d.run and codes(d) == ["recent"] and "58 min ago" in d.reasons[0].detail
    assert due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir).run  # no floor (the local default): due work runs
    assert codes(due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir, min_gap=gap, force=True)) == ["forced"]
    later = WED + timedelta(minutes=60)  # the slot after: 118 min since the last attempt
    assert codes(due.decide(engine, now=later, tz=UTC, cal_dir=cal_dir, min_gap=gap)) == ["results"]


def test_min_gap_also_spaces_retries_after_a_failed_update(engine, cal_dir):
    with_board(engine)
    ok_update(engine, WED - timedelta(hours=6))
    ok_update(engine, WED - timedelta(minutes=58), status="partial")  # e.g. the source site was down
    gap = timedelta(minutes=90)
    assert codes(due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir, min_gap=gap)) == ["recent"]
    assert codes(due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir)) == ["interval"]  # no floor: retried at once
    d = due.decide(engine, now=WED + timedelta(hours=1), tz=UTC, cal_dir=cal_dir, min_gap=gap)
    assert d.run and codes(d) == ["interval"]


def test_hourly_slots_give_a_full_update_every_third_slot_when_nothing_is_due(engine, cal_dir):
    """The settings of update.yml (2.5 h interval, 90 min gap) on slots one hour apart."""
    with_board(engine)
    start = datetime(2026, 10, 14, 3, 15)  # mid-week, far from midnight and from the odds hour
    ok_update(engine, start)
    ran = []
    for slot in range(1, 7):
        now = start + timedelta(hours=slot)
        d = due.decide(engine, now=now, tz=UTC, cal_dir=cal_dir, min_interval=timedelta(hours=2.5),
                       min_gap=timedelta(minutes=90))
        if d.run:
            ran.append(slot)
            ok_update(engine, now)
    assert ran == [3, 6]


def test_calendar_fingerprint_replaces_modification_times_once_recorded(engine, cal_dir):
    """A fresh checkout (GitHub runner) stamps every file with the current time: only content counts."""
    with_board(engine)
    fp = due.calendars_fingerprint(cal_dir)
    assert len(fp) == 16 and fp == due.calendars_fingerprint(cal_dir)
    ok_update(engine, WED - timedelta(minutes=30))
    checkout = (WED - timedelta(minutes=5)).replace(tzinfo=UTC).timestamp()
    os.utime(cal_dir / "laliga_2026.ics", (checkout, checkout))  # "checked out" after the last update
    assert codes(due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir)) == ["calendars"]  # nothing recorded yet
    assert due.recorded_calendars(engine) is None
    wid = ok_update(engine, WED - timedelta(minutes=31), job="worker")
    jobs.finish(engine, wid, "ok", {"action": "update", "calendars": fp})
    skipped = jobs.start(engine, "worker")  # newer cycles without a fingerprint do not hide it
    jobs.finish(engine, skipped, "skipped", {"action": "skipped"})
    assert due.recorded_calendars(engine) == fp
    assert not due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir).run  # same content: not due
    (cal_dir / "laliga_2026.ics").write_bytes(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")  # line endings only
    assert due.calendars_fingerprint(cal_dir) == fp
    (cal_dir / "seriea_2026.ics").write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n", encoding="utf-8")
    assert due.calendars_fingerprint(cal_dir) != fp
    d = due.decide(engine, now=WED, tz=UTC, cal_dir=cal_dir)
    assert d.run and codes(d) == ["calendars"]


def test_odds_hour_is_read_in_the_stated_zone(engine, cal_dir, monkeypatch):
    with_board(engine)
    add_fixture(engine, "2026-10-17", "2026-10-17 19:00")  # no odds yet
    ok_update(engine, datetime(2026, 10, 13, 6, 30))  # Tuesday 06:30 UTC
    now = datetime(2026, 10, 13, 7, 30)  # 09:30 in Paris, 07:30 UTC
    assert not due.decide(engine, now=now, tz=UTC, cal_dir=cal_dir).run  # 09:00 UTC has not passed
    d = due.decide(engine, now=now, tz=UTC, cal_dir=cal_dir, odds_tz=ZoneInfo("Europe/Paris"))
    assert codes(d) == ["odds"] and "Tue 09:00" in d.reasons[0].detail
    monkeypatch.setenv("SHOTS_ODDS_TZ", "Europe/Paris")  # the setting; the day boundary stays in `tz`
    assert codes(due.decide(engine, now=now, tz=UTC, cal_dir=cal_dir)) == ["odds"]
    monkeypatch.setenv("SHOTS_ODDS_TZ", "UTC")  # what update.yml states
    assert not due.decide(engine, now=now, tz=ZoneInfo("Europe/Paris"), cal_dir=cal_dir).run
    assert due.env_odds_tz() == ZoneInfo("UTC")


def test_settings_from_the_environment(monkeypatch):
    for name in ("SHOTS_WORKER_MIN_INTERVAL_HOURS", "SHOTS_WORKER_MIN_GAP_MINUTES", "SHOTS_ODDS_TZ"):
        monkeypatch.delenv(name, raising=False)
    assert (due.env_min_interval(), due.env_min_gap(), due.env_odds_tz()) == (timedelta(hours=2), timedelta(0), None)
    monkeypatch.setenv("SHOTS_WORKER_MIN_INTERVAL_HOURS", "2.5")
    monkeypatch.setenv("SHOTS_WORKER_MIN_GAP_MINUTES", "90")
    assert (due.env_min_interval(), due.env_min_gap()) == (timedelta(minutes=150), timedelta(minutes=90))


def test_next_match_end(engine):
    add_fixture(engine, "2026-10-17", "2026-10-17 19:00")
    assert due.next_match_end(engine, WED) == datetime(2026, 10, 17, 21, 0)
