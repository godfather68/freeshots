"""Provisional results (results_provisional/*.csv): hand-entered stand-ins for football-data results that
football-data has not published yet. Scope (football-data competitions only, enforced by the loader and by
triggers), settlement of the official record, automatic replacement by football-data's numbers with an
agreement audit, isolation from the modelling pipeline, the export / verifier fields, the worker's due
reason, the CLI and migration 0007."""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shots.db import __main__ as db_cli
from shots.db import jobs, load, migrate, provisional
from shots.db.engine import make_engine
from shots.db.repo import Repository
from shots.export import export
from shots.export import verify as verify_mod
from shots.ingest import provisional_results as pr
from shots.worker import due
from tests.test_cross_border import D1, DOM, M_DOM, M_EU1, UCL, seed_db, validate
from tests.test_db_load import played

HEADER = ",".join(pr.COLUMNS)
FILE = "spain-la-liga_2026-27.csv"
KICK = datetime.combine(D1, datetime.min.time()).replace(hour=18)  # M_DOM kick-off (UTC), from db_fixtures
LATER = KICK + timedelta(days=2)  # the loader's "now" in these tests
URL = "https://www.espn.com/soccer/team-stats/_/gameId/1"
SRC = Path(__file__).resolve().parents[1] / "src" / "shots"


