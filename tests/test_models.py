import numpy as np
import pandas as pd
import pytest

from shots.models import MODELS
from shots.models.base import CONFIDENCES, bound_column
from shots.models.common import FEATURES, IDENTIFIERS, TARGETS, lb_from_cdf, threshold_for_coverage
from shots.models.negbin import NegBinModel, convolve_rows, nb_pmf

SEASONS = ["2018/2019", "2019/2020", "2020/2021", "2021/2022"]
LB_COLS = [bound_column(q) for q in CONFIDENCES]


def synthetic(n_per_season=1500, seed=0, alpha=0.05) -> pd.DataFrame:
    """Feature-shaped frame whose targets are NB draws driven by the features."""
    rng = np.random.default_rng(seed)
    n = n_per_season * len(SEASONS)
    df = pd.DataFrame(index=pd.RangeIndex(1000, 1000 + n))  # non-default index on purpose
    df["season"] = np.repeat(SEASONS, n_per_season)
    start = {s: pd.Timestamp(f"{s[:4]}-08-01") for s in SEASONS}
    df["date"] = [start[s] + pd.Timedelta(days=int(d)) for s, d in
                  zip(df["season"], rng.integers(0, 280, n))]
    df = df.sort_values("date")
    df["match_id"] = [f"m{i}" for i in range(n)]
    df["competition"] = rng.choice(["L1", "L2", "L3"], n)
    df["home_team"], df["away_team"] = "H", "A"
    df["tier"] = df["competition"].str[1].astype(int)
    lvl = df["competition"].map({"L1": 12.5, "L2": 11.5, "L3": 10.5}).to_numpy()
    df["league_prior_shots"] = lvl + rng.normal(0, 0.2, n)
    df["league_prior_n"] = 100.0
    att_h, att_a = rng.lognormal(0, 0.2, n), rng.lognormal(0, 0.2, n)
    def_h, def_a = rng.lognormal(0, 0.2, n), rng.lognormal(0, 0.2, n)
    for side, att, de in (("home", att_h, def_h), ("away", att_a, def_a)):
        for w in ("l5", "l10", "std", "ewm", "venue_l5"):
            df[f"{side}_shots_for_{w}"] = lvl * att * rng.lognormal(0, 0.1, n)
            df[f"{side}_shots_against_{w}"] = lvl * de * rng.lognormal(0, 0.1, n)
            df[f"{side}_sot_for_{w}"] = df[f"{side}_shots_for_{w}"] / 3
            df[f"{side}_sot_against_{w}"] = df[f"{side}_shots_against_{w}"] / 3
        df[f"{side}_shots_for_shrunk"] = df[f"{side}_shots_for_l10"]
        df[f"{side}_shots_against_shrunk"] = df[f"{side}_shots_against_l10"]
        df[f"{side}_rest_days"] = rng.integers(3, 10, n).astype(float)
        df[f"{side}_n_prior_matches"] = 50.0
        df[f"{side}_n_prior_season"] = 10.0
        df[f"{side}_tier_change"] = 0.0
    df["mkt_p_home"] = rng.uniform(0.2, 0.6, n)
    df["mkt_p_away"] = rng.uniform(0.1, 0.8 - df["mkt_p_home"])
    df["mkt_p_draw"] = 1 - df["mkt_p_home"] - df["mkt_p_away"]
    df["mkt_p_over25"] = rng.uniform(0.3, 0.7, n)
    # sprinkle cold starts / missing market
    cold = rng.random(n) < 0.03
    for c in [c for c in df.columns if c.endswith(("_l5", "_l10", "_std", "_ewm"))]:
        df.loc[cold, c] = np.nan
    df.loc[rng.random(n) < 0.02, ["mkt_p_home", "mkt_p_draw", "mkt_p_away"]] = np.nan

    mu_h = 1.1 * lvl * att_h * def_a
    mu_a = 0.9 * lvl * att_a * def_h
    r = 1 / alpha
    df["home_shots"] = rng.negative_binomial(r, r / (r + mu_h)).astype(float)
    df["away_shots"] = rng.negative_binomial(r, r / (r + mu_a)).astype(float)
    df["total_shots"] = df["home_shots"] + df["away_shots"]
    # League-drift columns (read only by the drift model). Own random stream, added last, so every
    # column above (and the golden hashes of the production model) is unchanged.
    r2 = np.random.default_rng(seed + 10_000)
    df["league_prev_level_shots"] = lvl
    for c, sd in (("league_global_drift28", 0.3), ("league_recent28_drift", 0.2),
                  ("league_level_drift", 0.3), ("league_recent100_drift", 0.5)):
        df[c] = r2.normal(0, sd, n)
    df["days_since_season_start"] = (df["date"] - df["season"].map(start)).dt.days.astype(float)
    # Candidate features (families A / E / G, read only by fin_c). Own random stream, added last: every column
    # above and the golden hashes are unchanged. Ratings carry signal (they are logs of the true strengths).
    r3 = np.random.default_rng(seed + 20_000)
    for side, att, de in (("home", att_h, def_h), ("away", att_a, def_a)):
        df[f"{side}_adj_att"] = np.log(att) + r3.normal(0, 0.05, n)
        df[f"{side}_adj_def"] = np.log(de) + r3.normal(0, 0.05, n)
        df[f"{side}_adj_sot_att"] = df[f"{side}_adj_att"] + r3.normal(0, 0.05, n)
        df[f"{side}_adj_sot_def"] = df[f"{side}_adj_def"] + r3.normal(0, 0.05, n)
        df[f"{side}_adj_att_sd"] = r3.uniform(0.05, 0.2, n)
        df[f"{side}_adj_def_sd"] = r3.uniform(0.05, 0.2, n)
        df[f"{side}_ctx_gap_safety"] = r3.integers(-10, 30, n).astype(float)
        df[f"{side}_ctx_games_left"] = r3.integers(0, 38, n).astype(float)
        df[f"{side}_ctx_dead"] = (r3.random(n) < 0.05).astype(float)
    df["adj_lvl_home"], df["adj_lvl_away"] = 1.05 * lvl, 0.95 * lvl
    df["adj_exp_home_shots"] = df["adj_lvl_home"] * np.exp(df["home_adj_att"] + df["away_adj_def"])
    df["adj_exp_away_shots"] = df["adj_lvl_away"] * np.exp(df["away_adj_att"] + df["home_adj_def"])
    df["adj_exp_total_shots"] = df["adj_exp_home_shots"] + df["adj_exp_away_shots"]
    df["h2h_n"] = r3.integers(0, 6, n).astype(float)
    df["h2h_total_wmean"] = np.where(df["h2h_n"] > 0, df["adj_exp_total_shots"] + r3.normal(0, 3, n), np.nan)
    df["h2h_dev_shrunk"] = np.where(df["h2h_n"] > 0, r3.normal(0, 1, n), 0.0)
    df["h2h_total_shrunk"] = df["adj_exp_total_shots"] + df["h2h_dev_shrunk"]
    df["ctx_progress"] = df["days_since_season_start"].clip(0, 280) / 280
    df["ctx_dead_n"] = df["home_ctx_dead"] + df["away_ctx_dead"]
    # Candidate family H (recent form; read only by the 2026-10-26 over-shoot candidates). Own random stream,
    # added last: every column above and the golden hashes are unchanged.
    r4 = np.random.default_rng(seed + 30_000)
    df = df.copy()  # de-fragment (values unchanged)
    for side in ("home", "away"):
        df[f"{side}_form_res_l3"] = r4.normal(0, 4, n)
        df[f"{side}_form_tot_l10"] = 2 * lvl * r4.lognormal(0, 0.1, n)
        df[f"{side}_form_tot_l3"] = df[f"{side}_form_tot_l10"] * r4.lognormal(0, 0.15, n)
        df[f"{side}_form_goals_l5"] = r4.gamma(6, 0.45, n)
        df[f"{side}_form_promo"] = np.where(r4.random(n) < 0.1, np.nan, r4.choice([-1.0, 0.0, 0.0, 0.0, 1.0], n))
        df.loc[r4.random(n) < 0.02, [f"{side}_form_res_l3", f"{side}_form_tot_l3", f"{side}_form_tot_l10",
                                    f"{side}_form_goals_l5"]] = np.nan
    return df


