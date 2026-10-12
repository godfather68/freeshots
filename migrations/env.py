"""Alembic environment for data/shots.db.

Run through `python -m shots.db upgrade` (preferred) or `alembic -c migrations/alembic.ini upgrade head`.
The target DB is SHOTS_DB (default data/shots.db) unless a connection is passed in
config.attributes["connection"] (used by shots.db.migrate and the tests).
"""

from __future__ import annotations

from alembic import context

from shots.db.engine import make_engine
from shots.db.models import Base

config = context.config
target_metadata = Base.metadata


def _configure(connection) -> None:
    # render_as_batch: SQLite cannot ALTER most things in place; batch mode copies the table.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                      compare_type=True)


def run_migrations_offline() -> None:
    from shots.db.engine import db_url

    context.configure(url=config.get_main_option("sqlalchemy.url") or db_url(), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(config.get_main_option("sqlalchemy.url") or None)
    with engine.connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
