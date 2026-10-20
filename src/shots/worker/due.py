"""Should this worker cycle run the update, or is everything fresh? Decided from the DB.

A cycle runs the update when (first match wins, all reasons are reported):
  forced     --force
  never      no successful update recorded yet
  no-board   the DB has no board prediction run
  interval   the last successful update started at least `min_interval` ago (default 2 h)
otherwise only when something is due since the last successful update started (`since`):
  results    scheduled matches finished since then (kickoff_utc + 2 h; unknown kick-off: end of the
             match date, UTC), so their results can be settled and they leave the board
  odds       a football-data odds publication moment (Tue / Fri at 09:00) passed and fixtures in the
             next 7 days still lack odds. The zone of "09:00" is SHOTS_ODDS_TZ (an IANA name such as
             "UTC" or "Europe/London"); unset = this machine's zone
  new-day    the local date changed: the board window rolls forward a day (this machine's zone; the
             GitHub runner is UTC, which is also the day boundary of the static export)
  calendars  a season calendar (calendars/*.ics) changed: its content differs from the fingerprint
             the last successful worker cycle recorded (job_runs.counts.calendars). Without such a
             record the file modification times decide, which is only meaningful on a machine that
             keeps its files (a fresh git checkout stamps every file with the checkout time)
  manual-results  the hand-entered results (results_manual/*.csv) differ from those the last successful
             update loaded (its counts["db-manual"].fingerprint); without such a record, only when a file
             holds a data row
  provisional-results  the same for the provisional results of football-data leagues
             (results_provisional/*.csv; counts["db-provisional"].fingerprint)
Only `ok` updates count as successful: after a partial or failed update every cycle retries.

`min_gap` (default 0 = off; SHOTS_WORKER_MIN_GAP_MINUTES) is a floor between two update attempts of any
outcome: when the last attempt started less than `min_gap` ago the cycle is skipped (`recent`) whatever
is due, except `forced`, `never` and `no-board`. It bounds how often a frequently triggered worker
(the hourly GitHub schedule) rebuilds the board on a match day or retries a failing source.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select, text

from shots.db import jobs
from shots.db.models import JobRun

MATCH_LENGTH = timedelta(hours=2)  # kick-off to final whistle, with a margin
DEFAULT_MIN_INTERVAL = timedelta(hours=2)
DEFAULT_MIN_GAP = timedelta(0)  # no floor between attempts unless configured
ODDS_WEEKDAYS = (1, 4)  # Tuesday, Friday: football-data.co.uk publishes fixtures.csv with odds
ODDS_HOUR = 9  # hour (in the odds zone) by which the odds are expected (the old scheduled scan ran then)
ODDS_LOOKAHEAD_DAYS = 7

_END = ("COALESCE(datetime(kickoff_utc, '+{m} minutes'), datetime(date, '+1 day'))"
        .format(m=int(MATCH_LENGTH.total_seconds() // 60)))


@dataclass
class Reason:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class Decision:
    run: bool
    reasons: list[Reason] = field(default_factory=list)
    last_ok: dict | None = None  # last successful update job_run
    now: datetime | None = None  # naive UTC

    def summary(self) -> str:
        if self.run:
            return "run: " + "; ".join(str(r) for r in self.reasons)
        return "skip: " + ("; ".join(str(r) for r in self.reasons) or "nothing due")


# ----------------------------------------------------------------------------- settings

def env_min_interval() -> timedelta:
    """SHOTS_WORKER_MIN_INTERVAL_HOURS as a timedelta (unset / empty: 2 h)."""
    raw = os.environ.get("SHOTS_WORKER_MIN_INTERVAL_HOURS")
    return timedelta(hours=float(raw)) if raw else DEFAULT_MIN_INTERVAL


def env_min_gap() -> timedelta:
    """SHOTS_WORKER_MIN_GAP_MINUTES as a timedelta (unset / empty: no floor)."""
    raw = os.environ.get("SHOTS_WORKER_MIN_GAP_MINUTES")
    return timedelta(minutes=float(raw)) if raw else DEFAULT_MIN_GAP


def env_odds_tz() -> tzinfo | None:
    """SHOTS_ODDS_TZ as a zone (unset / empty: None = this machine's zone)."""
    raw = os.environ.get("SHOTS_ODDS_TZ")
    return ZoneInfo(raw) if raw else None


# ----------------------------------------------------------------------------- time helpers

def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def to_local(utc_naive: datetime, tz: tzinfo | None = None) -> datetime:
    """Naive UTC -> naive wall clock in tz (None = this machine's zone)."""
    aware = utc_naive.replace(tzinfo=timezone.utc)
    return (aware.astimezone(tz) if tz else aware.astimezone()).replace(tzinfo=None)


def to_utc(local_naive: datetime, tz: tzinfo | None = None) -> datetime:
    aware = local_naive.replace(tzinfo=tz) if tz else local_naive.astimezone()
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def odds_publications(start_local: date, days: int, weekdays=ODDS_WEEKDAYS, hour: int = ODDS_HOUR):
    """Odds publication moments (naive local) on the `days` dates from start_local."""
    for i in range(days):
        d = start_local + timedelta(days=i)
        if d.weekday() in weekdays:
            yield datetime.combine(d, time(hour, 0))


def last_odds_publication(now: datetime, tz: tzinfo | None = None, weekdays=ODDS_WEEKDAYS,
                          hour: int = ODDS_HOUR) -> datetime | None:
    """Latest publication moment <= now (naive UTC in and out)."""
    local = to_local(now, tz)
    moments = [m for m in odds_publications(local.date() - timedelta(days=7), 8, weekdays, hour) if m <= local]
    return to_utc(moments[-1], tz) if moments else None


def next_odds_publication(now: datetime, tz: tzinfo | None = None, weekdays=ODDS_WEEKDAYS,
                          hour: int = ODDS_HOUR) -> datetime | None:
    local = to_local(now, tz)
    moments = [m for m in odds_publications(local.date(), 8, weekdays, hour) if m > local]
    return to_utc(moments[0], tz) if moments else None


# ----------------------------------------------------------------------------- DB checks

def finished_since(engine: Engine, since: datetime, now: datetime) -> int:
    """Scheduled matches whose expected end is in (since, now]."""
    sql = text(f"SELECT count(*) FROM matches WHERE status = 'scheduled' AND {_END} > :since AND {_END} <= :now")
    with engine.connect() as c:
        return int(c.execute(sql, {"since": _fmt(since), "now": _fmt(now)}).scalar_one())


def next_match_end(engine: Engine, now: datetime) -> datetime | None:
    sql = text(f"SELECT min({_END}) FROM matches WHERE status = 'scheduled' AND {_END} > :now")
    with engine.connect() as c:
        v = c.execute(sql, {"now": _fmt(now)}).scalar_one()
    return datetime.fromisoformat(v) if v else None


def fixtures_without_odds(engine: Engine, today: date, days: int = ODDS_LOOKAHEAD_DAYS) -> int:
    sql = text("SELECT count(*) FROM matches WHERE status = 'scheduled' AND odds_over25 IS NULL "
               "AND date >= :d0 AND date <= :d1")
    with engine.connect() as c:
        return int(c.execute(sql, {"d0": today.isoformat(), "d1": (today + timedelta(days=days)).isoformat()}).scalar_one())


def has_board(engine: Engine) -> bool:
    with engine.connect() as c:
        return c.execute(text("SELECT 1 FROM prediction_runs WHERE kind = 'board' LIMIT 1")).first() is not None


def _cal_dir(cal_dir: Path | None) -> Path:
    if cal_dir is None:
        from shots.ingest.calendar_ics import CAL_DIR as cal_dir
    return Path(cal_dir)


def calendars_changed_since(since: datetime, cal_dir: Path | None = None) -> list[str]:
    """Calendar files modified after `since` (naive UTC)."""
    cut = since.replace(tzinfo=timezone.utc).timestamp()
    return sorted(p.name for p in _cal_dir(cal_dir).glob("*.ics") if p.stat().st_mtime > cut)


def calendars_fingerprint(cal_dir: Path | None = None) -> str:
    """Short hash of the names and contents of calendars/*.ics: the same on every machine and after
    every checkout, unlike modification times. Line endings are normalised (a Windows checkout may
    convert them)."""
    h = hashlib.sha256()
    for p in sorted(_cal_dir(cal_dir).glob("*.ics"), key=lambda f: f.name):
        body = p.read_bytes().replace(b"\r\n", b"\n")
        h.update(p.name.encode("utf-8") + b"\0" + str(len(body)).encode() + b"\0" + body)
    return h.hexdigest()[:16]


def recorded_calendars(engine: Engine) -> str | None:
    """Calendar fingerprint stored by the last successful worker cycle that ran an update, or None."""
    q = (select(JobRun.counts).where(JobRun.job == "worker", JobRun.status == "ok")
         .order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(20))
    with engine.connect() as c:
        for (counts,) in c.execute(q):
            if isinstance(counts, dict) and counts.get("action") == "update" and counts.get("calendars"):
                return str(counts["calendars"])
    return None


def recorded_manual(engine: Engine, step_name: str = "db-manual") -> str | None:
    """Fingerprint of the manual (step_name "db-manual") or provisional ("db-provisional") results loaded
    by the last successful update, or None."""
    last_ok = jobs.last(engine, "update", "ok")
    counts = (last_ok or {}).get("counts") or {}
    step = counts.get(step_name) if isinstance(counts, dict) else None
    return str(step["fingerprint"]) if isinstance(step, dict) and step.get("fingerprint") else None


def manual_results_changed(engine: Engine, manual_dir: Path | None = None) -> bool:
    from shots.ingest import manual_results as mr

    try:
        current = mr.fingerprint(manual_dir)
        recorded = recorded_manual(engine)
        return current != recorded and (recorded is not None or mr.has_rows(manual_dir))
    except OSError:
        return False


def provisional_results_changed(engine: Engine, provisional_dir: Path | None = None) -> bool:
    from shots.ingest import provisional_results as pr

    try:
        current = pr.fingerprint(provisional_dir)
        recorded = recorded_manual(engine, "db-provisional")
        return current != recorded and (recorded is not None or pr.has_rows(provisional_dir))
    except OSError:
        return False


# ----------------------------------------------------------------------------- decision

def due_work(engine: Engine, since: datetime, now: datetime, tz: tzinfo | None = None,
             cal_dir: Path | None = None, odds_tz: tzinfo | None = None,
             manual_dir: Path | None = None, provisional_dir: Path | None = None) -> list[Reason]:
    """Everything that became due between `since` and `now` (naive UTC). `tz` is the zone of the day
    boundary; `odds_tz` the zone of the odds publication hour (default: SHOTS_ODDS_TZ, else `tz`)."""
    odds_tz = odds_tz or env_odds_tz() or tz
    out: list[Reason] = []
    if not has_board(engine):
        out.append(Reason("no-board", "the database has no board yet"))
    n = finished_since(engine, since, now)
    if n:
        out.append(Reason("results", f"{n} match(es) finished since the last update"))
    pub = last_odds_publication(now, odds_tz)
    if pub is not None and pub > since:
        today = to_local(now, tz).date()
        missing = fixtures_without_odds(engine, today)
        if missing:
            out.append(Reason("odds", f"odds published {to_local(pub, odds_tz):%a %H:%M}; {missing} fixture(s) "
                                      f"in the next {ODDS_LOOKAHEAD_DAYS} days lack odds"))
    if to_local(since, tz).date() < to_local(now, tz).date():
        out.append(Reason("new-day", "a new day started: the board window rolls forward"))
    recorded = recorded_calendars(engine)
    if recorded is None:  # no cycle has recorded a fingerprint yet: modification times decide
        changed = calendars_changed_since(since, cal_dir)
        if changed:
            out.append(Reason("calendars", f"{len(changed)} calendar file(s) changed"))
    elif recorded != calendars_fingerprint(cal_dir):
        out.append(Reason("calendars", "the season calendars changed since the last worker update"))
    if manual_results_changed(engine, manual_dir):
        out.append(Reason("manual-results", "the manual results (results_manual/) changed since the last update"))
    if provisional_results_changed(engine, provisional_dir):
        out.append(Reason("provisional-results",
                          "the provisional results (results_provisional/) changed since the last update"))
    return out


def decide(engine: Engine, now: datetime | None = None, min_interval: timedelta = DEFAULT_MIN_INTERVAL,
           force: bool = False, tz: tzinfo | None = None, cal_dir: Path | None = None,
           min_gap: timedelta = DEFAULT_MIN_GAP, odds_tz: tzinfo | None = None,
           manual_dir: Path | None = None, provisional_dir: Path | None = None) -> Decision:
    now = now or jobs.utcnow()
    last_ok = jobs.last(engine, "update", "ok")
    d = Decision(run=True, last_ok=last_ok, now=now)
    if force:
        d.reasons.append(Reason("forced", "--force"))
        return d
    if last_ok is None:
        d.reasons.append(Reason("never", "no successful update recorded"))
        return d
    since = last_ok["started_at"]
    age = now - since
    if not has_board(engine):
        d.reasons.append(Reason("no-board", "the database has no board yet"))
        return d
    attempt = jobs.last(engine, "update")  # any outcome: a failed attempt minutes ago also counts
    if min_gap and attempt is not None and timedelta(0) <= now - attempt["started_at"] < min_gap:
        d.run = False
        d.reasons = [Reason("recent", f"last update attempt ({attempt['status']}) started "
                                      f"{(now - attempt['started_at']).total_seconds() / 60:.0f} min ago "
                                      f"(minimum gap {min_gap.total_seconds() / 60:g} min)")]
        return d
    if age >= min_interval:
        d.reasons.append(Reason("interval", f"last successful update {age.total_seconds() / 3600:.1f} h ago "
                                            f"(minimum interval {min_interval.total_seconds() / 3600:g} h)"))
        return d
    d.reasons = due_work(engine, since, now, tz, cal_dir, odds_tz, manual_dir, provisional_dir)
    d.run = bool(d.reasons)
    if not d.run:
        d.reasons = [Reason("fresh", f"last successful update {age.total_seconds() / 60:.0f} min ago and "
                                     "nothing due")]
    return d
