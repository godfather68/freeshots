"""Second, independent trigger for the Stage 1 update workflow (.github/workflows/watchdog.yml).

GitHub's `schedule` trigger is best effort: slots are delayed or dropped under load. The watchdog runs
on its own schedule (other minutes of the hour), looks at the recent runs of update.yml and starts one
through `workflow_dispatch` when the scheduler has evidently skipped it.

    python3 src/shots/worker/watchdog.py --runs RUNS.json [--site-meta META.json]

RUNS.json  output of `gh run list --workflow update.yml --limit 30 --json status,conclusion,createdAt,event`
META.json  the site's data/meta.json as visitors get it (optional; absent or unreadable = age unknown)

Prints `dispatch=true|false`, `force=true|false` and `reason=...` (GITHUB_OUTPUT format).

Rules (first match wins):
  1. an update run is queued or in progress (and younger than 3 h)  -> nothing
  2. no update run was created in the last 50 min                  -> dispatch with force=false: the run
     then behaves like a scheduled one (the worker decides whether anything is due)
  3. the site's built_at is older than 12 h and no manual / watchdog run was started in the last 6 h
                                                                     -> dispatch with force=true
  4. otherwise                                                       -> nothing

Standard library only and no import from the package: the watchdog job runs this file with the
runner's own Python, without installing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_RUN_AGE = timedelta(minutes=50)   # update.yml is scheduled hourly; the watchdog runs ~30 min after each slot
MAX_SITE_AGE = timedelta(hours=12)    # a healthy pipeline redeploys within ~6 h (shots.worker.ci.HEARTBEAT = 5 h)
FORCED_COOLDOWN = timedelta(hours=6)  # at most four forced runs a day, however stale the site looks
STUCK_AFTER = timedelta(hours=3)      # an "active" run older than this is not trusted (job timeouts sum to < 2 h)
ACTIVE = {"queued", "in_progress", "pending", "waiting", "requested"}


def parse_time(value) -> datetime | None:
    """ISO 8601 text -> naive UTC; None if unusable."""
    try:
        ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def decide(runs, site_built_at, now: datetime, max_run_age: timedelta = MAX_RUN_AGE,
           max_site_age: timedelta = MAX_SITE_AGE, forced_cooldown: timedelta = FORCED_COOLDOWN) -> dict:
    """{"dispatch": bool, "force": bool, "reason": str} from the recent runs of the update workflow
    (dicts with status, conclusion, createdAt, event) and the site's built_at (text or None)."""
    seen = []
    for r in runs or []:
        created = parse_time(r.get("createdAt")) if isinstance(r, dict) else None
        if created is not None:
            seen.append((created, str(r.get("status") or "").lower(), str(r.get("event") or "")))
    seen.sort(reverse=True)
    active = [c for c, status, _ in seen if status in ACTIVE and now - c < STUCK_AFTER]
    if active:
        return {"dispatch": False, "force": False,
                "reason": f"an update run is active (created {_ago(now - active[0])} ago)"}
    built = parse_time(site_built_at) if site_built_at else None
    site_stale = built is not None and now - built > max_site_age
    newest = seen[0][0] if seen else None
    if newest is None or now - newest > max_run_age:
        last = "no run found" if newest is None else f"last run created {_ago(now - newest)} ago"
        return {"dispatch": True, "force": False,
                "reason": f"the schedule missed a slot: {last} (limit {_ago(max_run_age)})"}
    if site_stale:
        manual = [c for c, _, event in seen if event == "workflow_dispatch" and now - c < forced_cooldown]
        if not manual:
            return {"dispatch": True, "force": True,
                    "reason": f"runs happen but the site was built {_ago(now - built)} ago (limit {_ago(max_site_age)})"}
        return {"dispatch": False, "force": False,
                "reason": f"site built {_ago(now - built)} ago, but a manual / watchdog run was started "
                          f"{_ago(now - manual[0])} ago: waiting"}
    site = "site age unknown" if built is None else f"site built {_ago(now - built)} ago"
    return {"dispatch": False, "force": False, "reason": f"last run created {_ago(now - newest)} ago; {site}"}


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def _read_json(path: Path | None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Decide whether to start the update workflow.")
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--site-meta", type=Path, default=None)
    args = ap.parse_args(argv)
    runs = _read_json(args.runs)
    if not isinstance(runs, list):  # the run list could not be read: do not guess
        print("cannot read the list of runs; not dispatching", file=sys.stderr)
        sys.exit(1)
    meta = _read_json(args.site_meta)
    built = meta.get("built_at") if isinstance(meta, dict) else None
    out = decide(runs, built, datetime.now(timezone.utc).replace(tzinfo=None))
    flag = {True: "true", False: "false"}
    sys.stdout.write(f"dispatch={flag[out['dispatch']]}\nforce={flag[out['force']]}\nreason={' '.join(out['reason'].split())}\n")


if __name__ == "__main__":
    main()
