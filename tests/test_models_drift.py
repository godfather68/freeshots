"""League-drift candidate ``negbin_pit_comp_drift`` (src/shots/models/drift.py)."""

import numpy as np
import pandas as pd
import pytest

from shots.models import MODELS
from shots.models.base import CONFIDENCES, bound_column
from shots.models.calibrated import NegBinPitComp
from shots.models.common import pinball
from shots.models.drift import DRIFT_SOURCES, DriftDesign, NegBinDriftModel, NegBinPitCompDrift
from shots.models.negbin import NegBinModel
from shots.models.persist import load, load_metadata, save
from tests.test_models import SEASONS, synthetic

LB = [bound_column(q) for q in CONFIDENCES]


def no_odds(f: pd.DataFrame) -> pd.DataFrame:
    f = f.copy()
    f[[c for c in f.columns if c.startswith("mkt_")]] = np.nan
    return f


@pytest.fixture(scope="module")
def data():
    df = synthetic(seed=5)
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


@pytest.fixture(scope="module")
def drifted():
    """Each competition-month has a league-wide level shift visible to the drift feature only:
    true rate = base rate * (1 + g / prev), g = league_global_drift28."""
    df = synthetic(seed=11)
    rng = np.random.default_rng(99)
    block = df["competition"] + df["date"].dt.to_period("M").astype(str)
    levels = {b: rng.normal(0, 1.5) for b in sorted(block.unique())}
    g = block.map(levels) + rng.normal(0, 0.1, len(df))
    df["league_global_drift28"] = g
    factor = (1 + g / df["league_prev_level_shots"]).to_numpy()
    for side in ("home", "away"):
        lam = df[f"{side}_shots"].to_numpy() + 0.5
        df[f"{side}_shots"] = rng.poisson(lam * factor).astype(float)
    df["total_shots"] = df["home_shots"] + df["away_shots"]
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


def test_registered_and_production_untouched():
    assert MODELS["negbin_pit_comp_drift"] is NegBinPitCompDrift
    assert MODELS["negbin_pit_comp"] is NegBinPitComp
    assert type(NegBinPitComp()._new_nb()) is NegBinModel
    assert type(NegBinPitCompDrift()._new_nb()) is NegBinDriftModel


def test_production_ignores_drift_columns(data):
    train, test = data
    m = NegBinPitComp(min_cal_rows=1000).fit(train)
    t2 = test.copy()
    for c in DRIFT_SOURCES.values():
        t2[c] = 5.0
    pd.testing.assert_frame_equal(m.predict(test), m.predict(t2))


@pytest.mark.parametrize("variant", ["odds", "noodds"])
def test_contract_and_team_methods(variant, data):
    train, test = data
    fn = (lambda x: x) if variant == "odds" else no_odds
    m = NegBinPitCompDrift(min_cal_rows=1000).fit(fn(train))
    pred = m.predict(fn(test))
    assert list(pred.columns) == ["median", *LB]
    assert pred.notna().all().all()
    assert (pred["lb_70"] >= pred["lb_80"]).all() and (pred["lb_80"] >= pred["lb_90"]).all()
    p = m.bound_probabilities(fn(test))
    for q in CONFIDENCES:
        assert (p[f"p_{round(q * 100)}"] >= q - 0.01).mean() > 0.99
    for side in ("home", "away"):
        tp = m.team_predict(fn(test), side)
        assert {"median", "mean", *LB} <= set(tp.columns)
        assert (m.team_prob_at_least(fn(test), side, tp["lb_80"].to_numpy()) >= 0.79).mean() > 0.99
    drift_cols = [c for c in m.nb_.sides_["home"]["cols"] if c.startswith("drift_")]
    assert drift_cols == ["drift_global", "drift_level"]
    if variant == "noodds":
        assert not any(c.startswith("mkt_p") for c in m.nb_.sides_["home"]["cols"])


def test_missing_drift_columns_raise(data):
    train, test = data
    m = NegBinPitCompDrift(min_cal_rows=1000).fit(train)
    with pytest.raises(KeyError, match="league_global_drift28"):
        m.predict(test.drop(columns=["league_global_drift28"]))


def test_predict_ignores_targets(data):
    train, test = data
    m = NegBinPitCompDrift(min_cal_rows=1000).fit(train)
    pd.testing.assert_frame_equal(m.predict(test), m.predict(test.drop(columns=["home_shots", "away_shots", "total_shots"])))


def test_drift_term_recovers_level_shift(drifted):
    train, test = drifted
    base = NegBinPitComp(min_cal_rows=1000).fit(train)
    drift = NegBinPitCompDrift(drift_terms=("global",), min_cal_rows=1000).fit(train)
    coef = drift.nb_.sides_["home"]["coef"]["drift_global"]
    assert 0.7 < coef < 1.3, coef  # log1p(g/prev) with full pass-through has coefficient 1
    y = test["total_shots"]
    pb, pd_ = base.predict(test), drift.predict(test)
    loss = lambda p: np.mean([pinball(y, p[bound_column(q)], 1 - q) for q in CONFIDENCES])
    assert loss(pd_) < loss(base.predict(test)) - 0.1
    assert (pd_["median"] - y).abs().mean() < (pb["median"] - y).abs().mean() - 0.2


def test_prev_base_only_changes_fallback_rows(data):
    train, _ = data
    rows = train.copy()
    rows["league_prior_n"] = np.where(np.arange(len(rows)) % 4 == 0, 6.0, 100.0)
    rows["league_prev_level_shots"] = rows["league_prior_shots"] + 1.0
    a = DriftDesign("home", ("global",), base="prior").fit(rows).transform(rows)
    b = DriftDesign("home", ("global",), base="prev").fit(rows).transform(rows)
    fb = rows["league_prior_n"].to_numpy() < 20
    np.testing.assert_allclose(b.loc[~fb, "log_prior"], a.loc[~fb, "log_prior"])
    np.testing.assert_allclose(b.loc[fb, "log_prior"], np.log(rows.loc[fb, "league_prev_level_shots"]))


def test_early_interaction_vanishes_late(data):
    train, _ = data
    X = DriftDesign("home", ("global",), early_days=60).fit(train).transform(train)
    late = train["days_since_season_start"].to_numpy() >= 60
    assert (X.loc[late, "drift_global_early"] == 0).all()
    early0 = train["days_since_season_start"].to_numpy() == 0
    np.testing.assert_allclose(X.loc[early0, "drift_global_early"], X.loc[early0, "drift_global"])


def test_unknown_term_rejected():
    with pytest.raises(ValueError):
        DriftDesign("home", ("nope",))


def test_save_load_metadata_lists_drift_features(data, tmp_path):
    train, test = data
    m = NegBinPitCompDrift(min_cal_rows=1000).fit(train)
    path = save(m, tmp_path / "drift", train)
    pd.testing.assert_frame_equal(m.predict(test), load(path).predict(test))
    meta = load_metadata(path)
    assert meta["model"] == "negbin_pit_comp_drift"
    assert {"league_prev_level_shots", "league_global_drift28", "league_level_drift"} <= set(meta["features"])
    assert meta["params"]["drift_terms"] == ["global", "level"]
