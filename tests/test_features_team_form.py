import numpy as np
import pandas as pd
import pytest

from shots.features.team_form import build_features
from shots.ingest.schema import CANONICAL_COLUMNS


def _mk(rows):
    """rows: (date, home, away, hs, as_) -> canonical frame."""
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team", "home_shots", "away_shots"])
    df["date"] = pd.to_datetime(df["date"])
    df["match_id"] = [f"m{i}" for i in range(len(df))]
    df["competition"], df["country"], df["source"] = "L1", "X", "t"
    df["home_team_id"], df["away_team_id"] = "x:" + df.home_team, "x:" + df.away_team
    df["tier"], df["season"] = 1, "2020/2021"
    df["home_sot"], df["away_sot"] = df.home_shots // 2, df.away_shots // 2
    df["home_goals"], df["away_goals"] = 1, 1
    for c in ("odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"):
        df[c] = np.nan
    return df[CANONICAL_COLUMNS]


def _league(n_days=40, seed=0):
    rng = np.random.default_rng(seed)
    teams = list("ABCDEF")
    rows = []
    for d in range(n_days):
        t = list(rng.permutation(teams))
        for i in range(0, 6, 2):
            rows.append((pd.Timestamp("2020-08-01") + pd.Timedelta(days=7 * d), t[i], t[i + 1],
                         int(rng.integers(5, 25)), int(rng.integers(5, 25))))
    return _mk(rows)


FEATURE_COLS = lambda f: [c for c in f.columns if c.startswith(("home_", "away_", "league_", "mkt_"))
                          and c not in ("home_shots", "away_shots", "home_team", "away_team", "home_team_id", "away_team_id")]


def test_one_row_per_match_and_targets():
    m = _league()
    f = build_features(m)
    assert len(f) == len(m) and f.match_id.is_unique
    assert (f.total_shots == m.home_shots + m.away_shots).all()


def test_no_leakage_from_own_and_later_matches():
    m = _league()
    base = build_features(m).set_index("match_id")
    k = 60
    m2 = m.copy()
    idx = m2.index[k:]
    m2.loc[idx, ["home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"]] += 37
    new = build_features(m2).set_index("match_id")
    cols = FEATURE_COLS(base.reset_index())
    pd.testing.assert_frame_equal(base.loc[m.match_id[:k], cols], new.loc[m.match_id[:k], cols])
    # the first modified match sees unchanged features too (own result excluded)
    pd.testing.assert_frame_equal(base.loc[[m.match_id[k]], cols], new.loc[[m.match_id[k]], cols])
    # sanity: later matches do change
    assert not base.loc[m.match_id.iloc[-1], cols].equals(new.loc[m.match_id.iloc[-1], cols])


def test_same_day_matches_do_not_see_each_other():
    rows = [("2020-08-01", "A", "B", 10, 8), ("2020-08-08", "A", "C", 12, 9),
            ("2020-08-08", "D", "A", 20, 30), ("2020-08-15", "A", "B", 11, 7)]
    f = build_features(_mk(rows)).set_index("match_id")
    # both matches on 08-08 see only the 08-01 match of A
    assert f.loc["m1", "home_shots_for_l5"] == 10
    assert f.loc["m2", "away_shots_for_l5"] == 10
    assert f.loc["m1", "home_n_prior_matches"] == 1 and f.loc["m2", "away_n_prior_matches"] == 1
    assert f.loc["m1", "home_rest_days"] == 7 and f.loc["m2", "away_rest_days"] == 7
    # 08-15 sees all three
    assert f.loc["m3", "home_shots_for_l5"] == pytest.approx((10 + 12 + 30) / 3)
    assert f.loc["m3", "home_rest_days"] == 7


def test_rolling_mean_and_rest_days_hand_checked():
    dates = ["2020-08-01", "2020-08-08", "2020-08-15", "2020-08-22", "2020-08-29", "2020-09-05", "2020-09-08"]
    shots = [10, 12, 14, 16, 18, 20, 22]
    opp = ["B", "C", "B", "C", "B", "C", "B"]
    f = build_features(_mk([(d, "A", o, s, 5) for d, o, s in zip(dates, opp, shots)]))
    a = f.set_index("date").home_shots_for_l5
    assert a.iloc[3] == pytest.approx(12.0)             # mean(10, 12, 14)
    assert a.iloc[5] == pytest.approx(14.0)             # mean(10..18)
    assert a.iloc[6] == pytest.approx(16.0)             # mean(12..20), window of 5
    assert f.home_shots_for_l10.iloc[6] == pytest.approx(15.0)  # mean(10..20)
    assert f.home_shots_against_l5.iloc[6] == 5
    assert list(f.home_rest_days.iloc[1:]) == [7, 7, 7, 7, 7, 3]
    assert f.home_shots_for_std.iloc[4] == pytest.approx(13.0)
    assert list(f.home_n_prior_matches) == [0, 1, 2, 3, 4, 5, 6]
    # venue-specific: A is always home, so away-only history is empty
    assert f.home_shots_for_venue_l5.iloc[3] == pytest.approx(12.0)
    assert f.away_shots_for_venue_l5.iloc[0:2].isna().all()  # B's first matches have no away history
    # B played away at m0, m2, m4, m6: venue away history of B
    assert f.away_shots_for_venue_l5.iloc[2] == pytest.approx(5.0)


