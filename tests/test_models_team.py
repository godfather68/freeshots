"""Per-team shot predictions of the NegBinCal family (team_predict / team_prob_at_least).

Also pins the total-shots outputs of negbin_pit_comp to the values produced *before* per-team
calibration was added (golden hashes computed with the pre-change code on 2026-10-09), so the
team addition provably leaves the product unchanged.
"""

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from shots.models import MODELS
from shots.models.base import CONFIDENCES, bound_column
from shots.models.calibrated import NegBinCal, NegBinPitComp, _side_cdf
from shots.models.common import TARGETS, lb_from_cdf
from shots.models.persist import _negbin_inputs, load, load_metadata, save
from tests.test_models import SEASONS, synthetic

LB = [bound_column(q) for q in CONFIDENCES]
CAL_MODELS = ["negbin_cal", "negbin_cal_comp", "negbin_pit_comp"]
FEATURES_PATH = Path(__file__).resolve().parents[1] / "data" / "processed" / "features.parquet"


def no_odds(f: pd.DataFrame) -> pd.DataFrame:
    f = f.copy()
    f[[c for c in f.columns if c.startswith("mkt_")]] = np.nan
    return f


@pytest.fixture(scope="module")
def data():
    df = synthetic(seed=3)
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


@pytest.fixture(scope="module")
def pit(data):
    return NegBinPitComp(min_cal_rows=1000).fit(data[0])


@pytest.fixture(scope="module")
def pit_noodds(data):
    return NegBinPitComp(min_cal_rows=1000).fit(no_odds(data[0]))


def team_cov(test, pred, side, q):
    return float((test[f"{side}_shots"] >= pred[bound_column(q)]).mean())


# --------------------------------------------------------------------------- contract

@pytest.mark.parametrize("name", CAL_MODELS)
@pytest.mark.parametrize("side", ["home", "away"])
def test_team_contract(name, side, data):
    train, test = data
    m = MODELS[name](min_cal_rows=1000).fit(train)
    pred = m.team_predict(test, side)
    assert pred.index.equals(test.index)
    assert list(pred.columns) == ["median", *LB, "mean"]
    assert pred["median"].dtype.kind == "f" and pred["mean"].dtype.kind == "f"
    for c in LB:
        assert pred[c].dtype.kind == "i" and (pred[c] >= 0).all()
    assert (pred["median"] >= pred["lb_70"]).all()
    assert (pred["lb_70"] >= pred["lb_80"]).all() and (pred["lb_80"] >= pred["lb_90"]).all()
    assert pred.notna().all().all()
    sub = test.sample(60, random_state=2)
    pd.testing.assert_frame_equal(m.team_predict(sub, side), pred.loc[sub.index])
    p = m.team_prob_at_least(sub, side, 11)
    assert p.shape == (60,)
    np.testing.assert_array_equal(p, m.team_prob_at_least(test, side, 11)[test.index.get_indexer(sub.index)])


def test_team_prob_at_least_is_a_survival_function(pit, data):
    _, test = data
    for side in ("home", "away"):
        P = np.stack([pit.team_prob_at_least(test, side, n) for n in range(0, 41)], axis=1)
        assert np.allclose(P[:, 0], 1.0)
        assert (P >= 0).all() and (P <= 1 + 1e-12).all()
        assert (np.diff(P, axis=1) <= 1e-12).all()
        # per-row n works and matches the scalar call
        n = np.where(np.arange(len(test)) % 2 == 0, 9, 13)
        exp = np.where(n == 9, P[:, 9], P[:, 13])
        np.testing.assert_allclose(pit.team_prob_at_least(test, side, n), exp)
        # stated confidence of the side's own bounds is >= q (pit target, monotone clip aside)
        bp = pit.team_bound_probabilities(test, side)
        for q in CONFIDENCES:
            assert (bp[f"p_{round(q * 100)}"] >= q - 0.03).mean() > 0.99


def test_bad_side_and_confidence(pit, data):
    _, test = data
    with pytest.raises(ValueError):
        pit.team_predict(test, "total")
    with pytest.raises(ValueError):
        pit.team_prob_at_least(test, "Home", 10)
    with pytest.raises(ValueError):
        pit.team_predict(test, "home", confidences=(0.95,))


def test_no_odds_variant_works(pit_noodds, data):
    _, test = data
    t = no_odds(test)
    for side in ("home", "away"):
        pred = pit_noodds.team_predict(t, side)
        assert pred.notna().all().all()
        for q in CONFIDENCES:
            assert team_cov(test, pred, side, q) >= q - 0.03


