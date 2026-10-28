"""Surge Watch in the static export: surge.json (the committed catalogue, copied), fixture `surge` labels (exactly
the board run's, checked against the catalogue), JSON Schema, the independent verifier."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from shots.db import load
from shots.db.repo import Repository
from shots.evaluation import surge as sv
from shots.export import ExportError, verify as verify_mod
from shots.export.build import build_surge
from tests.test_export import EXPORT_DAY, KICK1, M1, M2, M4, M5, M6, NAMES, board_frame, engine, meta_at, read, run_export, seeded, validate  # noqa: F401

LABELS = {M4: {"signals": ["dead_rubber", "h2h_above"], "against": ["winter"], "strong": True},  # odds route, 2+
          M6: {"signals": ["home_mismatch"], "against": []},     # no-odds route
          M5: {"signals": [], "against": []}}                                       # assessed, nothing found


def label(engine, fixtures: dict, version: str | None = None) -> None:
    """Record surge labels the way a board build does: an identical rebuild only refreshes the run's meta."""
    meta = meta_at(KICK1 + timedelta(minutes=20))
    meta["surge"] = {"version": version or sv.table_version(sv.load_table()), "fixtures": fixtures}
    assert load.import_board(engine, board_frame(2), meta)["status"] == "unchanged"


def test_catalogue_is_the_committed_table_and_follows_the_schema(seeded, tmp_path):
    run_export(seeded, tmp_path / "out")
    doc = read(tmp_path / "out")["surge"]
    validate("surge", doc)
    table = sv.load_table()
    assert doc == build_surge(table)
    assert [s["id"] for s in doc["signals"]] == [s["id"] for s in table["signals"]]
    dr = next(s for s in doc["signals"] if s["id"] == "dead_rubber")
    # the research's own numbers (reports/2026-10-26_overshoot_patterns.md, section 3a): 27.4% vs 21.5% stated
    assert dr["routes"]["odds"] == {"n": 1341, "over_rate": 0.2737, "stated_rate": 0.2146, "base_rate": 0.2142,
                                    "excess_ci": [0.0332, 0.0824], "seasons_held": 7, "seasons": 7, "applies": True}
    assert dr["routes"]["noodds"]["applies"] is False  # fin_c already absorbs it: shown, never used to label
    assert doc["matches"] == 49643 and doc["margin"] == 5 and "lean, not a promise" in doc["method"]
    for word in ("odds_", "mkt", "market", "goals", "source"):
        assert word not in json.dumps(doc)


def test_fixtures_carry_exactly_the_run_labels(seeded, tmp_path, migrated_db):
    label(seeded, LABELS)
    run_export(seeded, tmp_path / "out")
    docs = read(tmp_path / "out")
    validate("board", docs["board"])
    got = {f["id"]: f.get("surge") for f in docs["board"]["fixtures"]}
    assert got == LABELS
    assert list(next(f for f in docs["board"]["fixtures"] if f["id"] == M4))[-1] == "surge"
    res = verify_mod.verify(tmp_path / "out", migrated_db)
    assert res["ok"], res["problems"]
    assert res["checked"]["surge labelled fixtures"] == 3


def test_unassessed_fixtures_have_no_surge_key(seeded, tmp_path, migrated_db):
    label(seeded, {M4: LABELS[M4]})
    run_export(seeded, tmp_path / "out")
    fx = {f["id"]: f for f in read(tmp_path / "out")["board"]["fixtures"]}
    assert "surge" in fx[M4] and "surge" not in fx[M5] and "surge" not in fx[M6]
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]


def test_labels_of_another_catalogue_version_are_not_published(seeded, tmp_path, migrated_db):
    label(seeded, LABELS, version="0000000000000000")
    run_export(seeded, tmp_path / "out")
    assert all("surge" not in f for f in read(tmp_path / "out")["board"]["fixtures"])
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]


