"""Candidate feature families: leakage (own / same-day / later results), cold start, sanity of each family."""

import numpy as np
import pandas as pd
import pytest

from shots.features import candidates as C
from shots.features.adj_ratings import adjusted_ratings
from shots.features.team_form import build_features
from shots.ingest.schema import CANONICAL_COLUMNS

TEAMS = list("ABCDEFGH")


def _league(n_days=45, seed=0, comp="L1", season="2020/2021", start="2020-08-01"):
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        t = list(rng.permutation(TEAMS))
        for i in range(0, len(t), 2):
            rows.append((pd.Timestamp(start) + pd.Timedelta(days=3 + 4 * d), t[i], t[i + 1]))
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team"])
    n = len(df)
    df["match_id"] = [f"{comp}{season[:4]}m{i}" for i in range(n)]
    df["competition"], df["country"], df["source"], df["season"], df["tier"] = comp, "X", "t", season, 1
    df["home_team_id"], df["away_team_id"] = "x:" + df.home_team, "x:" + df.away_team
    df["home_shots"] = rng.integers(5, 25, n)
    df["away_shots"] = rng.integers(5, 25, n)
    df["home_sot"], df["away_sot"] = df.home_shots // 3, df.away_shots // 3
    df["home_goals"], df["away_goals"] = rng.integers(0, 4, n), rng.integers(0, 4, n)
    pm = rng.uniform(0.3, 0.5, n)
    ph = pm * 1.0
    df["odds_home"], df["odds_draw"], df["odds_away"] = 1 / (ph * 1.05), 1 / (0.25 * 1.05), 1 / ((0.75 - ph) * 1.05)
    df["odds_over25"], df["odds_under25"] = 1.9, 1.95
    df = df[CANONICAL_COLUMNS]
    ex = pd.DataFrame({"match_id": df.match_id})
    for c in ("home_corners", "away_corners", "home_fouls", "away_fouls", "home_yellow", "away_yellow",
              "home_red", "away_red"):
        ex[c] = rng.integers(0, 15, n).astype(float)
    ex["ah_line"] = rng.choice([-1.0, -0.5, -0.25, 0.0, 0.25, 0.5], n)
    ex["ah_home_odds"], ex["ah_away_odds"] = 1.95, 1.9
    ex["referee"] = rng.choice(["R1", "R2", "R3"], n)
    ex["kickoff_time"] = "15:00"
    return df, ex


@pytest.fixture(scope="module")
def data():
    return _league()


@pytest.fixture(scope="module")
def built(data):
    return C.build_candidates(*data)


def _cols(f):
    return [c for c in f.columns if c != "match_id"]


def test_one_row_per_match_and_no_overlap_with_production(data, built):
    df, _ = data
    assert built["match_id"].is_unique and set(built["match_id"]) == set(df["match_id"])
    prod = build_features(df)
    assert set(_cols(built)).isdisjoint(prod.columns), "candidate columns must not collide with production columns"
    assert C.OUT_PATH.name == "features_candidates.parquet"


def test_production_features_unchanged_by_candidates(data):
    """Building candidates must not alter production features (separate code path, no shared state)."""
    df, ex = data
    before = build_features(df)
    C.build_candidates(df, ex)
    pd.testing.assert_frame_equal(before, build_features(df))


def _perturb(df, ex, cut):
    """Change every result (shots, sot, goals, corners, fouls, cards) of matches dated >= cut."""
    d, e = df.copy(), ex.copy()
    late = (d["date"] >= cut).to_numpy()
    for c in ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"):
        d.loc[late, c] = d.loc[late, c] + 7
    for c in ("home_corners", "away_corners", "home_fouls", "away_fouls", "home_yellow", "away_yellow", "home_red", "away_red"):
        e.loc[late, c] = e.loc[late, c] + 5
    return d, e


