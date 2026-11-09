"""Research feature families (2026-11-06): leakage (own / same-day / later results), equivalences, closed doors,
game-state factors, and the opt-in model options (production defaults unchanged)."""

import numpy as np
import pandas as pd
import pytest

from shots.features import research as R
from shots.features.adj_ratings import adjusted_ratings

TEAMS = list("ABCDEFGH")


def _league(n_days=60, seed=0, country="England", start="2020-08-01", trail_boost=1.0):
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        t = list(rng.permutation(TEAMS))
        for i in range(0, len(t), 2):
            rows.append((pd.Timestamp(start) + pd.Timedelta(days=3 + 4 * d), t[i], t[i + 1]))
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team"])
    n = len(df)
    df["match_id"] = [f"m{i:05d}" for i in range(n)]
    df["competition"], df["country"], df["season"] = "L1", country, "2020/2021"
    df["home_team_id"], df["away_team_id"] = "x:" + df.home_team, "x:" + df.away_team
    df["home_ht_goals"], df["away_ht_goals"] = rng.integers(0, 3, n).astype(float), rng.integers(0, 3, n).astype(float)
    lam_h = np.where(df.home_ht_goals < df.away_ht_goals, 13 * trail_boost, 13.0)
    lam_a = np.where(df.away_ht_goals < df.home_ht_goals, 11 * trail_boost, 11.0)
    df["home_shots"], df["away_shots"] = rng.poisson(lam_h).astype(float), rng.poisson(lam_a).astype(float)
    df["home_sot"], df["away_sot"] = df.home_shots // 3, df.away_shots // 3
    df["home_goals"] = df.home_ht_goals + rng.integers(0, 2, n)
    df["away_goals"] = df.away_ht_goals + rng.integers(0, 2, n)
    df["home_corners"], df["away_corners"] = rng.poisson(5.5, n).astype(float), rng.poisson(4.5, n).astype(float)
    df["home_reds"], df["away_reds"] = (rng.random(n) < 0.1).astype(float), (rng.random(n) < 0.1).astype(float)
    return df


RESULT_COLS = ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals", "home_ht_goals",
               "away_ht_goals", "home_corners", "away_corners", "home_reds", "away_reds")


def _perturb(df, cut, seed=9):
    d = df.copy()
    late = (d["date"] >= cut).to_numpy()
    rng = np.random.default_rng(seed)
    for c in RESULT_COLS:
        d.loc[late, c] = rng.integers(0, 30, late.sum()).astype(float)
    return d


@pytest.fixture(scope="module")
def data():
    return _league()


@pytest.fixture(scope="module")
def built(data):
    return R.build_research(data)


def test_every_family_built(built, data):
    assert built["match_id"].is_unique and len(built) == len(data)
    for c in ("home_vr_att", "away_vr_def_dev", "vr_exp_total", "cd_closed", "cd_partial", "home_cr_att",
              "cr_exp_total", "home_gs_att", "gs_exp_total", "gs_f_trail"):
        assert c in built, c


@pytest.mark.parametrize("q", [0.3, 0.55, 0.8])
def test_no_leak_cut_date_rebuild(data, built, q):
    """Results changed on and after D (own, same-day and later matches): every feature of a row dated <= D is
    unchanged (pre-match only); later rows do change (the test has power)."""
    days = np.sort(data["date"].unique())
    cut = days[int(q * len(days))]
    other = R.build_research(_perturb(data, cut))
    dates = data.set_index("match_id")["date"]
    a, b = built.set_index("match_id"), other.set_index("match_id")
    keep = dates[dates <= cut].index
    pd.testing.assert_frame_equal(a.loc[keep], b.loc[keep], check_exact=True)
    later = dates[dates > cut].index
    assert not np.allclose(a.loc[later, "vr_exp_total"], b.loc[later, "vr_exp_total"])
    assert not np.allclose(a.loc[later, "gs_exp_total"], b.loc[later, "gs_exp_total"])


