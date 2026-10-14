"""Helpers for the scheduled GitHub Actions run (.github/workflows/update.yml). Plain, tested Python
instead of shell in the workflow. Nothing here is Linux- or GitHub-specific: every command also runs
locally.

    python -m shots.worker.ci keys                      cache keys / paths as `name=value` lines (GITHUB_OUTPUT format)
    python -m shots.worker.ci plan --event E [--force-input B] [--changed-files F] [--deployment F
                                   --deployment-status F] [--head SHA]
                                                        what this run does (read-only): update / force / site /
                                                        export, as `name=value` lines
    python -m shots.worker.ci deployment-id F           id of the newest deployment in a saved API answer
    python -m shots.worker.ci publish --site S --changed B [--deployed-at T] [--published-meta F]
                                                        build and deploy the site in this run? `deploy=true|false`
    python -m shots.worker.ci db-check [--write-baseline F | --baseline F]
                                                        checkpoint the WAL into data/shots.db, check integrity,
                                                        print counts; with --baseline fail if history shrank
    python -m shots.worker.ci prune-fixtures [--keep 3] delete all but the newest fixtures_*.csv downloads
    python -m shots.worker.ci describe META.json [--markdown]
                                                        one line about an export: commit message / run summary

State between runs (see docs/platform.md, "Stage 1"): the runner is ephemeral, so the database, the
current season's raw downloads and the derived files that must not be recomputed differently are
cached; past seasons' raw files (immutable) are cached once per season; models per dependency lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shots.db.engine import db_path
from shots.ingest import football_data as fd

FIRST_SEASON = 2010  # shots.ingest.football_data.fetch default
COUNT_TABLES = ("prediction_runs", "predictions", "job_runs")

# A push to main starts the workflow only when one of these paths changed (update.yml `on.push.paths`
# must equal push_paths(); a test keeps them equal). A change under PIPELINE_PATHS can change the
# predictions, so the update cycle is forced; a change under SITE_PATHS only needs a new build.
PIPELINE_PATHS = ("src/", "migrations/", "calendars/", "results_manual/", "requirements.lock", "pyproject.toml",
                  ".github/workflows/update.yml")
SITE_PATHS = ("frontend/", ".github/data-branch-README.md")
SCHEDULED, ALWAYS, IF_CHANGED = "schedule", "always", "if-changed"
# Unchanged data are still redeployed this long after the last deployment. Full updates are about 3 h apart,
# so 5 h means every second one: built_at on the site is at most about 6 h old while the pipeline is healthy.
HEARTBEAT = timedelta(hours=5)


def _rel(path: Path) -> str:
    """Path relative to the repository root, with forward slashes (what actions/cache expects)."""
    try:
        return Path(path).resolve().relative_to(fd.ROOT.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def cache_keys(today=None, lock_file: Path | None = None) -> dict:
    """season: football-data code of the current season ("2627"); lock: Python minor version + short
    hash of requirements.lock (the models cache key);
    past_paths: raw directories of finished seasons (never change once the season is over);
    current_paths: the running season's raw directory (re-fetched with conditional requests);
    state_paths: the live record and the derived files that carry over from one run to the next.
    state_paths never depend on the date: actions/cache only restores a cache saved with the very
    same path list, so a list that changed at the season rollover would orphan the database."""
    current = fd.current_season_start(today)
    lock_file = lock_file or fd.ROOT / "requirements.lock"
    # Saved models are pickles: valid only for the exact packages and Python minor version that wrote them.
    py = f"py{sys.version_info[0]}{sys.version_info[1]}"
    lock = f"{py}-" + (hashlib.sha256(lock_file.read_bytes()).hexdigest()[:16] if lock_file.exists() else "nolock")
    processed = fd.OUT_PATH.parent
    return {
        "season": fd.season_code(current),
        "lock": lock,
        "past_paths": [_rel(fd.RAW_DIR / fd.season_code(y)) for y in range(FIRST_SEASON, current)],
        "current_paths": [_rel(fd.RAW_DIR / fd.season_code(current))],
        "state_paths": [_rel(db_path()), _rel(fd.RAW_DIR / "fixtures"), _rel(fd.OUT_PATH),
                        _rel(processed / "track_backfill.parquet")],
    }


def format_outputs(values: dict) -> str:
    """GITHUB_OUTPUT text: `name=value`, lists as a multi-line value (name<<EOF ... EOF)."""
    lines = []
    for name, value in values.items():
        if isinstance(value, (list, tuple)):
            lines += [f"{name}<<__SHOTS_EOF__", *value, "__SHOTS_EOF__"]
        else:
            lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def db_check(path: Path | None = None) -> dict:
    """Fold the WAL into the main file (so the single .db file is the whole database), run SQLite's
    integrity check and count the append-only tables. Raises RuntimeError when the file is missing,
    not a database, or fails the check."""
    path = Path(path or db_path())
    if not path.exists():
        raise RuntimeError(f"{path} does not exist")
    try:
        con = sqlite3.connect(path, timeout=60)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            check = con.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"{path}: integrity check failed: {check}")
            fk = con.execute("PRAGMA foreign_key_check").fetchmany(5)
            if fk:
                raise RuntimeError(f"{path}: foreign key violations: {fk}")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] if t in tables else 0
                      for t in COUNT_TABLES}
            since = (con.execute("SELECT min(created_at) FROM predictions WHERE pre_match = 1").fetchone()[0]
                     if "predictions" in tables else None)
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"{path}: not a usable SQLite database: {exc}") from exc
    # After the TRUNCATE checkpoint the -wal file is empty: the .db file alone is the whole database
    # (only it is cached). The side files are left alone: another process may have the database open.
    return {"path": str(path), "size_bytes": path.stat().st_size, "counts": counts, "record_since": since}


def shrunk(before: dict, after: dict) -> list[str]:
    """Why `after` must not replace `before` as the saved state: an append-only table lost rows, or
    the live record's start moved (the database is not a continuation of the restored one)."""
    problems = [f"{t}: {before['counts'].get(t, 0)} -> {after['counts'].get(t, 0)}" for t in ("prediction_runs", "predictions")
                if after["counts"].get(t, 0) < before["counts"].get(t, 0)]
    if before.get("record_since") and after.get("record_since") != before.get("record_since"):
        problems.append(f"record_since: {before.get('record_since')} -> {after.get('record_since')}")
    return problems


