"""SQLite system of record (SQLAlchemy 2.0 + Alembic). See docs/platform.md.

    python -m shots.db upgrade        # create / migrate data/shots.db (SHOTS_DB overrides the path)
    python -m shots.db current        # show the schema revision
    python -m shots.db import         # one-off / catch-up import of parquet + picks CSVs + board
    python -m shots.db status         # what the app would show (counts, latest runs)
"""

from shots.db.engine import db_path, make_engine, session_scope

__all__ = ["db_path", "make_engine", "session_scope"]