@pytest.mark.parametrize("bad", [
    {M6: {"signals": ["dead_rubber"], "against": []}},              # does not apply on the no-odds route
    {M4: {"signals": ["winter"], "against": []}},                   # a damper listed as a boost
    {M4: {"signals": ["dead_team", "last_three"], "against": []}},  # two season-end labels
    {M4: {"signals": ["no_such_signal"], "against": []}},
    {M4: {"signals": ["h2h_above"]}},                               # malformed
])
def test_a_label_the_catalogue_does_not_allow_aborts_the_export(seeded, tmp_path, bad):
    label(seeded, bad)
    with pytest.raises(ExportError, match="surge"):
        run_export(seeded, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_unchanged_labels_rewrite_nothing(seeded, tmp_path):
    label(seeded, LABELS)
    out = tmp_path / "out"
    run_export(seeded, out)
    before = {n: (out / f"{n}.json").read_bytes() for n in NAMES}
    assert run_export(seeded, out)["written"] == []
    assert {n: (out / f"{n}.json").read_bytes() for n in NAMES} == before


@pytest.mark.parametrize("name, tamper, expect", [
    ("board", lambda d: d["fixtures"][0]["surge"].__setitem__("signals", ["low_goal_run"]), "differ from the board run"),
    ("board", lambda d: d["fixtures"][2].pop("surge"), "differ from the board run"),
    ("board", lambda d: d["fixtures"][0].__setitem__("surge", None), "never null"),
    ("surge", lambda d: d["signals"][0]["routes"]["odds"].__setitem__("over_rate", 0.31), "committed catalogue"),
    ("surge", lambda d: d["signals"].pop(), "committed catalogue"),
    ("surge", lambda d: d["routes"]["odds"].__setitem__("odds_home", 1.5), "privacy"),
])
def test_verifier_detects_surge_differences(seeded, tmp_path, migrated_db, name, tamper, expect):
    label(seeded, LABELS)
    out = tmp_path / "out"
    run_export(seeded, out)
    doc = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
    tamper(doc)
    (out / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db)
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


def test_verifier_rejects_a_label_not_allowed_on_the_route(seeded, tmp_path, migrated_db):
    """Even when the run itself recorded it (a corrupted run), a label must apply on the fixture's route."""
    label(seeded, LABELS)
    out = tmp_path / "out"
    run_export(seeded, out)
    with seeded.begin() as c:
        rid = Repository(seeded).board_meta()["run_id"]
        meta = json.loads(c.exec_driver_sql("SELECT meta FROM prediction_runs WHERE id = ?", (rid,)).scalar())
        meta["surge"]["fixtures"][M6] = {"signals": ["dead_rubber"], "against": []}
        c.exec_driver_sql("UPDATE prediction_runs SET meta = ? WHERE id = ?", (json.dumps(meta), rid))
    doc = json.loads((out / "board.json").read_text(encoding="utf-8"))
    next(f for f in doc["fixtures"] if f["id"] == M6)["surge"] = {"signals": ["dead_rubber"], "against": []}
    (out / "board.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db)
    assert not res["ok"] and any("does not apply" in p or "not a boost that applies" in p for p in res["problems"]), res["problems"]


# ----------------------------------------------------------------------------- table context on board fixtures

def side(pos, of=20, pts=21, gp=9, left=29, to_top=2, to_safety=12) -> dict:
    return {"pos": pos, "of": of, "pts": pts, "gp": gp, "left": left, "to_top": to_top, "to_safety": to_safety}


CONTEXT = {M4: {"phase": "middle", "home": side(3), "away": side(17, pts=8, to_top=15, to_safety=-1)},
           M6: {"phase": None, "home": side(1, left=None, to_top=0),
                "away": side(None, pts=0, gp=0, left=None, to_top=21, to_safety=0)}}


def label_ctx(engine, fixtures: dict, context: dict, version: str | None = None) -> None:
    meta = meta_at(KICK1 + timedelta(minutes=20))
    meta["surge"] = {"version": version or sv.table_version(sv.load_table()), "fixtures": fixtures, "context": context}
    assert load.import_board(engine, board_frame(2), meta)["status"] == "unchanged"


def test_fixtures_carry_the_recorded_table_context(seeded, tmp_path, migrated_db):
    label_ctx(seeded, LABELS, CONTEXT)
    run_export(seeded, tmp_path / "out")
    docs = read(tmp_path / "out")
    validate("board", docs["board"])
    fx = {f["id"]: f for f in docs["board"]["fixtures"]}
    assert fx[M4]["context"] == CONTEXT[M4] and fx[M6]["context"] == CONTEXT[M6] and "context" not in fx[M5]
    assert list(fx[M4])[-2:] == ["surge", "context"] and list(fx[M4]["context"]["home"]) == list(side(1))
    res = verify_mod.verify(tmp_path / "out", migrated_db)
    assert res["ok"], res["problems"]
    assert res["checked"]["context fixtures"] == 2


def test_context_does_not_depend_on_the_catalogue_version(seeded, tmp_path, migrated_db):
    label_ctx(seeded, LABELS, CONTEXT, version="0000000000000000")
    run_export(seeded, tmp_path / "out")
    fx = {f["id"]: f for f in read(tmp_path / "out")["board"]["fixtures"]}
    assert "surge" not in fx[M4] and fx[M4]["context"] == CONTEXT[M4]
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]


@pytest.mark.parametrize("bad", [
    {"phase": "playoffs", "home": side(3), "away": side(4)},          # unknown phase
    {"phase": "middle", "home": side(21), "away": side(4)},            # position outside the league
    {"phase": "middle", "home": side(3, to_top=-1), "away": side(4)},  # ahead of the leader
    {"phase": None, "home": side(3), "away": side(4)},                 # phase undefined but games left known
    {"phase": "late", "home": side(3, pts=2.5), "away": side(4)},      # not a whole number
    {"phase": "late", "home": side(3)},                                # malformed
])
def test_a_malformed_context_aborts_the_export(seeded, tmp_path, bad):
    label_ctx(seeded, LABELS, {M4: bad})
    with pytest.raises(ExportError, match="context|phase"):
        run_export(seeded, tmp_path / "out")


@pytest.mark.parametrize("tamper, expect", [
    (lambda f: f["context"]["home"].__setitem__("pos", 4), "differs from the board run"),
    (lambda f: f.pop("context"), "differs from the board run"),
    (lambda f: f.__setitem__("context", None), "never null"),
])
def test_verifier_detects_context_differences(seeded, tmp_path, migrated_db, tamper, expect):
    label_ctx(seeded, LABELS, CONTEXT)
    out = tmp_path / "out"
    run_export(seeded, out)
    doc = json.loads((out / "board.json").read_text(encoding="utf-8"))
    tamper(next(f for f in doc["fixtures"] if f["id"] == M4))
    (out / "board.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db)
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


# ----------------------------------------------------------------------------- labels of settled results
# M1's result comes from board 2 (board 3 was built after its kick-off), M2's from board 3 (tests/test_export.py).

def set_run_surge(engine, k: int, fixtures: dict, version: str = "1111111111111111") -> int:
    """Record surge labels in the k-th board run (0 = oldest), as its build would have; -> run id."""
    with engine.begin() as c:
        rid = c.exec_driver_sql("SELECT id FROM prediction_runs WHERE kind = 'board' ORDER BY built_at, id").all()[k][0]
        meta = json.loads(c.exec_driver_sql("SELECT meta FROM prediction_runs WHERE id = ?", (rid,)).scalar() or "{}")
        meta["surge"] = {"version": version, "fixtures": fixtures}
        c.exec_driver_sql("UPDATE prediction_runs SET meta = ? WHERE id = ?", (json.dumps(meta), rid))
    return rid


def test_results_carry_the_labels_of_their_own_pre_match_run(seeded, tmp_path, migrated_db):
    set_run_surge(seeded, 1, {M1: {"signals": ["h2h_above"], "against": []},
                              M2: {"signals": ["low_goal_run"], "against": []}})
    set_run_surge(seeded, 2, {M2: {"signals": [], "against": ["winter"]},
                              M1: {"signals": ["dead_rubber"], "against": []}})  # after M1's kick-off: not M1's run
    run_export(seeded, tmp_path / "out")
    docs = read(tmp_path / "out")
    validate("results", docs["results"])
    got = {r["id"]: r.get("surge") for r in docs["results"]["results"]}
    assert got == {M1: {"signals": ["h2h_above"], "against": []}, M2: {"signals": [], "against": ["winter"]}}
    assert list(next(r for r in docs["results"]["results"] if r["id"] == M1))[-1] == "surge"
    res = verify_mod.verify(tmp_path / "out", migrated_db)
    assert res["ok"], res["problems"]
    assert res["checked"]["results with surge labels"] == 2


def test_results_of_unassessed_fixtures_or_unlabelled_runs_have_no_key(seeded, tmp_path, migrated_db):
    set_run_surge(seeded, 2, {M5: {"signals": ["h2h_above"], "against": []}})  # M2 not assessed in its run
    run_export(seeded, tmp_path / "out")
    assert all("surge" not in r for r in read(tmp_path / "out")["results"]["results"])
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]


def test_result_labels_are_published_as_recorded_and_deterministic(seeded, tmp_path, migrated_db):
    """Recorded with the catalogue of that day (another version, a label today's catalogue would not put on that
    route): published as recorded, never re-judged; the same database gives the same bytes."""
    set_run_surge(seeded, 2, {M2: {"signals": ["dead_rubber", "some_future_signal"], "against": []}}, version="old")
    out = tmp_path / "out"
    run_export(seeded, out)
    m2 = next(r for r in read(out)["results"]["results"] if r["id"] == M2)
    assert m2["surge"] == {"signals": ["dead_rubber", "some_future_signal"], "against": []}
    assert verify_mod.verify(out, migrated_db)["ok"]
    before = (out / "results.json").read_bytes()
    assert "results" not in run_export(seeded, out)["written"] and (out / "results.json").read_bytes() == before


@pytest.mark.parametrize("bad", [{"signals": ["h2h_above"]}, {"signals": ["a", "a"], "against": []},
                                 {"signals": ["Not An Id"], "against": []}])
def test_malformed_result_labels_abort_the_export(seeded, tmp_path, bad):
    set_run_surge(seeded, 2, {M2: bad})
    with pytest.raises(ExportError, match="surge"):
        run_export(seeded, tmp_path / "out")


@pytest.mark.parametrize("tamper, expect", [
    (lambda r: r["surge"].__setitem__("signals", ["low_goal_run"]), "differs from its board run"),
    (lambda r: r.pop("surge"), "differs from its board run"),
    (lambda r: r.__setitem__("surge", None), "never null"),
])
def test_verifier_detects_result_label_differences(seeded, tmp_path, migrated_db, tamper, expect):
    set_run_surge(seeded, 2, {M2: {"signals": ["h2h_above"], "against": []}})
    out = tmp_path / "out"
    run_export(seeded, out)
    doc = json.loads((out / "results.json").read_text(encoding="utf-8"))
    tamper(next(r for r in doc["results"] if r["id"] == M2))
    (out / "results.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db)
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


def test_repository_returns_labels_by_run(seeded):
    rid = set_run_surge(seeded, 1, {M1: {"signals": ["h2h_above"], "against": []}}, version="v1")
    repo = Repository(seeded)
    got = repo.run_surge_labels([rid, rid, None, 999999])
    assert got == {rid: {"version": "v1", "fixtures": {M1: {"signals": ["h2h_above"], "against": []}}, "context": {}}}
    assert repo.run_surge_labels([]) == {}
    rows = repo.settled_results(None, EXPORT_DAY, experimental=None)
    assert {r["match_id"]: r["run_id"] for r in rows}[M1] == rid


# ----------------------------------------------------------------------------- strong candidates, stacking (2026-10-28)

def test_surge_json_carries_the_stacking_table(seeded, tmp_path, migrated_db):
    run_export(seeded, tmp_path / "out")
    doc = read(tmp_path / "out")["surge"]
    validate("surge", doc)
    st = sv.load_table()["stacking"]
    got = doc["stacking"]
    assert got["min_boosts"] == st["min_boosts"] == 2
    assert got["routes"]["odds"]["strong_applies"] is True and got["routes"]["noodds"]["strong_applies"] is False
    assert got["routes"]["odds"]["by_count"]["2+"] == st["routes"]["odds"]["by_count"]["2+"]
    assert set(got["routes"]["odds"]) == {"signals", "share", "by_count", "season_phase", "strong_applies"}
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]


@pytest.mark.parametrize("bad", [
    {M4: {"signals": ["dead_rubber", "h2h_above"], "against": []}},                     # 2+ on odds, strong missing
    {M4: {"signals": ["h2h_above"], "against": [], "strong": True}},                     # one boost is never strong
    {M6: {"signals": ["h2h_above", "home_mismatch"], "against": [], "strong": True}},    # no-odds route: not allowed
    {M4: {"signals": ["dead_rubber", "h2h_above"], "against": [], "strong": "yes"}},
])
def test_strong_must_follow_the_catalogue_rule(seeded, tmp_path, bad):
    label(seeded, bad)
    with pytest.raises(ExportError, match="strong"):
        run_export(seeded, tmp_path / "out")


def test_no_odds_fixture_with_two_signals_is_not_strong(seeded, tmp_path, migrated_db):
    label(seeded, {M6: {"signals": ["h2h_above", "home_mismatch"], "against": []}})
    run_export(seeded, tmp_path / "out")
    m6 = next(f for f in read(tmp_path / "out")["board"]["fixtures"] if f["id"] == M6)
    assert m6["surge"] == {"signals": ["h2h_above", "home_mismatch"], "against": []}
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]


