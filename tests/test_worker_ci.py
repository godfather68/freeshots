"""Helpers of the scheduled GitHub Actions run (shots.worker.ci): cache keys / paths, the database
check that guards the saved state, pruning of fixtures downloads."""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import update

from shots.db import jobs, load
from shots.db.engine import db_path, make_engine, session_scope
from shots.db.models import JobRun
from shots.worker import ci, due
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


# ----------------------------------------------------------------------------- plan / publish

HEAD, OLD = "3ba048387a80a94b3eeda078b16595626d50c32d", "4d4205a0000000000000000000000000000000aa"
# Shapes as returned by the GitHub REST API (trimmed), newest first
DEPLOYMENTS = [{"id": 7041925587, "sha": HEAD, "ref": "main", "environment": "github-pages",
                "created_at": "2026-10-13T14:57:27Z", "updated_at": "2026-10-13T14:57:48Z"}]
STATUSES = [{"state": "success", "created_at": "2026-10-13T14:57:48Z"}, {"state": "in_progress"}, {"state": "queued"}]
NOW = datetime(2026, 10, 14, 15, 0)  # a Wednesday afternoon (UTC)
RULES = {"min_interval": timedelta(hours=2.5), "min_gap": timedelta(minutes=90)}  # as in update.yml


@pytest.fixture
def state(migrated_db, monkeypatch):
    """A restored database whose last successful update started `age` before NOW."""
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    monkeypatch.delenv("SHOTS_ODDS_TZ", raising=False)
    engine = make_engine(migrated_db)
    load.import_board(engine, make_board(), dict(META))

    def updated(age: timedelta, status="ok"):
        jid = jobs.start(engine, "update")
        jobs.finish(engine, jid, status, {})
        with session_scope(engine) as s:
            s.execute(update(JobRun).where(JobRun.id == jid).values(started_at=NOW - age))
        wid = jobs.start(engine, "worker")  # the cycle records the calendars it saw (no mtime rule on a checkout)
        jobs.finish(engine, wid, "ok", {"action": "update", "calendars": due.calendars_fingerprint()})
        with session_scope(engine) as s:
            s.execute(update(JobRun).where(JobRun.id == wid).values(started_at=NOW - age))

    yield updated
    engine.dispose()


def test_scheduled_run_is_never_forced_and_stops_when_nothing_is_due(state):
    state(timedelta(minutes=58))
    live = ci.deployment(DEPLOYMENTS, STATUSES)
    p = ci.plan("schedule", deployed=live, head=HEAD, now=NOW, **RULES)
    assert p == {"update": False, "force": False, "site": "if-changed", "export": False,
                 "deployed_at": "2026-10-13T14:57:27Z", "reasons": p["reasons"]}
    assert p["reasons"].startswith("skip: recent")
    # `force` input, changed files: meaningless for a schedule event, never turn it into a forced run
    assert ci.plan("schedule", force_input=True, changed_files=["src/shots/x.py"], now=NOW, **RULES)["force"] is False


def test_scheduled_run_updates_when_due_without_force(state):
    state(timedelta(hours=3))
    p = ci.plan("schedule", deployed=ci.deployment(DEPLOYMENTS, STATUSES), head=HEAD, now=NOW, **RULES)
    assert (p["update"], p["force"], p["site"], p["export"]) == (True, False, "if-changed", True)
    assert p["reasons"].startswith("run: interval")


def test_plan_without_a_usable_database_runs_and_creates_nothing(migrated_db, monkeypatch, tmp_path):
    missing = tmp_path / "none" / "shots.db"
    monkeypatch.setenv("SHOTS_DB", str(missing))
    p = ci.plan("schedule", now=NOW, **RULES)
    assert p["update"] and not p["force"] and "never" in p["reasons"] and not missing.parent.exists()
    with sqlite3.connect(migrated_db) as c:  # a database from older code: the worker migrates it, the plan does not
        c.execute("UPDATE alembic_version SET version_num = '0001_initial'")
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    p = ci.plan("schedule", now=NOW, **RULES)
    assert p["update"] and "migrate" in p["reasons"]
    with sqlite3.connect(migrated_db) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001_initial"


