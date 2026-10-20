"""Load provisional results (results_provisional/*.csv, shots.ingest.provisional_results) into the database.

    load_provisional_results(engine, directory=None, now=None)   validate, resolve and upsert; audit every change
    check_provisional_results(engine, directory=None, now=None)  the same checks, nothing written (dry run)
    agreement(engine)                                            v_provisional_agreement rows + a summary

A row is resolved to an existing match of its competition by canonical match_id (competition, date,
alias-normalised home and away), exactly like a manual result. Its competition must have a football-data
division code (the opposite of a manual result), the match must have finished by `entered_at` (kick-off +
MATCH_MIN_LENGTH) and `entered_at` must not be in the future. Any problem rejects the whole file (nothing of
it is written) and is reported with the file and line, an unknown match with its closest candidates.

Writing (one transaction per file):
- new row: provisional_results row + the match becomes status 'played', source 'provisional' with the shot /
  goal columns; audit action 'insert'.
- changed row (a correction): both updated; audit action 'update' with the changed columns.
- row removed from the file: the match goes back to its previous source / status with empty result columns,
  the provisional_results row is deleted; audit action 'delete'.
football-data always wins: a match football-data has published (status 'played', source 'football-data') is
never written here. Its provisional row is still recorded / corrected (the provider's side of the agreement
audit), and removing it leaves the confirmed match untouched. The switch itself happens in the regular
results load (shots.db.load.upsert_matches overwrites source and values); a trigger then records the
'confirm' audit row with both values. Outcomes are views over `matches`, so settlement follows every step.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path

from sqlalchemy import Engine, and_, not_, or_, select, text, update

from shots.db.engine import session_scope
from shots.db.manual import FUTURE_TOLERANCE, MATCH_MIN_LENGTH, _candidates, _jsonable
from shots.db.models import Competition, Match, ProvisionalResult, ProvisionalResultEdit, utcnow
from shots.ingest import provisional_results as pr
from shots.ingest.manual_results import ManualFile
from shots.ingest.schema import make_match_id

log = logging.getLogger(__name__)

VALUE_COLS = ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals")
AUDIT_COLS = (*VALUE_COLS, "source_url", "entered_at", "notes")
SOURCE = "provisional"  # matches.source of a provisionally settled match


def _confirmed(m: dict) -> bool:
    return m["status"] == "played" and m["source"] == "football-data"


def _resolve(s, f: ManualFile, now: datetime) -> tuple[list[dict], list[str], int | None]:
    """-> (rows with their match, problems, competition id)."""
    name = f.path.name
    comp = s.execute(select(Competition.id, Competition.code).where(Competition.name == f.competition.name)).first()
    if comp is None:
        return [], [f"{name}: competition {f.competition.name!r} has no fixtures in the database"] if len(f.rows) else [], None
    if comp.code is None:
        return [], [f"{name}: {f.competition.name} has no football-data division code in the database; "
                    f"provisional results are refused"], comp.id
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
        if m["status"] == "played" and m["source"] not in ("football-data", SOURCE):
            problems.append(f"{where}: {r['match_id']} already has a {m['source']} result")
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
        rows.append(dict(r, match=m, provider=pr.provider(r["source_url"])))
    return rows, problems, comp.id


def _not_confirmed():
    """SQL condition: the match has no football-data result (football-data always wins)."""
    return not_(and_(Match.status == "played", Match.source == "football-data"))


def _write(s, f: ManualFile, rows: list[dict], comp_id: int) -> dict:
    now = utcnow()
    out = {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0, "confirmed": 0}
    existing = {r.match_id: r for r in s.scalars(
        select(ProvisionalResult).join(Match, Match.match_id == ProvisionalResult.match_id)
        .where(Match.competition_id == comp_id))}
    seen = set()
    for r in rows:
        mid = r["match_id"]
        seen.add(mid)
        new = {c: r[c] for c in AUDIT_COLS}
        cur = existing.get(mid)
        if cur is None:
            s.add(ProvisionalResult(match_id=mid, file=f.path.name, provider=r["provider"],
                                    prev_source=r["match"]["source"], prev_status=r["match"]["status"],
                                    created_at=now, updated_at=now, **new))
            s.add(ProvisionalResultEdit(match_id=mid, action="insert", file=f.path.name, entered_at=r["entered_at"],
                                        recorded_at=now, changes={c: [None, _jsonable(v)] for c, v in new.items()}))
            out["inserted"] += 1
        else:
            changes = {c: [_jsonable(getattr(cur, c)), _jsonable(v)] for c, v in new.items() if getattr(cur, c) != v}
            if changes:
                for c, v in new.items():
                    setattr(cur, c, v)
                cur.file, cur.provider, cur.updated_at = f.path.name, r["provider"], now
                s.add(ProvisionalResultEdit(match_id=mid, action="update", file=f.path.name,
                                            entered_at=r["entered_at"], recorded_at=now, changes=changes))
                out["updated"] += 1
            else:
                out["unchanged"] += 1
            cur.file = f.path.name  # a row moved between files belongs to the file that lists it now
        s.flush()
        if _confirmed(r["match"]):
            out["confirmed"] += 1
            continue  # football-data's result stands
        values = {c: r[c] for c in VALUE_COLS}
        differs = or_(Match.status.is_not("played"), Match.source.is_not(SOURCE),
                      *(getattr(Match, c).is_not(v) for c, v in values.items()))
        s.execute(update(Match).where(Match.match_id == mid, _not_confirmed(), differs)
                  .values(**values, status="played", source=SOURCE, updated_at=now))
    for mid, cur in existing.items():
        if mid in seen or cur.file != f.path.name:  # rows of another file (another season) are its business
            continue
        old = {c: _jsonable(getattr(cur, c)) for c in AUDIT_COLS}
        s.execute(update(Match).where(Match.match_id == mid, Match.source == SOURCE).values(
            **{c: None for c in VALUE_COLS}, status=cur.prev_status, source=cur.prev_source, updated_at=now))
        s.add(ProvisionalResultEdit(match_id=mid, action="delete", file=f.path.name, entered_at=None, recorded_at=now,
                                    changes={c: [v, None] for c, v in old.items()}))
        s.delete(cur)
        out["deleted"] += 1
    return out


def _orphans(s, files_seen: set[str]) -> int:
    """provisional_results rows whose file is no longer in the directory: removed like a deleted row."""
    now = utcnow()
    n = 0
    for cur in s.scalars(select(ProvisionalResult).where(ProvisionalResult.file.not_in(sorted(files_seen)))):
        n += 1
        s.execute(update(Match).where(Match.match_id == cur.match_id, Match.source == SOURCE).values(
            **{c: None for c in VALUE_COLS}, status=cur.prev_status, source=cur.prev_source, updated_at=now))
        s.add(ProvisionalResultEdit(match_id=cur.match_id, action="delete", file=cur.file, entered_at=None,
                                    recorded_at=now,
                                    changes={c: [_jsonable(getattr(cur, c)), None] for c in AUDIT_COLS}))
        s.delete(cur)
    return n


def _run(engine: Engine, directory: Path | None, now: datetime | None, write: bool) -> dict:
    now = now or utcnow()
    out = {"files": 0, "rows": 0, "inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0, "confirmed": 0,
           "fingerprint": pr.fingerprint(directory), "matched": []}
    problems: list[str] = []
    names: set[str] = set()
    for path in pr.files(directory):
        out["files"] += 1
        names.add(path.name)
        try:
            f = pr.read_file(path)
        except pr.ProvisionalResultsError as exc:
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
                                "kickoff_utc": _jsonable(r["match"]["kickoff_utc"]),
                                "total": r["home_shots"] + r["away_shots"], "provider": r["provider"],
                                "status": "confirmed" if _confirmed(r["match"]) else "provisional"}
                               for r in rows]
            if write and comp_id is not None:
                for k, v in _write(s, f, rows, comp_id).items():
                    out[k] += v
    if write and not problems:  # a whole file deleted from the folder: its rows are removed too
        with session_scope(engine) as s:
            out["deleted"] += _orphans(s, names)
    if problems:
        for p in problems:
            log.error("provisional results: %s", p)
        raise pr.ProvisionalResultsError(problems)
    return out


def load_provisional_results(engine: Engine, directory: Path | None = None, now: datetime | None = None) -> dict:
    """Validate and load every results_provisional/*.csv. Files without problems are written even when
    another file has one; the problems are then raised together (ProvisionalResultsError), so the update step
    fails visibly. -> {files, rows, inserted, updated, deleted, unchanged, confirmed (rows whose match
    football-data has already published: recorded, the match untouched), fingerprint}."""
    out = _run(engine, directory, now, write=True)
    out.pop("matched")
    return out


def check_provisional_results(engine: Engine, directory: Path | None = None, now: datetime | None = None) -> dict:
    """Dry run: the same checks, nothing written. `matched` lists every row with the match it resolved to
    (line, match_id, date, home, away, kickoff_utc, total, provider, status provisional | confirmed).
    Raises ProvisionalResultsError on any problem."""
    return _run(engine, directory, now, write=False)


def agreement(engine: Engine) -> dict:
    """Provider agreement audit: {"rows": [v_provisional_agreement rows as dicts], "summary": {provisional,
    confirmed, unsettled, exact_total (confirmed rows with the same total), mean_abs_diff_total,
    mean_diff_total (football-data minus provider), goals_agree}}."""
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(text(
            "SELECT * FROM v_provisional_agreement ORDER BY date, competition, home_team")).mappings()]
    conf = [r for r in rows if r["result_status"] == "confirmed"]
    diffs = [int(r["diff_total"]) for r in conf if r["diff_total"] is not None]
    summary = {"provisional": sum(r["result_status"] == "provisional" for r in rows), "confirmed": len(conf),
               "unsettled": sum(r["result_status"] == "unsettled" for r in rows),
               "exact_total": sum(d == 0 for d in diffs),
               "mean_abs_diff_total": round(sum(abs(d) for d in diffs) / len(diffs), 2) if diffs else None,
               "mean_diff_total": round(sum(diffs) / len(diffs), 2) if diffs else None,
               "goals_agree": sum(bool(r["goals_agree"]) for r in conf)}
    return {"rows": rows, "summary": summary}


__all__ = ["agreement", "check_provisional_results", "load_provisional_results"]
