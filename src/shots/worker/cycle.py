"""One worker cycle: lock -> decide -> (maybe) run the update -> record the cycle in job_runs.

The update itself is `shots.__main__.update` (results -> calendars -> picks -> board -> track ->
database); this module only adds the overlap lock, the skip-if-fresh decision and the bookkeeping.
Each cycle writes one `worker` job_run (status ok / partial / failed / skipped, counts.action =
update | skipped | locked | busy, counts.reasons, counts.purge = auth housekeeping, counts.calendars =
fingerprint of calendars/*.ics the update started from); the update writes its own `update` job_run.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import timedelta
from pathlib import Path

from shots.db import jobs, migrate
from shots.db.engine import db_path, make_engine
from shots.worker import due
from shots.worker.lock import DEFAULT_STALE_AFTER, WorkerLock, pid_alive

log = logging.getLogger("shots.worker")

BUSY_MAX_AGE = DEFAULT_STALE_AFTER  # a 'running' job row older than this is not trusted as a live run


def lock_path() -> Path:
    """data/worker.lock (next to the DB file; SHOTS_WORKER_LOCK overrides)."""
    return Path(os.environ.get("SHOTS_WORKER_LOCK") or db_path().parent / "worker.lock")


def _open_engine(upgrade: bool = True):
    """Engine on the DB, migrated to head (upgrade=False: only if already at head), or None."""
    try:
        engine = make_engine()
        if upgrade:
            migrate.upgrade(engine)
        elif not migrate.is_current(engine):
            engine.dispose()
            return None
        return engine
    except Exception as exc:  # the update still runs its file steps and reports the DB failure itself
        log.error("database unavailable: %s", exc)
        return None


def reap_abandoned(engine, own_ids: set[int] = frozenset()) -> tuple[list[dict], list[dict]]:
    """'running' job rows: those of a process that is gone (this host) become 'failed'; returns
    (abandoned, live) where live = runs of a live process started less than BUSY_MAX_AGE ago
    (e.g. a manual `python -m shots update`, which does not take the worker lock)."""
    host = socket.gethostname()[:80]
    abandoned, live = [], []
    now = jobs.utcnow()
    for r in jobs.running(engine):
        if r["id"] in own_ids or r["host"] != host:
            continue
        if r["pid"] != os.getpid() and pid_alive(r["pid"]) and now - r["started_at"] < BUSY_MAX_AGE:
            live.append(r)
        else:  # process gone (or our own pid reused, or far too old to be a live run)
            jobs.finish(engine, r["id"], "failed", r["counts"] or {},
                        "abandoned: the process ended without recording a result")
            abandoned.append(r)
    return abandoned, live


def purge_auth(engine) -> dict:
    """Auth housekeeping (expired sessions, old login attempts): {"sessions": n, "attempts": n} or
    {"error": "..."}. Never fails the cycle."""
    from shots.auth.service import AuthService

    try:
        return AuthService(engine).purge()
    except Exception as exc:
        log.error("auth purge failed: %s", type(exc).__name__)
        return {"error": type(exc).__name__}


def run_once(force: bool = False, min_interval: timedelta = due.DEFAULT_MIN_INTERVAL, days: int = 30,
             lock: WorkerLock | None = None, update=None, min_gap: timedelta = due.DEFAULT_MIN_GAP) -> int:
    """One cycle. Returns the exit code: 0 when skipped or the update succeeded, 1 if it failed."""
    if update is None:
        from shots.__main__ import update
    lock = lock or WorkerLock(lock_path())
    if not lock.acquire():
        state = lock.inspect()
        log.info("another worker cycle is running (%s); nothing to do", state.describe())
        engine = _open_engine(upgrade=False)  # never migrate under a running cycle
        if engine is not None:
            try:
                jid = jobs.start(engine, "worker")
                jobs.finish(engine, jid, "skipped", {"action": "locked", "holder_pid": state.pid})
            finally:
                engine.dispose()
        return 0
    try:
        if lock.taken_over:
            log.warning("took over a stale worker lock left by pid %s (started %s UTC)",
                        lock.taken_over.get("pid"), lock.taken_over.get("started_at"))
        engine = _open_engine()
        job_id = None
        purged: dict = {}
        if engine is not None:
            job_id = jobs.start(engine, "worker")
            purged = purge_auth(engine)
            log.info("auth purge: %s", purged)
            abandoned, live = reap_abandoned(engine, {job_id})
            for r in abandoned:
                log.warning("marked %s run #%d (pid %s, started %s) as failed: abandoned", r["job"], r["id"],
                            r["pid"], r["started_at"])
            if live:
                r = live[0]
                log.info("a %s run is in progress (pid %s, started %s UTC); skipping", r["job"], r["pid"], r["started_at"])
                jobs.finish(engine, job_id, "skipped", {"action": "busy", "running_job": r["id"], "purge": purged})
                engine.dispose()
                return 0
            decision = due.decide(engine, min_interval=min_interval, force=force, min_gap=min_gap)
        else:
            decision = due.Decision(run=True, reasons=[due.Reason("db", "database unavailable")])
        reasons = [str(r) for r in decision.reasons]
        log.info("decision: %s", decision.summary())
        if not decision.run:
            if engine is not None:
                jobs.finish(engine, job_id, "skipped", {"action": "skipped", "reasons": reasons, "purge": purged})
                engine.dispose()
            return 0
        if engine is not None:
            engine.dispose()  # the update opens its own connections
        try:  # what the update starts from; the next cycle compares (due: `calendars`)
            calendars = due.calendars_fingerprint()
        except OSError:
            calendars = None
        rc = 1
        error = None
        try:
            rc = update(days=days)
        except BaseException as exc:  # record, then re-raise interrupts / exits
            error = f"update crashed: {exc}"
            log.exception("update crashed")
            if not isinstance(exc, Exception):
                raise
        finally:
            engine = _open_engine() if job_id is not None else None
            if engine is not None:
                try:
                    upd = jobs.last(engine, "update")
                    if error or upd is None or (rc != 0 and upd["status"] == "failed"):
                        st = "failed"
                    else:
                        st = "ok" if rc == 0 else "partial"
                    jobs.finish(engine, job_id, st, {"action": "update", "reasons": reasons, "exit_code": rc,
                                                     "update_job": upd["id"] if upd else None,
                                                     "purge": purged, "calendars": calendars}, error)
                finally:
                    engine.dispose()
        log.info("cycle finished (update exit code %s)", rc)
        return 0 if rc == 0 else 1
    finally:
        lock.release()


def status(min_interval: timedelta = due.DEFAULT_MIN_INTERVAL, limit: int = 8, lock: WorkerLock | None = None,
           min_gap: timedelta = due.DEFAULT_MIN_GAP) -> dict:
    """Lock state, recent cycles and updates, what a cycle would do now, and the next due moments."""
    lock = lock or WorkerLock(lock_path())
    out: dict = {"lock": lock.inspect().describe(), "db": str(db_path())}
    if not db_path().exists():
        out["db_state"] = "missing (the first cycle creates it)"
        return out
    engine = make_engine()
    try:
        rev = migrate.current(engine)
        out["db_state"] = f"revision {rev}" + ("" if rev == migrate.head() else f" (head {migrate.head()}; next cycle migrates)")
        if rev is None:
            return out
        out["recent_runs"] = [
            {k: r[k] for k in ("id", "job", "status", "started_at", "finished_at")} | {
                "action": (r["counts"] or {}).get("action"), "error": (r["error"] or "")[:200] or None}
            for r in jobs.recent(engine, limit, ("worker", "update"))]
        try:
            d = due.decide(engine, min_interval=min_interval, min_gap=min_gap)
            out["now"] = d.summary()
            now = d.now
            nxt = {"next_match_end_utc": due.next_match_end(engine, now),
                   "next_odds_publication_utc": due.next_odds_publication(now)}
            if d.last_ok:
                nxt["fresh_until_utc"] = d.last_ok["started_at"] + min_interval
            out["next"] = nxt
        except Exception as exc:  # schema older than the worker's queries etc.
            out["now"] = f"cannot evaluate: {exc}"
    finally:
        engine.dispose()
    return out
