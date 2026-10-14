"""Manual results (results_manual/*.csv) for competitions without an automatic source: strict CSV
validation, resolution against the database, upsert with an append-only audit of corrections, settlement
of experimental predictions in their own record (never the official one), the export (results.json /
track.json), the independent verifier, the accuracy series math and the worker's due reason."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shots.db import __main__ as db_cli
from shots.db import jobs, load, manual, migrate
from shots.db.engine import make_engine
from shots.db.repo import Repository
from shots.export import export
from shots.export import verify as verify_mod
from shots.export.build import accuracy_series
from shots.ingest import manual_results as mr
from shots.worker import due
from tests.test_cross_border import D1, D2, DOM, M_DOM, M_EU1, M_EU2, UCL, seed_db
from tests.test_db_load import played

HEADER = ",".join(mr.COLUMNS)
FILE = "uefa-champions-league_2026-27.csv"
KICK1 = datetime.combine(D1, datetime.min.time()).replace(hour=18)  # EU1 kick-off (UTC), from db_fixtures
KICK2 = datetime.combine(D2, datetime.min.time()).replace(hour=18)
LATER = KICK2 + timedelta(days=1)  # the loader's "now" in these tests: both UCL matches are over
URL = "https://www.example.org/match/1"


def z(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def line(day=D1, home="Arsenal", away="Lille", hs="15", as_="11", hsot="6", asot="3", hg="2", ag="1", src=URL,
         entered=None, notes="") -> str:
    entered = entered if entered is not None else z(KICK1 + timedelta(hours=3))
    return ",".join(str(x) for x in (day, home, away, hs, as_, hsot, asot, hg, ag, src, entered, notes))


def write(d: Path, *rows: str, name: str = FILE, header: str = HEADER) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


@pytest.fixture
def seeded(engine, tmp_path):
    """Domestic + two UCL fixtures, a board and a live picks scan (both pre-match), then the domestic
    result from football-data (the UCL matches stay unsettled until a manual result arrives)."""
    seed_db(engine, tmp_path)
    with engine.begin() as c:  # as the calendar ingest stores them (the test helper says football-data)
        c.execute(text("UPDATE matches SET source = 'calendar' WHERE match_id IN (:a, :b)"), {"a": M_EU1, "b": M_EU2})
    res = played([(*DOM[:3], str(D1), "Betis", "Getafe", 14, 12)])
    res["home_team_id"], res["away_team_id"] = "spain:betis", "spain:getafe"
    load.upsert_matches(engine, res)
    return engine


def q(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


def problems(fn, *a, **k) -> str:
    with pytest.raises(mr.ManualResultsError) as exc:
        fn(*a, **k)
    return "\n".join(exc.value.problems)


# ----------------------------------------------------------------------------- the CSV on its own

def test_committed_file_parses():
    # Structure only: matching rows to fixtures needs the live DB (`python -m shots.db manual --check`).
    path = mr.MANUAL_DIR / FILE
    assert path.read_text(encoding="utf-8").splitlines()[0] == HEADER
    f = mr.read_file(path)
    assert f.competition.name == UCL and f.season_start == 2026
    assert f.rows.empty == (not mr.has_rows(mr.MANUAL_DIR))


def test_a_valid_row_is_parsed_and_normalised(tmp_path):
    f = mr.read_file(write(tmp_path, line(notes="provider counts blocked shots apart", hsot="", asot="")))
    r = f.rows.iloc[0]
    assert (r.line, r.date, r.home_team, r.away_team, r.home_shots, r.away_shots) == (2, D1, "Arsenal", "Lille", 15, 11)
    assert r.home_sot is None and r.away_sot is None and r.notes == "provider counts blocked shots apart"
    assert r.match_id == M_EU1 and r.entered_at == KICK1 + timedelta(hours=3) and r.source_url == URL


@pytest.mark.parametrize("row, expect", [
    (line(day="2026-13-01"), "is not a calendar date"),
    (line(day="14/10/2026"), "date must be YYYY-MM-DD"),
    (line(hs="12.0"), "home_shots must be a whole number"),
    (line(as_="-1"), "away_shots must be a whole number"),
    (line(hs=""), "home_shots is required"),
    (line(hs="61"), "is not plausible"),
    (line(hg="25"), "is not plausible"),
    (line(hsot="16"), "home_sot (16) is larger than home_shots (15)"),
    (line(asot=""), "give both home_sot and away_sot, or neither"),
    (line(src="www.example.org"), "source must be the http(s) URL"),
    (line(entered="2026-10-14 21:00"), "entered_at must be an ISO 8601 UTC time"),
    (line(entered="2026-10-14T21:00:00+02:00"), "entered_at must be an ISO 8601 UTC time"),
    (line(away="Arsenal"), "home and away are the same team"),
    ("2026-10-14,Arsenal", "2 fields, the header has 12"),
])
def test_each_row_error_is_reported_with_its_line(tmp_path, row, expect):
    msg = problems(mr.read_file, write(tmp_path, line(day=D2, home="Viking", away="Bayern Munich"), row))
    assert f"{FILE}:3: " in msg and expect in msg, msg


def test_duplicates_and_double_bookings_are_rejected(tmp_path):
    msg = problems(mr.read_file, write(tmp_path, line(), line(hs="16"), line(home="Arsenal", away="Inter")))
    assert f"{FILE}:3: duplicate of line 2" in msg and f"{FILE}:4: Arsenal already plays on {D1} (line 2)" in msg


@pytest.mark.parametrize("name, header, expect", [
    ("spain-la-liga_2026-27.csv", HEADER, "not a competition without an automatic results source"),
    ("uefa-champions-league.csv", HEADER, "file name must be"),
    ("uefa-champions-league_2026-28.csv", HEADER, "not two consecutive years"),
    (FILE, HEADER.replace(",notes", ""), "missing ['notes']"),
    (FILE, HEADER + ",xg", "unknown ['xg']"),
])
def test_file_level_errors(tmp_path, name, header, expect):
    assert expect in problems(mr.read_file, write(tmp_path, line(), name=name, header=header))


# ----------------------------------------------------------------------------- resolution against the database

def test_unknown_match_lists_close_candidates_and_swapped_teams(seeded, tmp_path):
    d = tmp_path / "manual"
    write(d, line(home="Arsenl"), line(day=D2, home="Bayern Munich", away="Viking", entered=z(KICK2 + timedelta(hours=3))))
    msg = problems(manual.load_manual_results, seeded, d, now=LATER)
    assert f"{FILE}:2: no {UCL} match {D1} Arsenl v Lille; closest: {D1} Arsenal v Lille [{M_EU1}]" in msg
    assert f"{FILE}:3:" in msg and "(home and away are swapped)" in msg
    assert q(seeded, "SELECT count(*) FROM manual_results") == [(0,)]  # nothing of a bad file is written


@pytest.mark.parametrize("entered, now, expect", [
    (KICK1 + timedelta(minutes=60), LATER, "is before the end of the match"),
    (KICK1 + timedelta(hours=3), KICK1 + timedelta(minutes=30), "has not finished yet"),
    (LATER + timedelta(hours=2), LATER, "is in the future"),
])
def test_results_must_be_entered_after_the_match(seeded, tmp_path, entered, now, expect):
    write(tmp_path / "m", line(entered=z(entered)))
    assert expect in problems(manual.load_manual_results, seeded, tmp_path / "m", now=now)


def test_manual_results_never_touch_domestic_matches(seeded, tmp_path):
    # a match that came from football-data is refused, whatever its competition
    with seeded.begin() as c:
        c.execute(text("UPDATE matches SET source = 'football-data' WHERE match_id = :m"), {"m": M_EU1})
    write(tmp_path / "fd", line())
    assert "comes from football-data" in problems(manual.load_manual_results, seeded, tmp_path / "fd", now=LATER)
    with seeded.begin() as c:
        c.execute(text("UPDATE matches SET source = 'calendar' WHERE match_id = :m"), {"m": M_EU1})
    # a competition with a football-data division code is refused (here: the UCL row pretends to have one)
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET code = 'XX' WHERE name = :n"), {"n": UCL})
    write(tmp_path / "m", line())
    assert "has an automatic results source" in problems(manual.load_manual_results, seeded, tmp_path / "m", now=LATER)
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET code = NULL WHERE name = :n"), {"n": UCL})
    # the database refuses it too, whatever the client
    for sql in ("UPDATE matches SET source = 'manual' WHERE match_id = :m",
                "INSERT INTO manual_results (match_id, file, home_shots, away_shots, home_goals, away_goals, source_url, "
                "entered_at, prev_source, prev_status, created_at, updated_at) VALUES (:m, 'x', 1, 1, 0, 0, 'u', "
                "datetime('now'), 'football-data', 'played', datetime('now'), datetime('now'))"):
        with pytest.raises(DBAPIError, match="without an automatic source"):
            with seeded.begin() as c:
                c.execute(text(sql), {"m": M_DOM})
    assert q(seeded, "SELECT source, home_shots FROM matches WHERE match_id = :m", m=M_DOM) == [("football-data", 14)]


# ----------------------------------------------------------------------------- loading, corrections, audit

def test_load_correct_and_remove_with_an_audit_trail(seeded, tmp_path):
    d = tmp_path / "m"
    write(d, line())
    out = manual.load_manual_results(seeded, d, now=LATER)
    assert {k: out[k] for k in ("files", "rows", "inserted", "updated", "deleted")} == dict(files=1, rows=1, inserted=1, updated=0, deleted=0)
    assert q(seeded, "SELECT status, source, home_shots, away_shots, home_sot, away_goals FROM matches WHERE match_id = :m",
             m=M_EU1) == [("played", "manual", 15, 11, 6, 1)]
    assert q(seeded, "SELECT source_url, prev_source, prev_status FROM manual_results") == [(URL, "calendar", "scheduled")]
    assert manual.load_manual_results(seeded, d, now=LATER)["unchanged"] == 1  # idempotent
    assert q(seeded, "SELECT count(*) FROM manual_result_edits") == [(1,)]

    write(d, line(hs="17", notes="corrected: provider update"))  # a correction
    assert manual.load_manual_results(seeded, d, now=LATER)["updated"] == 1
    assert q(seeded, "SELECT home_shots FROM matches WHERE match_id = :m", m=M_EU1) == [(17,)]
    [(action, changes)] = q(seeded, "SELECT action, changes FROM manual_result_edits WHERE action = 'update'")
    assert json.loads(changes) == {"home_shots": [15, 17], "notes": [None, "corrected: provider update"]}

    write(d)  # the row is removed: the match is unsettled again
    assert manual.load_manual_results(seeded, d, now=LATER)["deleted"] == 1
    assert q(seeded, "SELECT status, source, home_shots FROM matches WHERE match_id = :m", m=M_EU1) == [("scheduled", "calendar", None)]
    assert [a for (a,) in q(seeded, "SELECT action FROM manual_result_edits ORDER BY id")] == ["insert", "update", "delete"]
    for sql in ("UPDATE manual_result_edits SET action = 'insert'", "DELETE FROM manual_result_edits"):
        with pytest.raises(DBAPIError, match="append-only"):
            with seeded.begin() as c:
                c.execute(text(sql))


def test_check_is_a_dry_run_and_the_cli_reports(seeded, tmp_path, migrated_db, monkeypatch, capsys):
    d = tmp_path / "m"
    write(d, line())
    res = manual.check_manual_results(seeded, d, now=LATER)
    assert res["matched"] == [{"line": 2, "match_id": M_EU1, "date": str(D1), "home": "Arsenal", "away": "Lille",
                               "kickoff_utc": KICK1.isoformat(sep=" "), "total": 26}]
    assert q(seeded, "SELECT count(*) FROM manual_results") == [(0,)]
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    write(tmp_path / "bad", line(home="Nobody"))
    with pytest.raises(SystemExit) as exc:
        db_cli.main(["manual", "--check", "--dir", str(tmp_path / "bad")])
    assert exc.value.code == 1 and "no UEFA Champions League match" in capsys.readouterr().err


# ----------------------------------------------------------------------------- settlement

def test_experimental_predictions_settle_apart_from_the_official_record(seeded, tmp_path):
    official = q(seeded, "SELECT * FROM v_official_picks")
    assert [s for (s,) in q(seeded, "SELECT status FROM v_experimental_picks ORDER BY match_id")] == ["pending", "pending"]
    write(tmp_path / "m", line(hs="15", as_="11"),
          line(day=D2, home="Viking", away="Bayern Munich", hs="9", as_="13", entered=z(KICK2 + timedelta(hours=2))))
    manual.load_manual_results(seeded, tmp_path / "m", now=LATER)
    assert q(seeded, "SELECT * FROM v_official_picks") == official  # unchanged, row for row
    exp = dict(q(seeded, "SELECT match_id, status || ':' || actual_total || ':' || hit FROM v_experimental_picks"))
    assert exp == {M_EU1: "settled:26:1", M_EU2: "settled:22:0"}
    repo = Repository(seeded)
    assert [r["match_id"] for r in repo.official_results(D1, LATER.date())] == [M_DOM]
    assert {r["match_id"]: r["result_source"] for r in repo.settled_results(None, LATER.date(), experimental=True)} == {
        M_EU1: "manual", M_EU2: "manual"}
    # a correction propagates (outcomes are views)
    write(tmp_path / "m", line(hs="10", as_="11", hsot="", asot=""),
          line(day=D2, home="Viking", away="Bayern Munich", hs="9", as_="13", entered=z(KICK2 + timedelta(hours=2))))
    manual.load_manual_results(seeded, tmp_path / "m", now=LATER)
    assert q(seeded, "SELECT hit FROM v_experimental_picks WHERE match_id = :m", m=M_EU1) == [(0,)]


def test_manual_results_do_not_move_domestic_baselines_or_the_incremental_window(seeded, tmp_path):
    repo = Repository(seeded)
    before = repo.base_rates(24, LATER.date() + timedelta(days=1))
    since = load.incremental_since(seeded)
    write(tmp_path / "m", line(day=D2, home="Viking", away="Bayern Munich", hs="30", as_="30", hsot="", asot="",
                               entered=z(KICK2 + timedelta(hours=2))))
    manual.load_manual_results(seeded, tmp_path / "m", now=LATER)
    rates, overall = repo.base_rates(24, LATER.date() + timedelta(days=1))
    assert overall == before[1] and rates["Spain La Liga"] == before[0]["Spain La Liga"] and rates[UCL] == 1.0
    assert load.incremental_since(seeded) == since


# ----------------------------------------------------------------------------- export

def export_at(engine, out, today):
    built = datetime.combine(today, datetime.min.time()).replace(hour=9, tzinfo=timezone.utc)
    export(out, Repository(engine), built_at=built)
    return {n: json.loads((out / f"{n}.json").read_text(encoding="utf-8")) for n in ("meta", "board", "results", "track")}


def test_export_publishes_experimental_results_and_record(seeded, tmp_path, migrated_db):
    today = LATER.date()
    plain = export_at(seeded, tmp_path / "plain", today)
    assert plain["track"]["records"]["experimental"] == {
        "label": "Champions League (experimental, manual results)", "settled": 0, "pending": 2, "void": 0,
        "by_n": {}, "daily": [], "weekly": []}
    write(tmp_path / "m", line(), line(day=D2, home="Viking", away="Bayern Munich", hs="9", as_="13",
                                       entered=z(KICK2 + timedelta(hours=2))))
    manual.load_manual_results(seeded, tmp_path / "m", now=LATER)
    docs = export_at(seeded, tmp_path / "out", today)
    from tests.test_cross_border import validate
    for name, doc in docs.items():
        validate(name, doc)
    res = {r["id"]: r for r in docs["results"]["results"]}
    assert res[M_EU1]["experimental"] is True and res[M_EU1]["result_source"] == "manual"
    assert res[M_EU1]["source_url"] == URL and res[M_EU1]["actual"] == {"total": 26, "home": 15, "away": 11}
    assert res[M_DOM]["result_source"] == "football-data" and "experimental" not in res[M_DOM]
    # domestic outputs are unchanged by the manual results
    assert res[M_DOM] == {r["id"]: r for r in plain["results"]["results"]}[M_DOM]
    assert docs["track"]["records"]["live"] == plain["track"]["records"]["live"]
    exp = docs["track"]["records"]["experimental"]
    assert (exp["settled"], exp["pending"], exp["by_n"]["24"]["n"], exp["by_n"]["24"]["hits"]) == (2, 0, 2, 1)
    assert exp["by_n"]["24"]["mean_stated"] == 0.76
    assert [p["by_n"] for p in exp["daily"]] == [{"24": {"picks": 1, "hits": 1}}, {"24": {"picks": 1, "hits": 0}}]
    assert sum(p["matches"] for p in exp["weekly"]) == 2
    assert next(x for x in docs["meta"]["leagues"] if x["id"] == "uefa-champions-league")["featured"] is True
    ok = verify_mod.verify(tmp_path / "out", migrated_db, today=today)
    assert ok["ok"], ok["problems"]
    assert ok["checked"]["track experimental daily"] == 2


@pytest.mark.parametrize("name, tamper, expect", [
    ("results", lambda d, i: d["results"][i].__setitem__("source_url", "https://elsewhere.example"), "source_url differs"),
    ("results", lambda d, i: d["results"][i].pop("experimental"), ".experimental"),
    ("results", lambda d, i: d["results"][i].__setitem__("result_source", "football-data"), "result_source"),
    ("track", lambda d, i: d["records"]["experimental"]["daily"][0]["by_n"]["24"].__setitem__("hits", 0), "experimental.daily"),
    ("track", lambda d, i: d["records"]["experimental"]["weekly"][0].__setitem__("mae", 0.5), "experimental.weekly"),
    ("track", lambda d, i: d["records"]["experimental"].__setitem__("pending", 3), "experimental.pending"),
    ("track", lambda d, i: d["records"]["live"]["daily"][0].__setitem__("matches", 5), "live.daily"),
])
def test_verifier_detects_tampered_experimental_data(seeded, tmp_path, migrated_db, name, tamper, expect):
    write(tmp_path / "m", line(), line(day=D2, home="Viking", away="Bayern Munich", hs="9", as_="13",
                                       entered=z(KICK2 + timedelta(hours=2))))
    manual.load_manual_results(seeded, tmp_path / "m", now=LATER)
    out = tmp_path / "out"
    docs = export_at(seeded, out, LATER.date())
    i = next(k for k, r in enumerate(docs["results"]["results"]) if r["id"] == M_EU1)
    doc = docs[name]
    tamper(doc, i)
    (out / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db, today=LATER.date())
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


# ----------------------------------------------------------------------------- accuracy series math

def test_accuracy_series_on_a_hand_checkable_fixture():
    mon, tue, next_mon = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 19)

    def r(d, actual, expected, lb80):
        return {"date": d, "actual_total": actual, "expected_shots": expected, "lb_80": lb80}

    rows = [r(mon, 30, 26.0, 22), r(mon, 20, 25.0, 21), r(tue, 24, 24.0, 20), r(next_mon, 18, None, None)]
    picks = [(mon, 24, 0.75, True), (mon, 24, 0.71, False), (mon, 24, 0.40, False), (tue, 22, 0.80, True),
             (next_mon, 24, 0.70, False)]
    daily = accuracy_series(rows, picks, [22, 24], "day", 30)
    assert daily == [
        # |30-26| = 4, |20-25| = 5 -> mae 4.5; bias ((26-30) + (25-20)) / 2 = 0.5; floor 30>=22 yes, 20>=21 no
        {"date": "2026-10-12", "matches": 2, "mae": 4.5, "bias": 0.5, "floor80_held": 0.5,
         "by_n": {"22": {"picks": 0, "hits": 0}, "24": {"picks": 2, "hits": 1}}},
        {"date": "2026-10-13", "matches": 1, "mae": 0.0, "bias": 0.0, "floor80_held": 1.0,
         "by_n": {"22": {"picks": 1, "hits": 1}, "24": {"picks": 0, "hits": 0}}},
        {"date": "2026-10-19", "matches": 1, "mae": None, "bias": None, "floor80_held": None,
         "by_n": {"22": {"picks": 0, "hits": 0}, "24": {"picks": 1, "hits": 0}}},
    ]
    weekly = accuracy_series(rows, picks, [22, 24], "week", 52)
    # week of Monday 12th: |4|, |5|, |0| -> mae 3.0; bias (-4 + 5 + 0) / 3 = 0.33; floor 2 of 3
    assert weekly[0] == {"date": "2026-10-12", "matches": 3, "mae": 3.0, "bias": 0.33, "floor80_held": 0.6667,
                         "by_n": {"22": {"picks": 1, "hits": 1}, "24": {"picks": 2, "hits": 1}}}
    assert [w["date"] for w in weekly] == ["2026-10-12", "2026-10-19"]
    assert [p["date"] for p in accuracy_series(rows, picks, [24], "day", 2)] == ["2026-10-13", "2026-10-19"]


# ----------------------------------------------------------------------------- worker and migration

def test_worker_runs_when_the_manual_results_change(engine, tmp_path):
    d = tmp_path / "m"
    write(d)  # header only, nothing recorded yet: nothing due
    assert not due.manual_results_changed(engine, d)
    write(d, line())
    assert due.manual_results_changed(engine, d)
    jobs.finish(engine, jobs.start(engine, "update"), "ok", {"db-manual": {"fingerprint": mr.fingerprint(d)}})
    assert not due.manual_results_changed(engine, d)
    write(d, line(hs="16"))
    assert due.manual_results_changed(engine, d)
    since = jobs.utcnow()
    assert "manual-results" in [r.code for r in due.due_work(engine, since, since, manual_dir=d, cal_dir=tmp_path)]


def test_migration_0006_upgrades_a_0005_database_and_roundtrips(tmp_path):
    path = tmp_path / "old.db"
    migrate.upgrade(path, "0005_explain")
    e = make_engine(path)
    with e.connect() as c:
        views = set(c.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'view'")).all())
    e.dispose()
    assert migrate.upgrade(path) == "0006_manual_results"
    e = make_engine(path)
    with e.connect() as c:
        assert set(c.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'view'")).all()) == views  # views untouched
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"))}
    e.dispose()
    assert {"manual_results", "manual_result_edits", "trg_matches_manual_scope_update",
            "trg_manual_result_edits_no_delete"} <= names
    assert migrate.downgrade(path, "0005_explain") == "0005_explain"
    e = make_engine(path)
    with e.connect() as c:
        assert "manual_results" not in {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
    e.dispose()
    assert migrate.upgrade(path) == "0006_manual_results"
