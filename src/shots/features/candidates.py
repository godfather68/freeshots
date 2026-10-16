"""Candidate FEATURE families for the modeler (not used by production).

Usage:
    python -m shots.features.candidates     # matches.parquet (+ match_extras.parquet) -> features_candidates.parquet

Output ``data/processed/features_candidates.parquet``: one row per ``match_id`` of ``matches.parquet`` and
only candidate columns (``features.parquet`` is untouched; join on ``match_id``). Every feature of a
match uses matches dated strictly before the match date; all matches of a day are read before any is
written (same-day exclusion). Missing inputs stay NaN (never 0); counts (``*_n``) say why. Cold start:
ratings start at the league average with an explicit variance; counts/shrinkage are explicit.

Side columns carry a ``home_`` / ``away_`` prefix (table lists the stem).

A. Opponent-adjusted ratings (``shots.features.adj_ratings``, EKF on a log-NB model, see that module)
-----------------------------------  ------------------------------------------------------------------
adj_att / adj_def                    log rating: shots created / conceded vs an average opponent (def > 0 =
                                     leaky); relative to the league, opponent- and venue-adjusted, decayed
                                     via process noise q per day.
adj_att_sd / adj_def_sd              posterior sd of the above (uncertainty; large for new teams).
adj_sot_att / adj_sot_def            same for shots on target.
adj_acc_att / adj_acc_def            adj_sot_att - adj_att, adj_sot_def - adj_def (quality vs volume).
adj_exp_home/away/total_shots        model expectation of shots (league level x exp(ratings)).
adj_exp_home/away/total_sot          same for shots on target.
adj_lvl_home / adj_lvl_away          league home / away level used (trailing 150 matches, shrunk).
B. Style / volume proxies (per side; l10 = mean of last 10 prior matches, ewm = halflife 5 matches)
-----------------------------------  ------------------------------------------------------------------
style_{corners,fouls,cards}_{for,against}_{l10,ewm}
                                     corners / fouls / card points (yellow + 2 red) by the team ("for": fouls
                                     committed) and by opponents against it ("against": fouls suffered).
style_sotratio_{for,against}_{l10,ewm}
                                     sum(sot) / sum(shots) of the team / of its opponents (l10: ratio of
                                     sums over the window; ewm: ratio of ewms). Shot quality vs volume.
style_shots_{for,against}_sd10       std (>= 3 obs) of the team's shots for / against over the last 10.
style_total_sd10                     std of the total shots of the team's last 10 matches (match pace
                                     volatility).
C. Market structure (match level, pre-match prices only; never closing)
-----------------------------------  ------------------------------------------------------------------
mktx_ah_line / mktx_ah_abs           Asian handicap of the home team (negative = home favourite) and |line|.
mktx_p_ah_home                       de-vigged probability of the home AH side (1/odds normalised).
mktx_total_goals                     Poisson mean mu with P(N >= 3) = mkt_p_over25 (market expected goals).
mktx_gd_1x2                          expected goal difference: Skellam inversion of (p_home - p_away) given
                                     mktx_total_goals.
mktx_gd_ah                           -ah_line + (p_ah_home - 0.5) * sqrt(total) / 0.399 (price-tilted line;
                                     normal approximation of the GD density at the line).
mktx_lam_home / mktx_lam_away        (total +/- gd_1x2) / 2 (market expected goals per side).
mktx_abs_gd                          |gd_1x2| (mismatch / game-script proxy).
D. Schedule / congestion (per side unless noted)
-----------------------------------  ------------------------------------------------------------------
sched_n7 / n14 / n28                 prior matches of the team in the 7 / 14 / 28 days before the date.
sched_rest_prev                      rest days before the team's previous match (fatigue lag).
sched_short_rest_share_l10           share of the last 10 rest gaps that were <= 4 days.
sched_sunmon_share_l8                share of the last 8 prior matches played on Sunday / Monday
                                     (proxy for TV moves / European weeks).
sched_midweek_after_weekend          1 if the match is Tue-Thu, rest <= 4 days and previous match was Fri-Sun.
sched_days_to_next                   SCHEDULE information, not a result: days until the team's next fixture
                                     in the data (known when the calendar is published). NaN at season end.
sched_ucl_days_since / _to           days since / until the club's UEFA Champions League match, current season
                                     only (calendars/*.ics); NaN where the calendar has no UCL data
                                     (NOT 0). Fixture dates are known in advance.
sched_dow / sched_midweek            (match level) weekday 0=Mon and Tue-Thu flag.
sched_rest_diff                      (match level) home rest - away rest.
E. Head-to-head (match level; unordered pair, venue ignored)
-----------------------------------  ------------------------------------------------------------------
h2h_n / h2h_wsum                     prior meetings and their decay-weighted count (weight exp(-days / 1095)).
h2h_total_wmean                      weighted mean total shots of the prior meetings (NaN if none).
h2h_dev_shrunk                       sum(w * (total - adj_exp_total at that time)) / (sum w + 2): how much the pair
                                     over/under-shoots what ratings predicted; 0 if none.
h2h_total_shrunk                     (sum(w * total) + 2 * adj_exp_total_now) / (sum w + 2).
F. Referee (where the CSV has one; match level; key = country + name)
-----------------------------------  ------------------------------------------------------------------
ref_n                                prior matches of the referee in the data.
ref_shots_dev                        sum(total shots - adj_exp_total) / (n + 15) over his prior matches.
ref_fouls_dev / ref_cards_dev        same for total fouls / card points relative to the competition's trailing
                                     mean (last 300 matches).
G. Season context (per side unless noted; table built from results so far in the competition-season)
-----------------------------------  ------------------------------------------------------------------
ctx_gp / ctx_pts / ctx_rank_frac     games played, points, (rank - 1) / (n - 1) (points then goal difference).
ctx_gap_title / ctx_gap_safety       leader points - own; own points - points of the best team in the bottom
                                     round(15% n) places (<= 0 means in the drop zone).
ctx_games_left                       2 (n - 1) - gp, n = teams seen in the season (fixture list size).
ctx_dead                             1 if <= 10 games left, title and drop zone both out of mathematical reach
                                     (gap > 3 * games_left). Approximation: divisions with splits / playoffs and
                                     Europe spots are not modelled. Match level: ctx_progress, ctx_dead_n.
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import skellam

from shots.features.adj_ratings import adjusted_ratings, trailing_sums
from shots.features.team_form import _day_first, _stat

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
IN_PATH = ROOT / "data" / "processed" / "matches.parquet"
EXTRAS_PATH = ROOT / "data" / "processed" / "match_extras.parquet"
OUT_PATH = ROOT / "data" / "processed" / "features_candidates.parquet"

# tuned on NB log-lik of shots, matches dated 2012-08..2023-06 only (flat optimum, see report)
RATING_PARAMS = dict(q=3e-5, p0=0.03, omega=0.7, move_var=0.02)
H2H_TAU = 1095.0
H2H_K = 2.0
REF_K = 15.0
REF_LEVEL_WINDOW = 300

BASE_COLS = ["match_id", "competition", "country", "season", "date", "home_team_id", "away_team_id",
             "home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals",
             "odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"]
EXTRA_NUM = ["home_corners", "away_corners", "home_fouls", "away_fouls", "home_yellow", "away_yellow",
             "home_red", "away_red", "ah_line", "ah_home_odds", "ah_away_odds"]


# --------------------------------------------------------------------------- preparation

def _prepare(matches: pd.DataFrame, extras: pd.DataFrame | None) -> pd.DataFrame:
    m = matches.reset_index(drop=True).copy()
    if "country" not in m:
        m["country"] = m["competition"]
    for c in ("home_goals", "away_goals"):
        if c not in m:
            m[c] = np.nan
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    num = ["home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"]
    for c in num:
        m[c] = m[c].astype("float64")
    if extras is not None:
        ex = extras.drop_duplicates("match_id").set_index("match_id")
        m = m.join(ex.reindex(columns=["referee", *EXTRA_NUM]), on="match_id")
    for c in ["referee", *EXTRA_NUM]:
        if c not in m:
            m[c] = np.nan
    for c in EXTRA_NUM:
        m[c] = m[c].astype("float64")
    m["referee"] = m["referee"].astype("object")
    # stable processing order: date then match_id (features never depend on ties thanks to same-day exclusion)
    m = m.sort_values(["date", "match_id"], kind="mergesort").reset_index(drop=True)
    m["_day"] = ((m["date"] - pd.Timestamp("2000-01-01")) // pd.Timedelta(days=1)).astype("int64")
    return m


def _team_long(m: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for side, opp in (("home", "away"), ("away", "home")):
        d = {
            "row": np.arange(len(m)), "match_id": m["match_id"], "side": side, "is_home": side == "home",
            "team": m[f"{side}_team_id"], "date": m["date"], "day": m["_day"], "season": m["season"],
            "shots_for": m[f"{side}_shots"], "shots_against": m[f"{opp}_shots"],
            "sot_for": m[f"{side}_sot"], "sot_against": m[f"{opp}_sot"],
            "corners_for": m[f"{side}_corners"], "corners_against": m[f"{opp}_corners"],
            "fouls_for": m[f"{side}_fouls"], "fouls_against": m[f"{opp}_fouls"],
            "cards_for": m[f"{side}_yellow"] + 2 * m[f"{side}_red"],
            "cards_against": m[f"{opp}_yellow"] + 2 * m[f"{opp}_red"],
        }
        parts.append(pd.DataFrame(d))
    long = pd.concat(parts, ignore_index=True)
    long["shots_total"] = long["shots_for"] + long["shots_against"]
    # ratio inputs only where both counts exist
    for k in ("for", "against"):
        ok = long[f"shots_{k}"].notna() & long[f"sot_{k}"].notna()
        long[f"shots_ok_{k}"] = long[f"shots_{k}"].where(ok)
        long[f"sot_ok_{k}"] = long[f"sot_{k}"].where(ok)
    long = long.sort_values(["team", "date", "match_id"], kind="mergesort").reset_index(drop=True)
    return long


def _roll(long: pd.DataFrame, col: str, w: int, how: str, min_periods: int = 1) -> pd.Series:
    shifted = long.groupby("team", sort=False)[col].shift(1)
    r = shifted.groupby(long["team"], sort=False).rolling(w, min_periods=min_periods)
    out = getattr(r, how)()
    return out.droplevel(0).reindex(long.index)


def _to_side(long: pd.DataFrame, feats: dict[str, pd.Series], n: int) -> dict[str, np.ndarray]:
    """Scatter per-(team,row) values back to match rows as home_/away_ columns."""
    out: dict[str, np.ndarray] = {}
    rows = long["row"].to_numpy()
    home = long["is_home"].to_numpy()
    for name, s in feats.items():
        v = s.to_numpy(dtype=float)
        for side, mask in (("home", home), ("away", ~home)):
            a = np.full(n, np.nan)
            a[rows[mask]] = v[mask]
            out[f"{side}_{name}"] = a
    return out


# --------------------------------------------------------------------------- A. ratings

def family_ratings(m: pd.DataFrame, params: dict | None = None) -> dict[str, np.ndarray]:
    p = {**RATING_PARAMS, **(params or {})}
    out: dict[str, np.ndarray] = {}
    r = adjusted_ratings(m, "shots", **p)
    s = adjusted_ratings(m, "sot", **p)
    for side in ("home", "away"):
        out[f"{side}_adj_att"], out[f"{side}_adj_def"] = r[f"{side}_att"].to_numpy(), r[f"{side}_def"].to_numpy()
        out[f"{side}_adj_att_sd"], out[f"{side}_adj_def_sd"] = r[f"{side}_att_sd"].to_numpy(), r[f"{side}_def_sd"].to_numpy()
        out[f"{side}_adj_sot_att"], out[f"{side}_adj_sot_def"] = s[f"{side}_att"].to_numpy(), s[f"{side}_def"].to_numpy()
        out[f"{side}_adj_acc_att"] = out[f"{side}_adj_sot_att"] - out[f"{side}_adj_att"]
        out[f"{side}_adj_acc_def"] = out[f"{side}_adj_sot_def"] - out[f"{side}_adj_def"]
        out[f"adj_exp_{side}_shots"] = r[f"exp_{side}"].to_numpy()
        out[f"adj_exp_{side}_sot"] = s[f"exp_{side}"].to_numpy()
        out[f"adj_lvl_{side}"] = r[f"lvl_{side}"].to_numpy()
    out["adj_exp_total_shots"] = r["exp_total"].to_numpy()
    out["adj_exp_total_sot"] = s["exp_total"].to_numpy()
    return out


# --------------------------------------------------------------------------- B. style

def family_style(long: pd.DataFrame, n: int) -> dict[str, np.ndarray]:
    feats: dict[str, pd.Series] = {}
    team = ["team"]
    for met in ("corners", "fouls", "cards"):
        for d in ("for", "against"):
            col = f"{met}_{d}"
            feats[f"style_{col}_l10"] = _day_first(long, _stat(long, team, col, "roll", 10), team)
            feats[f"style_{col}_ewm"] = _day_first(long, _stat(long, team, col, "ewm"), team)
    for d in ("for", "against"):
        s10 = _roll(long, f"sot_ok_{d}", 10, "sum")
        h10 = _roll(long, f"shots_ok_{d}", 10, "sum")
        feats[f"style_sotratio_{d}_l10"] = _day_first(long, s10 / h10.where(h10 > 0), team)
        se = _stat(long, team, f"sot_ok_{d}", "ewm")
        he = _stat(long, team, f"shots_ok_{d}", "ewm")
        feats[f"style_sotratio_{d}_ewm"] = _day_first(long, se / he.where(he > 0), team)
        feats[f"style_shots_{d}_sd10"] = _day_first(long, _roll(long, f"shots_{d}", 10, "std", 3), team)
    feats["style_total_sd10"] = _day_first(long, _roll(long, "shots_total", 10, "std", 3), team)
    return _to_side(long, feats, n)


# --------------------------------------------------------------------------- C. market

_T_GRID = np.arange(0.6, 6.01, 0.1)
_GD_GRID = np.arange(-3.5, 3.501, 0.05)


def _norm(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ok = (a > 1.0) & (b > 1.0)
    ia, ib = np.where(ok, 1.0 / np.where(ok, a, 1.0), np.nan), np.where(ok, 1.0 / np.where(ok, b, 1.0), np.nan)
    return ia / (ia + ib)


def _skellam_diff(total: float, gd: np.ndarray) -> np.ndarray:
    lh = np.maximum((total + gd) / 2, 0.02)
    la = np.maximum((total - gd) / 2, 0.02)
    return skellam.sf(0, lh, la) - skellam.cdf(-1, lh, la)


_DIFF_TABLE: np.ndarray | None = None


def _diff_table() -> np.ndarray:
    global _DIFF_TABLE
    if _DIFF_TABLE is None:
        _DIFF_TABLE = np.vstack([_skellam_diff(t, _GD_GRID) for t in _T_GRID])
    return _DIFF_TABLE


def family_market(m: pd.DataFrame) -> dict[str, np.ndarray]:
    n = len(m)
    oh, od, oa = (m[c].to_numpy(float) for c in ("odds_home", "odds_draw", "odds_away"))
    ok3 = (oh > 1) & (od > 1) & (oa > 1)
    inv = np.column_stack([np.where(ok3, 1 / np.where(ok3, x, 1), np.nan) for x in (oh, od, oa)])
    p3 = inv / inv.sum(axis=1, keepdims=True)
    p_over = _norm(m["odds_over25"].to_numpy(float), m["odds_under25"].to_numpy(float))
    # total goals: P(N >= 3) = 1 - e^-mu (1 + mu + mu^2 / 2) is increasing in mu
    mu_grid = np.arange(0.3, 8.0, 0.01)
    p_grid = 1 - np.exp(-mu_grid) * (1 + mu_grid + mu_grid ** 2 / 2)
    total = np.interp(p_over, p_grid, mu_grid)
    total[~np.isfinite(p_over)] = np.nan
    # expected goal difference from 1X2 (Skellam inversion on a (total, gd) grid, linear between total rows)
    tab = _diff_table()
    obs = p3[:, 0] - p3[:, 2]
    gd = np.full(n, np.nan)
    ok = np.isfinite(total) & np.isfinite(obs)
    pos = np.clip((total - _T_GRID[0]) / 0.1, 0, len(_T_GRID) - 1.000001)
    lo = np.floor(np.nan_to_num(pos)).astype(int)
    w = pos - lo
    for i in np.unique(lo[ok]):
        sel = ok & (lo == i)
        g0 = np.interp(obs[sel], tab[i], _GD_GRID)
        g1 = np.interp(obs[sel], tab[i + 1], _GD_GRID)
        gd[sel] = (1 - w[sel]) * g0 + w[sel] * g1
    line = m["ah_line"].to_numpy(float)
    p_ah = _norm(m["ah_home_odds"].to_numpy(float), m["ah_away_odds"].to_numpy(float))
    gd_ah = -line + (p_ah - 0.5) * np.sqrt(total) / 0.399
    return {
        "mktx_ah_line": line, "mktx_ah_abs": np.abs(line), "mktx_p_ah_home": p_ah,
        "mktx_total_goals": total, "mktx_gd_1x2": gd, "mktx_gd_ah": gd_ah,
        "mktx_lam_home": (total + gd) / 2, "mktx_lam_away": (total - gd) / 2, "mktx_abs_gd": np.abs(gd),
    }


# --------------------------------------------------------------------------- D. schedule

def _ucl_dates() -> dict[str, np.ndarray]:
    """team id -> sorted day numbers of UEFA Champions League matches in the calendars (current season)."""
    try:
        from shots.ingest import calendar_ics as cal
        raw = cal.read_calendars()
        if raw.empty:
            return {}
        fx = cal.to_canonical(raw)
    except Exception as exc:  # pragma: no cover - calendars are optional
        log.warning("UCL calendar unavailable: %s", exc)
        return {}
    fx = fx[fx["competition"] == "UEFA Champions League"]
    days = ((pd.to_datetime(fx["date"]) - pd.Timestamp("2000-01-01")) // pd.Timedelta(days=1)).to_numpy()
    out: dict[str, list] = {}
    for col in ("home_team_id", "away_team_id"):
        for t, d in zip(fx[col], days):
            out.setdefault(t, []).append(int(d))
    return {t: np.unique(v) for t, v in out.items()}


def family_schedule(m: pd.DataFrame, long: pd.DataFrame, n: int, ucl: dict[str, np.ndarray] | None) -> dict[str, np.ndarray]:
    tcode = pd.factorize(long["team"])[0].astype("int64")
    day = long["day"].to_numpy("int64")
    key = tcode * 100000 + day                    # long is sorted by (team, date)
    # tcode from factorize follows first appearance = sorted team order, so key is sorted
    assert np.all(np.diff(key) >= 0)
    feats: dict[str, pd.Series] = {}
    for w in (7, 14, 28):
        c = np.searchsorted(key, key, side="left") - np.searchsorted(key, key - w, side="left")
        feats[f"sched_n{w}"] = pd.Series(c.astype(float))
    nxt_idx = np.searchsorted(key, key, side="right")
    has = nxt_idx < len(key)
    nxt_team = np.where(has, tcode[np.minimum(nxt_idx, len(key) - 1)], -1)
    gap_next = np.where(has & (nxt_team == tcode), day[np.minimum(nxt_idx, len(key) - 1)] - day, np.nan)
    feats["sched_days_to_next"] = pd.Series(gap_next)
    prev_day = long.groupby("team", sort=False)["day"].shift(1)
    rest = (long["day"] - prev_day).astype(float)
    dow = ((long["day"] + 5) % 7)  # 2000-01-01 was a Saturday (Mon = 0)
    long["_dow"] = dow
    long["_rest"] = rest
    long["_short"] = (rest <= 4).where(rest.notna()).astype(float)
    long["_sunmon"] = dow.isin([6, 0]).astype(float)
    team = ["team"]
    g = long.groupby("team", sort=False)
    feats["sched_rest_prev"] = _day_first(long, g["_rest"].shift(1), team)
    feats["sched_short_rest_share_l10"] = _day_first(long, _roll(long, "_short", 10, "mean"), team)
    feats["sched_sunmon_share_l8"] = _day_first(long, _roll(long, "_sunmon", 8, "mean"), team)
    prev_dow = g["_dow"].shift(1)
    mid = dow.isin([1, 2, 3])
    feats["sched_midweek_after_weekend"] = (mid & (rest <= 4) & prev_dow.isin([4, 5, 6])).astype(float).where(rest.notna())
    # UCL (current season only)
    if ucl:
        since = np.full(len(long), np.nan)
        until = np.full(len(long), np.nan)
        teams = long["team"].to_numpy()
        for t, arr in ucl.items():
            idx = np.flatnonzero(teams == t)
            if len(idx) == 0:
                continue
            d = day[idx]
            e = np.searchsorted(arr, d, side="left")      # UCL days strictly before
            since[idx] = np.where(e > 0, d - arr[np.maximum(e - 1, 0)], np.nan)
            f = np.searchsorted(arr, d, side="right")     # UCL days strictly after
            until[idx] = np.where(f < len(arr), arr[np.minimum(f, len(arr) - 1)] - d, np.nan)
        # only seasons covered by the calendar carry the feature (others stay NaN = unknown)
        covered = long["season"].to_numpy() == m["season"].max()
        feats["sched_ucl_days_since"] = pd.Series(np.where(covered, since, np.nan))
        feats["sched_ucl_days_to"] = pd.Series(np.where(covered, until, np.nan))
    out = _to_side(long, feats, n)
    d_all = m["_day"].to_numpy()
    out["sched_dow"] = ((d_all + 5) % 7).astype(float)
    out["sched_midweek"] = np.isin(out["sched_dow"], [1, 2, 3]).astype(float)
    out["sched_rest_diff"] = _rest_of(long, n, "home") - _rest_of(long, n, "away")
    if not ucl:
        for side in ("home", "away"):
            out[f"{side}_sched_ucl_days_since"] = np.full(n, np.nan)
            out[f"{side}_sched_ucl_days_to"] = np.full(n, np.nan)
    return out


def _rest_of(long: pd.DataFrame, n: int, side: str) -> np.ndarray:
    first = ~long.duplicated(["team", "date"])
    r = long["_rest"].where(first).groupby([long["team"], long["date"]], sort=False).transform("max")
    sel = (long["side"] == side).to_numpy()
    a = np.full(n, np.nan)
    a[long["row"].to_numpy()[sel]] = r.to_numpy()[sel]
    return a


# --------------------------------------------------------------------------- E. head-to-head

def family_h2h(m: pd.DataFrame, exp_total: np.ndarray) -> dict[str, np.ndarray]:
    n = len(m)
    h, a = m["home_team_id"].to_numpy(), m["away_team_id"].to_numpy()
    pair = np.where(h < a, h + "|" + a, a + "|" + h)
    pcode = pd.factorize(pair)[0]
    day = m["_day"].to_numpy()
    tot = (m["home_shots"] + m["away_shots"]).to_numpy(float)
    dev = tot - exp_total
    order = np.lexsort((np.arange(n), day, pcode))
    out = {k: np.full(n, np.nan) for k in ("h2h_n", "h2h_wsum", "h2h_total_wmean", "h2h_dev_shrunk", "h2h_total_shrunk")}
    cur_p, last_day = -1, 0
    sw = swt = swd = 0.0
    cnt = 0
    pend: list[int] = []    # rows of the current (pair, day) group, written after all are read

    def flush():
        nonlocal sw, swt, swd, cnt
        for k in pend:
            if tot[k] == tot[k]:
                sw += 1.0
                swt += tot[k]
                swd += dev[k] if dev[k] == dev[k] else 0.0
                cnt += 1
        pend.clear()

    for k in order:
        p, d = pcode[k], day[k]
        if p != cur_p:
            flush()
            cur_p, sw, swt, swd, cnt, last_day = p, 0.0, 0.0, 0.0, 0, d
        elif d != last_day:
            flush()
            decay = math.exp(-(d - last_day) / H2H_TAU)
            sw, swt, swd, last_day = sw * decay, swt * decay, swd * decay, d
        out["h2h_n"][k] = cnt
        out["h2h_wsum"][k] = sw
        out["h2h_total_wmean"][k] = swt / sw if sw > 0 else np.nan
        out["h2h_dev_shrunk"][k] = swd / (sw + H2H_K)
        out["h2h_total_shrunk"][k] = (swt + H2H_K * exp_total[k]) / (sw + H2H_K)
        pend.append(k)
    return out


# --------------------------------------------------------------------------- F. referee

def _prior_by_key(key: np.ndarray, day: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Per row: sum of ``values`` over rows with the same key dated strictly before (n x k)."""
    n = len(key)
    df = pd.DataFrame(values, columns=[f"v{i}" for i in range(values.shape[1])])
    df["key"], df["day"], df["row"] = key, day, np.arange(n)
    df = df.sort_values(["key", "day", "row"], kind="mergesort")
    cols = [c for c in df.columns if c.startswith("v")]
    cs = df.groupby("key", sort=False)[cols].cumsum() - df[cols]
    cs["key"], cs["day"] = df["key"], df["day"]
    first = cs.groupby(["key", "day"], sort=False)[cols].transform("first")
    out = np.zeros_like(values, dtype=float)
    out[df["row"].to_numpy()] = first.to_numpy()
    return out