def z(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def line(day=D1, home="Betis", away="Getafe", hs="15", as_="11", hsot="6", asot="3", hg="2", ag="1", src=URL,
         entered=None, notes="ESPN team stats") -> str:
    entered = entered if entered is not None else z(KICK + timedelta(hours=3))
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
    """Spain La Liga + two UCL fixtures with a board and a live picks scan (pre-match); no results yet:
    football-data has not published Betis v Getafe."""
    seed_db(engine, tmp_path)
    with engine.begin() as c:
        c.execute(text("UPDATE matches SET source = 'calendar' WHERE competition_id = "
                       "(SELECT id FROM competitions WHERE name = :n)"), {"n": UCL})
    return engine


def q(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


def problems(fn, *a, **k) -> str:
    with pytest.raises(pr.ProvisionalResultsError) as exc:
        fn(*a, **k)
    return "\n".join(exc.value.problems)


def football_data(engine, hs=16, as_=11):
    """football-data publishes Betis v Getafe (the regular results load)."""
    res = played([(*DOM[:3], str(D1), "Betis", "Getafe", hs, as_)])
    res["home_team_id"], res["away_team_id"] = "spain:betis", "spain:getafe"
    res["home_sot"], res["away_sot"], res["home_goals"], res["away_goals"] = 7, 3, 2, 1
    return load.upsert_matches(engine, res)


# ----------------------------------------------------------------------------- the CSV on its own

def test_committed_files_parse():
    # Structure only: matching rows to fixtures needs the live DB (`python -m shots.db provisional --check`).
    assert pr.COMMITTED_DIR.is_dir() and (pr.COMMITTED_DIR / "README.md").exists()
    for path in pr.files(pr.COMMITTED_DIR):
        assert path.read_text(encoding="utf-8").splitlines()[0] == HEADER
        f = pr.read_file(path)
        assert f.competition.code  # a football-data division


def test_a_valid_row_is_parsed_with_its_competition(tmp_path):
    f = pr.read_file(write(tmp_path, line()))
    r = f.rows.iloc[0]
    assert (f.competition.name, f.competition.code, f.season_start) == ("Spain La Liga", "SP1", 2026)
    assert (r.match_id, r.home_shots, r.away_shots, r.source_url) == (M_DOM, 15, 11, URL)
    assert pr.provider(URL) == "espn" and pr.provider("https://espn.co.uk/x") == "espn"
    assert pr.competition_for(Path("scotland-premiership_2026-27.csv"))[0].code == "SC0"


@pytest.mark.parametrize("name, row, expect", [
    ("uefa-champions-league_2026-27.csv", line(), "use results_manual/"),
    ("spain-liga_2026-27.csv", line(), "is not a competition football-data covers"),
    ("spain-la-liga.csv", line(), "file name must be"),
    ("spain-la-liga_2026-28.csv", line(), "not two consecutive years"),
    (FILE, line(src="https://www.football-data.co.uk/spainm.php"), "source is football-data itself"),
    (FILE, line(hs="61"), "is not plausible"),
    (FILE, line(entered="2026-10-14 21:00"), "entered_at must be an ISO 8601 UTC time"),
])
def test_file_and_row_errors(tmp_path, name, row, expect):
    assert expect in problems(pr.read_file, write(tmp_path, row, name=name))


# ----------------------------------------------------------------------------- resolution and scope

def test_unknown_match_and_timing_problems_reject_the_file(seeded, tmp_path):
    write(tmp_path / "a", line(home="Betiss"))
    msg = problems(provisional.load_provisional_results, seeded, tmp_path / "a", now=LATER)
    assert f"{FILE}:2: no Spain La Liga match {D1} Betiss v Getafe; closest: {D1} Betis v Getafe [{M_DOM}]" in msg
    write(tmp_path / "b", line(entered=z(KICK + timedelta(minutes=30))))
    assert "is before the end of the match" in problems(provisional.load_provisional_results, seeded, tmp_path / "b",
                                                        now=LATER)
    write(tmp_path / "c", line())
    assert "has not finished yet" in problems(provisional.load_provisional_results, seeded, tmp_path / "c",
                                              now=KICK + timedelta(minutes=20))
    assert q(seeded, "SELECT count(*) FROM provisional_results") == [(0,)]
    assert q(seeded, "SELECT status, source FROM matches WHERE match_id = :m", m=M_DOM) == [("scheduled", "football-data")]


def test_scope_is_football_data_competitions_only(seeded, tmp_path):
    # the loader: a competition without a division code in the database is refused
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET code = NULL WHERE name = 'Spain La Liga'"))
    write(tmp_path / "p", line())
    assert "no football-data division code" in problems(provisional.load_provisional_results, seeded, tmp_path / "p",
                                                        now=LATER)
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET code = 'SP1' WHERE name = 'Spain La Liga'"))
    # the database, whatever the client
    for sql, params in (
            ("UPDATE matches SET source = 'provisional', status = 'played' WHERE match_id = :m", {"m": M_EU1}),
            ("INSERT INTO provisional_results (match_id, file, home_shots, away_shots, home_goals, away_goals, "
             "source_url, provider, entered_at, prev_source, prev_status, created_at, updated_at) VALUES (:m, 'x', 1, "
             "1, 0, 0, 'u', 'espn', datetime('now'), 'calendar', 'scheduled', datetime('now'), datetime('now'))",
             {"m": M_EU1}),
            ("INSERT INTO matches (match_id, source, competition_id, date, status, home_team_id, away_team_id, "
             "home_team, away_team, search_key, created_at, updated_at) SELECT 'new1', 'provisional', competition_id, "
             "date, 'played', home_team_id, away_team_id, home_team, away_team, 'k', created_at, updated_at "
             "FROM matches WHERE match_id = :m", {"m": M_DOM})):
        with pytest.raises(DBAPIError, match="provisional"):
            with seeded.begin() as c:
                c.execute(text(sql), params)
    # 0006 still holds: a football-data competition never takes a 'manual' result
    with pytest.raises(DBAPIError, match="without an automatic source"):
        with seeded.begin() as c:
            c.execute(text("UPDATE matches SET source = 'manual' WHERE match_id = :m"), {"m": M_DOM})
    # and a played football-data match never becomes provisional
    football_data(seeded)
    with pytest.raises(DBAPIError, match="unconfirmed matches"):
        with seeded.begin() as c:
            c.execute(text("UPDATE matches SET source = 'provisional' WHERE match_id = :m"), {"m": M_DOM})


# ----------------------------------------------------------------------------- settlement and supersession

def test_provisional_result_settles_the_official_record_flagged(seeded, tmp_path):
    assert [s for (s,) in q(seeded, "SELECT status FROM v_official_picks")] == ["pending"]
    write(tmp_path / "p", line())
    out = provisional.load_provisional_results(seeded, tmp_path / "p", now=LATER)
    assert {k: out[k] for k in ("files", "rows", "inserted", "confirmed")} == dict(files=1, rows=1, inserted=1, confirmed=0)
    assert q(seeded, "SELECT status, source, home_shots, away_shots, home_sot, home_goals FROM matches WHERE match_id = :m",
             m=M_DOM) == [("played", "provisional", 15, 11, 6, 2)]
    assert q(seeded, "SELECT status, actual_total, hit FROM v_official_picks") == [("settled", 26, 1)]
    live = Repository(seeded).track_records("live")
    assert live["provisional"].tolist() == [True]
    [r] = Repository(seeded).official_results(D1, LATER.date())
    assert (r["match_id"], r["result_status"], r["result_source"], r["source_url"]) == (M_DOM, "provisional", "espn", URL)
    assert provisional.load_provisional_results(seeded, tmp_path / "p", now=LATER)["unchanged"] == 1  # idempotent
    assert Repository(seeded).data_status()["provisional_results"] == 1


def test_football_data_wins_automatically_and_the_agreement_is_recorded(seeded, tmp_path):
    d = tmp_path / "p"
    write(d, line(hs="15", as_="11"))
    provisional.load_provisional_results(seeded, d, now=LATER)
    football_data(seeded, hs=16, as_=11)  # the regular results load, nothing provisional-specific
    assert q(seeded, "SELECT status, source, home_shots, away_shots, home_sot FROM matches WHERE match_id = :m",
             m=M_DOM) == [("played", "football-data", 16, 11, 7)]
    assert q(seeded, "SELECT status, actual_total FROM v_official_picks") == [("settled", 27)]  # follows the view
    [r] = Repository(seeded).official_results(D1, LATER.date())
    assert (r["result_status"], r["result_source"], r["source_url"]) == ("confirmed", "football-data", None)
    [(changes,)] = q(seeded, "SELECT changes FROM provisional_result_edits WHERE action = 'confirm'")
    assert json.loads(changes) == {"home_shots": [15, 16], "away_shots": [11, 11], "home_sot": [6, 7],
                                   "away_sot": [3, 3], "home_goals": [2, 2], "away_goals": [1, 1]}
    ag = provisional.agreement(seeded)
    [row] = ag["rows"]
    assert (row["result_status"], row["prov_total"], row["fd_total"], row["diff_total"], row["diff_home_shots"],
            row["provider"], row["goals_agree"]) == ("confirmed", 26, 27, 1, 1, "espn", 1)
    assert ag["summary"] == {"provisional": 0, "confirmed": 1, "unsettled": 0, "exact_total": 0,
                             "mean_abs_diff_total": 1.0, "mean_diff_total": 1.0, "goals_agree": 1}
    # the provisional file never overrides football-data again: reload, correct, remove
    assert provisional.load_provisional_results(seeded, d, now=LATER)["confirmed"] == 1
    write(d, line(hs="20"))
    assert provisional.load_provisional_results(seeded, d, now=LATER)["updated"] == 1
    assert q(seeded, "SELECT home_shots FROM provisional_results") == [(20,)]
    write(d)
    assert provisional.load_provisional_results(seeded, d, now=LATER)["deleted"] == 1
    assert q(seeded, "SELECT status, source, home_shots FROM matches WHERE match_id = :m", m=M_DOM) == [
        ("played", "football-data", 16)]
    assert [a for (a,) in q(seeded, "SELECT action FROM provisional_result_edits ORDER BY id")] == [
        "insert", "confirm", "update", "delete"]


def test_a_row_for_an_already_confirmed_match_is_only_recorded(seeded, tmp_path):
    football_data(seeded, hs=16, as_=11)
    write(tmp_path / "p", line(hs="14"))
    res = provisional.check_provisional_results(seeded, tmp_path / "p", now=LATER)
    assert [(m["match_id"], m["status"], m["provider"], m["total"]) for m in res["matched"]] == [
        (M_DOM, "confirmed", "espn", 25)]
    assert provisional.load_provisional_results(seeded, tmp_path / "p", now=LATER)["confirmed"] == 1
    assert q(seeded, "SELECT source, home_shots FROM matches WHERE match_id = :m", m=M_DOM) == [("football-data", 16)]
    assert provisional.agreement(seeded)["rows"][0]["diff_total"] == 2


def test_removal_corrections_and_an_append_only_audit(seeded, tmp_path):
    d = tmp_path / "p"
    write(d, line())
    provisional.load_provisional_results(seeded, d, now=LATER)
    write(d, line(as_="9", notes="ESPN corrected"))
    assert provisional.load_provisional_results(seeded, d, now=LATER)["updated"] == 1
    assert q(seeded, "SELECT actual_total, hit FROM v_official_picks") == [(24, 1)]
    [(changes,)] = q(seeded, "SELECT changes FROM provisional_result_edits WHERE action = 'update'")
    assert json.loads(changes) == {"away_shots": [11, 9], "notes": ["ESPN team stats", "ESPN corrected"]}
    (d / FILE).unlink()  # the whole file removed: its rows go too, the match is unsettled again
    assert provisional.load_provisional_results(seeded, d, now=LATER)["deleted"] == 1
    assert q(seeded, "SELECT status, source, home_shots FROM matches WHERE match_id = :m", m=M_DOM) == [
        ("scheduled", "football-data", None)]
    assert q(seeded, "SELECT status FROM v_official_picks") == [("pending",)]
    for sql in ("UPDATE provisional_result_edits SET action = 'insert'", "DELETE FROM provisional_result_edits"):
        with pytest.raises(DBAPIError, match="append-only"):
            with seeded.begin() as c:
                c.execute(text(sql))


# ----------------------------------------------------------------------------- never in the modelling pipeline

MODELLING = ("features", "models", "predict", "evaluation")
INGEST_FOR_MODELS = ("football_data.py", "calendar_ics.py", "schema.py", "cross_border.py", "match_extras.py",
                     "quality.py", "statsbomb.py")


def _modelling_sources() -> list[Path]:
    out = [p for pkg in MODELLING for p in (SRC / pkg).rglob("*.py")]
    return out + [SRC / "ingest" / n for n in INGEST_FOR_MODELS if (SRC / "ingest" / n).exists()]


def test_the_modelling_code_never_reads_provisional_results():
    """Features, ratings, league levels and the models read matches.parquet (football-data only). No module
    of theirs imports the provisional loaders, the database results, or names the provisional folder; the
    only database helpers they use are pure time functions."""
    for path in _modelling_sources():
        src = path.read_text(encoding="utf-8")
        assert "results_provisional" not in src and "provisional_results" not in src, path
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("shots.db"):
                assert node.module == "shots.db.load" and {a.name for a in node.names} <= {"local_to_utc", "lock_moment"}, path
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("shots.db") for a in node.names), path


def _history() -> pd.DataFrame:
    from tests.test_features_team_form import _league

    m = _league(n_days=30, seed=3)
    m["competition"], m["country"], m["tier"], m["season"] = "Spain La Liga", "Spain", 1, "2026/2027"
    return m


def test_features_and_board_are_unchanged_by_provisional_results(seeded, tmp_path, monkeypatch):
    from shots.features.team_form import build_features
    from shots.ingest import football_data as fd
    from shots.predict import board as bd
    from tests import test_board as tb

    parquet = tmp_path / "matches.parquet"
    _history().to_parquet(parquet)
    monkeypatch.setattr(fd, "OUT_PATH", parquet)
    upcoming = _history().tail(3).assign(date=pd.Timestamp(D1), match_id=["f1", "f2", "f3"],
                                         home_shots=np.nan, away_shots=np.nan, home_sot=np.nan, away_sot=np.nan)

    def features():
        hist = pd.read_parquet(fd.OUT_PATH)
        return build_features(pd.concat([hist, upcoming], ignore_index=True))

    digest = hashlib.sha256(parquet.read_bytes()).hexdigest()
    before = features()
    # the board pipeline (stubbed models, same as tests/test_board.py) on the same history
    fx = tb.fixtures_frame()
    monkeypatch.setattr(bd.sc, "gather_fixtures", lambda start, days, use_calendar=True: fx)
    monkeypatch.setattr(bd.sc, "build_features", tb.fake_features)
    monkeypatch.setattr(bd.sc, "FEATURES_PATH", tmp_path / "features.parquet")
    monkeypatch.setattr(bd.sc, "CALENDAR_PATH", tmp_path / "no_calendar.parquet")
    monkeypatch.setattr(bd.sc, "latest_model", lambda name, last, age: tmp_path / "negbin_pit_comp_2026-10-01")
    monkeypatch.setattr(bd.sc, "load", lambda p: tb.StubModel())
    monkeypatch.setattr(bd.sc, "latest_no_odds_model", lambda name, played, age: (tb.StubModel(-2.0), "noodds_label"))
    board_before = bd.build(days=30, refresh=False, today=date(2026, 10, 8), out_path=tmp_path / "b1.parquet")

    write(tmp_path / "p", line())
    provisional.load_provisional_results(seeded, tmp_path / "p", now=LATER)
    assert q(seeded, "SELECT source FROM matches WHERE match_id = :m", m=M_DOM) == [("provisional",)]

    assert hashlib.sha256(parquet.read_bytes()).hexdigest() == digest  # the modelling cache is untouched
    pd.testing.assert_frame_equal(features(), before)
    board_after = bd.build(days=30, refresh=False, today=date(2026, 10, 8), out_path=tmp_path / "b2.parquet")
    pd.testing.assert_frame_equal(board_after, board_before)


def test_provisional_results_do_not_move_baselines_or_the_incremental_window(seeded, tmp_path):
    football_data_other = played([(*DOM[:3], str(D1 - timedelta(days=7)), "Sevilla", "Getafe", 10, 9)])
    football_data_other["home_team_id"], football_data_other["away_team_id"] = "spain:sevilla", "spain:getafe"
    load.upsert_matches(seeded, football_data_other)
    repo = Repository(seeded)
    before, since = repo.base_rates(24, LATER.date() + timedelta(days=1)), load.incremental_since(seeded)
    write(tmp_path / "p", line(hs="30", as_="30"))
    provisional.load_provisional_results(seeded, tmp_path / "p", now=LATER)
    assert repo.base_rates(24, LATER.date() + timedelta(days=1)) == before
    assert load.incremental_since(seeded) == since


# ----------------------------------------------------------------------------- export and verifier

def export_at(engine, out, today):
    built = datetime.combine(today, datetime.min.time()).replace(hour=9, tzinfo=timezone.utc)
    export(out, Repository(engine), built_at=built)
    return {n: json.loads((out / f"{n}.json").read_text(encoding="utf-8")) for n in ("meta", "board", "results", "track", "surge")}


def test_export_flags_provisional_results_and_counts_them(seeded, tmp_path, migrated_db):
    today = LATER.date()
    write(tmp_path / "p", line())
    provisional.load_provisional_results(seeded, tmp_path / "p", now=LATER)
    docs = export_at(seeded, tmp_path / "out", today)
    for name, doc in docs.items():
        validate(name, doc)
    [res] = docs["results"]["results"]
    assert (res["id"], res["result_status"], res["result_source"], res["source_url"]) == (M_DOM, "provisional", "espn", URL)
    assert res["actual"] == {"total": 26, "home": 15, "away": 11} and "experimental" not in res
    assert (docs["meta"]["counts"]["results"], docs["meta"]["counts"]["provisional_results"]) == (1, 1)
    live = docs["track"]["records"]["live"]
    assert (live["settled"], live["provisional"]) == (1, 1)
    assert [(p["matches"], p["provisional"]) for p in live["daily"]] == [(1, 1)]
    assert docs["track"]["records"]["experimental"]["provisional"] == 0
    ok = verify_mod.verify(tmp_path / "out", migrated_db, today=today)
    assert ok["ok"], ok["problems"]

    football_data(seeded, hs=16, as_=11)  # confirmed: the same files now say football-data
    docs = export_at(seeded, tmp_path / "out2", today)
    [res] = docs["results"]["results"]
    assert (res["result_status"], res["result_source"], res["actual"]["total"]) == ("confirmed", "football-data", 27)
    assert "source_url" not in res and docs["meta"]["counts"]["provisional_results"] == 0
    assert docs["track"]["records"]["live"]["provisional"] == 0
    ok = verify_mod.verify(tmp_path / "out2", migrated_db, today=today)
    assert ok["ok"], ok["problems"]


@pytest.mark.parametrize("name, tamper, expect", [
    ("results", lambda d: d["results"][0].__setitem__("result_status", "confirmed"), "result_status"),
    ("results", lambda d: d["results"][0].__setitem__("result_source", "football-data"), "result_source"),
    ("results", lambda d: d["results"][0].pop("source_url"), "source_url"),
    ("track", lambda d: d["records"]["live"].__setitem__("provisional", 0), "live.provisional"),
    ("track", lambda d: d["records"]["live"]["daily"][0].__setitem__("provisional", 0), "live.daily"),
    ("meta", lambda d: d["counts"].__setitem__("provisional_results", 0), "meta: counts"),
])
def test_verifier_detects_tampered_provisional_fields(seeded, tmp_path, migrated_db, name, tamper, expect):
    write(tmp_path / "p", line())
    provisional.load_provisional_results(seeded, tmp_path / "p", now=LATER)
    out = tmp_path / "out"
    docs = export_at(seeded, out, LATER.date())
    tamper(docs[name])
    (out / f"{name}.json").write_text(json.dumps(docs[name]), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db, today=LATER.date())
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


# ----------------------------------------------------------------------------- worker, CLI, migration

def test_worker_runs_when_the_provisional_results_change(engine, tmp_path):
    d = tmp_path / "p"
    write(d)  # header only, nothing recorded yet: nothing due
    assert not due.provisional_results_changed(engine, d)
    write(d, line())
    assert due.provisional_results_changed(engine, d)
    jobs.finish(engine, jobs.start(engine, "update"), "ok", {"db-provisional": {"fingerprint": pr.fingerprint(d)}})
    assert not due.provisional_results_changed(engine, d)
    write(d, line(hs="16"))
    since = jobs.utcnow()
    reasons = due.due_work(engine, since, since, cal_dir=tmp_path, manual_dir=tmp_path / "none", provisional_dir=d)
    assert [r.code for r in reasons if r.code.endswith("-results")] == ["provisional-results"]


def test_cli_check_lists_matches_and_fails_on_problems(seeded, tmp_path, migrated_db, monkeypatch, capsys):
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    write(tmp_path / "ok", line(entered=z(KICK + timedelta(hours=2))))
    monkeypatch.setattr(provisional, "utcnow", lambda: LATER)
    db_cli.main(["provisional", "--check", "--dir", str(tmp_path / "ok")])
    out = json.loads(capsys.readouterr().out)
    assert [(m["match_id"], m["status"], m["total"]) for m in out["matched"]] == [(M_DOM, "provisional", 26)]
    assert q(seeded, "SELECT count(*) FROM provisional_results") == [(0,)]  # a dry run
    db_cli.main(["provisional", "--dir", str(tmp_path / "ok")])
    assert json.loads(capsys.readouterr().out)["inserted"] == 1
    db_cli.main(["provisional", "--agreement"])
    assert json.loads(capsys.readouterr().out)["summary"]["provisional"] == 1
    write(tmp_path / "bad", line(home="Nobody"))
    with pytest.raises(SystemExit) as exc:
        db_cli.main(["provisional", "--check", "--dir", str(tmp_path / "bad")])
    assert exc.value.code == 1 and "no Spain La Liga match" in capsys.readouterr().err


def test_migration_0007_upgrades_a_0006_database_and_roundtrips(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    migrate.upgrade(path, "0006_manual_results")
    e = make_engine(path)
    with e.connect() as c:
        views = set(c.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'view'")).all())
    e.dispose()
    assert migrate.upgrade(path) == "0007_provisional_results"
    e = make_engine(path)
    seed_db(e, tmp_path)
    write(tmp_path / "p", line())
    provisional.load_provisional_results(e, tmp_path / "p", now=LATER)
    with e.connect() as c:
        after = set(c.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'view'")).all())
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"))}
    e.dispose()
    assert {n for n, _ in after - views} == {"v_provisional_agreement"} and views <= after  # existing views untouched
    assert {"provisional_results", "provisional_result_edits", "trg_matches_provisional_confirmed",
            "trg_provisional_result_edits_no_delete"} <= names
    assert migrate.downgrade(path, "0006_manual_results") == "0006_manual_results"
    e = make_engine(path)
    with e.connect() as c:  # the provisionally settled match is back to its fixture state
        assert c.execute(text("SELECT status, source, home_shots FROM matches WHERE match_id = :m"), {"m": M_DOM}).all() == [
            ("scheduled", "football-data", None)]
        assert "provisional_results" not in {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
    e.dispose()
    assert migrate.upgrade(path) == "0007_provisional_results"