def test_plan_is_read_only(state, migrated_db):
    state(timedelta(minutes=58))
    with sqlite3.connect(migrated_db) as c:
        before = c.execute("SELECT count(*), max(id) FROM job_runs").fetchone()
    for event in ("schedule", "push", "workflow_dispatch"):
        ci.plan(event, now=NOW, **RULES)
    with sqlite3.connect(migrated_db) as c:
        assert c.execute("SELECT count(*), max(id) FROM job_runs").fetchone() == before


def test_manual_run_is_forced_unless_asked_not_to(state):
    state(timedelta(minutes=10))
    for flag in (True, None):  # the input's default, or a trigger that carries no input
        p = ci.plan("workflow_dispatch", force_input=flag, now=NOW, **RULES)
        assert (p["update"], p["force"], p["site"], p["export"]) == (True, True, "always", True)
    p = ci.plan("workflow_dispatch", force_input=False, now=NOW, **RULES)  # the watchdog, an external cron
    assert (p["update"], p["force"], p["site"], p["export"]) == (False, False, "if-changed", False)


def test_push_forces_the_update_only_when_pipeline_files_changed(state):
    state(timedelta(minutes=10))
    code = ci.plan("push", changed_files=["src/shots/models/negbin.py", "docs/platform.md"], now=NOW, **RULES)
    assert (code["update"], code["force"], code["site"]) == (True, True, "always")
    for files in (["calendars/laliga.ics"], ["results_manual/uefa-champions-league_2026-27.csv"], ["requirements.lock"], [".github/workflows/update.yml"], ["migrations/versions/0005.py"]):
        assert ci.plan("push", changed_files=files, now=NOW, **RULES)["force"] is True
    front = ci.plan("push", changed_files=["frontend/src/App.tsx", "frontend/package-lock.json", ""], now=NOW, **RULES)
    assert front == {"update": False, "force": False, "site": "always", "export": True, "deployed_at": "",
                     "reasons": front["reasons"]}  # new build of the same data, no new prediction run
    assert "site files only" in front["reasons"]
    for unknown in (None, [], [""]):  # the diff could not be computed: assume the pipeline changed
        assert ci.plan("push", changed_files=unknown, now=NOW, **RULES)["force"] is True
    assert ci.touches_pipeline(["src/x.py"]) and not ci.touches_pipeline(["srcs/x.py", "frontend/src/a.ts", "pyproject.toml.bak"])
    assert ci.push_paths() == ["src/**", "migrations/**", "calendars/**", "results_manual/**", "requirements.lock", "pyproject.toml",
                               ".github/workflows/update.yml", "frontend/**", ".github/data-branch-README.md"]
    assert not any(p.startswith(("docs", "tests", "reports")) for p in ci.push_paths())


def test_catch_up_when_the_site_is_behind_the_branch_or_the_last_deployment_failed(state):
    """A push run that was replaced while waiting in the concurrency group never deployed: the next run
    (scheduled, or started by the watchdog) builds and deploys, without forcing an update."""
    state(timedelta(minutes=58))
    behind = ci.deployment([dict(DEPLOYMENTS[0], sha=OLD)], STATUSES)
    for event, flag in (("schedule", None), ("workflow_dispatch", False)):
        p = ci.plan(event, force_input=flag, deployed=behind, head=HEAD, now=NOW, **RULES)
        assert (p["update"], p["force"], p["site"], p["export"]) == (False, False, "always", True)
        assert "catch-up: the site was built from 4d4205a, the branch is at 3ba0483" in p["reasons"]
    failed = ci.deployment(DEPLOYMENTS, [{"state": "failure"}, {"state": "in_progress"}])
    p = ci.plan("schedule", deployed=failed, head=HEAD, now=NOW, **RULES)
    assert p["site"] == "always" and "did not succeed" in p["reasons"] and not p["update"]
    for unknown in (None, ci.deployment([], STATUSES), ci.deployment(DEPLOYMENTS, None), ci.deployment({"message": "Not Found"}, [])):
        assert unknown is None and ci.plan("schedule", deployed=unknown, head=HEAD, now=NOW, **RULES)["site"] == "if-changed"
    assert ci.plan("schedule", deployed=ci.deployment(DEPLOYMENTS, STATUSES), head=None, now=NOW, **RULES)["site"] == "if-changed"