@pytest.mark.parametrize("name, tamper, expect", [
    ("board", lambda d: next(f for f in d["fixtures"] if f["id"] == M4)["surge"].pop("strong"), "strong"),
    ("board", lambda d: next(f for f in d["fixtures"] if f["id"] == M6)["surge"].__setitem__("strong", True), "strong"),
    ("surge", lambda d: d["stacking"]["routes"]["noodds"].__setitem__("strong_applies", True), "committed catalogue"),
])
def test_verifier_detects_strong_and_stacking_differences(seeded, tmp_path, migrated_db, name, tamper, expect):
    label(seeded, LABELS)
    out = tmp_path / "out"
    run_export(seeded, out)
    doc = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
    tamper(doc)
    (out / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db)
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


# ----------------------------------------------------------------------------- table context of settled results

def set_run_context(engine, k: int, labels: dict, context: dict) -> int:
    rid = set_run_surge(engine, k, labels)
    with engine.begin() as c:
        meta = json.loads(c.exec_driver_sql("SELECT meta FROM prediction_runs WHERE id = ?", (rid,)).scalar())
        meta["surge"]["context"] = context
        c.exec_driver_sql("UPDATE prediction_runs SET meta = ? WHERE id = ?", (json.dumps(meta), rid))
    return rid


def test_results_carry_the_recorded_context_and_strong_flag(seeded, tmp_path, migrated_db):
    ctx = {"phase": "run_in", "home": side(3, left=4), "away": side(18, left=5, to_safety=-2)}
    set_run_context(seeded, 2, {M2: {"signals": ["dead_team", "h2h_above"], "against": [], "strong": True}}, {M2: ctx})
    run_export(seeded, tmp_path / "out")
    docs = read(tmp_path / "out")
    validate("results", docs["results"])
    m2 = next(r for r in docs["results"]["results"] if r["id"] == M2)
    assert m2["surge"] == {"signals": ["dead_team", "h2h_above"], "against": [], "strong": True}
    assert m2["context"] == ctx and list(m2)[-2:] == ["surge", "context"]
    m1 = next(r for r in docs["results"]["results"] if r["id"] == M1)
    assert "context" not in m1  # its own run (board 2) recorded none
    res = verify_mod.verify(tmp_path / "out", migrated_db)
    assert res["ok"], res["problems"]
    assert res["checked"]["results with context"] == 1


