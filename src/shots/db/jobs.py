"""job_runs: one row per update / worker cycle (start, end, status, counts, error).

status: running | ok | failed | partial | skipped (a worker cycle that found nothing to do or another
cycle running; its counts say why)."""

from __future__ import annotations

import os
import socket
from datetime import datetime

from sqlalchemy import Engine, select

from shots.db.engine import session_scope
from shots.db.models import JobRun, utcnow

STATUSES = ("running", "ok", "failed", "partial", "skipped")


def start(engine: Engine, job: str) -> int:
    with session_scope(engine) as s:
        run = JobRun(job=job, started_at=utcnow(), status="running", counts={}, host=socket.gethostname()[:80],
                     pid=os.getpid())
        s.add(run)
        s.flush()
        return run.id


def finish(engine: Engine, run_id: int, status: str, counts: dict | None = None, error: str | None = None) -> None:
    """Close a job run. counts must be JSON-serialisable; error is a short message (never secrets)."""
    if status not in STATUSES[1:]:
        raise ValueError(f"bad job status {status!r}")
    with session_scope(engine) as s:
        run = s.get(JobRun, run_id)
        run.finished_at = utcnow()
        run.status = status
        run.counts = _jsonable(counts or {})
        run.error = (error or None) and error[:4000]


def last(engine: Engine, job: str, status: str | None = None) -> dict | None:
    """Most recent run of `job` (optionally with this status) as a dict, or None."""
    q = select(JobRun).where(JobRun.job == job)
    if status is not None:
        q = q.where(JobRun.status == status)
    with engine.connect() as c:
        r = c.execute(q.order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


def recent(engine: Engine, limit: int = 10, jobs: tuple[str, ...] | None = None) -> list[dict]:
    q = select(JobRun)
    if jobs:
        q = q.where(JobRun.job.in_(jobs))
    with engine.connect() as c:
        return [dict(r) for r in c.execute(q.order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(limit)).mappings()]


def running(engine: Engine) -> list[dict]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(JobRun).where(JobRun.status == "running")).mappings()]


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)