def family_referee(m: pd.DataFrame, exp_total: np.ndarray) -> dict[str, np.ndarray]:
    n = len(m)
    tot = (m["home_shots"] + m["away_shots"]).to_numpy(float)
    fouls = (m["home_fouls"] + m["away_fouls"]).to_numpy(float)
    cards = (m["home_yellow"] + m["away_yellow"] + 2 * (m["home_red"] + m["away_red"])).to_numpy(float)
    comp = m["competition"].to_numpy()
    date = m["date"].to_numpy()
    lv = trailing_sums(comp, date, np.column_stack([np.nan_to_num(fouls), np.nan_to_num(cards),
                                                     np.isfinite(fouls).astype(float), np.isfinite(cards).astype(float)]),
                       REF_LEVEL_WINDOW)
    mean_f = lv[:, 0] / np.where(lv[:, 2] > 0, lv[:, 2], np.nan)
    mean_c = lv[:, 1] / np.where(lv[:, 3] > 0, lv[:, 3], np.nan)
    dev = np.column_stack([tot - exp_total, fouls - mean_f, cards - mean_c])
    ok = np.isfinite(dev)
    has_ref = m["referee"].notna().to_numpy()
    key = pd.factorize((m["country"].astype(str) + "|" + m["referee"].astype(str)).where(has_ref))[0]
    vals = np.column_stack([np.where(ok, dev, 0.0), ok.astype(float), has_ref.astype(float)])
    pr = _prior_by_key(np.where(has_ref, key, -1), m["_day"].to_numpy(), vals)
    res = {"ref_n": np.where(has_ref, pr[:, 6], np.nan)}
    for i, name in enumerate(("ref_shots_dev", "ref_fouls_dev", "ref_cards_dev")):
        v = pr[:, i] / (pr[:, 3 + i] + REF_K)
        res[name] = np.where(has_ref, v, np.nan)
    return res


