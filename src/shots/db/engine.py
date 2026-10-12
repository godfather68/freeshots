"""Engine and session factory for the SQLite system of record.

Every connection gets WAL journaling, foreign keys ON and a busy timeout, so the web app (reader)
and the update/worker process (writer) can run at the same time without "database is locked".
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = ROOT / "data" / "shots.db"
BUSY_TIMEOUT_MS = 10_000


def db_path() -> Path:
    """DB file location; the SHOTS_DB environment variable overrides it (tests, alternative DBs)."""
    return Path(os.environ.get("SHOTS_DB") or DEFAULT_PATH)


def db_url(target: str | Path | None = None) -> str:
    """target: a SQLAlchemy URL ("sqlite:///..."), a file path, or None for db_path()."""
    if isinstance(target, str) and "://" in target:
        return target
    path = Path(target) if target is not None else db_path()
    return f"sqlite:///{path.resolve().as_posix()}"


def _on_connect(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    cur.execute("PRAGMA foreign_keys = ON")
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; a power cut may lose the last commit only
    cur.execute("PRAGMA cache_size = -65536")  # 64 MB: bulk upserts stay in memory instead of spilling to the WAL
    cur.close()


def make_engine(target: str | Path | Engine | None = None, create_dir: bool = True) -> Engine:
    """Engine with the connection pragmas applied. Passing an Engine returns it unchanged."""
    if isinstance(target, Engine):
        return target
    url = make_url(db_url(target))
    if create_dir and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"timeout": BUSY_TIMEOUT_MS / 1000})
    event.listen(engine, "connect", _on_connect)
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any error."""
    with session_factory(engine)() as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
