import numpy as np
import pandas as pd
import pytest

from shots.evaluation import metrics as M
from shots.evaluation.backtest import LeakageError, walk_forward, write_report
from shots.models.base import ShotModel


def _synthetic(seasons=("2018/2019", "2019/2020", "2020/2021"), per_season=60, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for i, s in enumerate(seasons):
        y0 = int(s[:4])
        dates = pd.date_range(f"{y0}-08-01", f"{y0 + 1}-05-20", periods=per_season).normalize()
        for j, d in enumerate(dates):
            rows.append({
                "match_id": f"{s}-{j}", "date": d, "season": s,
                "competition": "A" if j % 2 else "B", "tier": 1 + j % 2,
                "home_n_prior_matches": float(j), "away_n_prior_matches": float(j + 2),
                "home_shots": float(rng.integers(5, 20)), "away_shots": float(rng.integers(5, 20)),
            })
    f = pd.DataFrame(rows)
    f["total_shots"] = f.home_shots + f.away_shots
    return f


class Spy(ShotModel):
    """Records what it was trained on / asked to predict; predicts the training mean."""
    name = "spy"
    calls: list = []

    def fit(self, train):
        self.train_max = train["date"].max()
        self.mean = train["total_shots"].mean()
        return self

    def predict(self, rows, confidences=(0.7, 0.8, 0.9)):
        Spy.calls.append((self.train_max, rows["date"].min(), set(rows.columns)))
        out = pd.DataFrame(index=rows.index)
        out["median"] = self.mean
        for q in confidences:
            out[f"lb_{round(q * 100)}"] = int(np.floor(self.mean)) - round(q * 10)
        return out


def test_walk_forward_strict_temporal_split_and_hidden_targets():
    f = _synthetic()
    Spy.calls = []
    p = walk_forward(Spy, f, ["2019/2020", "2020/2021"], min_train_rows=10)
    assert len(p) == 120 and set(p.season) == {"2019/2020", "2020/2021"}
    assert len(Spy.calls) == 2
    for train_max, test_min, cols in Spy.calls:
        assert train_max < test_min
        assert not {"total_shots", "home_shots", "away_shots"} & cols
    # season-level: trained on everything before the season's first date
    s20 = p[p.season == "2020/2021"]
    assert (s20.train_end == f[f.season == "2019/2020"].date.max()).all()
    assert (p.actual == f.set_index("match_id").loc[p.match_id, "total_shots"].to_numpy()).all()


def test_walk_forward_monthly_refit_is_strict():
    f = _synthetic()
    Spy.calls = []
    p = walk_forward(Spy, f, ["2020/2021"], refit="month", min_train_rows=10)
    assert p.block.nunique() == len(Spy.calls) > 5
    assert (p.train_end < p.date).all()
    for train_max, test_min, _ in Spy.calls:
        assert train_max < test_min


def test_same_day_rows_across_seasons_never_train():
    # a row of the previous season dated on the test season's first day must not be in training
    f = _synthetic()
    first = f[f.season == "2020/2021"].date.min()
    f.loc[f.index[59], "date"] = first  # last 2019/2020 row moved to the same day
    Spy.calls = []
    walk_forward(Spy, f, ["2020/2021"], min_train_rows=10)
    assert Spy.calls[0][0] < first


def test_guard_raises_on_bad_output():
    class Bad(Spy):
        def predict(self, rows, confidences=(0.7, 0.8, 0.9)):
            return super().predict(rows, confidences).iloc[:-1]
    with pytest.raises(ValueError):
        walk_forward(Bad, _synthetic(), ["2020/2021"], min_train_rows=10)


def test_leakage_error_exists():
    assert issubclass(LeakageError, AssertionError)


def test_pinball_hand_computed():
    # q=0.8 -> tau=0.2. y=[10,20], lb=15: u=[-5,5] -> losses [0.8*5, 0.2*5] = [4,1] -> mean 2.5
    assert M.pinball([10, 20], [15, 15], 0.8) == pytest.approx(2.5)
    assert M.pinball([15], [15], 0.7) == 0.0


def test_score_hand_computed():
    df = pd.DataFrame({"actual": [10, 20, 30, 40, np.nan], "median": [12, 18, 30, 50, 1],
                       "lb_80": [15, 15, 15, 15, 0], "lb_90": [5, 5, 5, 45, 0]})
    s = M.score(df, (0.8, 0.9))
    assert s["n"] == 4
    assert s["cov_80"] == pytest.approx(0.75)
    assert s["calib_80"] == pytest.approx(-0.05)
    assert s["cov_90"] == pytest.approx(0.75)
    # q=0.8 tau=.2: u=[-5,5,15,25] -> [4,1,3,5] -> 3.25
    assert s["pinball_80"] == pytest.approx(3.25)
    # q=0.9 tau=.1: u=[5,15,25,-5] -> [.5,1.5,2.5,4.5] -> 2.25
    assert s["pinball_90"] == pytest.approx(2.25)
    assert s["pinball_mean"] == pytest.approx((3.25 + 2.25) / 2)
    assert s["sharp_80"] == 15 and s["sharp_90"] == 15
    assert s["mae"] == pytest.approx((2 + 2 + 0 + 10) / 4)
    assert s["bias"] == pytest.approx((2 - 2 + 0 + 10) / 4)


def test_breakdowns_and_report(tmp_path):
    f = _synthetic()
    p = walk_forward(Spy, f, ["2019/2020", "2020/2021"], min_train_rows=10)
    b = M.all_breakdowns(p)
    assert set(b) == {"overall", "season", "competition", "tier", "cold_start"}
    assert b["competition"]["n"].sum() == len(p)
    assert b["cold_start"].loc[True, "n"] == (p.n_prior_min < 5).sum()
    p2 = pd.concat([p.assign(model="league_avg"), p.assign(model="spy")])
    out = tmp_path / "r.md"
    write_report(p2, out, (0.7, 0.8, 0.9), ["2019/2020"], ["2020/2021"], "season", {"spy": 1.0})
    text = out.read_text(encoding="utf-8")
    assert "## Overall" in text and "Partial season" in text and "Verdict" in text
