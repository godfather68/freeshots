import numpy as np
import pandas as pd
import pytest
from scipy import stats

from shots.evaluation import calibration_checks as C
from shots.evaluation.backtest import distribution_diagnostics, walk_forward
from shots.models.base import ShotModel


def _nb_frame(n=40000, shift=0.0, seed=1):
    """Calibrated (shift=0) NB predictive distributions with their pinball-optimal integer bounds."""
    rng = np.random.default_rng(seed)
    mu = rng.uniform(18, 32, n)
    r = 1 / 0.04
    y = stats.nbinom.rvs(r, r / (r + mu * np.exp(shift)), random_state=rng)
    k = np.arange(161)
    cdf = stats.nbinom.cdf(k[None, :], r, (r / (r + mu))[:, None])
    df = pd.DataFrame({"actual": y.astype(float), "match_id": np.arange(n),
                       "date": pd.Timestamp("2020-01-01") + pd.to_timedelta(np.arange(n) // 50, "D"),
                       "competition": np.where(np.arange(n) % 2, "A", "B"), "season": "s",
                       "median": mu})
    rows = np.arange(n)
    df["pit_lo"] = np.where(y > 0, cdf[rows, np.maximum(y - 1, 0)], 0.0)
    df["pit_hi"] = cdf[rows, y]
    for q in (0.7, 0.8, 0.9):
        kq = round(q * 100)
        lb = (cdf > 1 - q).argmax(axis=1)
        df[f"lb_{kq}"] = lb
        df[f"p_{kq}"] = 1 - np.where(lb > 0, cdf[rows, np.maximum(lb - 1, 0)], 0.0)
        df[f"pmf_{kq}"] = df[f"p_{kq}"] - (1 - cdf[rows, lb])
    return df


def test_pinball_optimal_integer_bound_of_calibrated_nb_over_covers_by_granularity():
    df = _nb_frame()
    t = C.granularity_table(df)
    for k, q in ((70, .7), (80, .8), (90, .9)):
        assert t[f"stated_{k}"] >= q                         # by construction
        assert t[f"cov_{k}"] > q + 0.01                      # visible over-coverage...
        assert t[f"cov_{k}"] == pytest.approx(t[f"stated_{k}"], abs=0.006)  # ...fully explained
        assert t[f"rand_{k}"] == pytest.approx(q, abs=0.006)  # randomized PIT: calibrated
        assert t[f"mid_{k}"] == pytest.approx(q, abs=0.01)
        assert t[f"strict_{k}"] < q < t[f"cov_{k}"]
    h = C.pit_histogram(df.pit_lo, df.pit_hi)
    assert h.sum() == pytest.approx(1) and np.abs(h - 0.1).max() < 0.006


def test_randomized_coverage_detects_a_mean_shift():
    t = C.granularity_table(_nb_frame(shift=0.05))  # outcomes 5% higher than predicted
    assert t["rand_80"] > 0.83 and t["cov_80"] > t["stated_80"] + 0.02


def test_randomized_coverage_point_mass_and_clip():
    assert C.randomized_coverage([0.1], [0.3], 0.8)[0] == pytest.approx(0.5)   # t = 0.2 mid-interval
    assert C.randomized_coverage([0.25, 0.0], [0.25, 0.1], 0.8).tolist() == [1.0, 0.0]


def test_reliability_table():
    p = np.array([0.72] * 100 + [0.88] * 100)
    hit = np.r_[np.ones(72), np.zeros(28), np.ones(80), np.zeros(20)]
    t = C.reliability_table(p, hit)
    assert t["n"].sum() == 200
    assert t["gap_pp"].iloc[0] == pytest.approx(0) and t["gap_pp"].iloc[1] == pytest.approx(-8)


def test_paired_bootstrap_block():
    a, b = np.array([1.0, 2, 3, 4]), np.array([1.0, 1, 1, 1])
    d, lo, hi = C.paired_bootstrap(a, b, block=[0, 0, 1, 1])
    assert d == 1.5 and lo <= d <= hi


def test_ship_check_accepts_calibrated_pinball_optimal_bounds_and_needs_to_beat_baseline():
    df = _nb_frame()
    worse = df.copy()
    for k in (70, 80, 90):
        worse[f"lb_{k}"] = worse[f"lb_{k}"] - 2
    ok, msgs = C.ship_check(df, worse, min_comp_n=100)
    assert ok, msgs
    ok2, msgs2 = C.ship_check(worse, df, min_comp_n=100)
    assert not ok2 and any("FAIL" in m and "mid" in m for m in msgs2)


def test_monthly_drift_flags_a_bad_month():
    df = _nb_frame(n=6000)
    df["date"] = pd.Timestamp("2020-01-01") + pd.to_timedelta(np.arange(6000) // 100, "D")
    bad = df["date"] >= "2020-02-01"
    df.loc[bad, "lb_80"] += 4
    g = C.monthly_drift(df)
    assert not g.loc["2020-01", "alert"] and g.loc["2020-02", "alert"]


class _NB(ShotModel):
    """Toy distributional model: total ~ Poisson(training mean); exposes total_pmf like negbin."""
    name = "toy_nb"

    def fit(self, train):
        self.mu = float(train["total_shots"].mean())
        return self

    def total_pmf(self, rows):
        return stats.poisson.pmf(np.arange(161)[None, :], np.full((len(rows), 1), self.mu))

    def predict(self, rows, confidences=(0.7, 0.8, 0.9)):
        cdf = np.cumsum(self.total_pmf(rows), axis=1)
        out = pd.DataFrame(index=rows.index)
        out["median"] = self.mu
        for q in confidences:
            out[f"lb_{round(q * 100)}"] = (cdf > 1 - q).argmax(axis=1)
        return out


def test_harness_diagnostics_match_the_model_distribution():
    rng = np.random.default_rng(0)
    n = 400
    f = pd.DataFrame({
        "match_id": np.arange(n), "date": pd.date_range("2018-08-01", periods=n, freq="D"),
        "competition": "A", "tier": 1, "home_n_prior_matches": 10.0, "away_n_prior_matches": 10.0,
        "home_shots": rng.poisson(12, n).astype(float), "away_shots": rng.poisson(12, n).astype(float)})
    f["season"] = np.where(f.date < "2019-07-01", "2018/2019", "2019/2020")
    f["total_shots"] = f.home_shots + f.away_shots
    p = walk_forward(_NB, f, ["2019/2020"], min_train_rows=10)
    mu = f[f.season == "2018/2019"].total_shots.mean()
    y = p.actual.to_numpy().astype(int)
    assert np.allclose(p.pit_hi, stats.poisson.cdf(y, mu))
    assert np.allclose(p.pit_lo, stats.poisson.cdf(y - 1, mu))
    assert np.allclose(p.p_80, stats.poisson.sf(p.lb_80 - 1, mu))
    assert np.allclose(p.pmf_80, stats.poisson.pmf(p.lb_80, mu))
    assert (p.p_80 >= 0.8).all()


def test_no_diagnostics_for_quantile_only_models():
    class Q(ShotModel):
        def fit(self, t):
            return self

        def predict(self, rows, confidences=(0.8,)):
            return pd.DataFrame({"median": 1.0, "lb_80": 1}, index=rows.index)
    assert distribution_diagnostics(Q(), pd.DataFrame(index=[0]), np.array([3.0]),
                                    pd.DataFrame({"lb_80": [1]}, index=[0]), (0.8,)) is None