@pytest.mark.parametrize("cut_day", [5, 20, 33])
def test_no_leakage_own_same_day_and_later_results(data, built, cut_day):
    df, ex = data
    cut = sorted(df["date"].unique())[cut_day]
    d2, e2 = _perturb(df, ex, cut)
    other = C.build_candidates(d2, e2)
    a = built.set_index("match_id")
    b = other.set_index("match_id").reindex(a.index)
    visible = df.set_index("match_id")["date"] <= cut     # own + same-day + earlier matches
    pd.testing.assert_frame_equal(a[visible.reindex(a.index)], b[visible.reindex(a.index)], check_exact=False, rtol=0, atol=1e-12)
    # and the perturbation does matter later on (the test is not vacuous)
    later = (df.set_index("match_id")["date"] > cut + pd.Timedelta(days=12)).reindex(a.index)
    if later.any():
        assert not np.allclose(a.loc[later, "home_adj_att"].to_numpy(), b.loc[later, "home_adj_att"].to_numpy())


def test_same_day_matches_do_not_see_each_other():
    df, ex = _league(n_days=10)
    day = sorted(df["date"].unique())[6]
    d2, e2 = df.copy(), ex.copy()
    sel = (d2["date"] == day).to_numpy()
    d2.loc[sel, "home_shots"] += 11
    e2.loc[sel, "home_fouls"] += 9
    a = C.build_candidates(df, ex).set_index("match_id")
    b = C.build_candidates(d2, e2).set_index("match_id").reindex(a.index)
    same = (df.set_index("match_id")["date"] == day).reindex(a.index)
    pd.testing.assert_frame_equal(a[same], b[same], check_exact=False, rtol=0, atol=1e-12)


def test_cold_start_is_explicit():
    df, ex = _league(n_days=3)
    f = C.build_candidates(df, ex).set_index("match_id")
    first = f.loc[df.sort_values("date")["match_id"].iloc[0]]
    assert first["home_adj_att"] == 0 and first["home_adj_def"] == 0
    assert first["home_adj_att_sd"] == pytest.approx(np.sqrt(C.RATING_PARAMS["p0"]))
    assert np.isnan(first["home_style_corners_for_l10"])          # no history: NaN, never 0
    assert first["home_sched_n7"] == 0 and np.isnan(first["home_sched_rest_prev"])
    assert first["h2h_n"] == 0 and first["h2h_dev_shrunk"] == 0 and np.isnan(first["h2h_total_wmean"])
    assert first["ref_n"] == 0 and first["ref_shots_dev"] == 0
    assert first["home_ctx_gp"] == 0 and first["home_ctx_pts"] == 0


def test_ratings_are_opponent_adjusted():
    """A creates 20 shots against everyone; B creates 10 against everyone. A's attack > B's, and the model
    attributes shots against weak defences (the team that concedes 20) to the opponent rather than the attacker."""
    rows = []
    rng = np.random.default_rng(1)
    day = pd.Timestamp("2020-08-01")
    others = list("CDEF")
    for k in range(80):
        a, b = rng.choice(others, 2, replace=False)
        for h, aw, hs, as_ in (("A", a, 20, 10), (b, "B", 10, 10), (a, "A", 10, 20), ("B", b, 10, 10)):
            rows.append((day + pd.Timedelta(days=k), h, aw, hs, as_))
    df = pd.DataFrame(rows, columns=["date", "home_team_id", "away_team_id", "home_shots", "away_shots"])
    df["competition"] = "L1"
    r = adjusted_ratings(df, "shots", **C.RATING_PARAMS)
    final = {}
    for side in ("home", "away"):
        for i in range(len(df)):
            final[df[f"{side}_team_id"].iloc[i]] = r[f"{side}_att"].iloc[i]
    assert final["A"] > final["B"] + 0.1
    assert r["exp_total"].iloc[:4].isna().all() and r["exp_total"].iloc[4:].notna().all()   # no league history on day 1


