"""Worker cycles: run the update when due, skip when fresh, never overlap, record every cycle."""

import json
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from shots.db import jobs, migrate
from shots.db.engine import make_engine, session_scope
from shots.db.models import JobRun
from shots.worker import cycle
from shots.worker import __main__ as cli
from shots.worker.lock import WorkerLock
from tests.test_worker_due import with_board
from tests.test_worker_lock import HOLDER, kill_and_wait


@pytest.fixture
def db(migrated_db, monkeypatch):
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    e = make_engine(migrated_db)
    yield e
    e.dispose()


@pytest.fixture
def lock(tmp_path):
    return WorkerLock(tmp_path / "worker.lock")


class FakeUpdate:
    """Stands in for shots.__main__.update: records its own `update` job_run like the real one."""

    def __init__(self, rc=0, status="ok"):
        self.calls, self.rc, self.status = [], rc, status

    def __call__(self, days=30, **kw):
        self.calls.append(days)
        engine = make_engine()
        try:
            jid = jobs.start(engine, "update")
            jobs.finish(engine, jid, self.status, {"steps": {}})
        finally:
            engine.dispose()
        return self.rc


def runs(engine, job=None):
    q = select(JobRun).order_by(JobRun.id)
    if job:
        q = q.where(JobRun.job == job)
    with engine.connect() as c:
        return [dict(r) for r in c.execute(q).mappings()]


def test_first_cycle_runs_the_update_and_records_it(db, lock):
    upd = FakeUpdate()
    assert cycle.run_once(lock=lock, update=upd, days=14) == 0
    assert upd.calls == [14]
    [w] = runs(db, "worker")
    assert w["status"] == "ok" and w["counts"]["action"] == "update" and w["finished_at"] is not None
    assert w["counts"]["update_job"] == runs(db, "update")[0]["id"]
    assert any(r.startswith("never") for r in w["counts"]["reasons"])
    assert not lock.held and lock.inspect().state == "free"


def test_second_cycle_is_skipped_when_fresh_unless_forced(db, lock):
    with_board(db)
    upd = FakeUpdate()
    cycle.run_once(lock=lock, update=upd)
    assert cycle.run_once(lock=lock, update=upd) == 0
    assert len(upd.calls) == 1  # idempotent: nothing due, nothing done
    w = runs(db, "worker")
    assert [r["status"] for r in w] == ["ok", "skipped"] and w[1]["counts"]["action"] == "skipped"
    assert cycle.run_once(lock=lock, update=upd, force=True) == 0
    assert len(upd.calls) == 2


def test_stale_last_run_triggers_the_update(db, lock):
    with_board(db)
    upd = FakeUpdate()
    cycle.run_once(lock=lock, update=upd)
    with session_scope(db) as s:
        s.execute(update(JobRun).where(JobRun.job == "update").values(started_at=jobs.utcnow() - timedelta(hours=3)))
    cycle.run_once(lock=lock, update=upd)
    assert len(upd.calls) == 2
    assert cycle.run_once(lock=lock, update=upd, min_interval=timedelta(hours=10)) == 0
    assert len(upd.calls) == 2


def test_failed_update_is_recorded_and_exit_code_is_1(db, lock):
    upd = FakeUpdate(rc=1, status="partial")
    assert cycle.run_once(lock=lock, update=upd) == 1
    [w] = runs(db, "worker")
    assert w["status"] == "partial" and w["counts"]["exit_code"] == 1
    assert cycle.run_once(lock=lock, update=FakeUpdate()) == 0  # a partial run is not fresh: retried


def test_crashing_update_is_recorded(db, lock):
    def boom(days=30):
        raise RuntimeError("disk full")
    assert cycle.run_once(lock=lock, update=boom) == 1
    [w] = runs(db, "worker")
    assert w["status"] == "failed" and "disk full" in w["error"]
    assert not lock.held


def test_overlapping_cycle_exits_without_running(db, lock):
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock.path)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    state, pid = p.stdout.readline().split()
    try:
        assert state == "locked"
        upd = FakeUpdate()
        assert cycle.run_once(lock=lock, update=upd) == 0
        assert upd.calls == []
        [w] = runs(db, "worker")
        assert w["status"] == "skipped" and w["counts"] == {"action": "locked", "holder_pid": int(pid)}
    finally:
        p.stdin.write("\n")
        p.stdin.flush()
        p.wait(timeout=30)
    assert cycle.run_once(lock=lock, update=FakeUpdate()) == 0
    assert runs(db, "worker")[-1]["counts"]["action"] == "update"


def test_manual_update_in_progress_is_not_overlapped(db, lock):
    """`python -m shots update` run by hand does not take the worker lock; its running job row does count."""
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock.path.with_name("other.lock"))],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    _, pid = p.stdout.readline().split()
    try:
        jid = jobs.start(db, "update")
        with session_scope(db) as s:
            s.execute(update(JobRun).where(JobRun.id == jid).values(pid=int(pid)))
        upd = FakeUpdate()
        assert cycle.run_once(lock=lock, update=upd) == 0 and upd.calls == []
        assert runs(db, "worker")[-1]["counts"] == {"action": "busy", "running_job": jid,
                                                     "purge": {"sessions": 0, "attempts": 0}}
    finally:
        kill_and_wait(int(pid))
        p.wait(timeout=30)
    # the manual run died without finishing: the next cycle marks it abandoned and runs
    upd = FakeUpdate()
    assert cycle.run_once(lock=lock, update=upd) == 0 and len(upd.calls) == 1
    first = runs(db, "update")[0]
    assert first["status"] == "failed" and "abandoned" in first["error"]


def test_status_reports_lock_runs_and_next_work(db, lock, capsys, monkeypatch):
    with_board(db)
    cycle.run_once(lock=lock, update=FakeUpdate())
    st = cycle.status(lock=lock)
    assert st["lock"] == "free" and st["db_state"] == f"revision {migrate.head()}"
    assert [r["job"] for r in st["recent_runs"]] == ["update", "worker"]  # newest first: the update started inside the cycle
    assert st["now"].startswith("skip") and "fresh_until_utc" in st["next"]
    monkeypatch.setenv("SHOTS_WORKER_LOCK", str(lock.path))
    with pytest.raises(SystemExit) as exit_:
        cli.main(["run-once", "--min-interval", "5"])
    assert exit_.value.code == 0
    cli.main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert out["lock"] == "free" and out["recent_runs"][0]["action"] == "skipped"


def test_status_without_database(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOTS_DB", str(tmp_path / "nope" / "shots.db"))
    st = cycle.status(lock=WorkerLock(tmp_path / "w.lock"))
    assert st["db_state"].startswith("missing") and not (tmp_path / "nope" / "shots.db").exists()


def test_default_lock_lives_next_to_the_db(monkeypatch, tmp_path):
    monkeypatch.delenv("SHOTS_WORKER_LOCK", raising=False)
    monkeypatch.setenv("SHOTS_DB", str(tmp_path / "data" / "shots.db"))
    assert cycle.lock_path() == tmp_path / "data" / "worker.lock"
