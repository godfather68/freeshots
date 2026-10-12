"""Board build (shots.predict.board) and the scan refactor it relies on."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from shots.predict import board as bd
from shots.predict import scan as sc


class StubModel:
    """Logistic curve around a per-row `hint` (+ shift), standing in for a fitted ShotModel."""

    def __init__(self, shift=0.0):
        self.shift = shift

    def _mu(self, rows):
        return rows["hint"].to_numpy(float) + self.shift

    def predict(self, rows):
        mu = self._mu(rows)
        return pd.DataFrame({"median": np.round(mu), "lb_70": np.round(mu - 3), "lb_80": np.round(mu - 4),
                             "lb_90": np.round(mu - 6)}, index=rows.index)

    def prob_at_least(self, rows, n):
        mu = self._mu(rows)
        return 1 / (1 + np.exp((np.broadcast_to(np.asarray(n), mu.shape) - mu) / 3))


def fake_features(combined: pd.DataFrame) -> pd.DataFrame:
    f = combined.copy()
    f["league_prior_shots"] = 14.0
    f["hint"] = f["match_id"].map({"u1": 30.0, "u2": 30.0, "u3": 24.0}).fillna(26.0)
    f["home_n_prior_matches"] = 10
    f["away_n_prior_matches"] = np.where(f["match_id"] == "u3", 2, 10)
    has = f["odds_home"].notna()
    for c in ("mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25"):
        f[c] = np.where(has, 0.4, np.nan)
    f["total_shots"] = f["home_shots"] + f["away_shots"]
    return f


def fixtures_frame() -> pd.DataFrame:
    base = dict(source="x", season="2026/2027", home_team_id="h", away_team_id="a", home_shots=pd.NA,
                away_shots=pd.NA, odds_draw=np.nan, odds_away=np.nan, odds_over25=np.nan, odds_under25=np.nan)
    rows = [
        dict(match_id="u1", competition="Spain La Liga", country="Spain", tier=1, date="2026-10-10",
             home_team="Alpha", away_team="Beta", kickoff="18:00", odds_home=2.0),
        dict(match_id="u2", competition="Italy Serie B", country="Italy", tier=2, date="2026-10-09",
             home_team="Gamma", away_team="Delta", kickoff="15:00", odds_home=np.nan),
        dict(match_id="u3", competition="Italy Serie B", country="Italy", tier=2, date="2026-10-09",
             home_team="Eps", away_team="Zeta", kickoff="12:30", odds_home=np.nan),
    ]
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    return df


def matches_frame() -> pd.DataFrame:
    df = fixtures_frame().drop(columns="kickoff").iloc[:1].copy()
    df["match_id"] = "old1"
    df["date"] = pd.Timestamp("2026-10-01")
    df["home_shots"], df["away_shots"] = 13, 12
    df["odds_home"] = 2.0
    return df


@pytest.fixture
def patched(tmp_path, monkeypatch):
    """Scan pipeline with feature building, model loading and file locations stubbed out."""
    fx, matches = fixtures_frame(), matches_frame()
    matches.to_parquet(tmp_path / "matches.parquet")
    monkeypatch.setattr(sc.fd, "OUT_PATH", tmp_path / "matches.parquet")
    monkeypatch.setattr(sc, "gather_fixtures", lambda start, days, use_calendar=True: fx)
    monkeypatch.setattr(sc, "build_features", fake_features)
    monkeypatch.setattr(sc, "FEATURES_PATH", tmp_path / "features.parquet")
    monkeypatch.setattr(sc, "PICKS_DIR", tmp_path / "picks")
    monkeypatch.setattr(sc, "CALENDAR_PATH", tmp_path / "no_calendar.parquet")
    monkeypatch.setattr(sc, "latest_model", lambda name, last, age: tmp_path / "negbin_pit_comp_2026-10-01")
    monkeypatch.setattr(sc, "load", lambda p: StubModel())
    monkeypatch.setattr(sc, "latest_no_odds_model", lambda name, played, age: (StubModel(-2.0), "noodds_label"))
    return tmp_path


def test_scan_output_unchanged(patched):
    out = sc.scan(shots=24, min_prob=0.7, refresh=False, today=date(2026, 10, 8), days=30)
    assert list(out.columns) == ["date", "kickoff", "competition", "home_team", "away_team", "p_at_least_24",
                                 "expected_shots", "league_avg_shots", "above_league", "lb_80", "odds",
                                 "mkt_p_over25", "cold_start", "match_id", "model", "selected"]
    # sorted by probability, descending: u1 (odds, mu 30) > u2 (no odds, mu 28) > u3 (no odds, mu 22)
    assert out["match_id"].tolist() == ["u1", "u2", "u3"]
    assert out["model"].tolist() == ["negbin_pit_comp_2026-10-01", "noodds_label", "noodds_label"]
    assert out["odds"].tolist() == ["yes", "no", "no"]
    assert out["expected_shots"].tolist() == [30.0, 28.0, 22.0]
    assert out["lb_80"].tolist() == [26, 24, 18]
    assert out["cold_start"].tolist() == [False, False, True]
    assert out["league_avg_shots"].tolist() == [28.0] * 3
    assert out["above_league"].tolist() == [2.0, 0.0, -6.0]
    expected_p = [round(1 / (1 + np.exp((24 - mu) / 3)), 3) for mu in (30.0, 28.0, 22.0)]
    assert out["p_at_least_24"].tolist() == expected_p
    assert out["selected"].tolist() == [p >= 0.7 for p in expected_p]
    assert out["kickoff"].tolist() == ["18:00", "15:00", "12:30"]
    assert len(list((patched / "picks").glob("*_at_least_24.csv"))) == 1


def test_board_columns_curve_and_routing(patched, monkeypatch):
    out_path = patched / "board.parquet"
    board = bd.build(days=30, refresh=False, today=date(2026, 10, 8), out_path=out_path)
    assert list(board.columns)[:21] == [
        "match_id", "date", "kickoff", "competition", "country", "tier", "round", "home_team", "away_team",
        "expected_shots", "lb_70", "lb_80", "lb_90", "league_avg_shots", "above_league", "odds",
        "mkt_p_over25", "cold_start", "model", "p_10", "p_11"]
    assert list(board.columns)[-1] == "p_45" and len([c for c in board if c.startswith("p_")]) == 36
    # ordered by date then kickoff
    assert board["match_id"].tolist() == ["u3", "u2", "u1"]
    r = board.set_index("match_id")
    assert r.loc["u1", "odds"] and not r.loc["u2", "odds"]
    assert r.loc["u1", "model"].startswith("negbin") and r.loc["u2", "model"] == "noodds_label"
    assert r.loc["u3", "cold_start"] and not r.loc["u1", "cold_start"]
    assert (r["lb_70"] >= r["lb_80"]).all() and (r["lb_80"] >= r["lb_90"]).all()
    # curve = the model's prob_at_least for each n, rounded; non-increasing in n
    for mid, mu in (("u1", 30.0), ("u2", 28.0)):
        for n in (10, 24, 45):
            assert r.loc[mid, f"p_{n}"] == pytest.approx(1 / (1 + np.exp((n - mu) / 3)), abs=1e-4)
    curve = board[bd.PCOLS].to_numpy()
    assert (np.diff(curve, axis=1) <= 1e-12).all()
    # persisted file round-trips
    again = pd.read_parquet(out_path)
    assert again["match_id"].tolist() == board["match_id"].tolist()
    meta = json.loads(bd.meta_path(out_path).read_text())
    assert meta["n_fixtures"] == 3 and meta["n_with_odds"] == 1 and meta["n_without_odds"] == 2
    assert meta["results_up_to"] == "2026-10-01" and meta["models"] == sorted(board["model"].unique())
    assert {"built_at", "window_days", "n_cold_start", "n_leagues"} <= meta.keys()


def test_board_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SHOTS_BOARD", str(tmp_path / "x.parquet"))
    assert bd.board_path() == tmp_path / "x.parquet"
    assert bd.meta_path() == tmp_path / bd.META_NAME
    monkeypatch.delenv("SHOTS_BOARD")
    assert bd.board_path() == bd.BOARD_PATH


def test_board_no_fixtures_exits(patched, monkeypatch):
    monkeypatch.setattr(sc, "gather_fixtures", lambda *a, **k: fixtures_frame().iloc[0:0])
    with pytest.raises(SystemExit):
        bd.build(days=30, refresh=False, today=date(2026, 10, 8), out_path=patched / "b.parquet")
