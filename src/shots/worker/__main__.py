"""python -m shots.worker <command>

    run-once [--force] [--min-interval H] [--days N]
        one cycle: take the overlap lock, decide whether anything is due, run the update
        (python -m shots update) if so, record the cycle in job_runs, exit. Exit code 0 when the
        cycle was skipped or the update succeeded, 1 when the update reported failures.
    status [--min-interval H]
        lock state, recent worker / update runs, what a cycle would do now, next due moments.

Settings: SHOTS_DB (database), SHOTS_WORKER_LOCK (lock file, default data/worker.lock),
SHOTS_WORKER_MIN_INTERVAL_HOURS (default 2).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import timedelta

from shots.worker import cycle, due


def _hours(default: timedelta) -> float:
    env = os.environ.get("SHOTS_WORKER_MIN_INTERVAL_HOURS")
    return float(env) if env else default.total_seconds() / 3600


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.worker", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-once", help="run one update cycle (skipped when fresh) and exit")
    run.add_argument("--force", action="store_true", help="run the update even if nothing is due")
    st = sub.add_parser("status", help="lock, recent runs and what is due")
    for p in (run, st):
        p.add_argument("--min-interval", type=float, default=_hours(due.DEFAULT_MIN_INTERVAL), metavar="HOURS",
                       help="skip when the last successful update is younger than this and nothing is due (default 2)")
    run.add_argument("--days", type=int, default=30, help="board window in days (default 30)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    interval = timedelta(hours=args.min_interval)
    if args.command == "run-once":
        sys.exit(cycle.run_once(force=args.force, min_interval=interval, days=args.days))
    print(json.dumps(cycle.status(min_interval=interval), indent=2, default=str))


if __name__ == "__main__":
    main()
