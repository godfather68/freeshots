"""Static checks of the GitHub Actions workflows and the dependency lock (Stage 1 deployment): the
properties that protect the published record and keep source data private. They cannot replace a run
on GitHub; they catch edits that would silently weaken the setup."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from shots.worker import ci as worker_ci  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PRIVATE = re.compile(r"shots\.db|data/raw|models_store|\.parquet|data/processed|calendars")


def load(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    doc["on"] = doc.pop(True) if True in doc else doc["on"]  # YAML 1.1 reads the key `on` as a boolean
    return doc


def steps(doc: dict):
    for job_name, job in doc["jobs"].items():
        for step in job["steps"]:
            yield job_name, step


@pytest.fixture(scope="module")
def update():
    return load("update.yml")


@pytest.fixture(scope="module")
def ci():
    return load("ci.yml")


@pytest.fixture(scope="module")
def watchdog():
    return load("watchdog.yml")


def cron_slots(crons):
    """(hour, minute) slots of a day for the simple `M H-spec * * *` crons used here."""
    out = set()
    for cron in crons:
        minute, hours, *rest = cron.split()
        assert rest == ["*", "*", "*"] and minute.isdigit(), cron
        if hours == "*":
            hs = range(24)
        else:
            span, _, step = hours.partition("/")
            lo, _, hi = span.partition("-")
            hs = range(0 if lo == "*" else int(lo), (int(hi) if hi else 23) + 1, int(step or 1))
        out |= {(h, int(minute)) for h in hs}
    return out


def test_update_triggers_schedule_push_and_manual(update):
    on = update["on"]
    assert set(on) == {"schedule", "push", "workflow_dispatch"}
    crons = [s["cron"] for s in on["schedule"]]
    slots = cron_slots(crons)
    # best-effort scheduler: a slot every hour, at two different minutes, never near the top of the hour
    assert len(crons) == 2 and {h for h, _ in slots} == set(range(24)) and len(slots) == 24
    minutes = {m for _, m in slots}
    assert len(minutes) == 2 and all(5 <= m <= 55 and m % 5 for m in minutes)
    # merged changes deploy themselves; documentation, tests and reports do not start the pipeline
    push = on["push"]
    assert push["branches"] == ["main"] and "paths-ignore" not in push and "tags" not in push
    assert sorted(push["paths"]) == sorted(worker_ci.push_paths())
    assert not any(p.startswith(("docs", "tests", "reports", "README", "CLAUDE", "**", "*")) for p in push["paths"])
    assert "data" not in push["branches"] and update["env"]["DATA_BRANCH"] == "data"  # bot commits never retrigger
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["force"] == {"description": inputs["force"]["description"], "type": "boolean", "default": True}
    assert inputs["allow_fresh_state"]["default"] is False
    # no overlap, and a running update is never cancelled
    assert update["concurrency"] == {"group": "shots-update", "cancel-in-progress": False}
    assert all("concurrency" not in job for job in update["jobs"].values())


def test_worker_rules_on_the_runner_are_stated(update):
    env = update["env"]
    assert env["TZ"] == "UTC" and env["SHOTS_ODDS_TZ"] == "UTC"  # every due rule is evaluated in a stated zone
    interval, gap = float(env["SHOTS_WORKER_MIN_INTERVAL_HOURS"]), float(env["SHOTS_WORKER_MIN_GAP_MINUTES"])
    assert 2 <= interval <= 3  # with hourly slots: a full update every third slot, about 3 h
    assert 60 < gap <= interval * 60  # due work waits for the second slot at most; never two runs in a row
    assert interval < worker_ci.HEARTBEAT.total_seconds() / 3600 <= 2 * interval  # every second full update at the latest


def test_permissions_are_minimal_per_job(update, ci, watchdog):
    assert update["permissions"] == {}  # nothing by default; each job states what it needs
    jobs = update["jobs"]
    assert jobs["update"]["permissions"] == {"contents": "write", "actions": "write", "deployments": "read"}
    assert jobs["build"]["permissions"] == {"contents": "read"}  # npm install / build never sees a write token
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["deploy"]["environment"]["name"] == "github-pages"
    assert jobs["surge_issue"]["permissions"] == {"issues": "write"}  # only the REVERT issue; no contents, no actions
    assert ci["permissions"] == {"contents": "read"} and all("permissions" not in j for j in ci["jobs"].values())
    assert watchdog["permissions"] == {} and list(watchdog["jobs"]) == ["check"]
    assert watchdog["jobs"]["check"]["permissions"] == {"contents": "read", "actions": "write"}


@pytest.mark.parametrize("name", ["update.yml", "watchdog.yml", "surge-weekly.yml"])
def test_no_expression_is_interpolated_into_a_shell_script_and_no_secret_is_used(name):
    doc = load(name)
    for _, s in steps(doc):
        assert "${{" not in s.get("run", ""), s["name"]  # values reach the shell through env only
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "secrets." not in text and "pull_request_target" not in text
    tokens = [s["name"] for _, s in steps(doc) if "github.token" in str(s.get("env", ""))]
    assert tokens == {"update.yml": ["Guard the live record", "Plan this run",
                                     "Delete superseded caches (keep the 5 newest databases)",
                                     "Check out the data branch",
                                     "Commit the data branch (only when something changed)",
                                     "Open or update the surge-monitor issue"],
                      "watchdog.yml": ["Look at the recent update runs and the site",
                                       "Start the update workflow"],
                      "surge-weekly.yml": ["Start the update workflow"]}[name]


@pytest.mark.parametrize("name", ["update.yml", "ci.yml", "watchdog.yml"])
def test_actions_are_pinned_and_official(name):
    doc = load(name)
    used = [s["uses"] for _, s in steps(doc) if "uses" in s]
    assert used
    for u in used:
        assert re.fullmatch(r"actions/[a-z-]+(/[a-z]+)?@[0-9a-f]{40}", u), u
    for _, s in steps(doc):
        if s.get("uses", "").startswith("actions/checkout@"):
            assert s["with"]["persist-credentials"] is False  # no token left in .git/config for later steps


def test_python_is_pinned_and_installed_from_the_lock(update, ci):
    for doc in (update, ci):
        assert doc["env"]["PYTHON_VERSION"] == "3.12"
        installs = [s["run"] for _, s in steps(doc) if "pip install" in s.get("run", "")]
        assert installs and all("-r requirements.lock" in r and "--no-deps -e ." in r for r in installs)
        assert not any("requirements.txt" in s.get("run", "") for _, s in steps(doc))
        for _, s in steps(doc):
            if s.get("uses", "").startswith("actions/setup-python@"):
                assert s["with"]["cache-dependency-path"] == "requirements.lock"
    assert all(j["runs-on"] == "ubuntu-24.04" for doc in (update, ci) for j in doc["jobs"].values())


def test_pipeline_order_state_is_saved_before_anything_is_published(update):
    names = [s["name"] for s in update["jobs"]["update"]["steps"]]
    order = ["Restore state (database, derived files)", "Guard the live record", "Plan this run",
             "Run the update cycle", "Check the database", "Save state", "Export JSON",
             "Commit the data branch (only when something changed)", "Decide whether to build and deploy the site",
             "Export JSON for the site (fresh timestamp)", "Hand the JSON to the site build",
             "Fail the run if the update cycle failed"]
    assert [n for n in names if n in order] == order
    by = {s["name"]: s for s in update["jobs"]["update"]["steps"]}
    assert by["Run the update cycle"]["continue-on-error"] is True
    assert "steps.dbcheck.outcome == 'success'" in by["Save state"]["if"]
    assert "shots.export.verify" in by["Export JSON"]["run"]  # the independent check runs on every export
    assert "allow_fresh_state" in str(update["on"]["workflow_dispatch"]["inputs"])
    assert "exit 1" in by["Guard the live record"]["run"] and "gh cache list" in by["Guard the live record"]["run"]


def test_update_cycle_is_forced_only_when_the_plan_says_so(update):
    by = {s["name"]: s for s in update["jobs"]["update"]["steps"]}
    plan, run = by["Plan this run"], by["Run the update cycle"]
    # the trigger reaches the plan as data; the plan (tests/test_worker_ci.py) never forces a scheduled run
    assert plan["env"]["EVENT"] == "${{ github.event_name }}" and plan["env"]["FORCE_INPUT"] == "${{ inputs.force }}"
    assert 'python -m shots.worker.ci plan --event "$EVENT" --force-input "$FORCE_INPUT"' in plan["run"]
    assert '--head "$GITHUB_SHA"' in plan["run"] and '>> "$GITHUB_OUTPUT"' in plan["run"]
    assert "if" not in plan  # always evaluated; nothing before it depends on the trigger
    assert run["if"] == "${{ steps.plan.outputs.update == 'true' }}"
    assert run["env"] == {"FORCE": "${{ steps.plan.outputs.force }}"}
    lines = [line.strip() for line in run["run"].splitlines() if line.strip() and not line.strip().startswith("#")]
    assert lines == ['if [ "$FORCE" = "true" ]; then', "python -m shots.worker run-once --force", "else",
                     "python -m shots.worker run-once", "fi"]
    # nowhere else is the worker started, and the event name is never compared in the workflow itself
    text = (WORKFLOWS / "update.yml").read_text(encoding="utf-8")
    assert text.count("shots.worker run-once --force\n") == 1 and text.count("shots.worker run-once\n") == 1
    assert "github.event_name ==" not in text and "github.event_name !=" not in text


def test_a_run_with_nothing_to_do_saves_no_cache_and_publishes_nothing(update):
    """Everything after the plan depends on it: with update=false and export=false (a scheduled run that
    finds nothing due) no step that writes a cache, a commit, an artifact or a deployment can run."""
    job = update["jobs"]["update"]["steps"]
    names = [s["name"] for s in job]
    after = job[names.index("Plan this run") + 1:]
    by = {s["name"]: s for s in job}
    updated, exported = "steps.plan.outputs.update == 'true'", "steps.plan.outputs.export == 'true'"
    assert exported in by["Check the database"]["if"]
    chain = {"Check the database": exported}  # step -> the plan output it (transitively) requires
    requires = {"steps.dbcheck.outcome == 'success'": "Check the database", "steps.export.outcome == 'success'": "Export JSON",
                "steps.publish.outputs.deploy == 'true'": "Decide whether to build and deploy the site",
                "steps.site.outcome == 'success'": "Export JSON for the site (fresh timestamp)",
                "steps.update.outcome == 'success'": "Run the update cycle",
                "steps.update.outcome == 'failure'": "Run the update cycle",
                "steps.mh.outputs.key != ''": "Fingerprint the models"}
    for s in after:
        cond = s.get("if", "")
        if s["name"] == "Summary":
            assert cond == "${{ always() }}" and "GITHUB_STEP_SUMMARY" in s["run"]
            continue
        gate = updated if updated in cond else exported if exported in cond else None
        if gate is None:
            parents = [parent for text, parent in requires.items() if text in cond]
            assert parents, f"{s['name']} is not gated by the plan: {cond!r}"
            gate = chain[parents[0]]
        chain[s["name"]] = gate
    # cache writers and deleters need a real update cycle, not merely an export
    for s in after:
        if s.get("uses", "").startswith("actions/cache/save@") or "gh cache delete" in s.get("run", ""):
            assert chain[s["name"]] == updated or "steps.update.outcome == 'success'" in s["if"], s["name"]
    assert updated in by["Save state"]["if"] and updated in by["Delete superseded caches (keep the 5 newest databases)"]["if"]
    # only the explicit restore / save actions are used: plain actions/cache would save in a post step
    cache = [s["uses"].split("@")[0] for s in job if s.get("uses", "").startswith("actions/cache")]
    assert set(cache) == {"actions/cache/restore", "actions/cache/save"}
    # before the plan only the state is restored (read-only) and guarded
    before = names[:names.index("Plan this run")]
    assert before == ["Check out", "Set up Python", "Install (pinned)", "Cache keys",
                      "Restore state (database, derived files)", "Guard the live record"]
    for name in ("Restore raw downloads of past seasons", "Restore raw downloads of the current season",
                 "Restore trained models"):
        assert by[name]["if"] == "${{ " + updated + " }}"


def test_site_is_built_and_deployed_only_when_the_publish_decision_says_so(update):
    by = {s["name"]: s for s in update["jobs"]["update"]["steps"]}
    commit, decide, site = (by["Commit the data branch (only when something changed)"],
                            by["Decide whether to build and deploy the site"],
                            by["Export JSON for the site (fresh timestamp)"])
    assert commit["id"] == "commit"
    assert commit["run"].count('echo "changed=false" >> "$GITHUB_OUTPUT"') == 1
    assert commit["run"].rstrip().endswith('echo "changed=true" >> "$GITHUB_OUTPUT"')  # only after the push succeeded
    assert decide["env"] == {"SITE": "${{ steps.plan.outputs.site }}", "CHANGED": "${{ steps.commit.outputs.changed }}",
                             "DEPLOYED_AT": "${{ steps.plan.outputs.deployed_at }}"}
    assert 'python -m shots.worker.ci publish --site "$SITE" --changed "$CHANGED" --deployed-at "$DEPLOYED_AT"' in decide["run"]
    assert "published-meta.json" in decide["run"] and "published-meta.json" in by["Check out the data branch"]["run"]
    assert site["if"] == "${{ !cancelled() && steps.publish.outputs.deploy == 'true' }}"
    assert 'echo "exported=true" >> "$GITHUB_OUTPUT"' in site["run"]
    jobs = update["jobs"]
    assert jobs["update"]["outputs"] == {"exported": "${{ steps.site.outputs.exported }}",
                                         "surge_verdict": "${{ steps.monitor.outputs.verdict }}",
                                         "surge_report": "${{ steps.monitor.outputs.report }}"}
    assert jobs["build"]["needs"] == "update" and jobs["deploy"]["needs"] == "build"
    assert jobs["build"]["if"] == "${{ !cancelled() && needs.update.outputs.exported == 'true' }}"
    assert "needs.build.result == 'success'" in jobs["deploy"]["if"]


def test_watchdog_is_an_independent_schedule_that_dispatches_the_update(update, watchdog):
    assert set(watchdog["on"]) == {"schedule", "workflow_dispatch"}
    own, main = cron_slots(s["cron"] for s in watchdog["on"]["schedule"]), cron_slots(s["cron"] for s in update["on"]["schedule"])
    assert {h for h, _ in own} == set(range(24))
    # other minutes than update.yml, at least 20 minutes after each of its slots and before the next
    assert all(20 <= (wm - m) % 60 <= 40 for _, wm in own for _, m in main)
    assert watchdog["concurrency"]["group"] != update["concurrency"]["group"]
    job = watchdog["jobs"]["check"]
    assert job["timeout-minutes"] <= 5 and job["runs-on"] == "ubuntu-24.04"
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "gh run list --workflow update.yml" in runs and "status,conclusion,createdAt,event" in runs
    assert "python3 src/shots/worker/watchdog.py" in runs and "pip install" not in runs
    start = next(s for s in job["steps"] if s["name"] == "Start the update workflow")
    assert start["if"] == "${{ steps.look.outputs.dispatch == 'true' }}"
    assert 'gh workflow run update.yml -f "force=$FORCE"' in start["run"]
    assert start["env"]["FORCE"] == "${{ steps.look.outputs.force }}"
    checkout = job["steps"][0]["with"]
    assert checkout["sparse-checkout"] == "src/shots/worker/watchdog.py" and checkout["persist-credentials"] is False
    # the script it runs must work without the package: standard library imports only
    import ast
    tree = ast.parse((ROOT / "src" / "shots" / "worker" / "watchdog.py").read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"__future__", "argparse", "json", "sys", "datetime", "pathlib"}


def test_state_cache_paths_are_constant_and_keys_unique_per_run(update):
    by = {s["name"]: s for s in update["jobs"]["update"]["steps"]}
    restore, save = by["Restore state (database, derived files)"]["with"], by["Save state"]["with"]
    assert restore["path"] == save["path"] == "${{ steps.keys.outputs.state_paths }}"
    assert restore["key"] == save["key"] and "github.run_id" in save["key"]  # caches are immutable: a new key per run
    assert restore["restore-keys"] == "${{ env.STATE_PREFIX }}"


def test_audit_branch_is_append_only(update):
    text = (WORKFLOWS / "update.yml").read_text(encoding="utf-8")
    runs = "\n".join(s.get("run", "") for _, s in steps(update))
    pushes = [line for line in runs.splitlines() if re.search(r"\bpush\b", line) and "git" in line
              and not line.lstrip().startswith("#")]
    assert len(pushes) == 1 and "HEAD:refs/heads/$DATA_BRANCH" in pushes[0]
    git_lines = "\n".join(line for line in runs.splitlines() if "git " in line)  # (`run-once --force` is not git)
    for flag in ("--force", " -f ", "+HEAD", "+refs", "--mirror", "--delete", "reset --hard", "--amend", "rebase"):
        assert flag not in git_lines, flag
    commit = next(s for _, s in steps(update) if s["name"].startswith("Commit the data branch"))
    assert "git diff --cached --quiet" in commit["run"]  # commit only when something changed
    assert "github-actions[bot]" in commit["run"]
    assert "git add -- README.md data/meta.json data/board.json data/results.json data/track.json data/surge.json" in commit["run"]
    assert "git add -A" not in text and "git add ." not in text


def test_source_data_and_database_are_never_uploaded_or_deployed(update):
    for job, s in steps(update):
        uses = s.get("uses", "")
        if uses.startswith("actions/upload-artifact@"):
            assert s["with"]["path"] == "site-export/*.json" and s["with"]["retention-days"] == 1
        elif uses.startswith("actions/upload-pages-artifact@"):
            assert s["with"]["path"] == "frontend/dist"
        elif uses.startswith("actions/cache"):
            assert job == "update"  # caches are private to the repository; nothing else may hold state
        elif "upload" in uses or "release" in uses:
            raise AssertionError(f"unexpected publishing step: {uses}")
    build = update["jobs"]["build"]["steps"]
    assert not any(PRIVATE.search(s.get("run", "") + str(s.get("with", ""))) for s in build
                   if s["name"] != "Check what will be published")
    guard = next(s for s in build if s["name"] == "Check what will be published")
    assert all(ext in guard["run"] for ext in ("*.db", "*.csv", "*.parquet", "*.joblib", "*.ics"))
    readme = (ROOT / ".github" / "data-branch-README.md").read_text(encoding="utf-8")
    assert "force-pushed" in readme and "No odds" in readme


def test_ci_runs_the_suite_without_a11y_and_a_separate_optional_a11y_job(ci):
    assert set(ci["on"]) == {"push", "pull_request"}
    runs = {job: [s.get("run", "") for s in j["steps"]] for job, j in ci["jobs"].items()}
    [suite] = [r for r in runs["tests"] if "pytest" in r]
    assert suite.startswith('python -m pytest -q -m "not a11y"')
    # exactly one known platform-specific exclusion (a bit-exact golden hash recorded on Windows); nothing else
    assert re.findall(r"--deselect \"([^\"]+)\"", suite) == ["tests/test_models_team.py::test_total_outputs_unchanged_synthetic"]
    assert " -k " not in suite and "--ignore" not in suite
    assert "python -m pytest -q -m a11y" in runs["a11y"] and ci["jobs"]["a11y"]["continue-on-error"] is True
    assert any("playwright install" in r for r in runs["a11y"])
    front = ci["jobs"]["frontend"]["steps"]
    assert any("frontend/package.json" in s.get("run", "") for s in front)
    assert [s["run"] for s in front if s.get("working-directory") == "frontend"] == [
        "npm ci", "npm run lint --if-present", "npm run test --if-present", "npm run build"]


def test_lock_file_pins_every_requirement():
    def norm(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    lock = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([0-9][A-Za-z0-9.+!]*)", line.strip())
            assert m, f"not an exact pin: {line!r}"
            lock[norm(m[1])] = m[2]
    wanted = [norm(re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0])
              for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")]
    assert wanted and not [w for w in wanted if w not in lock]
    assert {"lightgbm", "statsmodels", "pyarrow", "pandas", "numpy", "scipy", "scikit-learn"} <= set(lock)


# ----------------------------------------------------------------------------- weekly Surge rebuild + monitor

@pytest.fixture(scope="module")
def weekly():
    return load("surge-weekly.yml")


def test_weekly_workflow_dispatches_a_forced_update_with_the_monitor(weekly, update):
    assert set(weekly["on"]) == {"schedule", "workflow_dispatch"}
    assert [s["cron"] for s in weekly["on"]["schedule"]] == ["0 7 * * 1"]  # Monday 07:00 UTC
    assert weekly["permissions"] == {} and list(weekly["jobs"]) == ["dispatch"]
    job = weekly["jobs"]["dispatch"]
    assert job["permissions"] == {"actions": "write"}  # starting update.yml is all it does
    assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] <= 5
    assert weekly["concurrency"]["group"] not in (update["concurrency"]["group"], "shots-watchdog")
    assert not any("uses" in s for s in job["steps"])  # no checkout, no install, no third-party code
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "gh workflow run update.yml -f force=true -f surge_monitor=true" in runs
    assert "pip install" not in runs and "git " not in runs and "secrets." not in (WORKFLOWS / "surge-weekly.yml").read_text()


def test_update_runs_the_surge_monitor_only_when_asked_and_after_the_database_check(update, watchdog):
    inp = update["on"]["workflow_dispatch"]["inputs"]["surge_monitor"]
    assert inp["type"] == "boolean" and inp["default"] is False  # schedule, push and the watchdog never run it
    assert "surge_monitor" not in "\n".join(s.get("run", "") for s in watchdog["jobs"]["check"]["steps"])
    job = update["jobs"]["update"]["steps"]
    names = [s["name"] for s in job]
    by = {s["name"]: s for s in job}
    mon = by["Surge monitor"]
    assert mon["id"] == "monitor"
    assert mon["if"] == "${{ !cancelled() && inputs.surge_monitor && steps.dbcheck.outcome == 'success' }}"
    assert names.index("Check the database") < names.index("Surge monitor") < names.index("Export JSON")
    run = mon["run"]
    assert 'python -m shots.surge_monitor --out-dir "$out"' in run and 'out="$RUNNER_TEMP/surge-monitor"' in run
    assert "GITHUB_STEP_SUMMARY" in run and 'echo "verdict=$verdict" >> "$GITHUB_OUTPUT"' in run
    assert "2) verdict=REVERT" in run  # the monitor's exit code 2
    # read-only: no config change, no commit, nothing written into the repository or the published branch
    for word in ("config.py", "SHOTS_ODDS_MODEL", "git ", "site-data", "reports/", "sed "):
        assert word not in run, word
    assert "continue-on-error" not in mon and "|| rc=$?" in run  # a failing monitor never blocks publishing


def test_a_revert_verdict_opens_or_updates_an_issue_and_changes_nothing(update):
    job = update["jobs"]["surge_issue"]
    assert job["needs"] == "update" and job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] <= 5
    assert job["if"] == "${{ !cancelled() && needs.update.outputs.surge_verdict == 'REVERT' }}"
    [step] = job["steps"]
    assert "uses" not in step
    run = step["run"]
    assert "gh issue list --label surge-monitor --state open" in run
    assert 'gh issue comment "$num"' in run and "gh issue create" in run and "--label surge-monitor" in run
    assert step["env"]["REPORT_B64"] == "${{ needs.update.outputs.surge_report }}"
    for word in ("git ", "config.py", "gh workflow", "gh api", "gh pr"):
        assert word not in run, word
