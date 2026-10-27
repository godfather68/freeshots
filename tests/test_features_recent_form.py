"""Candidate family H (recent form, shots.features.recent_form): leakage, upcoming fixtures, windows, promotion."""

import numpy as np
import pandas as pd
import pytest

from shots.features import candidates as C
from shots.features.recent_form import NAMES
from shots.ingest.schema import CANONICAL_COLUMNS

TEAMS = list("ABCDEF")
FAM = ("adj", "form")


def _season(season="2020/2021", start="2020-08-01", n_days=20, seed=0, comp="L1", tier=1, teams=TEAMS):
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        t = list(rng.permutation(teams))
        for i in range(0, len(t), 2):
            rows.append((pd.Timestamp(start) + pd.Timedelta(days=4 * d), t[i], t[i + 1]))
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team"])
    n = len(df)
    df["match_id"] = [f"{comp}{season[:4]}m{i}" for i in range(n)]
    df["competition"], df["country"], df["source"], df["season"], df["tier"] = comp, "X", "t", season, tier
    df["home_team_id"], df["away_team_id"] = "x:" + df.home_team, "x:" + df.away_team
    df["home_shots"], df["away_shots"] = rng.integers(5, 25, n), rng.integers(5, 25, n)
    df["home_sot"], df["away_sot"] = df.home_shots // 3, df.away_shots // 3
    df["home_goals"], df["away_goals"] = rng.integers(0, 4, n), rng.integers(0, 4, n)
    df["odds_home"], df["odds_draw"], df["odds_away"] = 2.2, 3.3, 3.2
    df["odds_over25"], df["odds_under25"] = 1.9, 1.95
    return df[CANONICAL_COLUMNS]


def _form(f):
    return f[["match_id"] + [c for c in f.columns if "_form_" in c]].set_index("match_id").sort_index()


@pytest.fixture(scope="module")
def data():
    return _season()


@pytest.fixture(scope="module")
def built(data):
    return _form(C.build_candidates(data, None, families=FAM))


def test_columns(built):
    assert set(built.columns) == {f"{s}_{n}" for s in ("home", "away") for n in NAMES}


@pytest.mark.parametrize("k", [5, 25, 50])
def test_no_leakage_own_same_day_and_later_results(data, built, k):
    """Changing every result dated on or after a cut date leaves the form features of all matches dated on or
    before the cut unchanged (own result and same-day results included)."""
    cut = data["date"].sort_values().iloc[k]
    d = data.copy()
    late = (d["date"] >= cut).to_numpy()
    rng = np.random.default_rng(99)
    for c in ("home_shots", "away_shots", "home_goals", "away_goals"):
        d.loc[late, c] = rng.integers(0, 40, late.sum())
    after = _form(C.build_candidates(d, None, families=FAM))
    keep = data.loc[data["date"] <= cut, "match_id"]
    pd.testing.assert_frame_equal(built.loc[keep], after.loc[keep])
    # and the change is visible later on (the features do read results)
    later = data.loc[data["date"] > cut, "match_id"]
    assert not built.loc[later].equals(after.loc[later])


def test_window_values_by_hand(data, built):
    d = data.sort_values(["date", "match_id"]).reset_index(drop=True)
    tot = d.home_shots + d.away_shots
    goals = d.home_goals + d.away_goals
    team = "x:A"
    mine = d[(d.home_team_id == team) | (d.away_team_id == team)]
    r = mine.index[6]
    prev = mine.index[mine.index < r]
    side = "home" if d.loc[r, "home_team_id"] == team else "away"
    row = built.loc[d.loc[r, "match_id"]]
    assert row[f"{side}_form_tot_l3"] == pytest.approx(tot[prev[-3:]].mean())
    assert row[f"{side}_form_tot_l10"] == pytest.approx(tot[prev[-10:]].mean())
    assert row[f"{side}_form_goals_l5"] == pytest.approx(goals[prev[-5:]].mean())
    first = mine.index[0]
    s0 = "home" if d.loc[first, "home_team_id"] == team else "away"
    assert np.isnan(built.loc[d.loc[first, "match_id"], f"{s0}_form_tot_l3"])   # cold start: NaN, never 0