# --------------------------------------------------------------------------- G. season context

def family_context(m: pd.DataFrame) -> dict[str, np.ndarray]:
    n = len(m)
    names = ("gp", "pts", "rank_frac", "gap_title", "gap_safety", "games_left", "dead")
    res = {f"{s}_ctx_{k}": np.full(n, np.nan) for s in ("home", "away") for k in names}
    res["ctx_progress"] = np.full(n, np.nan)
    res["ctx_dead_n"] = np.full(n, np.nan)
    gh, ga = m["home_goals"].to_numpy(float), m["away_goals"].to_numpy(float)
    day = m["_day"].to_numpy()
    h_id, a_id = m["home_team_id"].to_numpy(), m["away_team_id"].to_numpy()
    for _, idx in m.groupby(["competition", "season"], sort=False).indices.items():
        idx = idx[np.argsort(day[idx], kind="mergesort")]
        teams = pd.unique(np.concatenate([h_id[idx], a_id[idx]]))
        tix = {t: i for i, t in enumerate(teams)}
        nt = len(teams)
        pts, gd, gp = np.zeros(nt), np.zeros(nt), np.zeros(nt)
        total_games = max(2 * (nt - 1), 1)
        rel = max(1, int(round(0.15 * nt)))
        hh = np.array([tix[t] for t in h_id[idx]])
        aa = np.array([tix[t] for t in a_id[idx]])
        d_loc = day[idx]
        i = 0
        while i < len(idx):
            j = i
            while j < len(idx) and d_loc[j] == d_loc[i]:
                j += 1
            order = np.argsort(-(pts * 1000.0 + gd), kind="stable")
            rank = np.empty(nt)
            rank[order] = np.arange(nt)
            spts = pts[order]
            leader = spts[0]
            zone_best = spts[nt - rel] if nt - rel >= 0 and nt > rel else spts[-1]
            for q in range(i, j):
                dead = []
                for side, t in (("home", hh[q]), ("away", aa[q])):
                    left = max(total_games - gp[t], 0.0)
                    gt, gs = leader - pts[t], pts[t] - zone_best
                    d = float(left <= 10 and gt > 3 * left and gs > 3 * left)
                    dead.append(d)
                    r_ = idx[q]
                    for k, v in zip(names, (gp[t], pts[t], rank[t] / max(nt - 1, 1), gt, gs, left, d)):
                        res[f"{side}_ctx_{k}"][r_] = v
                res["ctx_progress"][idx[q]] = min((gp[hh[q]] + gp[aa[q]]) / 2 / total_games, 1.0)
                res["ctx_dead_n"][idx[q]] = sum(dead)
            for q in range(i, j):
                if gh[idx[q]] == gh[idx[q]] and ga[idx[q]] == ga[idx[q]]:
                    x, y = gh[idx[q]], ga[idx[q]]
                    pts[hh[q]] += 3 if x > y else (1 if x == y else 0)
                    pts[aa[q]] += 3 if y > x else (1 if x == y else 0)
                    gd[hh[q]] += x - y
                    gd[aa[q]] += y - x
                gp[hh[q]] += 1
                gp[aa[q]] += 1
            i = j
    return res


