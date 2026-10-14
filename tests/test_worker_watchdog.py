"""The watchdog's decision (shots.worker.watchdog): start the update workflow when GitHub's scheduler
skipped it, never pile runs up. The script is run by .github/workflows/watchdog.yml as a plain file."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shots.worker import watchdog

NOW = datetime(2026, 10, 14, 15, 43)
SCRIPT = Path(watchdog.__file__)


def run(minutes_ago, event="schedule", status="completed", conclusion="success"):
    """One entry of `gh run list --json status,conclusion,createdAt,event`."""
    created = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"status": status, "conclusion": conclusion, "createdAt": created, "event": event}


def built(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_healthy_schedule_is_left_alone():
    d = watchdog.decide([run(26), run(90), run(146)], built(2), NOW)
    assert d == {"dispatch": False, "force": False, "reason": "last run created 26 min ago; site built 2.0 h ago"}
    # a skipped (no-op) or failed scheduled run still proves that the scheduler fired
    assert not watchdog.decide([run(26, conclusion="failure")], built(2), NOW)["dispatch"]


def test_missed_slot_is_replaced_by_an_unforced_run():
    d = watchdog.decide([run(86), run(146)], built(2), NOW)
    assert d["dispatch"] and not d["force"] and "missed a slot" in d["reason"] and "86 min" in d["reason"]
    # exactly today's situation: only two manual runs hours ago, the schedule never fired
    d = watchdog.decide([run(48, "workflow_dispatch"), run(430, "workflow_dispatch")], built(0.8), NOW)
    assert not d["dispatch"]
    d = watchdog.decide([run(108, "workflow_dispatch"), run(490, "workflow_dispatch")], built(1.8), NOW)
    assert d == {"dispatch": True, "force": False, "reason": d["reason"]}
    assert watchdog.decide([], None, NOW)["dispatch"] and "no run found" in watchdog.decide([], None, NOW)["reason"]


def test_watchdog_alone_keeps_an_hourly_rhythm():
    """If update.yml's own schedule never fires, the watchdog's dispatches (one hour apart) are never
    counted as 'recent enough' to skip the next one."""
    runs, started = [], []
    for hour in range(6):
        now = NOW + timedelta(hours=hour)
        d = watchdog.decide(runs, None, now)
        if d["dispatch"]:
            started.append(hour)
            runs.insert(0, {"status": "completed", "conclusion": "success", "event": "workflow_dispatch",
                            "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    assert started == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending", "waiting", "requested"])
def test_nothing_is_started_while_a_run_is_active(status):
    d = watchdog.decide([run(70, status=status, conclusion=None), run(200)], built(30), NOW)
    assert not d["dispatch"] and "active" in d["reason"]


def test_a_stuck_active_run_does_not_block_forever():
    d = watchdog.decide([run(4 * 60, status="in_progress", conclusion=None)], built(2), NOW)
    assert d["dispatch"] and not d["force"]


def test_stale_site_despite_runs_gets_one_forced_run_then_waits():
    d = watchdog.decide([run(26), run(90)], built(13), NOW)
    assert d["dispatch"] and d["force"] and "13.0 h" in d["reason"]
    d = watchdog.decide([run(26), run(60, "workflow_dispatch")], built(13), NOW)  # already tried recently
    assert not d["dispatch"] and "waiting" in d["reason"]
    d = watchdog.decide([run(26), run(7 * 60, "workflow_dispatch")], built(20), NOW)  # cooldown over
    assert d["dispatch"] and d["force"]
    assert not watchdog.decide([run(26)], built(11.9), NOW)["dispatch"]
    # at most four forced runs a day, and the limit is at least twice the publishing heartbeat
    from shots.worker import ci
    assert watchdog.FORCED_COOLDOWN == timedelta(hours=6) and watchdog.MAX_SITE_AGE >= 2 * ci.HEARTBEAT


def test_unknown_site_age_never_forces():
    for value in (None, "", "garbage", 12345):
        assert watchdog.decide([run(26)], value, NOW) == {
            "dispatch": False, "force": False, "reason": "last run created 26 min ago; site age unknown"}
    d = watchdog.decide([run(86)], "garbage", NOW)
    assert d["dispatch"] and not d["force"]


def test_missed_slot_with_a_stale_site_stays_unforced():
    """Forcing is reserved for 'runs happen but nothing gets published'; a dead scheduler only needs runs."""
    d = watchdog.decide([run(300)], built(30), NOW)
    assert d["dispatch"] and not d["force"]


def test_bad_entries_are_ignored_and_times_are_parsed():
    runs = [{"status": "completed"}, "junk", {"createdAt": "not a date"}, run(26)]
    assert not watchdog.decide(runs, built(1), NOW)["dispatch"]
    assert watchdog.parse_time("2026-10-13T14:56:53Z") == datetime(2026, 10, 13, 14, 56, 53)
    assert watchdog.parse_time("2026-10-13T16:56:53+02:00") == datetime(2026, 10, 13, 14, 56, 53)
    assert watchdog.parse_time(None) is None


def test_limits_fit_the_schedules():
    # update.yml: a slot every hour (:13 / :17); the watchdog runs at :43, 26-30 min after a slot.
    assert timedelta(minutes=30) < watchdog.MAX_RUN_AGE < timedelta(minutes=56)


def cli(tmp_path, runs, meta=None, raw=None):
    """Run the file exactly as the workflow does: by path, in isolated mode, without the package."""
    rp = tmp_path / "runs.json"
    rp.write_text(raw if raw is not None else json.dumps(runs), encoding="utf-8")
    args = [sys.executable, "-I", str(SCRIPT), "--runs", str(rp)]
    if meta is not None:
        mp = tmp_path / "meta.json"
        mp.write_text(meta, encoding="utf-8")
        args += ["--site-meta", str(mp)]
    else:
        args += ["--site-meta", str(tmp_path / "absent.json")]  # curl failed: the file is not there
    return subprocess.run(args, capture_output=True, text=True, cwd=tmp_path, timeout=60)


def stamp(delta):
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_cli_output_is_github_output_format(tmp_path):
    fresh = {"status": "completed", "conclusion": "success", "event": "schedule", "createdAt": stamp(timedelta(minutes=5))}
    old = dict(fresh, createdAt=stamp(timedelta(hours=5)))
    r = cli(tmp_path, [fresh], json.dumps({"built_at": stamp(timedelta(hours=1))}))
    assert r.returncode == 0, r.stderr
    lines = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert list(lines) == ["dispatch", "force", "reason"] and lines["dispatch"] == "false" and lines["force"] == "false"
    r = cli(tmp_path, [old])  # no site file
    assert r.returncode == 0 and "dispatch=true\nforce=false\n" in r.stdout
    r = cli(tmp_path, [fresh], json.dumps({"built_at": stamp(timedelta(hours=40))}))
    assert "dispatch=true\nforce=true\n" in r.stdout
    r = cli(tmp_path, [fresh], "<html>404</html>")  # the site answered with something else
    assert r.returncode == 0 and "dispatch=false" in r.stdout and "site age unknown" in r.stdout


def test_cli_fails_closed_when_the_run_list_is_unreadable(tmp_path):
    for raw in ("", "not json", '{"message": "Bad credentials"}'):
        r = cli(tmp_path, None, raw=raw)
        assert r.returncode == 1 and "dispatch" not in r.stdout and "not dispatching" in r.stderr