def test_truncation_equals_full_build(data, built):
    """Deleting every row after D gives the same features up to D (no look-ahead through future rows)."""
    days = np.sort(data["date"].unique())
    cut = days[len(days) // 2]
    short = R.build_research(data[data["date"] <= cut])
    a = built.set_index("match_id").loc[short["match_id"]]
    pd.testing.assert_frame_equal(a, short.set_index("match_id"), check_exact=True)


def test_upcoming_fixtures_get_features_and_never_update(data, built):
    last = data["date"].max()
    fx = data.tail(4).copy()
    fx["date"] = last + pd.Timedelta(days=7)
    fx["match_id"] = [f"fx{i}" for i in range(4)]
    for c in RESULT_COLS:
        fx[c] = np.nan
    both = R.build_research(pd.concat([data, fx], ignore_index=True)).set_index("match_id")
    pd.testing.assert_frame_equal(both.loc[built["match_id"]], built.set_index("match_id"), check_exact=True)
    assert both.loc[fx["match_id"], ["vr_exp_total", "cr_exp_total", "gs_exp_total"]].notna().all().all()


def test_venue_ratings_reduce_to_single_ekf_without_venue_variance(data):
    m = R._prepare(data, None)
    v = R.venue_ratings(m, "shots", p0v=0.0, qv=0.0)
    a = adjusted_ratings(m, "shots", **R.BASE_PARAMS)
    np.testing.assert_array_equal(v["exp_home"].to_numpy(), a["exp_home"].to_numpy())
    np.testing.assert_array_equal(v["home_att"].to_numpy(), a["home_att"].to_numpy())
    assert (v["home_att_dev"].fillna(0) == 0).all()


def test_closed_door_table_and_flags():
    t = R.load_closed_doors()
    assert {"England", "Germany", "Spain", "Italy", "France", "Netherlands", "Belgium", "Portugal", "Turkey",
            "Greece", "Scotland"} <= set(t["country"])
    assert t["note"].notna().all()
    m = pd.DataFrame({"country": ["England", "England", "Germany", "Spain", "France", "Italy"],
                      "date": pd.to_datetime(["2021-01-10", "2021-09-01", "2020-10-01", "2026-10-01",
                                              "2020-01-15", "2021-10-20"])})
    f = R.closed_door_flags(m)
    assert f["cd_closed"].tolist() == [1, 0, 0, 0, 0, 0]
    assert f["cd_partial"].tolist() == [0, 0, 1, 0, 0, 1]


def test_gamestate_factor_recovers_a_planted_trailing_effect():
    df = _league(n_days=400, seed=3, trail_boost=1.3)
    m = R._prepare(df, None)
    a = adjusted_ratings(m, "shots", **R.BASE_PARAMS)
    f = R.gamestate_factors(m, a["exp_home"].to_numpy(), a["exp_away"].to_numpy())
    assert f["f_trail"][-1] > 1.15 and abs(f["f_lead"][-1] - 1) < 0.08
    assert np.isnan(f["f_trail"][0]) or f["f_trail"][0] == 1.0   # nothing seen before the first date


def test_research_options_are_opt_in_and_production_unchanged():
    from shots.models import MODELS
    from shots.models.candidates import FIN_C_BASE, FlexNB, family_columns

    surge = MODELS["cand_odds_surge"]
    assert "closed_weight" not in surge.base_kw and not set(surge.base_kw.get("families", ())) & set("VKSZW")
    assert not set(FIN_C_BASE["families"]) & set("VKSZW")
    nb = FlexNB()
    assert nb.closed_weight is None and nb.partial_weight is None
    assert "cd_closed" not in nb.input_columns()
    rows = pd.DataFrame({"cd_closed": [1.0, 0.0], "cd_partial": [0.0, 1.0]})
    assert set(family_columns(rows, "home", "Z")) == {"Z_closed", "Z_partial"}


def test_closed_weight_zero_drops_rows():
    from shots.models.candidates import FlexNB

    nb = FlexNB(closed_weight=0.0, partial_weight=0.5)
    tr = pd.DataFrame({"cd_closed": [1.0, 0.0, 0.0, np.nan], "cd_partial": [0.0, 1.0, 0.0, 0.0]})
    out, w = nb._crowd_weights(tr)
    assert len(out) == 3 and w.tolist() == [0.5, 1.0, 1.0]


def test_venue_candidates_registered_but_not_production():
    from shots import config
    from shots.models import MODELS

    assert {"cand_odds_surge_v", "cand_fin_v"} <= set(MODELS)
    assert "V" in MODELS["cand_fin_v"].base_kw["families"] and "V" in MODELS["cand_odds_surge_v"].base_kw["families"]
    used = {getattr(config, k, None) for k in ("ODDS_MODEL", "NOODDS_MODEL")}
    assert not used & {"cand_odds_surge_v", "cand_fin_v"}


def test_research_build_is_off_in_production_and_on_when_opted_in(monkeypatch):
    from shots.predict import scan as sc
    from tests.test_features_candidates import _league as cand_league

    monkeypatch.delenv("SHOTS_ODDS_MODEL", raising=False)
    monkeypatch.delenv("SHOTS_NOODDS_MODEL", raising=False)
    monkeypatch.setattr(sc.config, "ODDS_MODEL", "cand_odds_surge")
    monkeypatch.setattr(sc.config, "NOODDS_MODEL", "fin_c")
    assert sc.research_families() == ()
    df, _ = cand_league(n_days=12)
    played, fx = df.iloc[:-4], df.iloc[-4:].copy()
    for c in ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"):
        fx[c] = np.nan
    assert not any(c.startswith("vr_") for c in sc.candidate_features(played, fx).columns)
    monkeypatch.setenv("SHOTS_ODDS_MODEL", "cand_odds_surge_v")
    assert sc.research_families() == ("V",)
    frame = sc.candidate_features(played, fx)
    assert frame.loc[fx["match_id"], "vr_exp_total"].notna().all()