def test_res_is_total_minus_rating_expectation(data):
    f = C.build_candidates(data, None, families=FAM)
    d = data.set_index("match_id").loc[f["match_id"]]
    res = (d.home_shots + d.away_shots).to_numpy(float) - f["adj_exp_total_shots"].to_numpy()
    f = f.assign(_res=res, date=d["date"].to_numpy(), h=d["home_team_id"].to_numpy(), a=d["away_team_id"].to_numpy())
    team = "x:B"
    mine = f[(f.h == team) | (f.a == team)].sort_values(["date", "match_id"])
    i = 8
    side = "home" if mine.iloc[i].h == team else "away"
    assert mine.iloc[i][f"{side}_form_res_l3"] == pytest.approx(mine.iloc[i - 3:i]["_res"].mean())


def test_upcoming_fixtures_take_no_slot(data, built):
    """Rows without a result (upcoming fixtures) never enter a window: played rows are unchanged, and every
    fixture of a team sees the same last played matches however far ahead it is."""
    last = data["date"].max()
    fut = pd.DataFrame({"date": [last + pd.Timedelta(days=4), last + pd.Timedelta(days=11)],
                        "home_team": ["A", "C"], "away_team": ["B", "A"]})
    fut["match_id"] = ["f1", "f2"]
    for c in data.columns:
        if c not in fut:
            fut[c] = data[c].iloc[0] if c in ("competition", "country", "source", "season", "tier") else np.nan
    fut["home_team_id"], fut["away_team_id"] = "x:" + fut.home_team, "x:" + fut.away_team
    both = pd.concat([data, fut[data.columns]], ignore_index=True)
    f = _form(C.build_candidates(both, None, families=FAM))
    pd.testing.assert_frame_equal(f.loc[built.index], built)
    a1 = f.loc["f1", [c for c in f.columns if c.startswith("home_")]].to_numpy(float)
    a2 = f.loc["f2", [c for c in f.columns if c.startswith("away_")]].to_numpy(float)
    np.testing.assert_allclose(a1, a2)   # team A: same window for both fixtures
    assert np.isfinite(a1[:4]).all()


def test_promotion_flag():
    s1 = _season("2019/2020", "2019-08-01", n_days=6, comp="L2", tier=2, teams=list("ABCDEF"))
    s1b = _season("2019/2020", "2019-08-01", n_days=6, comp="L1", tier=1, teams=list("GHIJKL"), seed=1)
    # 2020/21: A and B promoted to tier 1, G and H relegated to tier 2; M is new to the data
    s2 = _season("2020/2021", "2020-08-01", n_days=4, comp="L1", tier=1, teams=list("ABIJKM"), seed=2)
    s2b = _season("2020/2021", "2020-08-01", n_days=4, comp="L2", tier=2, teams=list("GHCDEF"), seed=3)
    m = pd.concat([s1, s1b, s2, s2b], ignore_index=True)
    f = C.build_candidates(m, None, families=FAM).set_index("match_id")
    long = pd.concat([m.assign(team=m.home_team, side="home"), m.assign(team=m.away_team, side="away")])
    long = long[long.season == "2020/2021"]
    got = {r.team: f.loc[r.match_id, f"{r.side}_form_promo"] for r in long.itertuples()}
    assert got["A"] == got["B"] == 1 and got["G"] == got["H"] == -1
    assert got["I"] == got["C"] == 0 and np.isnan(got["M"])
    # first season of the data: no previous season, so NaN (unknown), never 0
    assert f.loc[s1["match_id"], ["home_form_promo", "away_form_promo"]].isna().all().all()
