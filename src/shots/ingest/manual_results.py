"""Manual results: human-entered final shot counts for competitions without an automatic source.

football-data.co.uk has no UEFA Champions League. Its results are typed by hand into a committed CSV,
one file per competition and season:

    results_manual/<competition prefix>_<season>.csv      e.g. uefa-champions-league_2026-27.csv

The prefix is the calendar prefix of a cross-border competition (shots.ingest.cross_border.COMPETITIONS);
anything else is rejected, so a manual file can never describe a domestic (football-data) league.

Columns (header required, in any order, all present; optional columns may be left empty):

    date        YYYY-MM-DD, the match date as in our fixtures (venue-local: the home club's time zone)
    home, away  team names as spelled in the season calendar (.ics) or our canonical names; both go through
                team_aliases.csv (source "calendar"), exactly like the calendar ingest
    home_shots, away_shots   total shots (on + off target, blocked included), integers 0..60
    home_sot, away_sot       shots on target (optional; both or neither), 0 <= sot <= shots
    home_goals, away_goals   integers 0..20
    source      URL (http/https) of the page the numbers were read from
    entered_at  ISO 8601 UTC time the row was entered, e.g. 2026-10-14T21:05:00Z
    notes       optional free text (e.g. "provider counts blocked shots separately")

This module checks a file on its own (format, ranges, duplicates) and computes each row's canonical
match_id. shots.db.manual resolves the rows against the database (unknown match -> close candidates,
kick-off before entered_at, competition without an automatic source) and loads them.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from shots.ingest import cross_border as xb
from shots.ingest import football_data as fd
from shots.ingest.schema import make_match_id, normalize_team

MANUAL_DIR = fd.ROOT / "results_manual"
COMMITTED_DIR = MANUAL_DIR  # the repository folder (tests redirect MANUAL_DIR to an empty one)
ALIAS_SOURCE = "calendar"  # names are entered as the season calendars spell them
COLUMNS = ("date", "home", "away", "home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals",
           "source", "entered_at", "notes")
OPTIONAL = frozenset({"home_sot", "away_sot", "notes"})
MAX_TEAM_SHOTS = 60  # no professional match comes close; a larger number is a typo
MAX_GOALS = 20
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT = re.compile(r"^\d+$")
_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
_SEASON = re.compile(r"^(\d{4})-(\d{2})$")


class ManualResultsError(ValueError):
    """One or more problems in the manual results; `problems` lists them ("<file>:<line>: <what>")."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        head = f"{len(self.problems)} problem(s) in the manual results"
        super().__init__(head + ":\n  " + "\n  ".join(self.problems))


@dataclass
class ManualFile:
    path: Path
    competition: xb.CrossBorder
    season_start: int
    rows: pd.DataFrame  # one row per CSV data row; see read_file


def files(directory: Path | None = None) -> list[Path]:
    d = Path(directory or MANUAL_DIR)
    return sorted(d.glob("*.csv")) if d.exists() else []


def fingerprint(directory: Path | None = None) -> str:
    """Short hash of the names and contents of the manual results files (line endings normalised), so
    the worker can tell whether they changed since the last update."""
    h = hashlib.sha256()
    for p in files(directory):
        body = p.read_bytes().replace(b"\r\n", b"\n")
        h.update(p.name.encode("utf-8") + b"\0" + str(len(body)).encode() + b"\0" + body)
    return h.hexdigest()[:16]