@pytest.fixture(scope="module")
def data():
    df = synthetic()
    train = df[df["season"] < SEASONS[-1]]
    test = df[df["season"] == SEASONS[-1]]
    return train, test


@pytest.fixture(scope="module")
def fitted(data):
    train, _ = data
    return {name: cls().fit(train) for name, cls in MODELS.items()}


def test_feature_list_excludes_targets_and_ids():
    assert not set(FEATURES) & set(TARGETS + IDENTIFIERS)
    assert "competition" not in FEATURES


@pytest.mark.parametrize("name", list(MODELS))
def test_contract(name, data, fitted):
    _, test = data
    pred = fitted[name].predict(test)
    assert pred.index.equals(test.index)
    assert list(pred.columns) == ["median", *LB_COLS]
    assert pred["median"].dtype.kind == "f"
    for c in LB_COLS:
        assert pred[c].dtype.kind == "i", c
        assert (pred[c] >= 0).all()
    assert (pred["lb_70"] >= pred["lb_80"]).all()
    assert (pred["lb_80"] >= pred["lb_90"]).all()
    assert pred.notna().all().all()
    # shuffled subset: still aligned to the given index
    sub = test.sample(50, random_state=1)
    p2 = fitted[name].predict(sub)
    assert p2.index.equals(sub.index)
    pd.testing.assert_frame_equal(p2, pred.loc[sub.index])


