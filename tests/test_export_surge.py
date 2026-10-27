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
from tests.test_export import KICK1, M4, M5, M6, NAMES, board_frame, engine, meta_at, read, run_export, seeded, validate  # noqa: F401

LABELS = {M4: {"signals": ["dead_rubber", "h2h_above"], "against": ["winter"]},     # odds route
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
