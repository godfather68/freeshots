"""Opt-in candidate odds route (shots.config.ODDS_MODEL, default production; reports/2026-10-26_overshoot_patterns.md):
switch off = production routing untouched; switch on = odds fixtures scored by the candidate where the candidate
build has them, the production odds model otherwise; no-odds and cross-border routes unchanged."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from shots import config
from shots.features import candidates as C
from shots.models import MODELS
from shots.models.candidates import ODDS_CANDIDATE_REQUIRED, OOS_TERMS, missing_odds_candidate_inputs, oos_inputs
from shots.predict import scan as sc
from tests.test_cross_border import HintModel, scan_inputs, stub_models  # noqa: F401 (fixture)

ODDS = {"odds_home": 2.0, "odds_draw": 3.4, "odds_away": 3.6, "odds_over25": 1.8, "odds_under25": 2.0}


def _inputs():
    m, fx = scan_inputs(with_xb=True)
    dom = fx[~fx["competition"].map(sc.xb.is_cross_border).astype(bool)]
    with_odds = dom.index[:2]
    for c, v in ODDS.items():
        fx[c] = fx[c].astype(float)
        fx.loc[with_odds, c] = v
    return m, fx, dom, set(fx.loc[with_odds, "match_id"])


def _frame(ids, drop=()):
    cols = list(ODDS_CANDIDATE_REQUIRED) + oos_inputs(OOS_TERMS)
    f = pd.DataFrame(1.0, index=pd.Index(sorted(ids), name="match_id"), columns=list(dict.fromkeys(cols)))
    return f.drop(index=list(drop))


@pytest.fixture
def prod_noodds(monkeypatch):
    monkeypatch.setenv("SHOTS_NOODDS_MODEL", "negbin_pit_comp")


@pytest.fixture
def odds_switch(stub_models, prod_noodds, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SHOTS_ODDS_MODEL", "cand_odds_surge")
    monkeypatch.setattr(sc, "latest_candidate_odds_model",
                        lambda name, played, age: (HintModel(+4.0, team=True), f"{name}-odds_x"))
    return stub_models


def _prepare(monkeypatch, frame=None, error=None):
    m, fx, dom, odds_ids = _inputs()

    def build(matches, fixtures):
        if error:
            raise error
        return frame(set(dom["match_id"])) if callable(frame) else frame
    monkeypatch.setattr(sc, "candidate_features", build)
    return sc.prepare_upcoming(fx, m, max_model_age_days=10_000), odds_ids


def _routed(prep):
    return {lab: set(prep.upcoming.loc[mask, "match_id"]) for mask, _, _, lab in prep.routes()}


def test_adopted_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("SHOTS_ODDS_MODEL", raising=False)
    assert config.ODDS_MODEL == "cand_odds_surge" and config.odds_model() == "cand_odds_surge"
    assert config.candidate_odds()
    assert config.SURGE_EXCLUDED_COMPETITIONS == ("Scotland Championship",)
    assert config.odds_excluded_competitions() == ("Scotland Championship",)
    monkeypatch.setenv("SHOTS_ODDS_MODEL", "negbin_pit_comp")            # env still overrides (revert / tests)
    assert config.odds_model() == "negbin_pit_comp" and not config.candidate_odds()
    assert config.odds_excluded_competitions() == ()
    monkeypatch.setenv("SHOTS_ODDS_MODEL", "nope")
    with pytest.raises(ValueError):
        config.odds_model()
    assert "cand_odds_surge" in MODELS and "cand_odds_oos" in MODELS and all(n in MODELS for n in config.CANDIDATE_ODDS_MODELS)


def test_switch_off_keeps_production_odds_routing(stub_models, prod_noodds, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SHOTS_ODDS_MODEL", "negbin_pit_comp")
    prep, odds_ids = _prepare(monkeypatch, frame=lambda ids: pytest.fail("no candidate build when the switch is off"))
    assert prep.odds_fallback is None and prep.odds_info is None and prep.odds_rows is None
    assert _routed(prep)["negbin_pit_comp_2026-10-01"] == odds_ids


def test_excluded_competition_stays_on_production_and_is_not_a_fallback(odds_switch, monkeypatch):
    m, fx, dom, odds_ids = _inputs()
    turkey = set(fx.loc[fx["competition"].eq("Turkey Super Lig") & fx["match_id"].isin(odds_ids), "match_id"])
    assert len(turkey) == 1
    monkeypatch.setattr(config, "SURGE_EXCLUDED_COMPETITIONS", ("Turkey Super Lig",))
    prep, _ = _prepare(monkeypatch, frame=_frame)
    routed = _routed(prep)
    assert routed["negbin_pit_comp_2026-10-01"] == turkey
    assert routed["cand_odds_surge-odds_x"] == odds_ids - turkey
    info = prep.odds_info
    assert info["n_excluded"] == 1 and info["n_fallback"] == 0 and info["n_odds"] == 1
    assert info["excluded_competitions"] == ["Turkey Super Lig"]
    prep.check_fallback()                                     # excluded fixtures never fail the run


def test_only_excluded_odds_fixtures_need_no_candidate_build(odds_switch, monkeypatch):
    monkeypatch.setattr(config, "SURGE_EXCLUDED_COMPETITIONS", ("England Premier League", "Turkey Super Lig"))
    prep, odds_ids = _prepare(monkeypatch, frame=lambda ids: pytest.fail("nothing for the candidate to score"))
    assert _routed(prep)["negbin_pit_comp_2026-10-01"] == odds_ids
    assert prep.odds_info["n_excluded"] == 2 and prep.odds_info["n_candidate"] == 0
    prep.check_fallback()


def test_complete_build_routes_odds_fixtures_to_the_candidate(odds_switch, monkeypatch):
    prep, odds_ids = _prepare(monkeypatch, frame=_frame)
    routed = _routed(prep)
    assert routed["cand_odds_surge-odds_x"] == odds_ids
    assert "negbin_pit_comp_2026-10-01" not in routed
    assert any(lab == "noodds_label" for lab in routed)                       # no-odds route unchanged
    assert all(lab.startswith("noodds_label-") for lab in routed
               if lab not in ("cand_odds_surge-odds_x", "noodds_label"))       # cross-border unchanged
    assert prep.odds_info["n_odds"] == 2 and prep.odds_info["n_fallback"] == 0
    prep.check_fallback()


def test_fixture_missing_from_the_build_falls_back_to_production(odds_switch, monkeypatch):
    m, fx, dom, odds_ids = _inputs()
    gone = sorted(odds_ids)[0]
    prep, _ = _prepare(monkeypatch, frame=lambda ids: _frame(ids, drop=[gone]))
    routed = _routed(prep)
    assert routed["negbin_pit_comp_2026-10-01"] == {gone}
    assert routed["cand_odds_surge-odds_x"] == odds_ids - {gone}
    assert prep.odds_info["n_fallback"] == 1
    with pytest.raises(sc.CandidateFallbackError, match="odds fixtures"):
        prep.check_fallback()


def test_failed_build_scores_every_odds_fixture_with_production(odds_switch, monkeypatch):
    prep, odds_ids = _prepare(monkeypatch, error=RuntimeError("boom"))
    assert _routed(prep)["negbin_pit_comp_2026-10-01"] == odds_ids
    assert prep.odds_info["build_error"] == "RuntimeError: boom"
    with pytest.raises(sc.CandidateFallbackError, match="boom"):
        prep.check_fallback()


def test_one_build_serves_both_candidate_routes(monkeypatch):
    monkeypatch.setenv("SHOTS_ODDS_MODEL", "cand_odds_surge")
    monkeypatch.setenv("SHOTS_NOODDS_MODEL", "fin_c")
    assert sc.candidate_families() == ("adj", "h2h", "ctx", "form")
    monkeypatch.setenv("SHOTS_ODDS_MODEL", "negbin_pit_comp")
    assert sc.candidate_families() == C.PRODUCTION_FAMILIES       # production: the fin_c build, unchanged


def test_missing_inputs_detection():
    f = _frame({"a", "b"})
    f.loc["b", "h2h_total_shrunk"] = np.nan
    assert missing_odds_candidate_inputs(f).tolist() == [False, True]
    assert missing_odds_candidate_inputs(f.drop(columns="home_ctx_gp")).all()


def test_shadow_production_numbers_cover_surge_scored_fixtures_only(odds_switch, monkeypatch):
    from shots.predict import board as bd

    m, fx, dom, odds_ids = _inputs()
    turkey = set(fx.loc[fx["competition"].eq("Turkey Super Lig") & fx["match_id"].isin(odds_ids), "match_id"])
    monkeypatch.setattr(config, "SURGE_EXCLUDED_COMPETITIONS", ("Turkey Super Lig",))
    prep, _ = _prepare(monkeypatch, frame=_frame)
    published = pd.DataFrame({"match_id": prep.upcoming["match_id"]})
    sh = bd.shadow_production(prep, published)
    assert sh["model"] == "negbin_pit_comp_2026-10-01" and sh["n"] == 24
    assert set(sh["p"]) == odds_ids - turkey                      # Surge-scored only: not excluded, not no-odds
    mask = prep.upcoming["match_id"].isin(sh["p"])
    expect = prep.model.prob_at_least(prep.upcoming[mask], 24)
    np.testing.assert_allclose([sh["p"][k] for k in prep.upcoming.loc[mask, "match_id"]], expect, atol=1e-4)
