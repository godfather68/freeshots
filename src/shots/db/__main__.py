"""python -m shots.db <command>

    upgrade              create / migrate the database to the latest schema (idempotent)
    current              print the schema revision (and the head revision)
    import [--full]      load every cached file: results, fixtures, the current board, picks CSVs,
                         backfill (idempotent; --full re-checks all past seasons' results too)
    status               counts and freshness of what the app shows

The database is data/shots.db; set SHOTS_DB to use another file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from shots.db import load, migrate
from shots.db.engine import db_path, make_engine
from shots.db.repo import Repository


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.db", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("upgrade", help="migrate to the latest schema")
    sub.add_parser("current", help="show the schema revision")
    imp = sub.add_parser("import", help="load cached files into the database")
    imp.add_argument("--full", action="store_true", help="re-check every season's results, not only recent ones")
    sub.add_parser("status", help="what the app shows")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.command == "upgrade":
        print(f"{db_path()}: at revision {migrate.upgrade()}")
    elif args.command == "current":
        rev = migrate.current() if db_path().exists() else None  # never create the file just to look
        print(f"{db_path()}: revision {rev or '(none)'}, head {migrate.head()}")
    elif args.command == "import":
        print(json.dumps(load.sync_all(full=args.full), indent=2, default=str))
    elif args.command == "status":
        if not db_path().exists():
            sys.exit(f"{db_path()} does not exist; run `python -m shots.db upgrade` then `python -m shots.db import`")
        engine = make_engine()
        print(json.dumps(Repository(engine).data_status(), indent=2, default=str))
        engine.dispose()


if __name__ == "__main__":
    main()
