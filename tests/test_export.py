"""Static JSON export (docs/static-api.md): contract shape and JSON Schema, no odds / source data,
valid curves, deterministic change-aware writing, the official-results rule, the independent verifier
and the `python -m shots update --export-dir` wiring."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

import shots.__main__ as cli
from shots.db import jobs, load
from shots.db import load as dbload
from shots.db.engine import db_path, make_engine
from shots.db.models import JobRun
from shots.db.repo import Repository
from shots.export import ExportError, export, league_id, render
from shots.export import __main__ as export_cli
from shots.export import verify as verify_mod
from shots.export.build import DEFAULT_TEAM, DEFAULT_TOTAL
from shots.ingest import calendar_ics, football_data
from shots.ingest.schema import make_match_id
from shots.predict import board as board_mod
from shots.predict import scan, track
from shots.web import filters as flt
from tests.test_db_integrity import local
from tests.test_db_load import fixtures, played, write_picks
from tests.test_web import META, make_board, row, seed_backfill
from tests.test_web_team import with_team

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs" / "static-api.schema.json").read_text(encoding="utf-8"))
NAMES = ("meta", "board", "results", "track", "surge")

# Predictions are official only when inserted (DB clock) before kick-off, so every match is in the
# future when it is predicted; results are loaded afterwards and the export is taken "as of" EXPORT_DAY.
NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
D1, D2, D3, D5 = (date.today() + timedelta(days=k) for k in (3, 4, 6, 12))
EXPORT_DAY = D2 + timedelta(days=1)
BUILT = datetime.combine(EXPORT_DAY, datetime.min.time()).replace(hour=9, tzinfo=timezone.utc)
KICK1 = datetime.combine(D1, datetime.min.time()).replace(hour=14)  # UTC

ODDS = {"odds_home": 1.23457, "odds_draw": 7.77713, "odds_away": 9.87651, "odds_over25": 1.66613, "odds_under25": 2.34567}
MKT = 0.12357  # five decimals: cannot collide with a published 4-decimal probability

FIX = [  # competition, country, tier, date, home, away, kickoff
    ("Spain La Liga", "Spain", 1, D1, "Betis", "Getafe", "16:00"),
    ("Spain La Liga", "Spain", 1, D2, "Sevilla", "Celta", "18:30"),
    ("England Premier League", "England", 1, D3, "Fulham", "Brentford", "15:00"),
    ("England Premier League", "England", 1, D5, "Hull", "Leeds", "15:00"),
    ("Italy Serie B", "Italy", 2, D5, "Bari", "Cesena", ""),
    ("Germany 2. Bundesliga", "Germany", 2, D5, "Köln", "Fürth", "13:00"),
]
M1, M2, M3, M4, M5, M6 = (make_match_id(c, pd.Timestamp(d), h, a) for c, _, _, d, h, a, _ in FIX)


def board_frame(shift: float = 0.0) -> pd.DataFrame:
    rows = []
    for (comp, country, tier, day, home, away, ko), mid, exp in zip(FIX, (M1, M2, M3, M4, M5, M6), (27, 22, 30, 25, 24, 28)):
        rows.append(row(mid, str(day), ko, comp, country, tier, home, away, exp + shift, odds=mid == M4, cold=mid == M5))
    df = with_team(pd.DataFrame(rows))
    df.loc[df["odds"], "mkt_p_over25"] = MKT
    return df


def meta_at(utc: datetime) -> dict:
    return dict(META, built_at=local(utc).isoformat(timespec="seconds"), results_up_to=str(date.today()))


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


@pytest.fixture
def seeded(engine, tmp_path, migrated_db):
    """Fixtures with odds -> three boards (the last one claims a build time after M1's kick-off) ->
    a live picks scan -> results for M1 (26 shots), M2 (19) and M3 (no shots data) -> a backfill."""
    fx = fixtures([(c, co, t, str(d), h, a, ko) for c, co, t, d, h, a, ko in FIX]).assign(**ODDS)
    fx["kickoff_utc"] = [pd.Timestamp(KICK1), pd.NaT, pd.NaT, pd.Timestamp(datetime.combine(D5, datetime.min.time()).replace(hour=14)),
                         pd.NaT, pd.NaT]
    load.upsert_fixtures(engine, fx, today=date.today())
    load.import_board(engine, board_frame(0), meta_at(NOW - timedelta(hours=2)))
    load.import_board(engine, board_frame(1), meta_at(NOW - timedelta(hours=1)))
    load.import_board(engine, board_frame(2), meta_at(KICK1 + timedelta(minutes=10)))  # after M1 kicked off
    picks = tmp_path / "picks"
    picks.mkdir()
    write_picks(picks / f"{date.today():%Y-%m-%d}_0001_at_least_24.csv", [
        (str(D1), "16:00", "Spain La Liga", "Betis", "Getafe", 0.80, 27.0, M1),
        (str(D2), "18:30", "Spain La Liga", "Sevilla", "Celta", 0.75, 22.0, M2),
        (str(D5), "15:00", "England Premier League", "Hull", "Leeds", 0.60, 25.0, M4)])
    load.import_picks_dir(engine, picks)
    res = played([("Spain La Liga", "Spain", 1, str(D1), "Betis", "Getafe", 14, 12),
                  ("Spain La Liga", "Spain", 1, str(D2), "Sevilla", "Celta", 10, 9),
                  ("England Premier League", "England", 1, str(D3), "Fulham", "Brentford", None, None)])
    res = res.assign(home_goals=2, away_goals=1, home_sot=5, away_sot=4, **ODDS)
    load.upsert_matches(engine, res)
    rng = np.random.default_rng(3)
    back = []
    for i in range(40):
        day = pd.Timestamp("2025-08-15") + pd.Timedelta(days=6 * i)
        actual = int(rng.integers(15, 34))
        for n in (22, 24):
            back.append(dict(match_id=f"bf{i:03d}", date=day, competition="Spain La Liga", N=n,
                             p=round(float(rng.uniform(0.3, 0.95)), 6), actual=actual))
    seed_backfill(migrated_db, pd.DataFrame(back))
    return engine


def run_export(engine, out, **kw):
    kw.setdefault("built_at", BUILT)
    return export(out, Repository(engine), **kw)


def read(out) -> dict:
    return {n: json.loads((Path(out) / f"{n}.json").read_text(encoding="utf-8")) for n in NAMES}


def validate(name: str, doc) -> None:
    jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]}).validate(doc)


# ----------------------------------------------------------------------------- contract

def test_export_follows_the_contract_and_the_json_schema(seeded, tmp_path):
    summary = run_export(seeded, tmp_path / "out")
    assert sorted(summary["written"]) == sorted(NAMES) and summary["fixtures"] == 3 and summary["results"] == 2
    docs = read(tmp_path / "out")
    for name in NAMES:
        validate(name, docs[name])

    fx = docs["board"]["fixtures"]
    assert [f["id"] for f in fx] == [M5, M6, M4]  # date, kick-off ("" first), league, home; played / past ones gone
    hull = fx[2]
    assert hull == hull | {"league": "england-premier-league", "date": str(D5), "kickoff_local": "15:00",
                           "kickoff_utc": f"{D5}T14:00:00Z", "home": "Hull", "away": "Leeds", "expected": 27,
                           "lb": {"70": 24, "80": 23, "90": 21}, "odds": True, "cold_start": False,
                           "round": "Regular Season - 8", "league_avg": 26.0, "above_league": 1.0}
    assert list(hull) == ["id", "league", "round", "date", "kickoff_local", "kickoff_utc", "home", "away", "expected",
                          "lb", "league_avg", "above_league", "odds", "cold_start", "model", "curve", "team"]
    assert set(hull["team"]) == {"home", "away"} and list(hull["team"]["home"]) == ["mean", "median", "lb80", "curve"]
    bari = fx[0]
    assert (bari["kickoff_local"], bari["kickoff_utc"], bari["cold_start"], bari["odds"]) == (None, None, True, False)
    assert fx[1]["league"] == "germany-2-bundesliga" and fx[1]["home"] == "Köln"

    meta = docs["meta"]
    assert meta["built_at"] == f"{EXPORT_DAY}T09:00:00Z" and meta["results_up_to"] == str(date.today())
    assert meta["thresholds"] == {"total": [board_mod.N_MIN, board_mod.N_MAX], "team": [3, 25],
                                  "default_total": DEFAULT_TOTAL, "default_team": DEFAULT_TEAM}
    assert (DEFAULT_TOTAL, DEFAULT_TEAM) == (flt.N_DEFAULT, flt.TN_DEFAULT)  # same defaults as the server-rendered app
    assert meta["counts"] == {"fixtures": 3, "with_odds": 1, "cold_start": 1, "results": 2,
                              "provisional_results": 0} and meta["days"] == [str(D5)]
    assert meta["last_job"] is None and meta["record"]["since"].endswith("Z")
    assert meta["leagues"] == [
        {"id": "england-premier-league", "name": "Premier League", "country": "England", "tier": 1, "fixtures": 1},
        {"id": "germany-2-bundesliga", "name": "2. Bundesliga", "country": "Germany", "tier": 2, "fixtures": 1},
        {"id": "italy-serie-b", "name": "Serie B", "country": "Italy", "tier": 2, "fixtures": 1},
        {"id": "spain-la-liga", "name": "La Liga", "country": "Spain", "tier": 1, "fixtures": 0}]  # results / track only

    live = docs["track"]["records"]["live"]
    assert (live["settled"], live["pending"], live["void"]) == (2, 1, 0)
    assert (live["by_n"]["24"]["n"], live["by_n"]["24"]["hits"], live["by_n"]["24"]["hit_rate"]) == (2, 1, 0.5)
    assert live["by_n"]["24"]["by_league"] == [{"league": "spain-la-liga", "n": 2, "hit_rate": 0.5, "stated": 0.775}]
    back = docs["track"]["records"]["backfill"]
    assert back["label"] == "2025/26 season (out of sample)" and sorted(back["by_n"]) == ["22", "24"]
    e = back["by_n"]["24"]
    assert e["selected_min_prob"] == 0.7 and 0 < e["n"] <= 40 and 0 <= e["brier"] <= 1 and e["brier_baseline"] is None
    assert sum(b["n"] for b in e["reliability"]) == 40 and e["wilson95"][0] <= e["hit_rate"] <= e["wilson95"][1]


def test_results_use_the_official_rule(seeded, tmp_path):
    """Per finished match: the last board prediction made and inserted before kick-off. The board that
    claims a build time after M1's kick-off does not count for M1; a match without shots data and
    matches outside the 30-day window are not listed."""
    run_export(seeded, tmp_path / "out")
    res = read(tmp_path / "out")["results"]["results"]
    assert [r["id"] for r in res] == [M2, M1]  # newest first; M3 (void) is absent
    m1 = res[1]
    assert m1["actual"] == {"total": 26, "home": 14, "away": 12} and m1["expected"] == 28  # board 2 (27 + 1), not board 3
    assert res[0]["expected"] == 24 and res[0]["actual"]["total"] == 19  # M2: board 3 was still pre-match (22 + 2)
    assert m1["league"] == "spain-la-liga" and m1["odds"] is False and len(m1["curve"]) == 36
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", m1["predicted_at"])
    # the insert time, not the board's claimed build time (days later); NOW is taken at import, so a long suite run
    # can put the insert well after it: allow hours, still far from any build time
    assert abs(datetime.fromisoformat(m1["predicted_at"][:-1]) - NOW) < timedelta(hours=3)
    assert list(m1) == ["id", "league", "date", "home", "away", "actual", "predicted_at", "expected", "lb", "odds", "curve",
                        "result_source", "result_status"]
    assert m1["result_source"] == "football-data" and m1["result_status"] == "confirmed" and "source_url" not in m1 and "experimental" not in m1
    late = D1 + timedelta(days=31)  # M1 is now older than 30 days, M2 exactly 30
    run_export(seeded, tmp_path / "late", today=late)
    assert [r["id"] for r in read(tmp_path / "late")["results"]["results"]] == [M2]


def test_no_odds_values_or_source_columns_are_published(seeded, tmp_path):
    run_export(seeded, tmp_path / "out")
    text = "".join((tmp_path / "out" / f"{n}.json").read_text(encoding="utf-8") for n in NAMES)
    for value in (*ODDS.values(), MKT):
        assert str(value) not in text and f"{value:.4f}" not in text and f"{value:.3f}" not in text
    # the only "source" words allowed: each result's result_source ("football-data" | "manual")
    text = text.replace('"result_source":"football-data"', "")
    for word in ("odds_home", "odds_draw", "odds_away", "odds_over", "odds_under", "mkt", "market", "football-data", "calendar", "source", "goals", "sot", "search_key", "team_id"):
        assert word not in text, word
    allowed = {"schema", "built_at", "results_up_to", "last_job", "status", "finished_at", "models", "thresholds",
               "total", "team", "default_total", "default_team", "leagues", "id", "name", "country", "tier", "fixtures",
               "days", "counts", "with_odds", "cold_start", "record", "since", "league", "round", "date", "kickoff_local",
               "kickoff_utc", "home", "away", "expected", "lb", "70", "80", "90", "league_avg", "above_league", "odds",
               "model", "curve", "mean", "median", "lb80", "results", "actual", "predicted_at", "records", "backfill",
               "live", "label", "by_n", "selected_min_prob", "n", "hits", "hit_rate", "mean_stated", "wilson95", "brier",
               "brier_baseline", "reliability", "band", "stated", "realised", "by_league", "settled", "pending", "void",
               "22", "24", "result_source", "experimental", "daily", "weekly", "matches", "mae", "bias",
               "floor80_held", "picks", "result_status", "provisional", "provisional_results",
               "awaiting_results", "official", "oldest",
               # surge.json (Surge Watch catalogue) and a fixture's `surge` labels
               "report_date", "margin", "period", "method", "routes", "noodds", "base_rate", "signals", "direction",
               "explanation", "over_rate", "stated_rate", "excess_ci", "seasons_held", "seasons", "applies",
               "surge", "against",
               # surge.json `stacking` (2026-10-28), a fixture's `strong` flag and table `context`
               "stacking", "min_boosts", "note", "groups", "phases", "share", "by_count", "season_phase",
               "strong_applies", "0", "1", "2", "3+", "2+", "early", "middle", "late", "run_in", "strong",
               "context", "phase", "pos", "of", "pts", "gp", "left", "to_top", "to_safety"}

    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)

    docs = read(tmp_path / "out")
    assert {k for d in docs.values() for k in keys(d)} <= allowed
    assert all(isinstance(f["odds"], bool) for f in docs["board"]["fixtures"] + docs["results"]["results"])


def test_curves_are_non_increasing_and_aligned_with_the_thresholds(seeded, tmp_path):
    run_export(seeded, tmp_path / "out")
    docs = read(tmp_path / "out")
    th = docs["meta"]["thresholds"]
    n_total, n_team = th["total"][1] - th["total"][0] + 1, th["team"][1] - th["team"][0] + 1
    src = board_frame(2).set_index("match_id")
    curves = 0
    for f in docs["board"]["fixtures"]:
        assert f["curve"] == [src.loc[f["id"], f"p_{n}"] for n in range(th["total"][0], th["total"][1] + 1)]
        assert f["team"]["away"]["curve"] == [src.loc[f["id"], f"pa_{n}"] for n in range(th["team"][0], th["team"][1] + 1)]
        for c, size in ((f["curve"], n_total), (f["team"]["home"]["curve"], n_team), (f["team"]["away"]["curve"], n_team)):
            assert len(c) == size and all(0 <= v <= 1 and round(v, 4) == v for v in c)
            assert all(b <= a for a, b in zip(c, c[1:]))
            curves += 1
    for r in docs["results"]["results"]:
        assert len(r["curve"]) == n_total and all(b <= a for a, b in zip(r["curve"], r["curve"][1:]))
    assert curves == 9


def test_an_invalid_curve_aborts_the_export_and_writes_nothing(engine, tmp_path):
    bad = board_frame()
    bad["p_30"] = bad["p_29"] + 0.01  # P(total >= 30) above P(total >= 29)
    load.import_board(engine, bad, meta_at(NOW))
    with pytest.raises(ExportError, match="non-increasing"):
        run_export(engine, tmp_path / "out", today=date.today())
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_board_without_team_curves_exports_team_null(engine, tmp_path):
    load.import_board(engine, make_board(), dict(META))
    run_export(engine, tmp_path / "out", today=date(2026, 10, 8))
    docs = read(tmp_path / "out")
    for name in NAMES:
        validate(name, docs[name])
    assert docs["meta"]["thresholds"]["team"] is None and all(f["team"] is None for f in docs["board"]["fixtures"])
    assert [f["id"] for f in docs["board"]["fixtures"]] == ["a", "b", "c", "d"]
    assert docs["track"]["records"]["backfill"] is None and docs["results"]["results"] == []


def test_missing_database_exports_nothing(tmp_path, capsys):
    with pytest.raises(ExportError, match="missing"):
        export(tmp_path / "out")
    with pytest.raises(SystemExit) as exc:
        export_cli.main(["--out", str(tmp_path / "out")])
    assert exc.value.code == 2 and "export failed" in capsys.readouterr().err
    assert not db_path().exists() and not (tmp_path / "out").exists()


# ----------------------------------------------------------------------------- determinism

def test_unchanged_database_rewrites_nothing(seeded, tmp_path):
    out = tmp_path / "out"
    run_export(seeded, out)
    before = {n: (out / f"{n}.json").read_bytes() for n in NAMES}
    assert all(b"\r" not in b and b.endswith(b"\n") for b in before.values())
    stamps = {n: (out / f"{n}.json").stat().st_mtime_ns for n in NAMES}
    again = run_export(seeded, out, built_at=BUILT + timedelta(hours=3))
    assert again["written"] == [] and {n: (out / f"{n}.json").read_bytes() for n in NAMES} == before
    assert {n: (out / f"{n}.json").stat().st_mtime_ns for n in NAMES} == stamps
    fresh = run_export(seeded, tmp_path / "other", built_at=BUILT + timedelta(hours=3))  # same bytes from scratch
    assert sorted(fresh["written"]) == sorted(NAMES)
    assert all((tmp_path / "other" / f"{n}.json").read_bytes() == before[n] for n in ("board", "results", "track"))

    always = run_export(seeded, out, built_at=BUILT + timedelta(hours=3), meta="always")
    assert always["written"] == ["meta"] and read(out)["meta"]["built_at"] == f"{EXPORT_DAY}T12:00:00Z"


def test_meta_is_rewritten_only_for_real_changes(seeded, tmp_path):
    out = tmp_path / "out"
    run_export(seeded, out)
    jobs.finish(seeded, jobs.start(seeded, "update"), "ok", {})
    assert run_export(seeded, out)["written"] == ["meta"]  # last_job appeared
    assert read(out)["meta"]["last_job"]["status"] == "ok"
    first = read(out)["meta"]["last_job"]["finished_at"]
    jobs.finish(seeded, jobs.start(seeded, "update"), "ok", {})  # another ok run: only its end time moved
    assert run_export(seeded, out, built_at=BUILT + timedelta(hours=3))["written"] == []
    assert read(out)["meta"]["last_job"]["finished_at"] == first
    jobs.finish(seeded, jobs.start(seeded, "update"), "partial", {}, "results: no network")
    assert run_export(seeded, out)["written"] == ["meta"] and read(out)["meta"]["last_job"]["status"] == "partial"
    jobs.start(seeded, "update")  # a running job is not the last finished one
    assert run_export(seeded, out)["written"] == []
    load.import_board(seeded, board_frame(3), meta_at(KICK1 + timedelta(minutes=20)))  # a new board
    assert run_export(seeded, out, built_at=BUILT + timedelta(hours=6))["written"] == ["board", "meta"]
    (out / "meta.json").write_text("not json", encoding="utf-8")
    assert run_export(seeded, out)["written"] == ["meta"]


def test_render_is_line_oriented_and_stable():
    doc = {"schema": 1, "fixtures": [{"id": "a", "home": "Köln", "curve": [1.0, 0.5]}, {"id": "b"}]}
    text = render("board", doc)
    assert text == ('{\n "schema": 1,\n "fixtures": [\n  {"id":"a","home":"Köln","curve":[1.0,0.5]},\n  {"id":"b"}\n ]\n}\n')
    assert json.loads(text) == doc and json.loads(render("results", {"schema": 1, "results": []})) == {"schema": 1, "results": []}
    assert league_id("Germany 2. Bundesliga") == "germany-2-bundesliga" and league_id("Türkiye Süper Lig") == "turkiye-super-lig"


def test_schema_accepts_the_examples_of_the_contract():
    """docs/static-api.md is the binding contract: its own examples must validate (and the schema is
    closed, so an unknown key is rejected)."""
    md = (ROOT / "docs" / "static-api.md").read_text(encoding="utf-8")
    blocks = dict(re.findall(r"## `data/(\w+)\.json`.*?```json\n(.*?)```", md, flags=re.S))
    assert set(blocks) == set(NAMES)
    for name, block in blocks.items():
        validate(name, json.loads(block.replace("…", "x")))  # the document's placeholder for ids
    leaky = json.loads(blocks["board"])
    leaky["fixtures"][0]["odds_home"] = 2.1
    with pytest.raises(jsonschema.ValidationError):
        validate("board", leaky)


# ----------------------------------------------------------------------------- independent verifier

def test_verifier_agrees_with_the_export(seeded, tmp_path, migrated_db):
    run_export(seeded, tmp_path / "out")
    res = verify_mod.verify(tmp_path / "out", migrated_db)
    assert res["ok"], res["problems"]
    assert res["checked"] == {"board fixtures": 3, "results": 2, "track live thresholds": 1, "track backfill thresholds": 2,
                              "track live daily": 2, "track live weekly": res["checked"]["track live weekly"],
                              "track experimental thresholds": 0, "track experimental daily": 0,
                              "track experimental weekly": 0, "surge signals": len(verify_mod.json.loads(verify_mod.SURGE_TABLE.read_text(encoding="utf-8"))["signals"]), "surge labelled fixtures": 0,
                              "context fixtures": 0, "results with surge labels": 0, "results with context": 0}
    with pytest.raises(SystemExit) as exc:
        verify_mod.main(["--dir", str(tmp_path / "out"), "--db", str(migrated_db)])
    assert exc.value.code == 0


@pytest.mark.parametrize("name, tamper, expect", [
    ("board", lambda d: d["fixtures"][0]["curve"].__setitem__(3, 0.0001), "curve differs"),
    ("board", lambda d: d["fixtures"].pop(), "fixture ids or order differ"),
    ("board", lambda d: d["fixtures"][1].__setitem__("expected", 99), "expected"),
    ("board", lambda d: d["fixtures"][2]["team"]["home"].__setitem__("lb80", 1), "team.home.lb80"),
    ("board", lambda d: d["fixtures"][0].__setitem__("odds_home", 2.5), "privacy"),
    ("results", lambda d: d["results"][0]["actual"].__setitem__("total", 40), "actual"),
    ("results", lambda d: d["results"][1].__setitem__("expected", 29), "expected / lb"),  # board 3's value for M1
    ("results", lambda d: d["results"].pop(), "ids differ"),
    ("track", lambda d: d["records"]["live"]["by_n"]["24"].__setitem__("hits", 2), "n/hits"),
    ("track", lambda d: d["records"]["live"].__setitem__("pending", 0), "live.pending"),
    ("meta", lambda d: d["counts"].__setitem__("fixtures", 99), "counts"),
    ("meta", lambda d: d["thresholds"].__setitem__("total", [10, 44]), "thresholds.total"),
])
def test_verifier_detects_differences(seeded, tmp_path, migrated_db, name, tamper, expect):
    out = tmp_path / "out"
    run_export(seeded, out)
    doc = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
    tamper(doc)
    (out / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db)
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]
    with pytest.raises(SystemExit) as exc:
        verify_mod.main(["--dir", str(out), "--db", str(migrated_db)])
    assert exc.value.code == 1


def test_verifier_reads_the_database_read_only(seeded, tmp_path, migrated_db):
    run_export(seeded, tmp_path / "out")
    seeded.dispose()
    with sqlite3.connect(migrated_db) as c:
        before = c.execute("SELECT count(*), max(id) FROM job_runs").fetchone(), c.execute("PRAGMA data_version").fetchone()
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]
    with sqlite3.connect(migrated_db) as c:
        assert (c.execute("SELECT count(*), max(id) FROM job_runs").fetchone(), c.execute("PRAGMA data_version").fetchone()) == before
    with pytest.raises(SystemExit) as exc:
        verify_mod.main(["--dir", str(tmp_path / "out"), "--db", str(tmp_path / "nope.db")])
    assert exc.value.code == 2 and not (tmp_path / "nope.db").exists()


# ----------------------------------------------------------------------------- python -m shots update

def _stub_update(monkeypatch, with_board: bool):
    ok = lambda *a, **k: {"ok": 1}  # noqa: E731
    monkeypatch.setattr(football_data, "build", ok)
    monkeypatch.setattr(calendar_ics, "build", ok)
    monkeypatch.setattr(cli, "_calendars_changed", lambda: False)
    monkeypatch.setattr(scan, "scan", ok)
    monkeypatch.setattr(board_mod, "build", ok)
    monkeypatch.setattr(track, "build_dashboard", ok)
    monkeypatch.setattr(dbload, "sync_results", ok)
    monkeypatch.setattr(dbload, "sync_manual_results", ok)
    monkeypatch.setattr(dbload, "sync_predictions", ok)
    monkeypatch.setattr(dbload, "import_board_file",
                        (lambda engine: load.import_board(engine, board_frame(), meta_at(NOW))) if with_board else ok)


def _job():
    engine = make_engine()
    with engine.connect() as c:
        rows = c.execute(select(JobRun.status, JobRun.counts, JobRun.error, JobRun.finished_at)).all()
    engine.dispose()
    return rows


def test_update_does_not_export_by_default(monkeypatch, tmp_path):
    _stub_update(monkeypatch, with_board=True)
    monkeypatch.chdir(tmp_path)
    assert cli.update() == 0
    assert not list(tmp_path.rglob("meta.json")) and "export" not in _job()[0][1]["steps"]


@pytest.mark.parametrize("how", ["argument", "environment"])
def test_update_exports_last_when_asked_and_meta_reports_this_run(monkeypatch, tmp_path, how):
    _stub_update(monkeypatch, with_board=True)
    out = tmp_path / "site" / "data"
    if how == "environment":
        monkeypatch.setenv("SHOTS_EXPORT_DIR", str(out))
        assert cli.update() == 0
    else:
        assert cli.update(export_dir=str(out)) == 0
    [(status, counts, error, finished)] = _job()
    meta = read(out)["meta"]
    assert status == "ok" and error is None and "export" not in counts["steps"]
    assert meta["last_job"] == {"status": "ok", "finished_at": finished.replace(microsecond=0).isoformat() + "Z"}
    assert meta["counts"]["fixtures"] == 6
    assert verify_mod.verify(out)["ok"]  # SHOTS_DB: the per-test database


def test_failed_export_makes_the_update_partial(monkeypatch, tmp_path):
    _stub_update(monkeypatch, with_board=False)  # no board in the database: nothing to export
    assert cli.update(export_dir=str(tmp_path / "out")) == 1
    [(status, counts, error, _)] = _job()
    assert status == "partial" and counts["steps"]["export"] is False and counts["steps"]["db"] is True
    assert "export:" in error and not (tmp_path / "out").exists()


def test_cli_passes_the_export_dir(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "update", lambda *a: seen.setdefault("args", a) and 0)
    with pytest.raises(SystemExit):
        cli.main(["update", "--export-dir", str(tmp_path / "x")])
    assert seen["args"] == (30, 24, 0.70, str(tmp_path / "x"))


def test_meta_counts_finished_matches_still_waiting_for_a_result(seeded, tmp_path):
    run_export(seeded, tmp_path / "out")
    meta = read(tmp_path / "out")["meta"]
    validate("meta", meta)
    want = Repository(seeded).awaiting_results()
    assert meta["awaiting_results"] == {"official": want["official"], "experimental": want["experimental"],
                                        "oldest": want["oldest"].isoformat() if want["oldest"] else None}
    # measured against the board run, not the clock: a second export gives the same numbers
    run_export(seeded, tmp_path / "again", built_at=BUILT + timedelta(days=3))
    assert read(tmp_path / "again")["meta"]["awaiting_results"] == meta["awaiting_results"]


def test_awaiting_results_counts_predicted_matches_finished_before_the_board_run(seeded):
    repo = Repository(seeded)
    assert repo.awaiting_results() == {"official": 0, "experimental": 0, "oldest": None}  # nothing finished yet
    later = datetime.combine(D5, datetime.min.time()) + timedelta(days=2)
    load.import_board(seeded, board_frame(3), meta_at(later))
    got = repo.awaiting_results()
    assert got["official"] >= 1 and got["experimental"] == 0  # Hull v Leeds (D5) has a pre-match prediction, no result
    assert got["oldest"] is not None and got["oldest"] <= D5
