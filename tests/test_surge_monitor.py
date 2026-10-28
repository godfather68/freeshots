"""Surge monitor (shots.surge_monitor): shadow matching, call metrics, the revert rule, the CLI."""

from datetime import date

import numpy as np
import pandas as pd

from shots import surge_monitor as sm


def _picks(n, p, shadow_p, hit_rate_surge, hit=None, start="2026-01-01"):
    rng = np.random.default_rng(0)
    d = pd.date_range(start, periods=n, freq="6h")
    hits = hit if hit is not None else rng.random(n) < hit_rate_surge
    return pd.DataFrame({"match_id": [f"m{i}" for i in range(n)], "date": d.normalize(), "competition": "L",
                         "p": p, "hit": hits, "actual": 25, "model": "cand_odds_surge-odds_2026-01-01",
                         "scan_ts": d, "shadow": shadow_p})


def _boards(share=0.0):
    return pd.DataFrame({"run_id": [1], "built_at": [pd.Timestamp("2026-01-01")], "shadow": [{}], "shadow_n": [24],
                         "share_non_cold": [share], "n_odds": [40], "n_fallback": [0], "has_route": [True]})


def test_shadow_is_taken_from_the_closest_pre_match_board():
    picks = pd.DataFrame({"match_id": ["a", "b", "c"], "date": pd.to_datetime(["2026-03-07"] * 3), "competition": "L",
                          "p": [0.75, 0.72, 0.8], "hit": [True, False, True], "actual": [30, 20, 28],
                          "model": ["cand_odds_surge-odds_x", "cand_odds_surge-odds_x", "negbin_pit_comp_x"],
                          "scan_ts": pd.to_datetime(["2026-03-06 09:00"] * 3)})
    boards = pd.DataFrame({"run_id": [1, 2, 3], "built_at": pd.to_datetime(["2026-03-03 09:05", "2026-03-06 09:05",
                                                                            "2026-03-08 09:05"]),
                           "shadow": [{"a": 0.60}, {"a": 0.68}, {"a": 0.99, "b": 0.99}], "shadow_n": [24, 24, 24],
                           "share_non_cold": 0.0, "n_odds": 1, "n_fallback": 0, "has_route": True})
    sp = sm.attach_shadow(picks, boards)
    assert list(sp["match_id"]) == ["a", "b"]                     # production-scored pick is not Surge's
    assert sp.set_index("match_id").at["a", "shadow"] == 0.68     # closest pre-match board; never the later one
    assert np.isnan(sp.set_index("match_id").at["b", "shadow"])   # only a post-match board knows b


def test_revert_when_surge_trails_the_shadow_with_enough_calls():
    n = 800
    first = np.r_[np.ones(320, bool), np.zeros(80, bool)]    # matches both models call: 80% hit
    rest = np.r_[np.ones(240, bool), np.zeros(160, bool)]     # matches only Surge calls: 60% hit
    df = _picks(n, p=np.full(n, 0.75), shadow_p=np.r_[np.full(400, 0.75), np.full(400, 0.6)], hit_rate_surge=0,
                hit=np.r_[first, rest])
    res = sm.assess(df, _boards())
    assert res["surge"]["calls"]["n"] == 800 and res["shadow"]["calls"]["n"] == 400
    assert res["gap_pp"] < -sm.REVERT_PP and res["verdict"] == "REVERT"


def test_ok_and_not_enough_calls():
    n = 700
    df = _picks(n, p=np.full(n, 0.75), shadow_p=np.full(n, 0.74), hit_rate_surge=0.76)
    assert sm.assess(df, _boards())["verdict"] == "OK"               # same calls, same hits: gap 0
    small = _picks(50, p=np.full(50, 0.75), shadow_p=np.full(50, 0.75), hit_rate_surge=0.2)
    assert sm.assess(small, _boards())["verdict"] == "NOT ENOUGH CALLS"


def test_band_and_fallback_numbers():
    df = _picks(10, p=np.r_[np.full(5, 0.85), np.full(5, 0.5)], shadow_p=np.full(10, 0.82), hit_rate_surge=0.5,
                hit=np.r_[np.ones(4, bool), np.zeros(6, bool)])
    res = sm.assess(df, _boards(share=0.01))
    assert res["surge"]["band"]["n"] == 5 and res["surge"]["band"]["realised"] == 0.8
    assert res["shadow"]["band"]["n"] == 10 and res["fallback"]["max_share"] == 0.01


def test_cli_on_an_empty_database_writes_a_report_and_exits_zero(migrated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("SHOTS_DB", str(migrated_db))
    code = sm.main(["--as-of", "2026-12-15", "--out-dir", str(tmp_path / "mon")])
    text = (tmp_path / "mon" / "2026-12.md").read_text(encoding="utf-8")
    assert code == 0 and "NOT ENOUGH CALLS" in text and str(sm.MIN_CALLS) in text
    assert sm.window(date(2026, 12, 15), 12) == (date(2025, 12, 15), date(2026, 12, 15))
