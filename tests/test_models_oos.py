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