def prune_fixtures(keep: int = 3, directory: Path | None = None) -> list[str]:
    """Daily fixtures_YYYY-MM-DD.csv downloads accumulate; only the newest is read. Keep `keep`."""
    directory = Path(directory or fd.RAW_DIR / "fixtures")
    files = sorted(directory.glob("fixtures_*.csv")) if directory.exists() else []
    gone = files[:-keep] if keep > 0 else files
    for f in gone:
        f.unlink()
    return [f.name for f in gone]


def describe(meta: dict, markdown: bool = False) -> str:
    """One line about an export (its meta.json): the data-branch commit message, or a summary bullet."""
    job = (meta.get("last_job") or {}).get("status") or "none"
    if markdown:
        return (f"- built {meta.get('built_at')}; results up to {meta.get('results_up_to')}; "
                f"{meta['counts']['fixtures']} fixtures; last update `{job}`; "
                f"record since {(meta.get('record') or {}).get('since') or 'none'}")
    return f"data: {meta.get('built_at')} ({meta['counts']['fixtures']} fixtures, results up to {meta.get('results_up_to')})"


def push_paths() -> list[str]:
    """The `on.push.paths` filter of update.yml."""
    return [p + "**" if p.endswith("/") else p for p in PIPELINE_PATHS + SITE_PATHS]


def touches_pipeline(files) -> bool:
    return any(f == p or (p.endswith("/") and f.startswith(p)) for f in files for p in PIPELINE_PATHS)


