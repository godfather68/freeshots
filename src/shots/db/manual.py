"""Load manual results (results_manual/*.csv, shots.ingest.manual_results) into the database.

    load_manual_results(engine, directory=None, now=None)   validate, resolve and upsert; audit every change
    check_manual_results(engine, directory=None, now=None)  the same checks, nothing written (dry run)

A row is resolved to an existing match of its competition by canonical match_id (competition, date,
alias-normalised home and away). It must be a match of a competition without an automatic results source
(no football-data division code; the match not from football-data) that has finished by `entered_at`
(kick-off + MATCH_MIN_LENGTH; kick-off = matches.kickoff_utc, else 00:00 UTC of the match date), and
`entered_at` must not be in the future. Any problem rejects the whole file (nothing of it is written) and
is reported with the file and line, an unknown match with its closest candidates.

Writing (one transaction per file):
- new row: manual_results row + the match becomes status 'played', source 'manual', with the shot / goal
  columns; audit action 'insert'.
- changed row (a correction): both updated; audit action 'update' with {column: [old, new]} of the
  changed columns only.
- row removed from the file: the match goes back to its previous source / status with empty result
  columns, the manual_results row is deleted; audit action 'delete'.
Outcomes are views over `matches`, so a correction or removal re-settles every prediction of the match.
"""

from __future__ import annotations

import difflib
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import Engine, or_, select, update

from shots.db.engine import session_scope
from shots.db.models import Competition, ManualResult, ManualResultEdit, Match, utcnow
from shots.db.text import fold
from shots.ingest import manual_results as mr
from shots.ingest.schema import make_match_id

log = logging.getLogger(__name__)

MATCH_MIN_LENGTH = timedelta(minutes=105)  # two halves and the interval: no result exists earlier
FUTURE_TOLERANCE = timedelta(minutes=10)  # clock skew between the person entering a row and this machine
VALUE_COLS = ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals")
AUDIT_COLS = (*VALUE_COLS, "source_url", "entered_at", "notes")
N_CANDIDATES = 3


def _candidates(row: dict, matches: list[dict]) -> str:
    """The closest matches of the competition to a row that matched none: by team names, then date."""
    target = fold(f"{row['home_team']} | {row['away_team']}")

    def score(m):
        names = difflib.SequenceMatcher(None, target, fold(f"{m['home_team']} | {m['away_team']}")).ratio()
        days = abs((m["date"] - row["date"]).days)
        return names - min(days, 30) / 60  # a day off costs about as much as one differing letter

    best = sorted(matches, key=score, reverse=True)[:N_CANDIDATES]
    if not best:
        return "no match of this competition is in the database"
    return "closest: " + "; ".join(f"{m['date']} {m['home_team']} v {m['away_team']} [{m['match_id']}]" for m in best)


def _resolve(s, f: mr.ManualFile, now: datetime) -> tuple[list[dict], list[str], int | None]:
    """-> (rows with their match, problems, competition id)."""
    comp = s.execute(select(Competition.id, Competition.code).where(Competition.name == f.competition.name)).first()
    name = f.path.name
    if comp is None:
        return [], [f"{name}: competition {f.competition.name!r} has no fixtures in the database "
                    f"(load its calendar first)"] if len(f.rows) else [], None
    if comp.code is not None:
        return [], [f"{name}: {f.competition.name} has an automatic results source ({comp.code}); "
                    f"manual results are refused"], comp.id
    cols = (Match.match_id, Match.date, Match.home_team, Match.away_team, Match.kickoff_utc, Match.status,
            Match.source, Match.season)
    matches = [dict(r._mapping) for r in s.execute(select(*cols).where(Match.competition_id == comp.id))]
    by_id = {m["match_id"]: m for m in matches}
    rows, problems = [], []
    for r in f.rows.to_dict("records"):
        where = f"{name}:{r['line']}"
        m = by_id.get(r["match_id"])
        if m is None:
            swapped = make_match_id(f.competition.name, r["date"], r["away_team"], r["home_team"])
            hint = " (home and away are swapped)" if swapped in by_id else ""
            problems.append(f"{where}: no {f.competition.name} match {r['date']} {r['home_team']} v "
                            f"{r['away_team']}{hint}; {_candidates(r, matches)}")
            continue
        if m["source"] == "football-data":
            problems.append(f"{where}: {r['match_id']} comes from football-data; manual results are refused")
            continue
        if m["season"] and not str(m["season"]).startswith(str(f.season_start)):
            problems.append(f"{where}: the match belongs to season {m['season']}, the file to {f.season_start}")
            continue
        kickoff = m["kickoff_utc"] or datetime.combine(m["date"], time(0, 0))
        final = kickoff + MATCH_MIN_LENGTH
        if final > now:
            problems.append(f"{where}: the match has not finished yet (kick-off {kickoff:%Y-%m-%d %H:%M} UTC)")
            continue
        if r["entered_at"] < final:
            problems.append(f"{where}: entered_at {r['entered_at']:%Y-%m-%dT%H:%M}Z is before the end of the match "
                            f"(kick-off {kickoff:%Y-%m-%d %H:%M} UTC + {MATCH_MIN_LENGTH.seconds // 60} min)")
            continue
        if r["entered_at"] > now + FUTURE_TOLERANCE:
            problems.append(f"{where}: entered_at {r['entered_at']:%Y-%m-%dT%H:%M}Z is in the future")
            continue
        rows.append(dict(r, match=m))
    return rows, problems, comp.id