@pytest.mark.parametrize("tamper, expect", [
    (lambda r: r["context"].__setitem__("phase", "early"), "context differs"),
    (lambda r: r.pop("context"), "context differs"),
    (lambda r: r.__setitem__("context", None), "never null"),
])
def test_verifier_detects_result_context_differences(seeded, tmp_path, migrated_db, tamper, expect):
    set_run_context(seeded, 2, {M2: {"signals": ["h2h_above"], "against": []}},
                    {M2: {"phase": "late", "home": side(3), "away": side(4)}})
    out = tmp_path / "out"
    run_export(seeded, out)
    doc = json.loads((out / "results.json").read_text(encoding="utf-8"))
    tamper(next(r for r in doc["results"] if r["id"] == M2))
    (out / "results.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db)
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


def test_playoff_context_has_a_phase_without_games_left(seeded, tmp_path, migrated_db):
    """Playoff formats: games left unknown, phase from the regular-season share (surge_rules.phase_of)."""
    label_ctx(seeded, LABELS, {M4: {"phase": "late", "home": side(2, left=None), "away": side(5, left=None)}})
    run_export(seeded, tmp_path / "out")
    m4 = next(f for f in read(tmp_path / "out")["board"]["fixtures"] if f["id"] == M4)
    assert m4["context"]["phase"] == "late" and m4["context"]["home"]["left"] is None
    assert verify_mod.verify(tmp_path / "out", migrated_db)["ok"]