def test_cold_start():
    f = build_features(_league()).iloc[:3]  # first matchday, nobody has history
    for side in ("home", "away"):
        assert (f[f"{side}_n_prior_matches"] == 0).all()
        for c in ("shots_for_l5", "shots_against_l10", "shots_for_ewm", "shots_for_std", "rest_days",
                  "tier_change", "shots_for_venue_l5"):
            assert f[f"{side}_{c}"].isna().all(), c
    # league prior exists (global fallback is NaN on the very first day, see below)
    f2 = build_features(_league()).iloc[30:]
    assert f2.league_prior_shots.notna().all()
    assert f2.home_shots_for_shrunk.notna().all()


def test_shrunk_equals_prior_at_cold_start_and_blends_later():
    m = _league()
    f = build_features(m)
    # second matchday: everyone has exactly 1 prior match
    row = f.iloc[3]
    n, k = 1, 5
    expected = (n * row.home_shots_for_l10 + k * row.league_prior_shots) / (n + k)
    assert row.home_shots_for_shrunk == pytest.approx(expected)
    # a team with no history gets exactly the league prior
    cold = pd.concat([m, _mk([("2021-06-01", "Z", "A", 9, 9)]).assign(match_id="mz")], ignore_index=True)
    r = build_features(cold).set_index("match_id").loc["mz"]
    assert r.home_n_prior_matches == 0
    assert r.home_shots_for_shrunk == pytest.approx(r.league_prior_shots)


def test_league_prior_is_strictly_before_date():
    rows = [("2020-08-01", f"T{i}", f"U{i}", 10, 10) for i in range(15)]      # 30 obs, mean 10
    rows += [("2020-08-08", "T0", "U0", 40, 40), ("2020-08-08", "T1", "U1", 40, 40)]
    f = build_features(_mk(rows))
    same_day = f.iloc[-2:]
    assert (same_day.league_prior_shots == 10).all()
    assert (same_day.league_prior_n == 30).all()


def test_tier_change_and_cross_competition_history():
    m = _mk([("2020-08-01", "A", "B", 10, 8), ("2020-08-08", "A", "C", 12, 9)])
    m.loc[1, ["tier", "competition"]] = [2, "L2"]
    f = build_features(m)
    assert f.home_tier_change.iloc[1] == 1
    assert f.home_shots_for_l5.iloc[1] == 10   # history crosses competitions


def test_market_features():
    m = _league(2)
    m.loc[0, ["odds_home", "odds_draw", "odds_away"]] = [2.0, 4.0, 4.0]
    m.loc[0, ["odds_over25", "odds_under25"]] = [1.8, 2.2]
    f = build_features(m)
    assert f.loc[0, ["mkt_p_home", "mkt_p_draw", "mkt_p_away"]].sum() == pytest.approx(1.0)
    assert f.loc[0, "mkt_p_home"] == pytest.approx(0.5 / 1.0)  # 0.5+.25+.25 = 1.0 here
    assert f.loc[0, "mkt_p_over25"] == pytest.approx((1 / 1.8) / (1 / 1.8 + 1 / 2.2))
    assert f.loc[1, ["mkt_p_home", "mkt_p_over25"]].isna().all()


def test_missing_shots_not_treated_as_zero():
    rows = [("2020-08-01", "A", "B", 10, 8), ("2020-08-08", "A", "C", np.nan, np.nan),
            ("2020-08-15", "A", "B", 11, 7)]
    f = build_features(_mk(rows))
    assert f.home_shots_for_l5.iloc[2] == 10
    assert np.isnan(f.total_shots.iloc[1])


def _hist_n(f, match_id, side="home"):
    return f.set_index("match_id").loc[match_id, f"{side}_n_prior_matches"]


