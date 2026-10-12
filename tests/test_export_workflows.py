"""Static checks of the GitHub Actions workflows and the dependency lock (Stage 1 deployment): the
properties that protect the published record and keep source data private. They cannot replace a run
on GitHub; they catch edits that would silently weaken the setup."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

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


def test_update_runs_every_three_hours_and_never_overlaps(update):
    assert [s["cron"] for s in update["on"]["schedule"]] == ["23 */3 * * *"]
    assert "workflow_dispatch" in update["on"]
    assert update["concurrency"] == {"group": "shots-update", "cancel-in-progress": False}


def test_permissions_are_minimal_per_job(update, ci):
    assert update["permissions"] == {}  # nothing by default; each job states what it needs
    jobs = update["jobs"]
    assert jobs["update"]["permissions"] == {"contents": "write", "actions": "write"}
    assert jobs["build"]["permissions"] == {"contents": "read"}  # npm install / build never sees a write token
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["deploy"]["environment"]["name"] == "github-pages"
    assert ci["permissions"] == {"contents": "read"} and all("permissions" not in j for j in ci["jobs"].values())


@pytest.mark.parametrize("name", ["update.yml", "ci.yml"])
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
    order = ["Restore state (database, derived files)", "Guard the live record", "Run the update cycle",
             "Check the database", "Save state", "Export JSON", "Commit the data branch (only when something changed)",
             "Export JSON for the site (fresh timestamp)", "Hand the JSON to the site build",
             "Fail the run if the update cycle failed"]
    assert [n for n in names if n in order] == order
    by = {s["name"]: s for s in update["jobs"]["update"]["steps"]}
    assert by["Run the update cycle"]["run"] == "python -m shots.worker run-once --force"
    assert by["Run the update cycle"]["continue-on-error"] is True
    assert "steps.dbcheck.outcome == 'success'" in by["Save state"]["if"]
    assert "shots.export.verify" in by["Export JSON"]["run"]  # the independent check runs on every export
    assert "allow_fresh_state" in str(update["on"]["workflow_dispatch"]["inputs"])
    assert "exit 1" in by["Guard the live record"]["run"] and "gh cache list" in by["Guard the live record"]["run"]


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
    assert "git add -- README.md data/meta.json data/board.json data/results.json data/track.json" in commit["run"]
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
