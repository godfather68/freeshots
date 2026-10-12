"""Helpers of the scheduled GitHub Actions run (shots.worker.ci): cache keys / paths, the database
check that guards the saved state, pruning of fixtures downloads."""

import json
import re
import sqlite3
from datetime import date

import pytest

from shots.db import jobs, load
from shots.db.engine import db_path, make_engine
from shots.worker import ci
from tests.test_web import META, make_board


def test_cache_keys_and_paths(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("pandas==3.0.6\n")
    k1 = ci.cache_keys(date(2026, 10, 12), lock)
    k2 = ci.cache_keys(date(2027, 8, 1), lock)
    assert (k1["season"], k2["season"]) == ("2627", "2728")
    assert k1["current_paths"] == ["data/raw/football-data/2627"] and k2["current_paths"] == ["data/raw/football-data/2728"]
    assert k1["past_paths"][0] == "data/raw/football-data/1011" and k1["past_paths"][-1] == "data/raw/football-data/2526"
    assert k2["past_paths"] == k1["past_paths"] + ["data/raw/football-data/2627"]
    # actions/cache restores a cache only with the exact path list that saved it: the list holding the
    # database must not change with the date (season rollover), or the live record would be orphaned.
    assert k1["state_paths"] == k2["state_paths"]
    assert "data/raw/football-data/fixtures" in k1["state_paths"] and "data/processed/matches.parquet" in k1["state_paths"]
    assert not any(code in p for p in k1["state_paths"] for code in ("2627", "2728", "football-data/2"))
    assert re.fullmatch(r"py3\d+-[0-9a-f]{16}", k1["lock"]) and k1["lock"] == k2["lock"]
    lock.write_text("pandas==3.0.7\n")
    assert ci.cache_keys(date(2026, 10, 12), lock)["lock"] != k1["lock"]
    assert ci.cache_keys(date(2026, 10, 12), tmp_path / "missing.lock")["lock"].endswith("-nolock")


def test_state_paths_follow_the_database_location(monkeypatch):
    monkeypatch.setenv("SHOTS_DB", str(ci.fd.ROOT / "data" / "shots.db"))
    paths = ci.cache_keys(date(2026, 10, 12))["state_paths"]
    assert paths[0] == "data/shots.db" and all(not p.startswith("/") and "\\" not in p for p in paths)
    assert not any(p.startswith(("data/raw/statsbomb", "models_store")) for p in paths)


def test_github_output_format():
    text = ci.format_outputs({"season": "2627", "paths": ["a/b", "c"]})
    assert text == "season=2627\npaths<<__SHOTS_EOF__\na/b\nc\n__SHOTS_EOF__\n"


def test_db_check_checkpoints_and_counts(migrated_db):
    engine = make_engine(migrated_db)
    load.import_board(engine, make_board(), dict(META))
    jobs.finish(engine, jobs.start(engine, "update"), "ok", {})
    info = ci.db_check(migrated_db)
    assert info["counts"] == {"prediction_runs": 1, "predictions": 5, "job_runs": 1} and info["size_bytes"] > 0
    wal = migrated_db.with_name(migrated_db.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0  # everything is in the one file that gets cached
    engine.dispose()
    copy = migrated_db.with_name("copy.db")
    copy.write_bytes(migrated_db.read_bytes())  # the .db file alone is a complete database
    with sqlite3.connect(copy) as c:
        assert c.execute("SELECT count(*) FROM predictions").fetchone()[0] == 5


def test_db_check_rejects_missing_and_corrupt_files(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        ci.db_check(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 200)
    with pytest.raises(RuntimeError, match="not a usable SQLite database"):
        ci.db_check(bad)


def test_shrunk_detects_lost_history():
    before = {"counts": {"prediction_runs": 3, "predictions": 700}, "record_since": "2026-10-09 10:43:58"}
    assert ci.shrunk(before, {"counts": {"prediction_runs": 4, "predictions": 1300}, "record_since": "2026-10-09 10:43:58"}) == []
    assert ci.shrunk(before, before) == []
    lost = ci.shrunk(before, {"counts": {"prediction_runs": 1, "predictions": 600}, "record_since": "2026-10-20 09:00:00"})
    assert len(lost) == 3 and "predictions: 700 -> 600" in lost
    assert ci.shrunk({"counts": {}, "record_since": None}, {"counts": {"predictions": 5}, "record_since": "x"}) == []


def test_cli_db_check_baseline(migrated_db, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    engine = make_engine(migrated_db)
    load.import_board(engine, make_board(), dict(META))
    engine.dispose()
    base = tmp_path / "before.json"
    ci.main(["db-check", "--write-baseline", str(base)])
    assert json.loads(base.read_text())["counts"]["predictions"] == 5
    ci.main(["db-check", "--baseline", str(base)])  # unchanged: fine
    ci.main(["db-check", "--baseline", str(tmp_path / "no-baseline.json")])  # first run: nothing to compare
    base.write_text(json.dumps({"counts": {"prediction_runs": 9, "predictions": 999}, "record_since": None}))
    with pytest.raises(SystemExit) as exc:
        ci.main(["db-check", "--baseline", str(base)])
    assert exc.value.code == 1 and "lost history" in capsys.readouterr().err


def test_cli_db_check_fails_without_a_database(capsys):
    with pytest.raises(SystemExit) as exc:
        ci.main(["db-check"])
    assert exc.value.code == 1 and not db_path().exists()


def test_cli_keys(capsys):
    ci.main(["keys"])
    out = capsys.readouterr().out
    assert re.search(r"^season=\d{4}$", out, re.M) and "state_paths<<" in out and "past_paths<<" in out


def test_prune_fixtures_keeps_the_newest(tmp_path):
    for day in ("2026-10-02", "2026-10-08", "2026-10-09", "2026-10-12"):
        (tmp_path / f"fixtures_{day}.csv").write_text("Div\n")
    (tmp_path / "other.csv").write_text("x")
    assert ci.prune_fixtures(2, tmp_path) == ["fixtures_2026-10-02.csv", "fixtures_2026-10-08.csv"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fixtures_2026-10-09.csv", "fixtures_2026-10-12.csv", "other.csv"]
    assert ci.prune_fixtures(3, tmp_path) == [] and ci.prune_fixtures(3, tmp_path / "missing") == []


def test_describe_an_export(tmp_path, capsys):
    meta = {"built_at": "2026-10-12T15:34:41Z", "results_up_to": "2026-10-05", "last_job": None,
            "counts": {"fixtures": 472, "with_odds": 0, "cold_start": 0}, "record": {"since": "2026-10-09T10:43:58Z"}}
    assert ci.describe(meta) == "data: 2026-10-12T15:34:41Z (472 fixtures, results up to 2026-10-05)"
    line = ci.describe(dict(meta, last_job={"status": "partial", "finished_at": "x"}), markdown=True)
    assert line.startswith("- built 2026-10-12T15:34:41Z") and "`partial`" in line and "2026-10-09T10:43:58Z" in line
    assert "`none`" in ci.describe(meta, markdown=True) and "since none" in ci.describe(dict(meta, record=None), markdown=True)
    path = tmp_path / "meta.json"
    path.write_text(json.dumps(meta), encoding="utf-8")
    ci.main(["describe", str(path)])
    assert capsys.readouterr().out.strip() == ci.describe(meta)