def test_rows_without_the_statistic_get_features_but_never_update():
    df, _ = _league(n_days=15)
    base = adjusted_ratings(df, "shots")
    df2 = df.copy()
    df2.loc[df2.index[-8:], ["home_shots", "away_shots"]] = np.nan   # future fixtures
    r2 = adjusted_ratings(df2, "shots")
    n = len(df) - 8
    pd.testing.assert_frame_equal(base.iloc[:n], r2.iloc[:n])
    assert r2["exp_total"].iloc[-8:].notna().all()


def test_market_inversion_recovers_goal_expectations():
    from scipy.stats import poisson
    lh, la = 1.7, 0.9
    k = np.arange(0, 15)
    ph, pa = poisson.pmf(k, lh), poisson.pmf(k, la)
    grid = np.outer(ph, pa)
    p_home, p_draw, p_away = np.tril(grid, -1).sum(), np.trace(grid), np.triu(grid, 1).sum()
    p_over = 1 - poisson.cdf(2, lh + la)
    m = pd.DataFrame({
        "odds_home": [1 / p_home], "odds_draw": [1 / p_draw], "odds_away": [1 / p_away],
        "odds_over25": [1 / p_over], "odds_under25": [1 / (1 - p_over)],
        "ah_line": [-0.75], "ah_home_odds": [1.9], "ah_away_odds": [1.9],
    })
    f = C.family_market(m)
    assert f["mktx_total_goals"][0] == pytest.approx(lh + la, abs=0.03)
    assert f["mktx_gd_1x2"][0] == pytest.approx(lh - la, abs=0.08)
    assert f["mktx_gd_ah"][0] == pytest.approx(0.75, abs=1e-9)
    assert f["mktx_ah_abs"][0] == 0.75


def test_market_missing_inputs_stay_nan():
    m = pd.DataFrame({"odds_home": [np.nan], "odds_draw": [3.0], "odds_away": [3.0], "odds_over25": [np.nan],
                      "odds_under25": [np.nan], "ah_line": [np.nan], "ah_home_odds": [np.nan], "ah_away_odds": [np.nan]})
    f = C.family_market(m)
    assert all(np.isnan(v[0]) for v in f.values())


def test_schedule_counts_and_same_day_exclusion():
    rows = [("2020-08-01", "A", "B"), ("2020-08-05", "A", "C"), ("2020-08-05", "B", "D"),
            ("2020-08-08", "A", "D"), ("2020-08-20", "A", "B")]
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team"])
    df["date"] = pd.to_datetime(df["date"])
    df["match_id"] = [f"m{i}" for i in range(len(df))]
    df["competition"], df["country"], df["season"], df["tier"] = "L1", "X", "2020/2021", 1
    df["home_team_id"], df["away_team_id"] = "x:" + df.home_team, "x:" + df.away_team
    for c in ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"):
        df[c] = 5
    for c in ("odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"):
        df[c] = np.nan
    f = C.build_candidates(df, None).set_index("match_id")
    assert f.loc["m3", "home_sched_n7"] == 2 and f.loc["m3", "home_sched_n14"] == 2    # A: 08-01 and 08-05, not 08-08 itself
    assert f.loc["m3", "home_sched_rest_prev"] == 4
    assert f.loc["m4", "home_sched_n7"] == 0 and f.loc["m4", "home_sched_n28"] == 3
    assert f.loc["m1", "home_sched_days_to_next"] == 3
    assert np.isnan(f.loc["m4", "home_sched_days_to_next"])
    assert f.loc["m1", "sched_midweek"] == 1.0 and f.loc["m1", "home_sched_midweek_after_weekend"] == 1.0 and f.loc["m3", "home_sched_midweek_after_weekend"] == 0.0


