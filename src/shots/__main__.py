"""Project-level commands.

Usage:
    python -m shots update            # results -> calendars -> picks -> UI board -> track record -> database
    python -m shots update --days 14  # board window (default 30 days)
    python -m shots update --export-dir site/data   # then write the static JSON export (docs/static-api.md)

Each step runs even if an earlier one failed (e.g. no network: the rest uses cached data);
the exit code is non-zero when any step failed. The last steps load everything into the database
the web app reads (data/shots.db, SHOTS_DB overrides), and each run is recorded in its job_runs table.
With --export-dir (or SHOTS_EXPORT_DIR; off by default) the static JSON files of the Stage 1 site are
written last, after the run is recorded, so meta.json reports this run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback

log = logging.getLogger("shots.update")


def _calendars_changed() -> bool:
    from shots.ingest import calendar_ics as cal

    sources = list(cal.CAL_DIR.glob("*.ics"))
    if not sources:
        return False
    if not cal.OUT_PATH.exists():
        return True
    return max(p.stat().st_mtime for p in sources) > cal.OUT_PATH.stat().st_mtime


def _open_db():
    """(engine, job_run_id) with the schema migrated, or (None, None) if the DB is unusable: the
    file-based steps must still run, and the db steps will then report the failure themselves."""
    from shots.db import jobs, migrate
    from shots.db.engine import make_engine

    try:
        engine = make_engine()
        migrate.upgrade(engine)
        return engine, jobs.start(engine, "update")
    except Exception as exc:
        log.error("[db] cannot open the database, the update will not be recorded: %s", exc)
        return None, None


def _export(engine, export_dir) -> dict:
    """Static JSON export (python -m shots.export) on the already open database."""
    from shots.db.repo import Repository
    from shots.export import export

    if engine is None:
        raise RuntimeError("database unavailable")
    return export(export_dir, Repository(engine))


def update(days: int = 30, shots: int = 24, min_prob: float = 0.70, export_dir: str | None = None) -> int:
    from shots.db import jobs
    from shots.db import load as dbload
    from shots.ingest import calendar_ics as cal
    from shots.ingest import football_data as fd
    from shots.predict import board, scan, track

    engine, job_id = _open_db()
    counts: dict = {}

    def db_step(name, fn):
        def run():
            if engine is None:
                raise RuntimeError("database unavailable")
            counts[name] = fn(engine)
            log.info("  %s", counts[name])
        return run

    steps = [
        ("results", "refresh current-season results and odds", lambda: fd.build(refresh_current=True)),
        ("calendars", "re-read season calendars if they changed",
         lambda: cal.build() if _calendars_changed() else log.info("  calendars unchanged")),
        ("picks", f"save this week's picks (P(total >= {shots}) >= {min_prob:.0%}) for the track record",
         lambda: scan.scan(shots, min_prob, refresh=False)),
        ("board", f"rebuild the UI board ({days} days)", lambda: board.build(days=days, refresh=False)),
        ("track", "settle picks and rebuild the dashboard", lambda: track.build_dashboard(min_prob)),
        ("db", "load results and fixtures into the database", db_step("db", dbload.sync_results)),
        ("db-board", "record the board as a prediction run", db_step("db-board", dbload.import_board_file)),
        ("db-picks", "record picks scans and the backfill; outcomes", db_step("db-picks", dbload.sync_predictions)),
    ]
    failed, errors = [], []
    for name, what, run in steps:
        t0 = time.time()
        log.info("[%s] %s", name, what)
        try:
            run()
            log.info("[%s] done in %.0fs", name, time.time() - t0)
        except (Exception, SystemExit) as exc:  # keep going: later steps can use cached data
            failed.append(name)
            errors.append(f"{name}: {exc}")
            log.error("[%s] FAILED: %s", name, exc)
            log.debug(traceback.format_exc())
    def record() -> None:
        if job_id is None:
            return
        try:
            names = [n for n, _, _ in steps] + (["export"] if "export" in failed else [])
            status = "ok" if not failed else ("failed" if len(failed) == len(names) else "partial")
            jobs.finish(engine, job_id, status, {"steps": {n: n not in failed for n in names}, **counts},
                        "; ".join(errors) or None)
        except Exception as exc:
            log.error("[db] could not record the update run: %s", exc)

    record()
    export_dir = export_dir or os.environ.get("SHOTS_EXPORT_DIR") or None
    if export_dir:  # after the run is recorded: meta.json's last_job is this run
        log.info("[export] write the static JSON export to %s", export_dir)
        try:
            log.info("  %s", _export(engine, export_dir))
        except Exception as exc:
            failed.append("export")
            errors.append(f"export: {exc}")
            log.error("[export] FAILED: %s", exc)
            record()  # the run becomes partial / failed with the export error
    if engine is not None:
        engine.dispose()
    if failed:
        log.error("update finished with failures: %s", ", ".join(failed))
        return 1
    log.info("update finished")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    up = sub.add_parser("update", help="refresh data, picks, UI board, track record and the database")
    up.add_argument("--days", type=int, default=30, help="UI board window in days (default 30)")
    up.add_argument("--shots", type=int, default=24, help="threshold for saved picks (default 24)")
    up.add_argument("--min-prob", type=float, default=0.70, help="probability for saved picks (default 0.70)")
    up.add_argument("--export-dir", default=None, metavar="DIR",
                    help="also write the static JSON export there (default: SHOTS_EXPORT_DIR, else no export)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    sys.exit(update(args.days, args.shots, args.min_prob, args.export_dir))


if __name__ == "__main__":
    main()