def _utc(value) -> datetime | None:
    """ISO 8601 text (with Z or an offset, or naive = UTC) -> naive UTC datetime; None if unusable."""
    try:
        ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_json(path: Path | None):
    """Parsed file, or None when it is absent / empty / not JSON (an API call that failed)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None


def deployment(deployments, statuses) -> dict | None:
    """The newest GitHub Pages deployment from the REST answers `GET /repos/R/deployments?environment=
    github-pages&per_page=1` and `GET /repos/R/deployments/ID/statuses?per_page=1` (newest first):
    {"id", "sha", "at" (created_at), "ok" (its latest status is success)}. None = unknown (never
    deployed, or the API could not be read): the caller then assumes nothing."""
    if not isinstance(deployments, list) or not deployments or not isinstance(deployments[0], dict):
        return None
    first = deployments[0]
    if not first.get("sha") or not isinstance(statuses, list) or not statuses or not isinstance(statuses[0], dict):
        return None
    return {"id": first.get("id"), "sha": str(first["sha"]), "at": first.get("created_at"),
            "ok": statuses[0].get("state") == "success"}


def _due(min_interval: timedelta, min_gap: timedelta, now: datetime | None) -> tuple[bool, str]:
    """Would a worker cycle run the update now? Read-only: never creates or migrates the database."""
    from shots.db import migrate
    from shots.db.engine import make_engine
    from shots.worker import due

    if not db_path().exists():
        return True, "run: never: no database yet"
    engine = make_engine()
    try:
        if not migrate.is_current(engine):
            return True, "run: migrate: the database schema is behind the code"
        d = due.decide(engine, now=now, min_interval=min_interval, min_gap=min_gap)
        return d.run, d.summary()
    except Exception as exc:  # the worker itself re-decides and reports; never skip on a doubt
        return True, f"run: unknown: the due check failed ({type(exc).__name__})"
    finally:
        engine.dispose()


def plan(event: str, *, force_input: bool | None = None, changed_files=None, deployed: dict | None = None,
         head: str | None = None, now: datetime | None = None, min_interval: timedelta | None = None,
         min_gap: timedelta | None = None) -> dict:
    """What one workflow run does, from its trigger and the restored state.

    force   run-once --force: a manual run (unless started with force=false, which is what the watchdog
            and an external cron send), or a push that changed PIPELINE_PATHS (unknown file list: yes).
            Never for `schedule`.
    update  run the update cycle at all: forced, or the worker's own rules say something is due.
    site    `always`: build and deploy whatever the data (forced runs, every push that got through the
            path filter, and catch-up: the newest deployment failed or was built from another commit
            than `head`, e.g. the run of a push was cancelled while waiting). `if-changed`: only when the
            export differs from the published one (or the heartbeat is due): see publish().
    export  anything to do after the plan (update or site == always); false = the run ends here and
            saves no cache.
    """
    from shots.worker import due

    reasons: list[str] = []
    if event == SCHEDULED:
        force, site = False, IF_CHANGED
    elif event == "push":
        files = None if changed_files is None else [f.strip() for f in changed_files if f.strip()]
        force = not files or touches_pipeline(files)
        site = ALWAYS
        reasons.append("push: " + ("changed files unknown" if not files else
                                   "pipeline files changed" if force else "site files only"))
    else:  # workflow_dispatch (and anything unforeseen): forced unless asked not to
        force = force_input is not False
        site = ALWAYS if force else IF_CHANGED
    if site == IF_CHANGED and deployed is not None:
        if not deployed["ok"]:
            site = ALWAYS
            reasons.append("catch-up: the last deployment did not succeed")
        elif head and deployed["sha"] != head:
            site = ALWAYS
            reasons.append(f"catch-up: the site was built from {deployed['sha'][:7]}, the branch is at {head[:7]}")
    if force:
        update = True
        reasons.append("run: forced")
    else:
        update, why = _due(min_interval if min_interval is not None else due.env_min_interval(),
                           min_gap if min_gap is not None else due.env_min_gap(), now)
        reasons.append(why)
    return {"update": update, "force": force, "site": site, "export": update or site == ALWAYS,
            "deployed_at": (deployed or {}).get("at") or "", "reasons": " | ".join(reasons)}


def publish(site: str, changed: bool, last_published: datetime | None, now: datetime | None = None,
            heartbeat: timedelta = HEARTBEAT) -> tuple[bool, str]:
    """Build and deploy the site in this run? Yes when the plan says always, when the exported data
    changed, or as a heartbeat when the last deployment (else the last published change) is at least
    `heartbeat` old or unknown: meta.json `built_at` on the site then keeps saying when the pipeline
    last ran even in a week without football."""
    now = now or _now()
    if site == ALWAYS:
        return True, "requested by the trigger (push, manual run or catch-up)"
    if changed:
        return True, "the published data changed"
    if last_published is None:
        return True, "no earlier publication is known"
    age = now - last_published
    if age >= heartbeat:
        return True, f"heartbeat: last published {age.total_seconds() / 3600:.1f} h ago, data unchanged"
    return False, f"data unchanged, last published {age.total_seconds() / 3600:.1f} h ago"


def _flag(text: str | None) -> bool | None:
    """true / false (any case) -> bool; anything else (an input that does not exist for this trigger
    arrives as an empty string) -> None."""
    return {"true": True, "false": False}.get((text or "").strip().lower())


def _line(value) -> str:
    """One GITHUB_OUTPUT value: booleans as true / false, no line breaks."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return " ".join(str(value).split())


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m shots.worker.ci", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("keys", help="cache keys and paths (GITHUB_OUTPUT format)")
    chk = sub.add_parser("db-check", help="checkpoint, integrity check, counts")
    chk.add_argument("--write-baseline", type=Path, default=None, help="save the counts to this file")
    chk.add_argument("--baseline", type=Path, default=None,
                     help="fail (exit 1) if the database has fewer predictions than recorded in this file")
    pr = sub.add_parser("prune-fixtures", help="delete old fixtures.csv downloads")
    pr.add_argument("--keep", type=int, default=3)
    ds = sub.add_parser("describe", help="one line about an export's meta.json")
    ds.add_argument("meta", type=Path)
    ds.add_argument("--markdown", action="store_true", help="a summary bullet instead of a commit message")
    pl = sub.add_parser("plan", help="what this workflow run does (read-only)")
    pl.add_argument("--event", required=True, help="github.event_name")
    pl.add_argument("--force-input", default="", help="the `force` input of a manual run (true / false / empty)")
    pl.add_argument("--changed-files", type=Path, default=None, help="file with the paths changed by a push")
    pl.add_argument("--deployment", type=Path, default=None, help="saved answer of the deployments API")
    pl.add_argument("--deployment-status", type=Path, default=None, help="saved answer of its statuses API")
    pl.add_argument("--head", default="", help="commit being run (github.sha)")
    di = sub.add_parser("deployment-id", help="id of the newest deployment in a saved API answer (empty if none)")
    di.add_argument("file", type=Path)
    pb = sub.add_parser("publish", help="build and deploy the site in this run?")
    pb.add_argument("--site", required=True, choices=(ALWAYS, IF_CHANGED))
    pb.add_argument("--changed", default="", help="true when the data branch got a commit")
    pb.add_argument("--deployed-at", default="", help="time of the last deployment (plan output)")
    pb.add_argument("--published-meta", type=Path, default=None, help="meta.json as published before this run")
    pb.add_argument("--heartbeat-hours", type=float, default=HEARTBEAT.total_seconds() / 3600)
    args = ap.parse_args(argv)
    if args.command == "plan":
        files = None
        if args.changed_files and args.changed_files.exists():
            files = args.changed_files.read_text(encoding="utf-8").splitlines()
        result = plan(args.event, force_input=_flag(args.force_input), changed_files=files,
                      deployed=deployment(_load_json(args.deployment), _load_json(args.deployment_status)),
                      head=args.head or None)
        sys.stdout.write("".join(f"{k}={_line(v)}\n" for k, v in result.items()))
    elif args.command == "deployment-id":
        found = _load_json(args.file)
        print(found[0].get("id", "") if isinstance(found, list) and found and isinstance(found[0], dict) else "")
    elif args.command == "publish":
        last = _utc(args.deployed_at) if args.deployed_at else None
        if last is None:
            last = _utc((_load_json(args.published_meta) or {}).get("built_at"))
        deploy, why = publish(args.site, _flag(args.changed) is True, last,
                              heartbeat=timedelta(hours=args.heartbeat_hours))
        sys.stdout.write(f"deploy={_line(deploy)}\nreason={_line(why)}\n")
    elif args.command == "describe":
        print(describe(json.loads(args.meta.read_text(encoding="utf-8")), args.markdown))
    elif args.command == "keys":
        sys.stdout.write(format_outputs(cache_keys()))
    elif args.command == "db-check":
        try:
            info = db_check()
        except RuntimeError as exc:
            print(f"db-check failed: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(info, indent=2))
        if args.write_baseline:
            args.write_baseline.write_text(json.dumps(info), encoding="utf-8")
        if args.baseline and args.baseline.exists():
            problems = shrunk(json.loads(args.baseline.read_text(encoding="utf-8")), info)
            if problems:
                print("db-check failed: the database lost history since it was restored: " + "; ".join(problems),
                      file=sys.stderr)
                sys.exit(1)
    elif args.command == "prune-fixtures":
        print(json.dumps({"deleted": prune_fixtures(args.keep)}))


if __name__ == "__main__":
    main()