# --------------------------------------------------------------------------- leakage

def test_team_outputs_ignore_targets_of_predict_rows(pit, data):
    _, test = data
    tampered = test.copy()
    tampered[TARGETS] = 0.0
    dropped = test.drop(columns=TARGETS)
    for side in ("home", "away"):
        ref = pit.team_predict(test, side)
        pd.testing.assert_frame_equal(ref, pit.team_predict(tampered, side))
        pd.testing.assert_frame_equal(ref, pit.team_predict(dropped, side))
        np.testing.assert_array_equal(pit.team_prob_at_least(test, side, 12),
                                      pit.team_prob_at_least(dropped, side, 12))


def test_team_calibration_uses_only_training_window(data):
    """Future rows with targets must not change the fit if they are not passed to fit; rows
    without targets passed to fit are ignored; the calibration window is inside train."""
    train, test = data
    a = NegBinPitComp(min_cal_rows=1000).fit(train)
    extra = test.copy()
    extra[TARGETS] = np.nan
    b = NegBinPitComp(min_cal_rows=1000).fit(pd.concat([train, extra]))
    assert set(a.cal_seasons_) <= set(train["season"])
    for side in ("home", "away"):
        pd.testing.assert_frame_equal(a.team_predict(test, side), b.team_predict(test, side))
        assert a.team_calibrator_[side].summary() == b.team_calibrator_[side].summary()
        n_cal = a.team_cal_report_[side][0.8]["n"]
        assert n_cal == int(train["season"].isin(a.cal_seasons_).sum())


# --------------------------------------------------------------------------- calibration

def test_team_bounds_calibrated_on_well_specified_data(pit, data):
    _, test = data
    for side in ("home", "away"):
        pred = pit.team_predict(test, side)
        bp = pit.team_bound_probabilities(test, side)
        for q in CONFIDENCES:
            cov = team_cov(test, pred, side, q)
            assert cov >= q - 0.02
            assert abs(bp[f"p_{round(q * 100)}"].mean() - cov) < 0.03  # stated ~= realised
        y = test[f"{side}_shots"].to_numpy()
        cdf = _side_cdf(pit.nb_, test, side)
        for n in (8, 10, 12, 14):
            p = pit.team_prob_at_least(test, side, n)
            # the test season's realised frequencies are noisy (the true NB is off by ~2.7pp
            # here too), so: close to realised, and the PIT map is ~identity on NB data
            assert abs(p.mean() - np.mean(y >= n)) < 0.035
            assert abs(p.mean() - (1 - cdf[:, n - 1]).mean()) < 0.015


def test_team_calibration_corrects_a_side_specific_shift():
    """Home shots drop by 3 from the 2nd season on (calibration window + test, not the NB fit
    rows): the raw home NB over-states, the per-side recalibration restores coverage, and the
    away side (unshifted) stays calibrated."""
    df = synthetic(seed=7)
    late = df["season"] >= SEASONS[1]
    df.loc[late, "home_shots"] = (df.loc[late, "home_shots"] - 3).clip(lower=0)
    df["total_shots"] = df["home_shots"] + df["away_shots"]
    train, test = df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]
    m = NegBinPitComp(min_cal_rows=1000, refit_full=False).fit(train)
    cdf = _side_cdf(m.nb_, test, "home")
    for q in CONFIDENCES:
        raw = float((test["home_shots"] >= lb_from_cdf(cdf, q)).mean())
        cal = team_cov(test, m.team_predict(test, "home"), "home", q)
        assert raw < q - 0.04
        assert q - 0.02 <= cal <= q + 0.07  # integer granularity of ~10-shot counts
        assert team_cov(test, m.team_predict(test, "away"), "away", q) >= q - 0.02
    y = test["home_shots"].to_numpy()
    assert abs(m.team_prob_at_least(test, "home", 10).mean() - np.mean(y >= 10)) < 0.03


def test_exact_target_team_coverage_close_to_q(data):
    train, test = data
    m = NegBinCal(min_cal_rows=1000).fit(train)  # target="exact"
    for side in ("home", "away"):
        pred = m.team_predict(test, side)
        for q in CONFIDENCES:
            assert abs(team_cov(test, pred, side, q) - q) < 0.035


# --------------------------------------------------------------------------- persistence