def test_ucl_calendar_distances_only_in_current_season():
    df, ex = _league(n_days=6)
    day = (pd.Timestamp("2020-08-20") - pd.Timestamp("2000-01-01")).days
    f = C.build_candidates(df, ex, ucl={"x:A": np.array([day])}).set_index("match_id")
    ha = df[df.home_team == "A"]
    row = f.loc[ha["match_id"].iloc[0]]
    assert np.isfinite(row["home_sched_ucl_days_to"]) and np.isnan(row["home_sched_ucl_days_since"])
    noucl = f.loc[df[df.home_team == "B"]["match_id"].iloc[0]]
    assert np.isnan(noucl["home_sched_ucl_days_to"])      # unknown stays NaN, never 0


def test_h2h_uses_only_previous_meetings():
    df, ex = _league(n_days=60, seed=3)
    f = C.build_candidates(df, ex).set_index("match_id")
    d = df.copy()
    d["pair"] = [tuple(sorted(p)) for p in zip(d.home_team, d.away_team)]
    d["total"] = d.home_shots + d.away_shots
    pair = d["pair"].iloc[0]
    g = d[d["pair"] == pair].sort_values("date")
    assert len(g) >= 3
    r1 = f.loc[g["match_id"].iloc[1]]
    assert r1["h2h_n"] == 1 and r1["h2h_total_wmean"] == pytest.approx(g["total"].iloc[0])
    r2 = f.loc[g["match_id"].iloc[2]]
    assert r2["h2h_n"] == 2 and g["total"].iloc[:2].min() <= r2["h2h_total_wmean"] <= g["total"].iloc[:2].max()
    assert f.loc[g["match_id"].iloc[0], "h2h_n"] == 0


def test_referee_is_nan_without_referee_and_counts_prior_matches():
    df, ex = _league(n_days=12)
    ex2 = ex.copy()
    ex2["referee"] = None
    f = C.build_candidates(df, ex2)
    assert f["ref_n"].isna().all() and f["ref_shots_dev"].isna().all()
    ex3 = ex.copy()
    ex3["referee"] = "Same"
    f3 = C.build_candidates(df, ex3).sort_values("ref_n")
    d_sorted = df.sort_values("date")
    # one referee for all: ref_n is the number of strictly earlier matches
    counts = d_sorted.groupby("date").size().cumsum().shift(1, fill_value=0)
    expect = d_sorted["date"].map(counts)
    got = f3.set_index("match_id")["ref_n"].reindex(d_sorted["match_id"])
    assert (got.to_numpy() == expect.to_numpy()).all()


def test_context_table_points_and_rank():
    rows = [("2020-08-01", "A", "B", 2, 0), ("2020-08-01", "C", "D", 1, 1), ("2020-08-08", "A", "C", 0, 0),
            ("2020-08-08", "B", "D", 1, 0)]
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team", "home_goals", "away_goals"])
    df["date"] = pd.to_datetime(df["date"])
    df["match_id"] = [f"m{i}" for i in range(len(df))]
    df["competition"], df["country"], df["season"], df["tier"] = "L1", "X", "2020/2021", 1
    df["home_team_id"], df["away_team_id"] = "x:" + df.home_team, "x:" + df.away_team
    for c in ("home_shots", "away_shots", "home_sot", "away_sot"):
        df[c] = 5
    for c in ("odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"):
        df[c] = np.nan
    f = C.build_candidates(df, None, families=("adj", "ctx")).set_index("match_id")
    assert f.loc["m2", "home_ctx_pts"] == 3 and f.loc["m2", "away_ctx_pts"] == 1      # A beat B; C drew
    assert f.loc["m2", "home_ctx_rank_frac"] == 0.0                                      # A leads
    assert f.loc["m2", "home_ctx_gap_title"] == 0 and f.loc["m2", "away_ctx_gap_title"] == 2
    assert f.loc["m3", "home_ctx_pts"] == 0 and f.loc["m3", "away_ctx_pts"] == 1
    assert f.loc["m2", "home_ctx_games_left"] == 5      # 4 teams: 6 games each, 1 played
