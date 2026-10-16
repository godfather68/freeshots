"""Structural candidates (src/shots/models/candidates.py, nbmle.py, screen.py). Production
(``negbin_pit_comp``) is covered by its golden-hash tests; nothing here may change it."""

import numpy as np
import pandas as pd
import pytest

from shots.models import MODELS
from shots.models.candidates import CANDIDATES, FlexNB, JointShare, _quantile_average, nb_pmf_fast
from shots.models.nbmle import fit_nb, recency_weights
from shots.models.negbin import NegBinModel, nb_pmf
from tests.test_models import SEASONS, synthetic


@pytest.fixture(scope="module")
def data():
    df = synthetic()
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


def test_candidates_registered_without_touching_production():
    for name in CANDIDATES:
        assert name.startswith("cand_") and MODELS[name] is CANDIDATES[name]
    assert MODELS["negbin_pit_comp"].__module__ == "shots.models.calibrated"


def test_fast_pmf_equals_scipy():
    mu = np.array([3.0, 11.5, 25.0])
    np.testing.assert_allclose(nb_pmf_fast(mu, 0.04, 80), nb_pmf(mu, 0.04, 80), atol=1e-12)


def test_nbmle_matches_statsmodels(data):
    train, _ = data
    nb = NegBinModel().fit(train)
    s = nb.sides_["home"]
    X = s["design"].transform(train.dropna(subset=["home_shots"]))[s["cols"]].to_numpy()
    y = train["home_shots"].to_numpy(float)
    r = fit_nb(y, X, x_const=0)
    assert r.converged
    # synthetic rate features are collinear (coefficients not identified): compare the fitted means
    np.testing.assert_allclose(np.exp(X @ r.beta), np.exp(X @ s["beta"]), rtol=2e-3)
    assert np.exp(r.gamma[0]) == pytest.approx(s["alpha"], rel=0.05)


def test_nbmle_recovers_dispersion_submodel():
    rng = np.random.default_rng(3)
    n = 40000
    g = rng.integers(0, 2, n)
    mu = np.exp(2.3 + 0.2 * rng.normal(size=n))
    alpha = np.where(g == 1, 0.15, 0.02)
    r = 1 / alpha
    y = rng.negative_binomial(r, r / (r + mu)).astype(float)
    Z = np.column_stack([np.ones(n), g])
    fit = fit_nb(y, np.zeros((n, 0)), Z, offset=np.log(mu), x_const=None, z_const=0)
    a0, a1 = np.exp(fit.gamma[0]), np.exp(fit.gamma.sum())
    assert a0 == pytest.approx(0.02, abs=0.006) and a1 == pytest.approx(0.15, rel=0.1)


def test_recency_weights_half_life():
    d = pd.to_datetime(["2020-01-01", "2021-01-01", "2021-12-31"]).to_numpy()
    w = recency_weights(d, 364)
    assert w[-1] == 1.0 and w[1] == pytest.approx(0.5, rel=0.01)
    assert recency_weights(d, None) is None


def test_flexnb_default_is_production_nb(data):
    train, test = data
    prod, flex = NegBinModel().fit(train), FlexNB().fit(train)
    for side in ("home", "away"):
        np.testing.assert_allclose(flex.side_mu(test, side), prod.side_mu(test, side), rtol=2e-3)
    np.testing.assert_allclose(flex.total_cdf(test)[:, :80], np.cumsum(prod.total_pmf(test), axis=1)[:, :80],
                               atol=3e-3)


@pytest.mark.parametrize("kw", [dict(total="alpha_z"), dict(total="rho_z"), dict(side_z=("comp", "logmu")),
                                dict(comp_intercept=True, half_life_days=365)])
def test_flexnb_variants_give_proper_cdfs(data, kw):
    train, test = data
    m = FlexNB(**kw).fit(train)
    c = m.total_cdf(test)
    assert np.all(np.diff(c, axis=1) >= -1e-12) and np.all(c[:, -1] > 0.999999)
    s = m.side_cdf(test, "home")
    assert s.shape[1] >= 81 and np.all(s[:, -1] > 0.999)


