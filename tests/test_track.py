import math
from pathlib import Path

import pandas as pd
import pytest

from shots.predict import track


def write_picks(d, name, rows, n=24):
    df = pd.DataFrame(rows).rename(columns={"p": f"p_at_least_{n}"})
    for c, v in dict(kickoff="15:00", competition="X", home_team="A", away_team="B").items():
        df[c] = v
    df.to_csv(d / name, index=False)


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "picks"
    d.mkdir()
    m = pd.DataFrame({"match_id": ["a", "b", "c", "v"], "home_shots": [14, 8, 10, None],
                      "away_shots": [12, 9, 11, None]})
    m.to_parquet(tmp_path / "m.parquet")
    return d, tmp_path


def test_parse_filename():
    r = track.parse_filename(Path("2026-10-05_1330_at_least_26.csv"))
    assert r == dict(source="live", scan_ts=pd.Timestamp("2026-10-05 13:30"), n=26)
    r = track.parse_filename(Path("replay_2026-10-02_at_least_24.csv"))
    assert r["source"] == "replay" and r["n"] == 24
    assert track.parse_filename(Path("notes.csv")) is None


def test_settle_dedup_void_pending(env):
    d, tmp = env
    write_picks(d, "2026-10-01_0900_at_least_24.csv", [
        dict(date="2026-10-03", match_id="a", p=0.60), dict(date="2026-10-03", match_id="b", p=0.80)])
    write_picks(d, "2026-10-02_0900_at_least_24.csv", [dict(date="2026-10-03", match_id="a", p=0.75)])
    # scan after match day: not a pre-match prediction
    write_picks(d, "2026-10-04_0900_at_least_24.csv", [dict(date="2026-10-03", match_id="a", p=0.99)])
    write_picks(d, "2026-10-02_0900_at_least_22.csv", [dict(date="2026-10-03", match_id="a", p=0.9)], n=22)
    write_picks(d, "2026-10-02_1000_at_least_24.csv", [
        dict(date="2026-10-09", match_id="z", p=0.7), dict(date="2026-10-03", match_id="v", p=0.7)])
    out = track.settle(d, tmp / "m.parquet", tmp / "rec.parquet")
    a24 = out[(out.match_id == "a") & (out.n == 24)]
    assert len(a24) == 1 and a24.p.iloc[0] == 0.75 and a24.scan_file.iloc[0].startswith("2026-10-02")
    assert len(out[(out.match_id == "a") & (out.n == 22)]) == 1
    st = out.set_index(["match_id", "n"])["status"]
    assert st[("a", 24)] == "settled" and st[("z", 24)] == "pending" and st[("v", 24)] == "void"
    h = out.set_index(["match_id", "n"])["hit"]
    assert h[("a", 24)] and h[("a", 22)] and not h[("b", 24)]  # 26 and 17 shots
    assert pd.isna(h[("v", 24)]) and pd.isna(h[("z", 24)])
    assert (tmp / "rec.parquet").exists()


def test_wilson():
    lo, hi = track.wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    assert track.wilson(0, 10)[0] == 0.0 and track.wilson(10, 10)[1] == 1.0
    assert math.isnan(track.wilson(0, 0)[0])


def test_reliability_bands():
    df = pd.DataFrame({"p": [0.05, 0.72, 0.78, 0.75, 1.0, 0.7], "hit": [False, True, True, False, True, True]})
    r = track.reliability(df).set_index("lo")
    assert r["n"].sum() == 6
    assert r.loc[0.7, "n"] == 4 and r.loc[0.7, "realised"] == pytest.approx(0.75)
    assert r.loc[0.7, "mean_p"] == pytest.approx((0.72 + 0.78 + 0.75 + 0.7) / 4)
    assert r.loc[0.9, "n"] == 1  # p == 1.0 lands in the last band


def test_metrics_brier_and_selected():
    df = pd.DataFrame({"date": pd.to_datetime(["2026-01-01", "2026-01-09", "2026-01-10"]), "competition": "X",
                       "n": 24, "p": [0.8, 0.6, 0.9], "hit": [True, False, True]})
    feats = pd.DataFrame({"date": pd.to_datetime(["2025-01-01"] * 4), "competition": "X",
                          "total_shots": [30, 30, 10, 10]})
    m = track.metrics(df, 0.7, feats)
    assert m["selected"]["n"] == 2 and m["selected"]["hits"] == 2
    assert m["brier"]["brier"] == pytest.approx((0.04 + 0.36 + 0.01) / 3)
    assert m["brier"]["brier_base"] == pytest.approx((0.25 + 0.25 + 0.25) / 3)
    assert len(m["cumulative"]) == 2  # two distinct weeks among selected


def test_render_smoke(env):
    d, tmp = env
    write_picks(d, "replay_2026-10-03_at_least_24.csv", [dict(date="2026-10-03", match_id="a", p=0.75)])
    rec = track.settle(d, tmp / "m.parquet", None)
    back = pd.DataFrame({"match_id": ["a"], "date": pd.to_datetime(["2026-01-01"]), "competition": "X",
                         "N": 24, "p": 0.8, "actual": 30})
    assert "Shots track record" in track.render(rec, back, None)