@pytest.mark.parametrize("name", list(MODELS))
def test_predictions_ignore_targets_of_predict_rows(name, data, fitted):
    _, test = data
    tampered = test.copy()
    tampered[TARGETS] = 0.0
    pd.testing.assert_frame_equal(fitted[name].predict(test), fitted[name].predict(tampered))
    dropped = test.drop(columns=TARGETS)
    pd.testing.assert_frame_equal(fitted[name].predict(test), fitted[name].predict(dropped))


@pytest.mark.parametrize("name", list(MODELS))
def test_fit_ignores_rows_without_targets(name, data):
    train, _ = data
    extra = train.tail(30).copy()
    extra.index = extra.index + 10**6
    extra[TARGETS] = np.nan
    a = MODELS[name]().fit(train).predict(train.head(40))
    b = MODELS[name]().fit(pd.concat([train, extra])).predict(train.head(40))
    pd.testing.assert_frame_equal(a, b)


def test_negbin_coverage_on_nb_data(data, fitted):
    _, test = data
    m = fitted["negbin"]
    pred = m.predict(test)
    for q in CONFIDENCES:
        cov = (test["total_shots"] >= pred[bound_column(q)]).mean()
        assert abs(cov - q) < 0.04, (q, cov)
    # independent sides in the generator -> rho close to 0 and alpha recovered
    assert abs(m.rho_) < 0.06
    assert 0.02 < m.sides_["home"]["alpha"] < 0.09


def test_qgbm_conformal_hits_target_on_calibration(fitted):
    m = fitted["qgbm"]
    for q in CONFIDENCES:
        assert m.cal_report_[q]["cal_coverage"] >= q
        assert m.cal_report_[q]["cal_coverage"] < q + 0.05


def test_lb_from_cdf_matches_definition():
    pmf = nb_pmf(np.array([20.0, 5.0]), 0.1, 120)
    cdf = np.cumsum(pmf, axis=1)
    for q in (0.7, 0.9):
        lb = lb_from_cdf(cdf, q)
        for i in range(2):
            surv = 1 - np.concatenate([[0], cdf[i]])  # surv[n] = P(T >= n)
            assert surv[lb[i]] >= q > surv[lb[i] + 1]


def test_convolution_sums_to_one_and_mean_adds():
    ph, pa = nb_pmf(np.array([12.0]), 0.04, 80), nb_pmf(np.array([10.0]), 0.04, 80)
    t = convolve_rows(ph, pa)
    assert t.sum() == pytest.approx(1.0, abs=1e-6)
    assert (t[0] * np.arange(t.shape[1])).sum() == pytest.approx(22.0, abs=1e-3)


def test_threshold_for_coverage():
    s = np.arange(100, dtype=float)
    t = threshold_for_coverage(s, 0.8)
    assert np.mean(s <= t) >= 0.8


def test_negbin_moment_mode_widens_for_positive_rho(data):
    train, test = data
    m = NegBinModel(dependence="moment").fit(train)
    base = m.predict(test)
    m.rho_ = 0.3
    wider = m.predict(test)
    assert (wider["lb_90"] <= base["lb_90"]).all()
    assert (wider["lb_90"] < base["lb_90"]).any()