def test_joint_share_team_curves_are_mixtures(data):
    train, test = data
    m = JointShare().fit(train)
    t = test.head(30)
    h, a = m.side_cdf(t, "home"), m.side_cdf(t, "away")
    assert np.all(h[:, -1] > 0.9999) and np.all(a[:, -1] > 0.9999)
    # E[H] + E[A] = E[T]
    eh, ea = (1 - h[:, :-1]).sum(axis=1), (1 - a[:, :-1]).sum(axis=1)
    et = (1 - m.total_cdf(t)[:, :-1]).sum(axis=1)
    np.testing.assert_allclose(eh + ea, et, rtol=2e-3)


def test_quantile_average_of_identical_cdfs_is_identity():
    c = np.cumsum(nb_pmf_fast(np.array([20.0, 25.0]), 0.03, 80), axis=1)
    q = _quantile_average(c, c, 0.4, n_levels=4000)
    assert np.abs(q - c[:, :q.shape[1]]).max() < 2e-3


def test_candidate_does_not_support_explain(data):
    from shots.models import explain as ex

    train, _ = data
    m = CANDIDATES["cand_disp_total"]().fit(train)
    assert not ex.supports(m)          # would need an NB GLM decomposition
    assert ex.supports(MODELS["negbin_pit_comp"]().fit(train))


def _with_candidate_columns(df, seed=5):
    """Fake family A / G columns (shape only) for the synthetic frame."""
    rng = np.random.default_rng(seed)
    out = df.copy()
    n = len(out)
    for side in ("home", "away"):
        for c in ("adj_att", "adj_def", "adj_sot_att", "adj_sot_def"):
            out[f"{side}_{c}"] = rng.normal(0, 0.15, n)
        out[f"{side}_adj_att_sd"] = out[f"{side}_adj_def_sd"] = 0.08
        out[f"adj_exp_{side}_shots"] = out[f"{side}_shots_for_l10"].fillna(12.0)
        out[f"{side}_ctx_gap_safety"] = rng.normal(0, 8, n)
        out[f"{side}_ctx_games_left"] = rng.integers(0, 30, n).astype(float)
    out["ctx_dead_n"] = rng.integers(0, 3, n).astype(float)
    out["ctx_progress"] = rng.uniform(0, 1, n)
    return out


def test_families_and_shrink_fit_and_stay_consistent(data):
    train, test = (_with_candidate_columns(d) for d in data)
    m = FlexNB(total="alpha_z", families=("A", "G"), shrink=True).fit(train)
    c, b = m.shrink_
    assert 0.2 < b < 1.5 and abs(c) < 0.2
    mh, ma = m.side_mu(test, "home"), m.side_mu(test, "away")
    mu, _ = m.total_params(test)
    np.testing.assert_allclose(mh + ma, mu, rtol=1e-10)   # team means add up to the shrunk total
    blk = m.side_fits_["home"]["block"]
    assert "A_log_exp" in blk.cols_ and "G_last3_s" in blk.cols_


def test_rule1_columns_are_refused():
    from shots.models.candidates import _num

    df = pd.DataFrame({"home_sched_days_to_next": [3.0], "home_sched_ucl_days_to": [2.0]})
    for c in df.columns:
        with pytest.raises(ValueError):
            _num(df, c)


def test_candidate_median_is_read_off_the_published_curve(data):
    from shots.evaluation.scoring import row_scores

    train, test = data
    m = CANDIDATES["cand_disp_total"]().fit(train)
    t = test.head(200)
    pred = m.predict(t)
    from shots.models.screen import total_curve

    F = total_curve(m, t, 120)
    df = pd.DataFrame(F, columns=[f"cdf_{k}" for k in range(F.shape[1])]).assign(actual=t["total_shots"].to_numpy())
    np.testing.assert_array_equal(row_scores(df)["median"].to_numpy(float), pred["median"].to_numpy())
