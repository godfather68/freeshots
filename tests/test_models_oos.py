"""Out-of-sample correction terms of FlexNB (shots.models.candidates.OOS_TERMS, 2026-10-26): fitted on the carved
latest training season only, recover a planted signal, leave fin_c's shrinkage-only path untouched."""

import numpy as np
import pytest

from shots.models import MODELS
from shots.models.candidates import FIN_C_BASE, OOS_TERMS, FlexNB, oos_inputs, oos_raw
from tests.test_models import SEASONS, synthetic


@pytest.fixture(scope="module")
def planted():
    df = synthetic()
    mom = df[["home_form_res_l3", "away_form_res_l3"]].mean(axis=1).fillna(0.0)
    bump = np.round(0.6 * mom).astype(float)        # recent over-shooting carries on: + 0.6 shot per shot
    df["home_shots"] = (df["home_shots"] + np.floor(bump / 2)).clip(lower=0)
    df["away_shots"] = (df["away_shots"] + np.ceil(bump / 2)).clip(lower=0)
    df["total_shots"] = df["home_shots"] + df["away_shots"]
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


def test_terms_are_fitted_on_the_carved_season_and_recover_the_signal(planted):
    train, test = planted
    m = FlexNB(oos_terms=OOS_TERMS).fit(train)
    assert m.oos_ is not None and m.oos_["terms"] == OOS_TERMS
    coef = dict(zip(m.oos_["terms"], m.oos_["coef"]))
    assert coef["mom"] > 0.05
    assert np.isfinite(m.total_params(test)[0]).all()
    # the planted effect is visible out of sample: residual vs momentum flattens
    mom = np.nan_to_num(oos_raw(test, np.ones(len(test)), "mom"))
    base = FlexNB().fit(train)
    for model, sign in ((base, 1), (m, 0)):
        r = test["total_shots"].to_numpy() - model.total_params(test)[0]
        slope = np.polyfit(mom, r, 1)[0]
        if sign:
            assert slope > 1.0
        else:
            assert abs(slope) < 1.0


def test_fin_c_path_is_unchanged(planted):
    train, _ = planted
    m = FlexNB(**FIN_C_BASE).fit(train)
    assert m.oos_ is None and m.shrink_ is not None and m.oos_terms == ()


def test_targets_of_later_rows_do_not_reach_the_correction(planted):
    """The correction uses training rows only: changing targets of rows after the training window changes nothing."""
    train, test = planted
    a = FlexNB(oos_terms=OOS_TERMS).fit(train)
    b = FlexNB(oos_terms=OOS_TERMS).fit(train)
    t2 = test.copy()
    t2["total_shots"] = 0.0
    np.testing.assert_allclose(a.total_params(test)[0], b.total_params(t2)[0])


def test_inputs_are_listed_for_persistence():
    cols = MODELS["cand_odds_oos"]().base_kw
    assert cols["oos_rows"] == 12000 and cols["shrink"] is True
    m = FlexNB(oos_terms=OOS_TERMS)
    assert set(oos_inputs(OOS_TERMS)) <= set(m.input_columns())


# --------------------------------------------------------------------------- "Surge": damping and explanation factor

@pytest.fixture(scope="module")
def surge(planted):
    train, test = planted
    return MODELS["cand_odds_surge"]().fit(train), test


def test_damping_scales_the_whole_log_correction(surge):
    model, test = surge
    nb = model.nb_
    rh, ra = nb._raw_mu(test, "home"), nb._raw_mu(test, "away")
    lam, eta, T = nb.ratio_parts(test, rh, ra)
    assert lam == 0.4 and T is not None
    np.testing.assert_allclose(nb._ratio(test, rh, ra), np.exp(0.4 * (eta + T.sum(axis=1))))


def test_surge_factor_is_exact_and_labelled(surge):
    import json

    from shots.models import explain as ex

    model, test = surge
    rows = test.head(60)
    raw = ex.raw_explain(model, rows)
    sg = [c for c in raw.columns if c.startswith("sg_")]
    assert sg and "fx_surge" in raw and "fx_shrinkage" in raw
    np.testing.assert_allclose(raw[sg].sum(axis=1), raw["fx_surge"], atol=1e-9)
    nb = model.nb_
    _, _, unshrunk, total = ex.flex_effects(nb, rows)
    np.testing.assert_allclose(raw["fx_surge"] + raw["fx_shrinkage"], total - unshrunk, atol=1e-9)
    fx = [c for c in raw.columns if c.startswith("fx_")]
    np.testing.assert_allclose(raw["baseline"] + raw[fx].sum(axis=1) + raw["residual"], raw["expected_mean"], atol=1e-9)
    schema = json.loads(open("docs/static-api.schema.json", encoding="utf-8").read())
    assert "surge" in ex.FACTOR_ORDER and "surge" in json.dumps(schema)
    seen = 0
    for e in ex.explain_rows(model, rows, odds_used=True):
        for f in e["factors"]:
            if f["id"] == "surge":
                seen += 1
                assert f["label"] == "Surge correction" and 0 < len(f["detail"]) <= 160
    assert seen > 0


def test_fin_c_has_no_surge_factor(planted):
    from shots.models import explain as ex

    train, test = planted
    raw = ex.raw_explain(MODELS["fin_c"]().fit(train), test.head(20))
    assert "fx_surge" not in raw and not any(c.startswith("sg_") for c in raw.columns)


def test_surge_text():
    from shots.models.explain import _surge_text

    t = _surge_text({"last3": 0.6, "dead": 0.3, "goals": 0.4, "mom": -0.2, "h2h": 0.04})
    assert t == "season end +0.9; low-goal run +0.4; momentum -0.2"


def test_cap_and_undamped_terms(surge):
    model, test = surge
    nb = model.nb_
    rh, ra = nb._raw_mu(test, "home"), nb._raw_mu(test, "away")
    lam, eta, T = nb.ratio_parts(test, rh, ra)
    nb.oos_cap = 0.01
    try:
        r = np.log(nb._ratio(test, rh, ra)) - lam * eta
        assert np.all(np.abs(r) <= 0.01 + 1e-12)
    finally:
        nb.oos_cap = None
    nb.oos_keep = ("last3", "dead")
    try:
        _, _, T2 = nb.ratio_parts(test, rh, ra)
        j = [nb.oos_["terms"].index(t) for t in ("last3", "dead")]
        np.testing.assert_allclose(lam * T2[:, j], T[:, j])          # season-end terms at full strength
        other = [i for i in range(T.shape[1]) if i not in j]
        np.testing.assert_allclose(T2[:, other], T[:, other])
    finally:
        nb.oos_keep = ()
