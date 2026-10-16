"""Leakage guards and helpers of the diagnosis candidates (src/shots/evaluation/diagnosis.py)."""

import numpy as np
import pandas as pd
import pytest

from shots.evaluation import diagnosis as D


def _toy(seed=0, n_days=30):
    rng = np.random.default_rng(seed)
    teams = [f"x:t{i}" for i in range(6)]
    rows = []
    for day in range(n_days):
        perm = rng.permutation(teams)
        for j in range(3):
            h, a = perm[2 * j], perm[2 * j + 1]
            rows.append({"match_id": f"m{day}_{j}", "date": pd.Timestamp("2024-08-01") + pd.Timedelta(days=day),
                         "home_team_id": h, "away_team_id": a, "country": "x", "competition": "X League",
                         "home_corners": rng.integers(0, 12), "away_corners": rng.integers(0, 12),
                         "home_goals": rng.integers(0, 4), "away_goals": rng.integers(0, 4),
                         "total_shots": rng.integers(10, 40), "league_prior_shots": 12.0,
                         "home_fouls": rng.integers(5, 20), "away_fouls": rng.integers(5, 20),
                         "referee": ["R1", "R2", "R3"][j]})
    return pd.DataFrame(rows)


def _perturb_from(df, day):
    """Scramble every outcome on and after ``day``."""
    out = df.copy()
    m = out["date"] >= pd.Timestamp("2024-08-01") + pd.Timedelta(days=day)
    rng = np.random.default_rng(99)
    for c in ("home_corners", "away_corners", "home_goals", "away_goals", "total_shots", "home_fouls", "away_fouls"):
        out.loc[m, c] = rng.integers(0, 30, m.sum())
    return out, m


def test_rolling_team_uses_strictly_earlier_dates():
    df = _toy()
    a = D._rolling_team(df, {"corners": ("home_corners", "away_corners")})
    pert, m = _perturb_from(df, 15)
    b = D._rolling_team(pert, {"corners": ("home_corners", "away_corners")})
    day15 = df.loc[df["date"] == pd.Timestamp("2024-08-16"), "match_id"]
    pd.testing.assert_frame_equal(a.loc[day15], b.loc[day15])        # same-day and future results unseen
    first = df.groupby("home_team_id")["date"].transform("min") == df["date"]
    # a team's very first match has no history
    t0 = df.sort_values("date").iloc[0]
    assert np.isnan(a.loc[t0["match_id"], "home_pre_corners_for"])
    # explicit value: EWM of the team's earlier corners
    mid = df.iloc[40]
    team = mid["home_team_id"]
    hist = df[(df["date"] < mid["date"]) & ((df["home_team_id"] == team) | (df["away_team_id"] == team))].sort_values("date")
    vals = np.where(hist["home_team_id"] == team, hist["home_corners"], hist["away_corners"]).astype(float)
    expect = pd.Series(vals).ewm(halflife=5.0, ignore_na=True).mean().iloc[-1]
    assert a.loc[mid["match_id"], "home_pre_corners_for"] == pytest.approx(expect)
    assert first.any()


def test_referee_tendency_and_elo_ignore_same_day_and_future():
    df = _toy()
    pert, _ = _perturb_from(df, 15)
    r1 = D._referee_tendency(df).set_index("match_id")
    r2 = D._referee_tendency(pert).set_index("match_id")
    upto = df.loc[df["date"] <= pd.Timestamp("2024-08-16"), "match_id"]
    pd.testing.assert_frame_equal(r1.loc[upto], r2.loc[upto])
    e1 = D._elo(df).set_index("match_id")
    e2 = D._elo(pert).set_index("match_id")
    pd.testing.assert_frame_equal(e1.loc[upto], e2.loc[upto])
    # two referees sharing a name on one date never see each other
    same = df.copy()
    same["referee"] = "R1"
    r = D._referee_tendency(same).set_index("match_id")
    day0 = df.loc[df["date"] == df["date"].min(), "match_id"]
    assert (r.loc[day0, "ref_n"] == 0).all()


def test_shift_and_spread_cdf():
    from scipy import stats
    F = stats.poisson.cdf(np.arange(61)[None, :], np.array([[20.0], [25.0]]))
    G = D.shift_cdf(F, np.array([3.0, -2.0]))
    assert np.allclose(G[0, 3:], F[0, :-3])
    assert np.allclose(G[1, :-2], F[1, 2:])
    assert np.allclose(D.spread_cdf(F, np.array([20.0, 25.0]), np.ones(2)), F)
    W = D.spread_cdf(F, np.array([20.0, 25.0]), np.array([1.2, 1.2]))
    var = lambda C: (np.diff(np.hstack([np.zeros((2, 1)), C]), axis=1) * (np.arange(61) - [[20.0], [25.0]]) ** 2).sum(1)  # noqa: E731
    assert (var(W) > var(F)).all()