def test_team_save_load_roundtrip(pit, data, tmp_path):
    train, test = data
    path = save(pit, tmp_path / "m", train)
    m2 = load(path)
    for side in ("home", "away"):
        pd.testing.assert_frame_equal(pit.team_predict(test, side), m2.team_predict(test, side))
        np.testing.assert_array_equal(pit.team_prob_at_least(test, side, 12),
                                      m2.team_prob_at_least(test, side, 12))
    meta = load_metadata(path)
    assert set(meta["team_calibration"]) == {"home", "away"}
    assert meta["team_calibration"]["home"]["thresholds"]


def test_model_pickled_before_team_calibration_raises_clear_error(pit, data, tmp_path):
    _, test = data
    path = save(pit, tmp_path / "old", data[0])
    old = load(path)
    for attr in ("team_calibrator_", "team_pit_map_", "team_cal_report_"):
        delattr(old, attr)  # = the state of a model pickled before this change
    assert not old.has_team_calibration and pit.has_team_calibration
    pd.testing.assert_frame_equal(old.predict(test), pit.predict(test))  # totals still work
    with pytest.raises(RuntimeError, match="shots.models.train"):
        old.team_predict(test, "home")
    with pytest.raises(RuntimeError, match="Retrain"):
        old.team_prob_at_least(test, "away", 10)


# --------------------------------------------------------------------------- totals unchanged

def total_outputs(m, rows) -> pd.DataFrame:
    out = m.predict(rows).join(m.bound_probabilities(rows))
    for n in range(10, 46):
        out[f"P{n}"] = m.prob_at_least(rows, n)
    return out


def digest(df: pd.DataFrame) -> str:
    x = np.round(df.to_numpy(float), 9) + 0.0
    return hashlib.sha256(np.ascontiguousarray(x).tobytes()).hexdigest()


# sha256 of total_outputs rounded to 1e-9, computed with the code before per-team calibration.
GOLDEN_SYN = {
    "odds": "beb907fc094d8dc3fa0ccf8b73d96e37a773c592a525c2de6318a10abae37db7",
    "noodds": "03d059e4480d333692b242d00f36aa0fce2a04a42200abeba45d59083642c105",
}
REAL_COMPS = ["England Premier League", "England Championship"]
REAL_TRAIN = ["2018/2019", "2019/2020", "2020/2021", "2021/2022"]
REAL_TEST = "2022/2023"
REAL_INPUT = "2c073402ffd9cad377fffafef3b361480c5e7a689bf9ce8e4d0fb5aba660c6de"
GOLDEN_REAL = {
    "odds": "33953457b80987623e0d95bfc923fdfc1447ab5abdefaad71234f49f0a750e23",
    "noodds": "910408bd32f17184627a01393af18c6cefdbc1e7a3bba48c53dbb0c81d8a7cc3",
}


@pytest.mark.parametrize("variant", ["odds", "noodds"])
def test_total_outputs_unchanged_synthetic(variant, data, pit, pit_noodds):
    _, test = data
    m, rows = (pit, test) if variant == "odds" else (pit_noodds, no_odds(test))
    assert digest(total_outputs(m, rows)) == GOLDEN_SYN[variant]


@pytest.mark.parametrize("variant", ["odds", "noodds"])
def test_total_outputs_unchanged_real_sample(variant):
    if not FEATURES_PATH.exists():
        pytest.skip("data/processed/features.parquet not built")
    f = pd.read_parquet(FEATURES_PATH)
    f = f[f["competition"].isin(REAL_COMPS) & f["season"].isin([*REAL_TRAIN, REAL_TEST])]
    cols = sorted(_negbin_inputs() | {"season", "competition", "date", *TARGETS})
    if set(cols) - set(f.columns):
        pytest.skip("features.parquet lacks columns of the golden sample")
    h = pd.util.hash_pandas_object(f[cols].reset_index(drop=True), index=False).to_numpy()
    if hashlib.sha256(h.tobytes()).hexdigest() != REAL_INPUT:
        pytest.skip("features.parquet changed since the golden sample was taken (2026-10-09)")
    fn = (lambda x: x) if variant == "odds" else no_odds
    tr, te = f[f["season"].isin(REAL_TRAIN)], f[f["season"] == REAL_TEST]
    m = NegBinPitComp(min_cal_rows=900).fit(fn(tr))
    assert digest(total_outputs(m, fn(te))) == GOLDEN_REAL[variant]