# --------------------------------------------------------------------------- driver

FAMILIES = ("adj", "style", "mktx", "sched", "h2h", "ref", "ctx")


def build_candidates(matches: pd.DataFrame, extras: pd.DataFrame | None = None, params: dict | None = None,
                     ucl: dict[str, np.ndarray] | None = None, families=FAMILIES) -> pd.DataFrame:
    """Candidate features keyed by ``match_id`` (rows in date, match_id order)."""
    m = _prepare(matches, extras)
    n = len(m)
    cols: dict[str, np.ndarray] = {}
    fam = set(families)
    adj = family_ratings(m, params)
    if "adj" in fam:
        cols.update(adj)
    exp_total = adj["adj_exp_total_shots"]
    long = None
    if fam & {"style", "sched"}:
        long = _team_long(m)
    if "style" in fam:
        cols.update(family_style(long, n))
    if "mktx" in fam:
        cols.update(family_market(m))
    if "sched" in fam:
        cols.update(family_schedule(m, long, n, ucl))
    if "h2h" in fam:
        cols.update(family_h2h(m, exp_total))
    if "ref" in fam:
        cols.update(family_referee(m, exp_total))
    if "ctx" in fam:
        cols.update(family_context(m))
    out = pd.DataFrame(cols)
    out.insert(0, "match_id", m["match_id"].to_numpy())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, default=IN_PATH)
    ap.add_argument("--extras", type=Path, default=EXTRAS_PATH)
    ap.add_argument("--output", type=Path, default=OUT_PATH)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()
    extras = pd.read_parquet(args.extras) if args.extras.exists() else None
    if extras is None:
        log.warning("no match_extras.parquet: style/market/referee families will be empty (run shots.ingest.match_extras)")
    out = build_candidates(pd.read_parquet(args.input), extras, ucl=_ucl_dates())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.output, index=False)
    log.info("wrote %d rows x %d cols to %s in %.1fs", len(out), out.shape[1], args.output, time.time() - t0)


if __name__ == "__main__":
    main()
