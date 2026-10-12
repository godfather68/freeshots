"""Programmatic Alembic: upgrade / current / downgrade against an engine (default: SHOTS_DB)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from shots.db.engine import ROOT, make_engine

MIGRATIONS = ROOT / "migrations"
logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)  # per-plugin setup lines are noise


def alembic_config(connection=None) -> Config:
    cfg = Config(str(MIGRATIONS / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


@contextmanager
def _engine(target):
    """Use a passed Engine as is; create (and dispose, closing the file) one for a path/URL."""
    if isinstance(target, Engine):
        yield target
        return
    engine = make_engine(target)
    try:
        yield engine
    finally:
        engine.dispose()


def upgrade(target: str | Path | Engine | None = None, revision: str = "head") -> str | None:
    """Apply migrations up to `revision`; returns the resulting revision. Idempotent."""
    with _engine(target) as engine:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), revision)
        return current(engine)


def downgrade(target: str | Path | Engine | None = None, revision: str = "base") -> str | None:
    with _engine(target) as engine:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), revision)
        return current(engine)


def current(target: str | Path | Engine | None = None) -> str | None:
    """Revision the DB is at (None = never migrated)."""
    with _engine(target) as engine, engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def is_current(target: str | Path | Engine | None = None) -> bool:
    return current(target) == head()
