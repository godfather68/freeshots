"""python -m shots.worker <command>

    run-once [--force] [--min-interval H] [--min-gap MIN] [--days N]
        one cycle: take the overlap lock, decide whether anything is due, run the update
        (python -m shots update) if so, record the cycle in job_runs, exit. Exit code 0 when the
        cycle was skipped or the update succeeded, 1 when the update reported failures.
    status [--min-interval H] [--min-gap MIN]
        lock state, recent worker / update runs, what a cycle would do now, next due moments.

Settings: SHOTS_DB (database), SHOTS_WORKER_LOCK (lock file, default data/worker.lock),
SHOTS_WORKER_MIN_INTERVAL_HOURS (default 2), SHOTS_WORKER_MIN_GAP_MINUTES (floor between two update
attempts, default 0 = none), SHOTS_ODDS_TZ (zone of the Tue / Fri 09:00 odds rule, default: this machine's).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta

from shots.worker import cycle, due


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.worker", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-once", help="run one update cycle (skipped when fresh) and exit")
    run.add_argument("--force", action="store_true", help="run the update even if nothing is due")
    st = sub.add_parser("status", help="lock, recent runs and what is due")
    for p in (run, st):
        p.add_argument("--min-interval", type=float, default=due.env_min_interval().total_seconds() / 3600, metavar="HOURS",
                       help="skip when the last successful update is younger than this and nothing is due (default 2)")
        p.add_argument("--min-gap", type=float, default=due.env_min_gap().total_seconds() / 60, metavar="MINUTES",
                       help="never start an update less than this after the previous attempt, unless forced (default 0)")
    run.add_argument("--days", type=int, default=30, help="board window in days (default 30)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    interval = timedelta(hours=args.min_interval)
    gap = timedelta(minutes=args.min_gap)
    if args.command == "run-once":
        sys.exit(cycle.run_once(force=args.force, min_interval=interval, days=args.days, min_gap=gap))
    print(json.dumps(cycle.status(min_interval=interval, min_gap=gap), indent=2, default=str))


if __name__ == "__main__":
    main()