def test_same_name_clubs_in_different_countries_keep_separate_histories():
    m = _mk([
        ("2020-09-01", "Rovers", "Other1", 10, 10),
        ("2020-09-08", "Rovers", "Other1", 10, 10),
        ("2020-09-15", "Rovers", "Other2", 20, 20),  # the "other" Rovers
        ("2020-09-22", "Rovers", "Other1", 10, 10),
    ])
    m["home_team_id"] = ["a:rovers", "a:rovers", "b:rovers", "a:rovers"]
    m["away_team_id"] = ["a:other1", "a:other1", "b:other2", "a:other1"]
    f = build_features(m)
    assert _hist_n(f, "m2") == 0           # country b's Rovers has no history
    assert _hist_n(f, "m3") == 2           # country a's Rovers only counts its own two matches
    assert f.set_index("match_id").loc["m3", "home_shots_for_l10"] == 10


def test_club_moving_between_divisions_keeps_one_history():
    m = _mk([
        ("2020-09-01", "Town", "Other1", 10, 10),
        ("2020-09-08", "Town", "Other1", 14, 10),
        ("2020-09-15", "Town", "Other2", 12, 10),
    ])
    m["tier"] = [1, 1, 2]
    m["competition"] = ["X Top", "X Top", "X Second"]
    f = build_features(m).set_index("match_id")
    assert f.loc["m2", "home_n_prior_matches"] == 2
    assert f.loc["m2", "home_tier_change"] == 1
    assert f.loc["m2", "home_shots_for_l10"] == 12


# --------------------------------------------------------------------------- league drift features

DRIFT_COLS = ["league_prev_level_shots", "league_prev_is_fallback", "league_level_shrunk", "league_level_drift",
              "league_recent28_n", "league_recent28_shots", "league_recent28_drift", "league_recent100_drift",
              "league_global_n28", "league_global_drift28", "days_since_season_start",
              "league_season_matches_prior"]


def _two_seasons(level1=20, level2=26, seed=1):
    a = _league(30, seed)
    b = _league(30, seed + 1)
    b["date"] = b["date"] + pd.Timedelta(days=365)
    b["season"] = "2021/2022"
    b["match_id"] = "b" + b["match_id"]
    a["home_shots"], a["away_shots"] = level1 // 2 + a["home_shots"] * 0, level1 // 2 + a["away_shots"] * 0
    b["home_shots"], b["away_shots"] = level2 // 2 + b["home_shots"] * 0, level2 // 2 + b["away_shots"] * 0
    return pd.concat([a, b], ignore_index=True)


def test_drift_features_no_leakage_including_same_day():
    m = _two_seasons()
    base = build_features(m).set_index("match_id")
    # modify one match and everything after it, but keep the matches of the same day as the first one
    k = 100
    day = m.loc[k, "date"]
    later = m.index[m["date"] > day]
    m2 = m.copy()
    m2.loc[later, ["home_shots", "away_shots"]] += 37
    new = build_features(m2).set_index("match_id")
    keep = m.loc[m["date"] <= day, "match_id"]  # includes same-day matches: they must not see each other
    pd.testing.assert_frame_equal(base.loc[keep, DRIFT_COLS], new.loc[keep, DRIFT_COLS])
    # changing a match's own result (and its same-day siblings) does not change its own features
    same = m.index[m["date"] == day]
    m3 = m.copy()
    m3.loc[same, ["home_shots", "away_shots"]] += 50
    new3 = build_features(m3).set_index("match_id")
    ids = m.loc[same, "match_id"]
    pd.testing.assert_frame_equal(base.loc[ids, DRIFT_COLS], new3.loc[ids, DRIFT_COLS])


def test_drift_cold_start_and_shift_detection():
    m = _two_seasons(20, 26)
    f = build_features(m).set_index("match_id")
    first = f.loc[m.match_id[0]]
    assert first.league_season_matches_prior == 0 and first.days_since_season_start == 0
    # the very first match ever has no history at all: level is NaN (explicit), count-based drifts are 0
    assert np.isnan(first.league_prev_level_shots) and np.isnan(first.league_level_drift)
    assert first.league_recent28_drift == 0 and first.league_global_drift28 == 0
    assert first.league_prev_is_fallback == 1
    s2 = f[f.index.str.startswith("b")]
    start = s2.iloc[0]  # first match of season 2: prev level known (20), nothing seen yet
    assert start.league_prev_is_fallback == 0 and start.league_prev_level_shots == pytest.approx(10)  # per team-match
    assert start.league_level_drift == 0
    late = s2.iloc[-1]  # level 26 observed for a whole season -> drift moves toward +3 per team-match
    assert 1.0 < late.league_level_drift < 3
    assert 1.5 < late.league_recent100_drift <= 3
    assert 0 < late.league_global_drift28 < 3
    assert late.league_recent28_shots == pytest.approx(13)
