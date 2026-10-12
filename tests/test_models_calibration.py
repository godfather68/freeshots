"""Tests for the 3b models: conformal/PIT calibration, blend, persistence (save/load, train CLI).

Contract compliance and target-leakage tests for the new models run in test_models.py, which is
parametrized over the whole registry.
"""

import json

import numpy as np
import pandas as pd
import pytest

from shots.models import MODELS
from shots.models.base import CONFIDENCES, bound_column
from shots.models.calibrated import Blend, NegBinCal, NegBinPitComp, carve_window
from shots.models.calibration import (ConformalShift, PitLevel, PitMap, cdf_at, conformal_quantile,
                                      continuous_quantile, lb_at_level, pit_level)
from shots.models.common import lb_from_cdf
from shots.models.negbin import NegBinModel, nb_pmf
from shots.models.persist import load, load_metadata, save
from shots.models.train import train_and_save
from tests.test_models import SEASONS, synthetic

NEW = ["negbin_cal", "negbin_cal_comp", "negbin_pit_comp", "blend"]


@pytest.fixture(scope="module")
def data():
    df = synthetic(seed=3)
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


@pytest.fixture(scope="module")
def shifted():
    """Shot volume drops by 2 per side from the 3rd season on (cal + test), not in the fit rows."""
    df = synthetic(seed=5)
    late = df["season"] >= SEASONS[2]
    for c in ("home_shots", "away_shots"):
        df.loc[late, c] = (df.loc[late, c] - 2).clip(lower=0)
    df["total_shots"] = df["home_shots"] + df["away_shots"]
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


def coverage(test, pred, q):
    return float((test["total_shots"] >= pred[bound_column(q)]).mean())


# --------------------------------------------------------------------------- primitives

def test_conformal_quantile_weighted_ones_equals_unweighted():
    rng = np.random.default_rng(0)
    s = rng.normal(size=501)
    for q in CONFIDENCES:
        assert conformal_quantile(s, q) == conformal_quantile(s, q, np.ones_like(s))
        assert np.mean(s <= conformal_quantile(s, q)) >= q


def test_recency_weights_shift_quantile_towards_recent_scores():
    s = np.r_[np.zeros(500), np.ones(500)]          # old scores 0, recent scores 1
    dates = pd.Series(pd.date_range("2020-01-01", periods=1000, freq="D"))
    q_flat = ConformalShift().fit({0.4: s}, dates=dates).Q_[0.4]["__global__"]
    q_rec = ConformalShift(half_life_days=60).fit({0.4: s}, dates=dates).Q_[0.4]["__global__"]
    assert q_flat == 0.0 and q_rec == 1.0


def test_group_fallback_to_global():
    rng = np.random.default_rng(1)
    s = np.r_[rng.normal(0, 1, 1000), rng.normal(5, 1, 50)]
    g = np.r_[np.repeat("big", 1000), np.repeat("small", 50)]
    cs = ConformalShift("competition", min_group=300).fit({0.8: s}, pd.Series(g))
    th = cs.threshold(0.8, pd.Series(["big", "small", "unseen"]), 3)
    assert th[0] == cs.Q_[0.8]["big"]
    assert th[1] == th[2] == cs.Q_[0.8]["__global__"]
    assert "small" not in cs.Q_[0.8]


def test_pit_level_is_identity_for_a_calibrated_discrete_model():
    rng = np.random.default_rng(2)
    mu = rng.uniform(15, 30, 20000)
    cdf = np.cumsum(nb_pmf(mu, 0.04, 120), axis=1)
    r = 1 / 0.04
    y = rng.negative_binomial(r, r / (r + mu))
    F, Fm1 = cdf_at(cdf, y), cdf_at(cdf, y - 1)
    for q in CONFIDENCES:
        t = pit_level(Fm1, F, 1 - q)
        assert abs(t - (1 - q)) < 0.015
        # integer bounds read at the calibrated level over-cover (granularity), never under-cover
        cov = np.mean(y >= lb_at_level(cdf, np.full(len(y), t)))
        assert q - 0.01 < cov < q + 0.06


def test_continuous_quantile_ceil_matches_integer_bound():
    cdf = np.cumsum(nb_pmf(np.array([22.0, 7.5, 40.0]), 0.05, 160), axis=1)
    for q in CONFIDENCES:
        x = continuous_quantile(cdf, 1 - q)
        assert (np.ceil(x - 1e-9).astype(int) == lb_from_cdf(cdf, q)).all()


def test_carve_window_takes_latest_seasons_from_train_only(data):
    train, _ = data
    fit_df, cal = carve_window(train, 2, min_rows=1000)  # synthetic seasons have 1500 rows
    assert sorted(cal["season"].unique()) == SEASONS[1:3]
    assert sorted(carve_window(train, 1, 1000)[1]["season"].unique()) == [SEASONS[2]]
    assert fit_df["date"].max() < cal["date"].min()
    assert len(fit_df) + len(cal) == len(train)


