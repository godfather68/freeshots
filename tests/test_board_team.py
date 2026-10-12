"""Board build: experimental per-team columns (needs models with team calibration)."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from shots.predict import board as bd
from shots.predict import scan as sc
from tests.test_board import StubModel, patched  # noqa: F401  (patched is a fixture)


class StubTeamModel(StubModel):
    """StubModel plus the team API (home takes 55% of the hint, away 45%)."""

    has_team_calibration = True
    share = {"home": 0.55, "away": 0.45}

    def _tmu(self, rows, side):
        return self._mu(rows) * self.share[side]

    def team_predict(self, rows, side):
        mu = self._tmu(rows, side)
        return pd.DataFrame({"median": np.round(mu), "lb_70": np.round(mu - 2), "lb_80": np.round(mu - 3),
                             "lb_90": np.round(mu - 4), "mean": mu}, index=rows.index)

    def team_prob_at_least(self, rows, side, n):
        mu = self._tmu(rows, side)
        return 1 / (1 + np.exp((np.broadcast_to(np.asarray(n), mu.shape) - mu) / 2))


def test_board_team_columns(patched, monkeypatch):
    monkeypatch.setattr(sc, "load", lambda p: StubTeamModel())
    monkeypatch.setattr(sc, "latest_no_odds_model", lambda *a: (StubTeamModel(-2.0), "noodds_label"))
    out_path = patched / "board.parquet"
    board = bd.build(days=30, refresh=False, today=date(2026, 10, 8), out_path=out_path)
    # existing columns untouched; team columns appended after the total curve
    assert list(board.columns)[:21][-1] == "p_11" and "p_45" in board.columns
    assert board.columns.get_loc("home_mean") > board.columns.get_loc("p_45")
    for c in ("home_mean", "away_mean", "home_median", "away_median", "home_lb_70", "home_lb_80", "away_lb_90"):
        assert c in board.columns
    assert [c for c in board if c.startswith("ph_")] == [f"ph_{n}" for n in range(3, 26)]
    assert [c for c in board if c.startswith("pa_")] == [f"pa_{n}" for n in range(3, 26)]
    r = board.set_index("match_id")
    assert r.loc["u1", "home_mean"] == pytest.approx(30 * 0.55)  # odds route
    assert r.loc["u2", "away_mean"] == pytest.approx(round(28 * 0.45, 2))  # no-odds route (shift -2)
    assert r.loc["u1", "ph_10"] == pytest.approx(1 / (1 + np.exp((10 - 16.5) / 2)), abs=1e-4)
    assert (np.diff(board[[f"ph_{n}" for n in range(3, 26)]].to_numpy(), axis=1) <= 1e-12).all()
    assert (r["home_lb_70"] >= r["home_lb_80"]).all() and (r["away_lb_80"] >= r["away_lb_90"]).all()
    meta = json.loads(bd.meta_path(out_path).read_text())
    assert meta["team_predictions"] is True and meta["team_thresholds"] == [3, 25]
    again = pd.read_parquet(out_path)
    assert again["home_lb_80"].tolist() == board["home_lb_80"].tolist()


def test_board_without_team_calibration(patched):
    path = patched / "b.parquet"
    board = bd.build(days=30, refresh=False, today=date(2026, 10, 8), out_path=path)
    assert not any(c.startswith(("ph_", "pa_", "home_mean")) for c in board.columns)
    assert json.loads(bd.meta_path(path).read_text())["team_predictions"] is False
