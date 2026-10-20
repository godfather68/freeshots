from sqlalchemy import select

import shots.__main__ as cli
from shots.db import load as dbload
from shots.db.engine import make_engine
from shots.db.models import JobRun
from shots.ingest import calendar_ics, football_data
from shots.predict import board, scan, track

ALL = ["results", "calendars", "picks", "board", "track", "db", "db-manual", "db-provisional", "db-board", "db-picks"]


def _stub(monkeypatch, calls, fail=()):
    def make(name):
        def run(*a, **k):
            calls.append(name)
            if name in fail:
                raise RuntimeError(f"{name} broke")
            return {"ok": 1}
        return run
    monkeypatch.setattr(football_data, "build", make("results"))
    monkeypatch.setattr(calendar_ics, "build", make("calendars"))
    monkeypatch.setattr(cli, "_calendars_changed", lambda: True)
    monkeypatch.setattr(scan, "scan", make("picks"))
    monkeypatch.setattr(board, "build", make("board"))
    monkeypatch.setattr(track, "build_dashboard", make("track"))
    monkeypatch.setattr(dbload, "sync_results", make("db"))
    monkeypatch.setattr(dbload, "sync_manual_results", make("db-manual"))
    monkeypatch.setattr(dbload, "sync_provisional_results", make("db-provisional"))
    monkeypatch.setattr(dbload, "import_board_file", make("db-board"))
    monkeypatch.setattr(dbload, "sync_predictions", make("db-picks"))


def _jobs():
    engine = make_engine()  # SHOTS_DB: per-test temp file (tests/conftest.py)
    with engine.connect() as c:
        rows = c.execute(select(JobRun.job, JobRun.status, JobRun.counts, JobRun.error, JobRun.finished_at)).all()
    engine.dispose()
    return rows


def test_update_runs_every_step_in_order(monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    assert cli.update() == 0
    assert calls == ALL
    [(job, status, counts, error, finished)] = _jobs()
    assert (job, status, error) == ("update", "ok", None) and finished is not None
    assert all(counts["steps"].values()) and counts["db-board"] == {"ok": 1}


def test_failed_step_does_not_stop_later_steps(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, fail={"results"})
    assert cli.update() == 1
    assert calls == ALL
    [(_, status, counts, error, _)] = _jobs()
    assert status == "partial" and counts["steps"]["results"] is False and "results broke" in error


def test_board_systemexit_is_reported_not_raised(monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    def no_fixtures(*a, **k):
        raise SystemExit("no upcoming fixtures in supported divisions")
    monkeypatch.setattr(board, "build", no_fixtures)
    assert cli.update() == 1
    assert calls[-1] == "db-picks" and "track" in calls


def test_unusable_db_does_not_stop_file_steps(monkeypatch, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    monkeypatch.setenv("SHOTS_DB", str(bad))
    calls = []
    _stub(monkeypatch, calls)
    assert cli.update() == 1  # db steps fail (database unavailable), file steps still ran
    assert calls == ["results", "calendars", "picks", "board", "track"]