# --------------------------------------------------------------------------- models

def test_cal_window_never_uses_predict_rows(data):
    train, test = data
    m = NegBinCal(min_cal_rows=1000).fit(train)  # synthetic seasons have 1500 rows
    assert m.cal_seasons_ == [SEASONS[2]]
    assert MODELS["negbin_cal_comp"](min_cal_rows=1000).fit(train).cal_seasons_ == SEASONS[1:3]
    b = Blend(min_cal_rows=1000).fit(train)
    assert b.cal_seasons_ == [SEASONS[2]]
    assert test["season"].iloc[0] not in m.cal_seasons_ + b.cal_seasons_


@pytest.mark.parametrize("name", ["negbin_cal", "negbin_cal_comp", "blend"])
def test_exact_conformal_hits_target(name, data):
    train, test = data
    m = MODELS[name]().fit(train)
    pred = m.predict(test)
    for q in CONFIDENCES:
        rep = m.cal_report_[q]
        assert rep["cal_coverage"] >= q - 0.005  # ties in discrete scores can cost a hair
        assert rep["cal_coverage"] < q + 0.03
        assert abs(coverage(test, pred, q) - q) < 0.035, (q, coverage(test, pred, q))


def test_pit_model_is_valid_and_close_to_raw_on_well_specified_data(data):
    train, test = data
    pit = NegBinPitComp().fit(train).predict(test)
    raw = NegBinModel().fit(train).predict(test)
    for q in CONFIDENCES:
        c = bound_column(q)
        assert coverage(test, pit, q) > q - 0.02
        assert (pit[c] - raw[c]).abs().mean() < 0.5  # calibrated NB -> (almost) no change


def test_conformal_corrects_a_level_shift(shifted):
    """Fit rows have higher shot volume than cal/test rows: raw NB under-covers, the conformal
    correction (estimated on the latest training season) restores coverage."""
    train, test = shifted
    fit_rows = train[train["season"] < SEASONS[2]]
    raw = NegBinModel().fit(fit_rows).predict(test)
    cal = NegBinCal(refit_full=False, min_cal_rows=1000).fit(train).predict(test)
    pit = NegBinCal(target="pit", refit_full=False, min_cal_rows=1000).fit(train).predict(test)
    for q in CONFIDENCES:
        assert coverage(test, raw, q) < q - 0.05
        assert abs(coverage(test, cal, q) - q) < 0.035
        assert coverage(test, pit, q) > q - 0.025


# --------------------------------------------------------------------------- persistence

@pytest.mark.parametrize("name", ["negbin", "qgbm", *NEW, "league_avg", "team_avg"])
def test_save_load_roundtrip(name, data, tmp_path):
    train, test = data
    m = MODELS[name]().fit(train)
    path = save(m, tmp_path / name, train)
    m2 = load(path)
    pd.testing.assert_frame_equal(m.predict(test), m2.predict(test))
    meta = load_metadata(path)
    assert meta["model"] == name
    assert meta["training"]["date_max"] == str(train["date"].max().date())
    assert meta["features"] and "numpy" in meta["packages"]
    if name in NEW:
        assert meta["cal_seasons"] and meta["calibration"]["thresholds"]
        assert set(meta["cal_report"]) == {str(q) for q in CONFIDENCES}


def test_train_cli_respects_until(data, tmp_path):
    train, test = data
    feats = pd.concat([train, test])
    fp = tmp_path / "features.parquet"
    feats.to_parquet(fp)
    until = str(test["date"].min().date())
    out = train_and_save("negbin_cal", until, fp, tmp_path / "store")
    assert out.name == f"negbin_cal_{until}"
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["training"]["date_max"] <= until
    assert meta["until"] == until
    later = test[test["date"] > pd.Timestamp(until)]
    pred = load(out).predict(later)
    assert pred.index.equals(later.index)


def test_pit_map_identity_when_calibrated_and_probabilities_match_coverage(data):
    rng = np.random.default_rng(4)
    a = rng.uniform(0, 1, 5000)
    pm = PitMap().fit(a, a)  # continuous uniform PIT -> G(t) = t
    t = np.linspace(0, 1, 11)
    assert np.allclose(pm(t), t, atol=0.02)

    train, test = data
    m = NegBinPitComp(min_cal_rows=1000).fit(train)
    probs = m.bound_probabilities(test)
    pred = m.predict(test)
    for q in CONFIDENCES:
        p = probs[f"p_{round(q * 100)}"]
        assert (p >= q - 0.03).all() and (p <= 1).all()
        # calibrated probabilities: mean stated probability ~= realised coverage
        assert abs(p.mean() - coverage(test, pred, q)) < 0.03
