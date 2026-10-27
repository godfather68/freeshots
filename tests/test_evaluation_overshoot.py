"""Over-shoot study helpers (shots.evaluation.overshoot): strictly-prior windows and the stated over-shoot rate."""

import numpy as np
import pandas as pd

from shots.evaluation import overshoot as o


def _long():
    d = pd.DataFrame({
        "match_id": ["m1", "m2", "m3", "m4", "m5"],
        "date": pd.to_datetime(["2020-01-01", "2020-01-05", "2020-01-05", "2020-01-09", "2020-01-12"]),
        "home_team_id": ["A", "A", "C", "B", "A"], "away_team_id": ["B", "C", "A", "A", "D"],
        "home_v": [1.0, 2.0, 4.0, 8.0, 16.0], "away_v": [1.0, 2.0, 4.0, 8.0, 16.0]})
    return o._team_long(d, {"v": ("home_v", "away_v")})


def test_prior_rolling_is_strictly_before_and_same_day_blind():
    long = _long()
    r = o.prior_rolling(long, "v", 2)
    a = long.assign(r=r.to_numpy())
    a = a[a.team == "A"].set_index("match_id")["r"]
    assert np.isnan(a["m1"])                      # no history
    assert a["m2"] == a["m3"] == 1.0              # same day: neither sees the other
    assert a["m4"] == 3.0                         # m2 and m3 (both 5 Jan), window 2
    assert a["m5"] == 6.0                         # m3 (4) and m4 (8)


def test_stated_overshoot_is_read_off_the_published_curve(tmp_path):
    k = np.arange(81)
    from scipy.stats import nbinom
    F = nbinom.cdf(k[None, :], 8, 8 / (8 + 24.0)).repeat(3, axis=0)
    df = pd.DataFrame(F, columns=[f"cdf_{i}" for i in k])
    med = (F >= 0.5).argmax(axis=1)
    meta = pd.DataFrame({"model": "x", "variant": "odds", "match_id": ["a", "b", "c"],
                         "date": pd.to_datetime(["2020-01-01"] * 3), "season": "2019/2020", "competition": "L",
                         "tier": 1, "actual": [med[0] + 5.0, med[0] + 4.0, med[0] - 5.0], "home_actual": 10.0,
                         "away_actual": 10.0, "median_curve": med, "lb_70": 20, "lb_80": 18})
    pd.concat([meta, df], axis=1).to_parquet(tmp_path / "x__odds__2019-2020__month.parquet")
    r = o.load_route(tmp_path, "x", "odds")
    assert r["over5"].tolist() == [1.0, 0.0, 0.0] and r["under5"].tolist() == [0.0, 0.0, 1.0]
    assert np.allclose(r["st_over5"], 1 - F[0, med[0] + 4])
    assert np.allclose(r["st_under5"], F[0, med[0] - 5])
