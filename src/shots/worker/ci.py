"""Helpers for the scheduled GitHub Actions run (.github/workflows/update.yml). Plain, tested Python
instead of shell in the workflow. Nothing here is Linux- or GitHub-specific: every command also runs
locally.

    python -m shots.worker.ci keys                      cache keys / paths as `name=value` lines (GITHUB_OUTPUT format)
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
from pathlib import Path

from shots.db.engine import db_path
from shots.ingest import football_data as fd

FIRST_SEASON = 2010  # shots.ingest.football_data.fetch default
COUNT_TABLES = ("prediction_runs", "predictions", "job_runs")


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
    args = ap.parse_args(argv)
    if args.command == "describe":
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