def _jsonable(v):
    return v.isoformat(sep=" ") if isinstance(v, datetime) else v


def _write(s, f: mr.ManualFile, rows: list[dict], comp_id: int) -> dict:
    now = utcnow()
    out = {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    existing = {r.match_id: r for r in s.scalars(
        select(ManualResult).join(Match, Match.match_id == ManualResult.match_id).where(Match.competition_id == comp_id))}
    seen = set()
    for r in rows:
        mid = r["match_id"]
        seen.add(mid)
        new = {c: r[c] for c in AUDIT_COLS}
        cur = existing.get(mid)
        if cur is None:
            s.add(ManualResult(match_id=mid, file=f.path.name, prev_source=r["match"]["source"],
                               prev_status=r["match"]["status"], created_at=now, updated_at=now, **new))
            s.add(ManualResultEdit(match_id=mid, action="insert", file=f.path.name, entered_at=r["entered_at"],
                                   recorded_at=now, changes={c: [None, _jsonable(v)] for c, v in new.items()}))
            out["inserted"] += 1
        else:
            changes = {c: [_jsonable(getattr(cur, c)), _jsonable(v)] for c, v in new.items() if getattr(cur, c) != v}
            if changes:
                for c, v in new.items():
                    setattr(cur, c, v)
                cur.file, cur.updated_at = f.path.name, now
                s.add(ManualResultEdit(match_id=mid, action="update", file=f.path.name, entered_at=r["entered_at"],
                                       recorded_at=now, changes=changes))
                out["updated"] += 1
            else:
                out["unchanged"] += 1
        s.flush()
        values = {c: r[c] for c in VALUE_COLS}
        differs = or_(Match.status.is_not("played"), Match.source.is_not("manual"),
                      *(getattr(Match, c).is_not(v) for c, v in values.items()))
        s.execute(update(Match).where(Match.match_id == mid, differs)
                  .values(**values, status="played", source="manual", updated_at=now))
    for mid, cur in existing.items():
        if mid in seen:
            continue
        old = {c: _jsonable(getattr(cur, c)) for c in AUDIT_COLS}
        s.execute(update(Match).where(Match.match_id == mid).values(
            **{c: None for c in VALUE_COLS}, status=cur.prev_status, source=cur.prev_source, updated_at=now))
        s.add(ManualResultEdit(match_id=mid, action="delete", file=f.path.name, entered_at=None, recorded_at=now,
                               changes={c: [v, None] for c, v in old.items()}))
        s.delete(cur)
        out["deleted"] += 1
    return out


def _run(engine: Engine, directory: Path | None, now: datetime | None, write: bool) -> dict:
    now = now or utcnow()
    out = {"files": 0, "rows": 0, "inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0,
           "fingerprint": mr.fingerprint(directory), "matched": []}
    problems: list[str] = []
    for path in mr.files(directory):
        out["files"] += 1
        try:
            f = mr.read_file(path)
        except mr.ManualResultsError as exc:
            problems += exc.problems
            continue
        with session_scope(engine) as s:
            rows, bad, comp_id = _resolve(s, f, now)
            if bad:
                problems += bad
                continue  # nothing of a file with a problem is written
            out["rows"] += len(rows)
            out["matched"] += [{"line": r["line"], "match_id": r["match_id"], "date": str(r["date"]),
                                "home": r["match"]["home_team"], "away": r["match"]["away_team"],
                                "kickoff_utc": _jsonable(r["match"]["kickoff_utc"]), "total": r["home_shots"] + r["away_shots"]}
                               for r in rows]
            if write and comp_id is not None:
                for k, v in _write(s, f, rows, comp_id).items():
                    out[k] += v
    if problems:
        for p in problems:
            log.error("manual results: %s", p)
        raise mr.ManualResultsError(problems)
    return out


def load_manual_results(engine: Engine, directory: Path | None = None, now: datetime | None = None) -> dict:
    """Validate and load every results_manual/*.csv. Files without problems are written even when another
    file has one; the problems are then raised together (ManualResultsError), so the update step fails
    visibly. -> {files, rows, inserted, updated, deleted, unchanged, fingerprint, matched}."""
    out = _run(engine, directory, now, write=True)
    out.pop("matched")
    return out


def check_manual_results(engine: Engine, directory: Path | None = None, now: datetime | None = None) -> dict:
    """Dry run: the same checks, nothing written. `matched` lists every row with the match it resolved to
    (line, match_id, date, home, away, kickoff_utc, total). Raises ManualResultsError on any problem."""
    return _run(engine, directory, now, write=False)


__all__ = ["MATCH_MIN_LENGTH", "check_manual_results", "load_manual_results"]
