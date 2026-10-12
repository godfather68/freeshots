"""Should this worker cycle run the update, or is everything fresh? Decided from the DB.

A cycle runs the update when (first match wins, all reasons are reported):
  never      no successful update recorded yet
  interval   the last successful update started at least `min_interval` ago (default 2 h)
  forced     --force
otherwise only when something is due since the last successful update started (`since`):
  no-board   the DB has no board prediction run
  results    scheduled matches finished since then (kickoff_utc + 2 h; unknown kick-off: end of the
             match date, UTC), so their results can be settled and they leave the board
  odds       a football-data odds publication moment (Tue / Fri at 09:00 local by default) passed
             and fixtures in the next 7 days still lack odds
  new-day    the local date changed: the board window rolls forward a day
  calendars  a season calendar (calendars/*.ics) changed
Only `ok` updates count as successful: after a partial or failed update every cycle retries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

from sqlalchemy import Engine, text

from shots.db import jobs

MATCH_LENGTH = timedelta(hours=2)  # kick-off to final whistle, with a margin
DEFAULT_MIN_INTERVAL = timedelta(hours=2)
ODDS_WEEKDAYS = (1, 4)  # Tuesday, Friday: football-data.co.uk publishes fixtures.csv with odds
ODDS_HOUR = 9  # local time by which the odds are expected (the old scheduled scan ran then)
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


def calendars_changed_since(since: datetime, cal_dir: Path | None = None) -> list[str]:
    """Calendar files modified after `since` (naive UTC)."""
    if cal_dir is None:
        from shots.ingest.calendar_ics import CAL_DIR as cal_dir
    cut = since.replace(tzinfo=timezone.utc).timestamp()
    return sorted(p.name for p in Path(cal_dir).glob("*.ics") if p.stat().st_mtime > cut)


# ----------------------------------------------------------------------------- decision

def due_work(engine: Engine, since: datetime, now: datetime, tz: tzinfo | None = None,
             cal_dir: Path | None = None) -> list[Reason]:
    """Everything that became due between `since` and `now` (naive UTC)."""
    out: list[Reason] = []
    if not has_board(engine):
        out.append(Reason("no-board", "the database has no board yet"))
    n = finished_since(engine, since, now)
    if n:
        out.append(Reason("results", f"{n} match(es) finished since the last update"))
    pub = last_odds_publication(now, tz)
    if pub is not None and pub > since:
        today = to_local(now, tz).date()
        missing = fixtures_without_odds(engine, today)
        if missing:
            out.append(Reason("odds", f"odds published {to_local(pub, tz):%a %H:%M}; {missing} fixture(s) "
                                      f"in the next {ODDS_LOOKAHEAD_DAYS} days lack odds"))
    if to_local(since, tz).date() < to_local(now, tz).date():
        out.append(Reason("new-day", "a new day started: the board window rolls forward"))
    changed = calendars_changed_since(since, cal_dir)
    if changed:
        out.append(Reason("calendars", f"{len(changed)} calendar file(s) changed"))
    return out


def decide(engine: Engine, now: datetime | None = None, min_interval: timedelta = DEFAULT_MIN_INTERVAL,
           force: bool = False, tz: tzinfo | None = None, cal_dir: Path | None = None) -> Decision:
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
    if age >= min_interval:
        d.reasons.append(Reason("interval", f"last successful update {age.total_seconds() / 3600:.1f} h ago "
                                            f"(minimum interval {min_interval.total_seconds() / 3600:g} h)"))
        return d
    d.reasons = due_work(engine, since, now, tz, cal_dir)
    d.run = bool(d.reasons)
    if not d.run:
        d.reasons = [Reason("fresh", f"last successful update {age.total_seconds() / 60:.0f} min ago and "
                                     "nothing due")]
    return d
