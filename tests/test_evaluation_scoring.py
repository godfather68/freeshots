"""Common scoring API (src/shots/evaluation/scoring.py, docs/evaluation-protocol.md)."""

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from shots.evaluation import scoring as S

K = 80


def _frame(cdf: np.ndarray, y, comp=None, dates=None) -> pd.DataFrame:
    df = pd.DataFrame(cdf, columns=[f"cdf_{k}" for k in range(cdf.shape[1])])
    df["actual"] = np.asarray(y, float)
    df["match_id"] = [f"m{i}" for i in range(len(df))]
    df["date"] = pd.to_datetime("2024-01-01") + pd.to_timedelta(
        np.arange(len(df)) // 10 if dates is None else dates, unit="D")
    df["competition"] = "A" if comp is None else comp
    return df


def _nb_cdf(mu, alpha=0.05):
    mu = np.asarray(mu, float)[:, None]
    n = 1 / alpha
    return stats.nbinom.cdf(np.arange(K + 1)[None, :], n, n / (n + mu))


def test_rps_of_point_mass_is_absolute_error():
    cdf = (np.arange(K + 1)[None, :] >= np.array([20, 25, 30])[:, None]).astype(float)
    r = S.row_scores(_frame(cdf, [20, 22, 35]))
    assert r["rps"].tolist() == [0.0, 3.0, 5.0]
    assert r["median"].tolist() == [20, 25, 30]


def test_rps_equals_crps_energy_form():
    rng = np.random.default_rng(1)
    cdf = _nb_cdf(rng.uniform(15, 30, 20))
    cdf[:, -1] = 1.0  # close the support so the energy form is exact
    y = rng.integers(10, 40, 20)
    r = S.row_scores(_frame(cdf, y))
    pmf = np.diff(np.hstack([np.zeros((20, 1)), cdf]), axis=1)
    k = np.arange(K + 1)
    for i in range(20):
        e1 = (pmf[i] * np.abs(k - y[i])).sum()
        e2 = (pmf[i][:, None] * pmf[i][None, :] * np.abs(k[:, None] - k[None, :])).sum()
        assert r["rps"].iloc[i] == pytest.approx(e1 - 0.5 * e2, abs=1e-6)


def test_log_score_and_bounds():
    mu = np.array([18.0, 24.0, 31.0])
    cdf = stats.poisson.cdf(np.arange(K + 1)[None, :], mu[:, None])
    y = np.array([15, 24, 40])
    r = S.row_scores(_frame(cdf, y))
    assert np.allclose(r["logs"], -stats.poisson.logpmf(y, mu))
    for q in S.QS:
        k = round(q * 100)
        surv = 1 - np.hstack([np.zeros((3, 1)), cdf[:, :-1]])       # P(T >= n), n = 0..K
        expect = [max(n for n in range(K + 1) if surv[i, n] >= q) for i in range(3)]
        assert r[f"lb_{k}"].tolist() == expect
        up = [min(n for n in range(K + 1) if cdf[i, n] >= q) for i in range(3)]
        assert r[f"ub_{k}"].tolist() == up
        assert (r[f"stated_{k}"] >= q - 1e-9).all()


def test_ge_columns_equal_cdf_columns_and_partial_range_rejected():
    rng = np.random.default_rng(2)
    cdf = _nb_cdf(rng.uniform(15, 30, 50))
    y = rng.integers(8, 45, 50)
    a = _frame(cdf, y)
    b = pd.DataFrame({f"ge_{n}": 1 - cdf[:, n - 1] for n in range(1, K + 2)})
    b["actual"] = y
    sa, sb = S.score(a), S.score(b)
    for key in ("rps", "logs", "pin_mean", "cov_80", "mae", "rel_ece_pp"):
        assert sa[key] == pytest.approx(sb[key])
    with pytest.raises(ValueError, match="contiguously"):
        S.curve_matrix(b[[f"ge_{n}" for n in range(10, 46)]])


def test_actual_beyond_grid_and_nan_rejected():
    cdf = _nb_cdf([20.0])[:, :31]
    with pytest.raises(ValueError, match="grid"):
        S.row_scores(_frame(cdf, [40]))
    with pytest.raises(ValueError, match="NaN"):
        S.row_scores(_frame(cdf, [np.nan]))


def test_calibrated_nb_is_calibrated_by_every_measure():
    rng = np.random.default_rng(3)
    n = 20000
    mu = rng.uniform(16, 32, n)
    alpha = 0.05
    y = rng.negative_binomial(1 / alpha, (1 / alpha) / (1 / alpha + mu))
    s = S.score(_frame(_nb_cdf(mu, alpha), y))
    for q in S.QS:
        k = round(q * 100)
        assert s[f"cov_{k}"] >= q - 0.01                    # integer bounds over-cover
        assert abs(s[f"cov_{k}"] - s[f"stated_{k}"]) < 0.012  # ... by exactly what the model states
        assert abs(s[f"rand_{k}"] - q) < 0.012
        assert abs(s[f"mid_{k}"] - q) < 0.02
        assert s[f"ucov_{k}"] >= q - 0.01
    assert s["rel_ece_pp"] < 1.0
    assert s["pit_max_dev_pp"] < 1.0


def test_compare_and_competition_guard_detect_a_worse_model():
    rng = np.random.default_rng(4)
    n = 6000
    mu = rng.uniform(16, 32, n)
    y = rng.negative_binomial(20, 20 / (20 + mu))
    comp = np.where(np.arange(n) % 2 == 0, "A", "B")
    good = _frame(_nb_cdf(mu), y, comp)
    shifted = mu * np.where(comp == "B", 1.25, 1.0)          # wrong only in competition B
    bad = _frame(_nb_cdf(shifted), y, comp)
    c = S.compare(bad, good)
    assert c["rps"]["lo"] > 0 and c["logs"]["lo"] > 0
    g = S.competition_guard(bad, good)
    assert bool(g.loc["B", "worse"]) and not bool(g.loc["A", "worse"])
    assert g.loc["A", "diff"] == pytest.approx(0.0)
    with pytest.raises(ValueError, match="different matches"):
        S.compare(bad.iloc[:-1], good)


def test_score_by_groups():
    rng = np.random.default_rng(5)
    mu = rng.uniform(16, 32, 400)
    y = rng.poisson(mu)
    df = _frame(_nb_cdf(mu), y, np.where(np.arange(400) < 100, "A", "B"))
    t = S.score_by(df, "competition")
    assert t.loc["A", "n"] == 100 and t.loc["B", "n"] == 300
    assert "rps" in t and "pit_hist" not in t