def has_rows(directory: Path | None = None) -> bool:
    """Whether any manual results file holds a data row (not just its header)."""
    for p in files(directory):
        lines = [x for x in p.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        if len(lines) > 1:
            return True
    return False


def competition_for(path: Path) -> tuple[xb.CrossBorder, int]:
    """'uefa-champions-league_2026-27.csv' -> (the competition, 2026). ValueError if not a manual file."""
    prefix, sep, season = Path(path).stem.partition("_")
    m = _SEASON.match(season)
    if not sep or not m:
        raise ValueError(f"{Path(path).name}: file name must be <competition>_<YYYY-YY>.csv")
    start, end = int(m.group(1)), int(m.group(2))
    if (start + 1) % 100 != end:
        raise ValueError(f"{Path(path).name}: season {season} is not two consecutive years")
    comp = xb.COMPETITIONS.get(prefix)
    if comp is None:
        raise ValueError(f"{Path(path).name}: {prefix!r} is not a competition without an automatic results source "
                         f"(known: {', '.join(sorted(xb.COMPETITIONS))})")
    return comp, start


def parse_utc(text: str) -> datetime | None:
    """'2026-10-14T21:05:00Z' (or +00:00) -> naive UTC; None if not an ISO time with a UTC offset."""
    try:
        ts = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if ts.tzinfo is None or ts.utcoffset() != timezone.utc.utcoffset(None):
        return None
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def _count(raw: str, what: str, hi: int, problems: list[str], where: str) -> int | None:
    if not _INT.match(raw):
        problems.append(f"{where}: {what} must be a whole number >= 0, got {raw!r}")
        return None
    value = int(raw)
    if value > hi:
        problems.append(f"{where}: {what} = {value} is not plausible (maximum {hi})")
        return None
    return value


def read_file(path: Path) -> ManualFile:
    """Parse and check one file. Raises ManualResultsError listing every problem found.
    rows columns: line, date (datetime.date), home, away (as entered), home_team, away_team (normalised),
    match_id, home_shots, away_shots, home_sot, away_sot (None if empty), home_goals, away_goals,
    source_url, entered_at (naive UTC), notes (None if empty)."""
    path = Path(path)
    try:
        comp, season = competition_for(path)
    except ValueError as exc:
        raise ManualResultsError([str(exc)]) from None
    problems: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            raise ManualResultsError([f"{path.name}: empty file (the header line is required)"])
        header = [h.strip() for h in header]
        missing = [c for c in COLUMNS if c not in header]
        unknown = [c for c in header if c not in COLUMNS]
        dup = sorted({c for c in header if header.count(c) > 1})
        if missing or unknown or dup:
            raise ManualResultsError([f"{path.name}:1: header must have the columns {', '.join(COLUMNS)}"
                                      + (f"; missing {missing}" if missing else "")
                                      + (f"; unknown {unknown}" if unknown else "")
                                      + (f"; repeated {dup}" if dup else "")])
        raw_rows = [(i, rec) for i, rec in enumerate(reader, start=2) if any(x.strip() for x in rec)]
    out = []
    for line, rec in raw_rows:
        where = f"{path.name}:{line}"
        if len(rec) != len(header):
            problems.append(f"{where}: {len(rec)} fields, the header has {len(header)}")
            continue
        r = {k: v.strip() for k, v in zip(header, rec)}
        n_before = len(problems)
        for c in COLUMNS:
            if c not in OPTIONAL and not r[c]:
                problems.append(f"{where}: {c} is required")
        if len(problems) > n_before:
            continue
        day = None
        if not _DATE.match(r["date"]):
            problems.append(f"{where}: date must be YYYY-MM-DD, got {r['date']!r}")
        else:
            try:
                day = date.fromisoformat(r["date"])
            except ValueError:
                problems.append(f"{where}: {r['date']!r} is not a calendar date")
        vals = {c: _count(r[c], c, MAX_TEAM_SHOTS, problems, where) for c in ("home_shots", "away_shots")}
        vals |= {c: _count(r[c], c, MAX_GOALS, problems, where) for c in ("home_goals", "away_goals")}
        if bool(r["home_sot"]) != bool(r["away_sot"]):
            problems.append(f"{where}: give both home_sot and away_sot, or neither")
        for side in ("home", "away"):
            sot = None
            if r[f"{side}_sot"]:
                sot = _count(r[f"{side}_sot"], f"{side}_sot", MAX_TEAM_SHOTS, problems, where)
                shots = vals[f"{side}_shots"]
                if sot is not None and shots is not None and sot > shots:
                    problems.append(f"{where}: {side}_sot ({sot}) is larger than {side}_shots ({shots})")
            vals[f"{side}_sot"] = sot
        if not _URL.match(r["source"]):
            problems.append(f"{where}: source must be the http(s) URL the numbers were read from, got {r['source']!r}")
        entered = parse_utc(r["entered_at"])
        if entered is None:
            problems.append(f"{where}: entered_at must be an ISO 8601 UTC time such as 2026-10-14T21:05:00Z, "
                            f"got {r['entered_at']!r}")
        home_team, away_team = normalize_team(ALIAS_SOURCE, r["home"]), normalize_team(ALIAS_SOURCE, r["away"])
        if home_team == away_team:
            problems.append(f"{where}: home and away are the same team ({home_team})")
        if len(problems) > n_before:
            continue
        out.append({"line": line, "date": day, "home": r["home"], "away": r["away"], "home_team": home_team,
                    "away_team": away_team,
                    "match_id": make_match_id(comp.name, pd.Timestamp(day), home_team, away_team),
                    **vals, "source_url": r["source"], "entered_at": entered, "notes": r["notes"] or None})
    seen: dict[str, int] = {}
    playing: dict[tuple[date, str], int] = {}
    for r in out:
        where = f"{path.name}:{r['line']}"
        if r["match_id"] in seen:
            problems.append(f"{where}: duplicate of line {seen[r['match_id']]} "
                            f"({r['date']} {r['home_team']} v {r['away_team']})")
            continue
        seen[r["match_id"]] = r["line"]
        for team in (r["home_team"], r["away_team"]):
            other = playing.setdefault((r["date"], team), r["line"])
            if other != r["line"]:
                problems.append(f"{where}: {team} already plays on {r['date']} (line {other})")
    if problems:
        raise ManualResultsError(problems)
    cols = ["line", "date", "home", "away", "home_team", "away_team", "match_id", "home_shots", "away_shots",
            "home_sot", "away_sot", "home_goals", "away_goals", "source_url", "entered_at", "notes"]
    return ManualFile(path=path, competition=comp, season_start=season,
                      rows=pd.DataFrame(out, columns=cols).astype(object))
