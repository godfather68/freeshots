"""fin_c adoption behind switches (reports/2026-10-16_fin_c_adoption.md): season-format fix of the season context
(Scotland / Greece / Belgium), league size from the calendar, upcoming fixtures out of the trailing league level,
the per-fixture fallback to production, the explain decomposition of fin_c, and the median switch. Both
switches default to OFF; production is covered by the golden tests elsewhere."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from shots import config
from shots.features import candidates as C
from shots.models import MODELS
from shots.models import explain as ex
from shots.models.calibrated import _cdf, curve_median
from shots.models.candidates import FinC, family_inputs, missing_candidate_inputs
from shots.models.common import lb_from_cdf
from shots.predict import scan as sc
from tests.test_cross_border import HintModel, scan_inputs, stub_models  # noqa: F401 (fixture)
from tests.test_models import SEASONS, synthetic


# --------------------------------------------------------------------------- switches

def test_switches_adopted(monkeypatch):
    # Adopted 2026-10-20 with the user's approval (see CLAUDE.md); the env vars still override.
    monkeypatch.delenv("SHOTS_NOODDS_MODEL", raising=False)
    monkeypatch.delenv("SHOTS_MEDIAN_FROM_CURVE", raising=False)
    assert config.NOODDS_MODEL == "fin_c" and config.MEDIAN_FROM_CURVE is True
    assert config.noodds_model() == "fin_c" and config.candidate_noodds()
    assert config.median_from_curve() is True
    monkeypatch.setenv("SHOTS_NOODDS_MODEL", "negbin_pit_comp")
    monkeypatch.setenv("SHOTS_MEDIAN_FROM_CURVE", "0")
    assert config.noodds_model() == "negbin_pit_comp" and config.median_from_curve() is False
    monkeypatch.setenv("SHOTS_NOODDS_MODEL", "nope")
    with pytest.raises(ValueError):
        config.noodds_model()


# --------------------------------------------------------------------------- season context (condition 1)

def _round_robin(teams, rounds, comp, season, start="2020-08-01", goals=True):
    """``rounds`` round robins of ``teams`` (circle method), one matchday per week."""
    t = list(teams)
    n = len(t)
    rows, day = [], 0
    for r in range(rounds):
        for k in range(n - 1):
            pairs = [(t[i], t[n - 1 - i]) for i in range(n // 2)]
            for h, a in pairs:
                h, a = (h, a) if (r + k) % 2 == 0 else (a, h)
                rows.append((pd.Timestamp(start) + pd.Timedelta(days=7 * day), h, a))
            t = [t[0]] + [t[-1]] + t[1:-1]
            day += 1
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team"])
    df["match_id"] = [f"{comp[:3]}{season[:4]}m{i}" for i in range(len(df))]
    df["competition"], df["country"], df["season"], df["tier"] = comp, "X", season, 1
    df["home_team_id"], df["away_team_id"] = "x:" + df.home_team, "x:" + df.away_team
    rng = np.random.default_rng(len(df))
    for c in ("home_shots", "away_shots"):
        df[c] = rng.integers(5, 20, len(df)).astype(float)
    df["home_sot"], df["away_sot"] = df.home_shots // 3, df.away_shots // 3
    g = rng.integers(0, 4, (len(df), 2)).astype(float)
    df["home_goals"], df["away_goals"] = (g[:, 0], g[:, 1]) if goals else (np.nan, np.nan)
    for c in ("odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"):
        df[c] = np.nan
    return df


def _ctx(df, **kw):
    return C.build_candidates(df, None, families=("adj", "ctx"), **kw).set_index("match_id")


def test_scheduled_games_per_format():
    assert C.scheduled_games("England Premier League", "2025/2026", 20) == (38, True)
    assert C.scheduled_games("Scotland Premiership", "2025/2026", 12) == (38, True)
    assert C.scheduled_games("Scotland Championship", "2025/2026", 10) == (36, True)
    assert C.scheduled_games("Scotland League Two", "2025/2026", 10) == (36, True)
    assert C.scheduled_games("Belgium Pro League", "2024/2025", 16) == (30, False)
    assert C.scheduled_games("Greece Super League", "2023/2024", 14) == (26, False)
    assert C.scheduled_games("Greece Super League", "2018/2019", 16) == (30, True)   # before the playoff format


def test_scotland_four_round_robins_have_the_right_season_end():
    teams = list("ABCD")  # 4 rounds of 3 matchdays: 12 games each (a double round robin would say 6)
    df = _round_robin(teams, 4, "Scotland Championship", "2022/2023")
    f = _ctx(df)
    first = df.match_id.iloc[0]
    assert f.loc[first, "home_ctx_games_left"] == 12
    last3 = (f["home_ctx_games_left"] <= 3).groupby(df.set_index("match_id").date).first()
    assert last3.sum() == 3 and last3.iloc[-3:].all()      # only the final three matchdays
    assert f["ctx_progress"].max() < 1.0


def test_scotland_premiership_split_is_38_games():
    teams = [f"T{i}" for i in range(12)]
    df = _round_robin(teams, 2, "Scotland Premiership", "2022/2023")   # only 22 of the 38 games in the data
    f = _ctx(df)
    assert f.loc[df.match_id.iloc[0], "home_ctx_games_left"] == 38
    assert (f["home_ctx_games_left"] >= 38 - 22).all()                # mid-season: never the season end
    assert (f["home_ctx_dead"] == 0).all()


@pytest.mark.parametrize("comp,season", [("Belgium Pro League", "2023/2024"), ("Greece Super League", "2021/2022")])
def test_playoff_formats_have_no_season_end_flag(comp, season):
    df = _round_robin(list("ABCDEF"), 3, comp, season)   # regular season + "playoff" games in the data
    f = _ctx(df)
    assert f[["home_ctx_games_left", "away_ctx_games_left"]].isna().all().all()
    assert (f["ctx_dead_n"] == 0).all()
    assert f["ctx_progress"].max() == 1.0                 # progress over the regular season, capped
    m = synthetic().head(5).assign(home_ctx_games_left=np.nan, away_ctx_games_left=np.nan)
    assert not missing_candidate_inputs(m, FinC.families).any()   # NaN by design is not a missing build


def test_greece_before_the_playoff_format_is_a_double_round_robin():
    df = _round_robin(list("ABCD"), 2, "Greece Super League", "2018/2019")
    assert _ctx(df).loc[df.match_id.iloc[0], "home_ctx_games_left"] == 6


def test_league_size_from_the_calendar_removes_early_season_hindsight():
    """Week 1 with only half of the league in the rows: with the calendar's size the features equal those of
    the full-season build (no team of the future is needed to know the league size)."""
    teams = list("ABCDEF")
    full = _round_robin(teams, 2, "L1", "2020/2021")
    f_full = _ctx(full)
    first = full[full.date == full.date.min()].iloc[:1]           # one match of matchday 1 only: 2 teams seen
    blind = _ctx(first)
    sized = _ctx(first, league_sizes={("L1", "2020/2021"): 6})
    cols = ["home_ctx_games_left", "away_ctx_games_left", "ctx_progress", "home_ctx_rank_frac"]
    mid = first.match_id.iloc[0]
    assert blind.loc[mid, "home_ctx_games_left"] == 2               # 2 (n - 1) with n = 2: the old bug
    pd.testing.assert_series_equal(sized.loc[mid, cols], f_full.loc[mid, cols])
    cal = full.assign(competition="L1")
    assert C.calendar_league_sizes(cal) == {("L1", "2020/2021"): 6}


def test_upcoming_fixtures_never_fill_the_trailing_league_level():
    hist = _round_robin(list("ABCDEF"), 2, "L1", "2020/2021")
    fut = _round_robin(list("ABCDEF"), 1, "L1", "2021/2022", start="2021-08-07")
    fut[["home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"]] = np.nan
    a = _ctx(hist)
    b = _ctx(pd.concat([hist, fut], ignore_index=True))
    pd.testing.assert_frame_equal(a, b.loc[a.index])                 # played rows unchanged
    lv = b.loc[fut.match_id, ["adj_lvl_home", "adj_lvl_away"]]
    assert (lv.nunique() == 1).all()                                  # every upcoming row sees the same played window


# --------------------------------------------------------------------------- fin_c model and explain

@pytest.fixture(scope="module")
def data():
    df = synthetic()
    return df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]


@pytest.fixture(scope="module")
def finc(data):
    return MODELS["fin_c"]().fit(data[0])


def test_fin_c_reads_the_candidate_families(finc):
    assert isinstance(finc, FinC) and finc.nb_.families == ("A", "E", "G") and finc.nb_.shrink_ is not None
    assert set(family_inputs(FinC.families)) <= set(finc.nb_.input_columns())
    assert ex.supports(finc)


def test_fin_c_explanation_adds_up(data, finc):
    rows = data[1].head(200)
    raw = ex.raw_explain(finc, rows)
    fx = raw[[c for c in raw.columns if c.startswith("fx_")]]
    np.testing.assert_allclose(raw["baseline"] + fx.sum(axis=1), raw["expected_mean"], atol=1e-9)
    assert raw["residual"].abs().max() < 1e-6
    ids = {c[3:] for c in fx.columns}
    assert {"home_rating_attack", "away_rating_defence", "head_to_head", "season_context", "shrinkage"} <= ids
    assert ids <= set(ex.FACTOR_ORDER)
    # shrinkage + calibration bridge the raw (unshrunk) model mean and the published mean
    unshrunk = raw["raw_mean"] - raw["fx_shrinkage"]
    np.testing.assert_allclose(raw["baseline"] + fx.drop(columns=["fx_shrinkage", "fx_calibration"]).sum(axis=1),
                               unshrunk, atol=1e-9)
    for e in ex.explain_rows(finc, rows, odds_used=False):
        tenths = round(10 * e["baseline"]["value"]) + sum(round(10 * f["effect"]) for f in e["factors"])
        assert tenths + round(10 * e["residual"]) == round(10 * e["expected_mean"])
        assert all(f["id"] in ex.FACTOR_ORDER and len(f["detail"]) <= 160 for f in e["factors"])


def test_fin_c_reference_matchup_has_no_rating_effects(data, finc):
    rows = data[1].head(20).copy()
    for s in ("home", "away"):
        rows[[f"{s}_adj_att", f"{s}_adj_def", f"{s}_adj_sot_att", f"{s}_adj_sot_def"]] = 0.0
    rows["adj_exp_home_shots"], rows["adj_exp_away_shots"] = rows["adj_lvl_home"], rows["adj_lvl_away"]
    rows["adj_exp_total_shots"] = rows["adj_exp_home_shots"] + rows["adj_exp_away_shots"]
    rows["h2h_total_shrunk"], rows["h2h_dev_shrunk"] = rows["adj_exp_total_shots"], 0.0
    raw = ex.raw_explain(finc, rows)
    for c in ("fx_home_rating_attack", "fx_away_rating_attack", "fx_home_rating_defence", "fx_away_rating_defence",
              "fx_head_to_head"):
        assert raw[c].abs().max() < 1e-9, c


def test_missing_candidate_inputs_are_detected(data):
    rows = data[1].head(10).copy()
    assert not missing_candidate_inputs(rows, FinC.families).any()
    rows.loc[rows.index[3], "home_adj_att"] = np.nan
    assert missing_candidate_inputs(rows, FinC.families).tolist() == [i == 3 for i in range(10)]
    assert missing_candidate_inputs(rows.drop(columns=["ctx_progress"]), FinC.families).all()


# --------------------------------------------------------------------------- median switch (condition 5)

def test_median_switch(data, finc, monkeypatch):
    rows = data[1].head(300)
    prod = MODELS["negbin_pit_comp"]().fit(data[0])
    for m in (prod, finc):
        monkeypatch.setenv("SHOTS_MEDIAN_FROM_CURVE", "0")
        off = m.predict(rows)
        cdf = _cdf(m.nb_, rows)
        np.testing.assert_array_equal(off["median"].to_numpy(), lb_from_cdf(cdf, 0.5).astype(float))
        monkeypatch.setenv("SHOTS_MEDIAN_FROM_CURVE", "1")
        on = m.predict(rows)
        med = curve_median(m.pit_map_, cdf, rows["competition"])
        np.testing.assert_array_equal(on["median"].to_numpy(), med)
        pd.testing.assert_frame_equal(on.drop(columns="median"), off.drop(columns="median"))   # bounds unchanged
        # on the published curve: P(total >= median + 1) <= 0.5 <= P(total >= median)
        assert (m.prob_at_least(rows, med + 1) <= 0.5 + 1e-12).all()
        assert (m.prob_at_least(rows, med) >= 0.5 - 1e-12).all()


# --------------------------------------------------------------------------- routing and fallback (conditions 2, 4)

class _Cand(HintModel):
    families = FinC.families


@pytest.fixture
def cand_switch(stub_models, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SHOTS_NOODDS_MODEL", "fin_c")

    def nomodel(name, played, age):
        return (_Cand(+3.0, team=True), "fin_c-noodds_x") if name == "fin_c" else (HintModel(-2.0, team=True),
                                                                                   "noodds_label")
    monkeypatch.setattr(sc, "latest_no_odds_model", nomodel)
    return stub_models


def _cand_frame(fixtures, missing=()):
    cols = family_inputs(FinC.families) + ["adj_lvl_home", "adj_lvl_away", "h2h_n", "h2h_total_wmean"]
    f = pd.DataFrame(1.0, index=pd.Index(fixtures["match_id"], name="match_id"), columns=cols)
    f.loc[list(missing), "home_adj_att"] = np.nan
    return f


def _prepare(monkeypatch, frame=None, error=None):
    m, fx = scan_inputs(with_xb=True)
    dom = fx[~fx["competition"].map(sc.xb.is_cross_border).astype(bool)]

    def build(matches, fixtures):
        if error:
            raise error
        return frame(dom) if callable(frame) else frame
    monkeypatch.setattr(sc, "candidate_features", build)
    return sc.prepare_upcoming(fx, m, max_model_age_days=10_000), dom


def test_switch_off_keeps_production_routing(stub_models, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SHOTS_NOODDS_MODEL", "negbin_pit_comp")
    prep, _ = _prepare(monkeypatch, frame=lambda d: pytest.fail("no candidate build when the switch is off"))
    assert prep.fallback is None and prep.fallback_info is None and prep.cand_rows is None
    assert prep.nolabel == "noodds_label"


def test_complete_candidates_route_to_fin_c_and_cross_border_stays_production(cand_switch, monkeypatch):
    prep, dom = _prepare(monkeypatch, frame=_cand_frame)
    labels = {lab: int(mask.sum()) for mask, _, _, lab in prep.routes()}
    assert labels.get("fin_c-noodds_x") == len(dom)
    assert all(lab.startswith("noodds_label-") for lab in labels if lab not in ("fin_c-noodds_x",))
    assert prep.fallback_info["n_fallback"] == 0
    prep.check_fallback()


def test_stale_build_falls_back_to_production_instead_of_median_fill(cand_switch, monkeypatch):
    m, fx = scan_inputs(with_xb=False)
    missing_id = fx["match_id"].iloc[1]
    prep, dom = _prepare(monkeypatch, frame=lambda d: _cand_frame(d).drop(index=[missing_id]))  # no row at all
    routed = {lab: set(prep.upcoming.loc[mask, "match_id"]) for mask, _, _, lab in prep.routes()}
    assert routed["noodds_label"] == {missing_id}
    assert missing_id not in routed["fin_c-noodds_x"]
    assert prep.fallback_info["n_fallback"] == 1
    with pytest.raises(sc.CandidateFallbackError):   # 1 of 3 non-cold-start fixtures > 2%
        prep.check_fallback()


def test_failed_build_scores_everything_with_production_and_fails_loudly(cand_switch, monkeypatch):
    prep, dom = _prepare(monkeypatch, error=RuntimeError("boom"))
    assert prep.nolabel == "noodds_label" and prep.fallback_info["build_error"] == "RuntimeError: boom"
    assert {lab for _, _, _, lab in prep.routes() if not lab.startswith("noodds_label-")} <= {"negbin_pit_comp_2026-10-01",
                                                                                           "noodds_label"}
    with pytest.raises(sc.CandidateFallbackError, match="boom"):
        prep.check_fallback()


def test_fallback_threshold(cand_switch, monkeypatch):
    prep, _ = _prepare(monkeypatch, frame=_cand_frame)
    prep.fallback_info.update(n_fallback=1, n_non_cold=100, n_fallback_non_cold=2, share_non_cold=0.02)
    prep.check_fallback()                                     # exactly 2%: allowed
    prep.fallback_info.update(n_fallback_non_cold=3, share_non_cold=0.03)
    with pytest.raises(sc.CandidateFallbackError):
        prep.check_fallback()