def test_publish_decision():
    now = datetime(2026, 10, 14, 15, 0)
    recent, old = now - timedelta(hours=2), now - timedelta(hours=6, minutes=1)
    assert ci.publish("always", False, recent, now)[0] is True  # push, manual run, catch-up
    assert ci.publish("if-changed", True, recent, now) == (True, "the published data changed")
    deploy, why = ci.publish("if-changed", False, recent, now)  # scheduled update, identical export
    assert deploy is False and "data unchanged" in why and "2.0 h" in why
    deploy, why = ci.publish("if-changed", False, old, now)
    assert deploy is True and why.startswith("heartbeat")
    assert ci.publish("if-changed", False, None, now)[0] is True  # nothing known about the site: publish
    assert ci.publish("if-changed", False, old, now, heartbeat=timedelta(hours=12))[0] is False


def test_cli_plan_and_publish(state, tmp_path, capsys, monkeypatch):
    def out(argv):
        ci.main(argv)
        return dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())

    state(timedelta(days=30))  # the command line uses the real clock: long ago, so the interval rule applies
    monkeypatch.setenv("SHOTS_WORKER_MIN_INTERVAL_HOURS", "2.5")
    monkeypatch.setenv("SHOTS_WORKER_MIN_GAP_MINUTES", "90")
    dep, st, changed = tmp_path / "deployment.json", tmp_path / "status.json", tmp_path / "changed.txt"
    dep.write_text(json.dumps([dict(DEPLOYMENTS[0], sha=OLD)]), encoding="utf-8")
    st.write_text(json.dumps(STATUSES), encoding="utf-8")
    ci.main(["deployment-id", str(dep)])
    assert capsys.readouterr().out.strip() == "7041925587"
    for bad in ("[]", "", '{"message": "Bad credentials"}'):
        (tmp_path / "bad.json").write_text(bad, encoding="utf-8")
        ci.main(["deployment-id", str(tmp_path / "bad.json")])
        assert capsys.readouterr().out.strip() == ""
    p = out(["plan", "--event", "schedule", "--force-input", "", "--changed-files", str(tmp_path / "absent.txt"),
             "--deployment", str(dep), "--deployment-status", str(st), "--head", HEAD])
    assert list(p) == ["update", "force", "site", "export", "deployed_at", "reasons"]
    assert (p["update"], p["force"], p["site"], p["export"]) == ("true", "false", "always", "true")
    assert p["deployed_at"] == "2026-10-13T14:57:27Z" and "catch-up" in p["reasons"] and "run: interval" in p["reasons"]
    # API answers missing (the calls failed): no catch-up, the schedule still works
    p = out(["plan", "--event", "schedule", "--deployment", str(tmp_path / "absent.json"), "--head", HEAD])
    assert (p["force"], p["site"], p["deployed_at"]) == ("false", "if-changed", "")
    changed.write_text("frontend/src/App.tsx\nfrontend/index.html\n", encoding="utf-8")
    p = out(["plan", "--event", "push", "--changed-files", str(changed), "--head", HEAD])
    assert (p["force"], p["site"]) == ("false", "always")
    assert out(["plan", "--event", "workflow_dispatch", "--force-input", "true"])["force"] == "true"
    assert out(["plan", "--event", "workflow_dispatch", "--force-input", "false"])["force"] == "false"

    meta = tmp_path / "meta.json"
    long_ago, just_now = "2026-01-01T00:00:00Z", ci._now().strftime("%Y-%m-%dT%H:%M:%SZ")
    meta.write_text(json.dumps({"built_at": just_now}), encoding="utf-8")
    base = ["publish", "--site", "if-changed", "--published-meta", str(meta)]
    assert out(base + ["--changed", "false", "--deployed-at", ""])["deploy"] == "false"  # falls back to the meta file
    assert out(base + ["--changed", "", "--deployed-at", just_now])["deploy"] == "false"
    assert out(base + ["--changed", "true", "--deployed-at", just_now])["deploy"] == "true"
    r = out(base + ["--changed", "false", "--deployed-at", long_ago])  # the deployment time wins over the file
    assert r["deploy"] == "true" and r["reason"].startswith("heartbeat")
    assert out(["publish", "--site", "if-changed", "--changed", "false", "--published-meta", str(tmp_path / "no.json")])["deploy"] == "true"
    assert out(["publish", "--site", "always", "--changed", "false", "--deployed-at", just_now])["deploy"] == "true"


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
