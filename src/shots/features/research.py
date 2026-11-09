"""Research feature families (2026-11-06). NOT production; opt-in candidates only.

Evidence and decisions: reports/2026-11-06_research_features.md.

    python -m shots.features.research --output <path>   # matches (+ extras) -> research features keyed by match_id

Every feature of a match uses matches dated strictly before the match date (all matches of a date are read
before any is written, as in ``shots.features.adj_ratings``). Rows without the statistic never update a
state, so upcoming fixtures can be scored with the same functions. The closed-doors flags are a calendar
fact (whether crowds were allowed on the match date), known before kick-off.

V. Venue-split ratings (GAP-style; Wheatcroft 2020, 2021). Per team four ratings: home attack, home defence,
   away attack, away defence, each = a general component + a venue deviation (Kalman filter on a log-NB
   model, same observation model as ``adj_ratings``):

       log E[shots_home] = log L_home + (g_att[h] + v_att[h, home]) + (g_def[a] + v_def[a, away])
       log E[shots_away] = log L_away + (g_att[a] + v_att[a, away]) + (g_def[h] + v_def[h, home])

   The general part is the existing single EKF rating (identical when the venue variance is 0, tested); the
   venue deviation is a team-specific home edge, shrunk to 0 by its own prior variance ``p0v`` and process
   noise ``qv``. Columns (``home_`` = home team at home, ``away_`` = away team away):
   ``{side}_vr_att``, ``{side}_vr_def``, ``{side}_vr_att_dev``, ``{side}_vr_def_dev`` (venue parts),
   ``{side}_vr_att_sd`` / ``_def_sd``, ``vr_exp_home``, ``vr_exp_away``, ``vr_exp_total``.
Z. Closed doors (Reade et al. 2022; Wunderlich et al. 2021): ``cd_closed`` = 1 when the match date falls in a
   no-crowd period of the country (``closed_doors.csv``), ``cd_partial`` = 1 in a documented reduced-capacity
   period; 0 otherwise.
K. Corner ratings: ``adj_ratings`` on corners (``{side}_cr_att``, ``{side}_cr_def``, ``cr_exp_home/away/total``).
W. Game-state-adjusted shot ratings (Skripnikov et al. 2025; StatsBomb score effects): each past match's shots
   are divided by a multiplicative game-state factor before they enter the EKF. States: half-time lead / level
   / deficit of the shooting team (needs ``home_ht_goals``, ``away_ht_goals``) and, if available, red cards
   (own / opponent sent off; full-match counts, timing unknown). Factors are estimated causally: on each date,
   from all earlier matches, ratio of realised to rating-expected shots in each state, relative to the neutral
   state, shrunk with K = 200 matches. Columns ``{side}_gs_att``, ``{side}_gs_def``, ``gs_exp_home/away/total``,
   and the factors used on the date ``gs_f_lead``, ``gs_f_trail`` (diagnostics).
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from shots.features.adj_ratings import DEFAULTS, adjusted_ratings, league_level

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
CLOSED_DOORS_PATH = Path(__file__).with_name("closed_doors.csv")
IN_PATH = ROOT / "data" / "processed" / "matches.parquet"
EXTRAS_PATH = ROOT / "data" / "processed" / "match_extras.parquet"

# general component as in shots.features.candidates.RATING_PARAMS (tuned 2012-08..2023-06)
BASE_PARAMS = dict(q=3e-5, p0=0.03, omega=0.7, move_var=0.02)
# venue deviation: every venue variance lowers the one-step-ahead NB log-lik of shots on 2019-07..2023-06 (report);
# 0.01 without drift only gives the deviation enough content to be tested as a model input
VENUE_PARAMS = dict(p0v=0.01, qv=0.0)
GS_K = 200.0
FAMILIES = ("venue", "closed", "corners", "gamestate")


# --------------------------------------------------------------------------- Z. closed doors

def load_closed_doors(path: Path = CLOSED_DOORS_PATH) -> pd.DataFrame:
    t = pd.read_csv(path)
    t["start"], t["end"] = pd.to_datetime(t["start"]), pd.to_datetime(t["end"])
    assert set(t["status"]) <= {"closed", "partial"}, t["status"].unique()
    assert (t["end"] >= t["start"]).all()
    for c, g in t.sort_values("start").groupby("country"):   # periods of a country never overlap
        assert (g["start"].to_numpy()[1:] > g["end"].to_numpy()[:-1]).all(), c
    return t


def closed_door_flags(m: pd.DataFrame, table: pd.DataFrame | None = None) -> dict[str, np.ndarray]:
    t = load_closed_doors() if table is None else table
    d = pd.to_datetime(m["date"]).dt.normalize().to_numpy()
    country = m["country"].astype(str).to_numpy()
    out = {"cd_closed": np.zeros(len(m)), "cd_partial": np.zeros(len(m))}
    for r in t.itertuples():
        sel = (country == r.country) & (d >= np.datetime64(r.start)) & (d <= np.datetime64(r.end))
        out[f"cd_{r.status}"][sel] = 1.0
    return out


# --------------------------------------------------------------------------- V. venue-split EKF

def venue_ratings(m: pd.DataFrame, stat: str = "shots", home_col: str | None = None,
                  away_col: str | None = None, **params) -> pd.DataFrame:
    """Pre-match venue-split ratings for every row of ``m`` (index preserved). With ``p0v = 0`` the general
    component and ``exp_*`` equal ``adj_ratings.adjusted_ratings`` exactly."""
    p = {**DEFAULTS, **BASE_PARAMS, **VENUE_PARAMS, **params}
    q, p0, r, omega, move_var, clip_hi = p["q"], p["p0"], p["r"], p["omega"], p["move_var"], p["clip_hi"]
    p0v, qv = p["p0v"], p["qv"]
    hc, ac = home_col or f"home_{stat}", away_col or f"away_{stat}"
    n = len(m)
    mm = m.reset_index(drop=True)
    day = ((pd.to_datetime(mm["date"]).astype("datetime64[ns]") - pd.Timestamp("2000-01-01"))
           // pd.Timedelta(days=1)).to_numpy()
    order = np.argsort(day, kind="mergesort")
    lvl = mm[["date", "competition"]].copy()
    lvl["h"], lvl["a"] = mm[hc].astype(float).to_numpy(), mm[ac].astype(float).to_numpy()
    lh, la, _ = league_level(lvl, "h", "a", p["window"], p["k_level"])
    codes, _uniq = pd.factorize(pd.concat([mm["home_team_id"], mm["away_team_id"]], ignore_index=True))
    hi, ai = codes[:n], codes[n:]
    ccode = pd.factorize(mm["competition"])[0]
    nt = int(codes.max()) + 1 if n else 0
    ga, gd = np.zeros(nt), np.zeros(nt)          # general attack / defence
    pga, pgd = np.full(nt, p0), np.full(nt, p0)
    va, vd = np.zeros((nt, 2)), np.zeros((nt, 2))   # venue deviation [team, 0 = home, 1 = away]
    pva, pvd = np.full((nt, 2), p0v), np.full((nt, 2), p0v)
    last = np.full(nt, -1, dtype=np.int64)
    lastv = np.full((nt, 2), -1, dtype=np.int64)
    lastc = np.full(nt, -1, dtype=np.int64)
    yh, ya = mm[hc].to_numpy(dtype=float), mm[ac].to_numpy(dtype=float)
    feat = np.full((n, 14), np.nan)

    def read(t, v, d, c):
        a_g, d_g = pga[t], pgd[t]
        if last[t] >= 0:
            gap = d - last[t]
            a_g, d_g = min(a_g + q * gap, p0), min(d_g + q * gap, p0)
            if lastc[t] != c:
                a_g, d_g = a_g + move_var, d_g + move_var
        a_v, d_v = pva[t, v], pvd[t, v]
        if lastv[t, v] >= 0 and p0v > 0:
            gap = d - lastv[t, v]
            a_v, d_v = min(a_v + qv * gap, p0v), min(d_v + qv * gap, p0v)
        return a_g, d_g, a_v, d_v

    def update(ta, va_, td, vd_, d, c, mu, y):
        """attack of ``ta`` at venue ``va_`` produced y against the defence of ``td`` at venue ``vd_``."""
        a_g, _, a_v, _ = read(ta, va_, d, c)
        _, d_g, _, d_v = read(td, vd_, d, c)
        fisher = mu * r / (mu + r)
        z = min((y - mu) / mu, clip_hi)
        s = a_g + a_v + d_g + d_v + omega / fisher
        ga[ta] += a_g / s * z
        va[ta, va_] += a_v / s * z
        gd[td] += d_g / s * z
        vd[td, vd_] += d_v / s * z
        pga[ta] = a_g - a_g * a_g / s
        pva[ta, va_] = a_v - a_v * a_v / s
        pgd[td] = d_g - d_g * d_g / s
        pvd[td, vd_] = d_v - d_v * d_v / s

    i = 0
    while i < n:
        j = i
        d0 = day[order[i]]
        while j < n and day[order[j]] == d0:
            j += 1
        grp = order[i:j]
        d = int(d0)
        for k in grp:   # 1) read every match of the date
            h, a, c = hi[k], ai[k], ccode[k]
            hag, hdg, hav, hdv = read(h, 0, d, c)
            aag, adg, aav, adv = read(a, 1, d, c)
            h_att, h_def = ga[h] + va[h, 0], gd[h] + vd[h, 0]
            a_att, a_def = ga[a] + va[a, 1], gd[a] + vd[a, 1]
            feat[k, :12] = (h_att, h_def, va[h, 0], vd[h, 0], math.sqrt(hag + hav), math.sqrt(hdg + hdv),
                            a_att, a_def, va[a, 1], vd[a, 1], math.sqrt(aag + aav), math.sqrt(adg + adv))
            feat[k, 12] = lh[k] * math.exp(h_att + a_def) if lh[k] == lh[k] else np.nan
            feat[k, 13] = la[k] * math.exp(a_att + h_def) if la[k] == la[k] else np.nan
        for k in grp:   # 2) then write
            if not (feat[k, 12] == feat[k, 12] and feat[k, 13] == feat[k, 13]):
                continue
            if yh[k] != yh[k] or ya[k] != ya[k]:
                continue
            h, a, c = hi[k], ai[k], ccode[k]
            update(h, 0, a, 1, d, c, max(feat[k, 12], 1e-3), yh[k])
            update(a, 1, h, 0, d, c, max(feat[k, 13], 1e-3), ya[k])
            last[h] = last[a] = d
            lastv[h, 0] = lastv[a, 1] = d
            lastc[h], lastc[a] = c, c
        i = j
    out = pd.DataFrame(feat, index=m.index, columns=[
        "home_att", "home_def", "home_att_dev", "home_def_dev", "home_att_sd", "home_def_sd",
        "away_att", "away_def", "away_att_dev", "away_def_dev", "away_att_sd", "away_def_sd",
        "exp_home", "exp_away"])
    out["lvl_home"], out["lvl_away"] = lh, la
    out["exp_total"] = out["exp_home"] + out["exp_away"]
    return out


def nb_loglik(y: np.ndarray, mu: np.ndarray, r: float) -> np.ndarray:
    """NB2 log pmf with size r (Var = mu + mu^2 / r)."""
    from scipy.special import gammaln

    return (gammaln(y + r) - gammaln(r) - gammaln(y + 1) + r * np.log(r / (r + mu)) + y * np.log(mu / (r + mu)))


def predictive_loglik(m: pd.DataFrame, exp_home: np.ndarray, exp_away: np.ndarray, start: str, end: str,
                      stat: str = "shots", r: float = 10.0) -> float:
    """Mean one-step-ahead NB log-likelihood of both sides' ``stat`` on matches dated in [start, end]."""
    d = pd.to_datetime(m["date"]).to_numpy()
    sel = (d >= np.datetime64(start)) & (d <= np.datetime64(end))
    y = np.concatenate([m[f"home_{stat}"].to_numpy(float)[sel], m[f"away_{stat}"].to_numpy(float)[sel]])
    mu = np.concatenate([np.asarray(exp_home)[sel], np.asarray(exp_away)[sel]])
    ok = np.isfinite(y) & np.isfinite(mu)
    return float(nb_loglik(y[ok], np.maximum(mu[ok], 1e-3), r).mean())


# --------------------------------------------------------------------------- W. game state

def gamestate_factors(m: pd.DataFrame, exp_home: np.ndarray, exp_away: np.ndarray, k: float = GS_K,
                      use_red: bool = True) -> dict[str, np.ndarray]:
    """Per match row: causal multiplicative factors for each side's shots given the match's game state.

    For every side-observation (team shots y, rating expectation e) the state is HT lead / level / trail of the
    shooting team, and own / opponent red card (if ``home_red`` exists and ``use_red``). The factor of a state
    on date D = (sum y / sum e over earlier matches in that state) / (same over earlier neutral-state matches),
    shrunk toward 1 with ``k`` observations; level-HT / no-red is the reference (factor 1). Effects of the two
    dimensions are estimated separately and multiplied. Returns the factor of each side of each row (NaN where
    the state is unknown), plus the date's lead / trail factors."""
    n = len(m)
    mm = m.reset_index(drop=True)
    day = ((pd.to_datetime(mm["date"]) - pd.Timestamp("2000-01-01")) // pd.Timedelta(days=1)).to_numpy()
    yh, ya = mm["home_shots"].to_numpy(float), mm["away_shots"].to_numpy(float)
    eh, ea = np.asarray(exp_home, float), np.asarray(exp_away, float)
    hth = mm["home_ht_goals"].to_numpy(float) if "home_ht_goals" in mm else np.full(n, np.nan)
    hta = mm["away_ht_goals"].to_numpy(float) if "away_ht_goals" in mm else np.full(n, np.nan)
    have_red = use_red and "home_red" in mm
    rh = mm["home_red"].to_numpy(float) if have_red else np.full(n, np.nan)
    ra = mm["away_red"].to_numpy(float) if have_red else np.full(n, np.nan)
    # side-observation arrays: (row, y, e, ht state: 0 level / 1 lead / 2 trail, red state: 0 none, 1 own, 2 opp, 3 both)
    rows = np.concatenate([np.arange(n), np.arange(n)])
    y = np.concatenate([yh, ya])
    e = np.concatenate([eh, ea])
    diff = np.concatenate([hth - hta, hta - hth])
    ht = np.where(np.isnan(diff), -1, np.where(diff > 0, 1, np.where(diff < 0, 2, 0)))
    own = np.concatenate([rh, ra])
    opp = np.concatenate([ra, rh])
    red = np.where(np.isnan(own) | np.isnan(opp), -1, (own > 0).astype(int) + 2 * (opp > 0).astype(int))
    dd = np.concatenate([day, day])
    ok = np.isfinite(y) & np.isfinite(e) & (e > 0)
    out = {"f_home": np.full(n, np.nan), "f_away": np.full(n, np.nan), "f_lead": np.full(n, np.nan),
           "f_trail": np.full(n, np.nan), "f_red_own": np.full(n, np.nan), "f_red_opp": np.full(n, np.nan)}
    # cumulative sums by state over dates strictly before
    udays = np.unique(dd)
    pos = np.searchsorted(udays, dd)
    def cum(state, nstates):
        sy = np.zeros((len(udays) + 1, nstates))
        se = np.zeros((len(udays) + 1, nstates))
        sn = np.zeros((len(udays) + 1, nstates))
        good = ok & (state >= 0)
        np.add.at(sy, (pos[good] + 1, state[good]), y[good])
        np.add.at(se, (pos[good] + 1, state[good]), e[good])
        np.add.at(sn, (pos[good] + 1, state[good]), 1.0)
        return np.cumsum(sy, axis=0), np.cumsum(se, axis=0), np.cumsum(sn, axis=0)  # row p: days < udays[p]

    def table(cy, ce, cn, nstates):
        """factor[p, s] for p = 0..len(udays)-1 (sums of days strictly before udays[p])."""
        cy, ce, cn = cy[:-1], ce[:-1], cn[:-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            ref = cy[:, 0] / ce[:, 0]
            ratio = (cy / ce) / ref[:, None]
        ratio = np.where(np.isfinite(ratio), ratio, 1.0)
        f = (cn * ratio + k) / (cn + k)
        f[:, 0] = 1.0
        return f

    fh_tab = table(*cum(ht, 3), 3)
    fr_tab = table(*cum(red, 4), 4)
    f_ht = np.where(ht >= 0, fh_tab[pos, np.maximum(ht, 0)], np.nan)
    f_red = np.where(red >= 0, fr_tab[pos, np.maximum(red, 0)], 1.0 if not have_red else np.nan)
    f = f_ht * np.where(np.isnan(f_red), 1.0, f_red)
    out["f_home"], out["f_away"] = f[:n], f[n:]
    p_row = pos[:n]
    out["f_lead"], out["f_trail"] = fh_tab[p_row, 1], fh_tab[p_row, 2]
    out["f_red_own"], out["f_red_opp"] = fr_tab[p_row, 1], fr_tab[p_row, 2]
    return out


def gamestate_ratings(m: pd.DataFrame, params: dict | None = None, use_red: bool = True) -> dict[str, np.ndarray]:
    """EKF ratings (``adj_ratings``) on game-state-adjusted shots: shots / causal factor of the match's state."""
    p = {**BASE_PARAMS, **(params or {})}
    base = adjusted_ratings(m, "shots", **p)
    fac = gamestate_factors(m, base["exp_home"].to_numpy(), base["exp_away"].to_numpy(), use_red=use_red)
    mm = m.copy()
    mm["home_gsshots"] = mm["home_shots"].astype(float).to_numpy() / fac["f_home"]
    mm["away_gsshots"] = mm["away_shots"].astype(float).to_numpy() / fac["f_away"]
    # unknown state (no HT score): use the raw count, so a team's history is never dropped
    for s in ("home", "away"):
        miss = ~np.isfinite(mm[f"{s}_gsshots"].to_numpy())
        mm.loc[miss, f"{s}_gsshots"] = mm.loc[miss, f"{s}_shots"].astype(float)
    r = adjusted_ratings(mm, "gsshots", **p)
    out = {}
    for side in ("home", "away"):
        out[f"{side}_gs_att"], out[f"{side}_gs_def"] = r[f"{side}_att"].to_numpy(), r[f"{side}_def"].to_numpy()
    out["gs_exp_home"], out["gs_exp_away"] = r["exp_home"].to_numpy(), r["exp_away"].to_numpy()
    out["gs_exp_total"] = r["exp_total"].to_numpy()
    out["gs_f_lead"], out["gs_f_trail"] = fac["f_lead"], fac["f_trail"]
    out["gs_f_red_own"], out["gs_f_red_opp"] = fac["f_red_own"], fac["f_red_opp"]
    return out


# --------------------------------------------------------------------------- driver

def _prepare(matches: pd.DataFrame, extras: pd.DataFrame | None) -> pd.DataFrame:
    m = matches.reset_index(drop=True).copy()
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    if "country" not in m:
        m["country"] = m["competition"]
    for c in ("home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"):
        if c in m:
            m[c] = m[c].astype("float64")
    for side in ("home", "away"):   # canonical name of the ingest (2026-11-06) -> name used here
        if f"{side}_reds" in m and f"{side}_red" not in m:
            m[f"{side}_red"] = m[f"{side}_reds"]
    if extras is not None:
        cols = [c for c in ("home_corners", "away_corners", "home_red", "away_red", "home_ht_goals",
                            "away_ht_goals") if c in extras.columns and c not in m.columns]
        if cols:
            m = m.join(extras.drop_duplicates("match_id").set_index("match_id")[cols], on="match_id")
    for c in ("home_corners", "away_corners", "home_red", "away_red", "home_ht_goals", "away_ht_goals"):
        if c in m:
            m[c] = m[c].astype("float64")
    return m.sort_values(["date", "match_id"], kind="mergesort").reset_index(drop=True)


def build_research(matches: pd.DataFrame, extras: pd.DataFrame | None = None, families=FAMILIES,
                   venue_params: dict | None = None) -> pd.DataFrame:
    """Research features keyed by ``match_id`` (rows in date, match_id order)."""
    m = _prepare(matches, extras)
    cols: dict[str, np.ndarray] = {}
    fam = set(families)
    if "venue" in fam:
        v = venue_ratings(m, "shots", **(venue_params or {}))
        for side in ("home", "away"):
            for k in ("att", "def", "att_dev", "def_dev", "att_sd", "def_sd"):
                cols[f"{side}_vr_{k}"] = v[f"{side}_{k}"].to_numpy()
        for k in ("home", "away", "total"):
            cols[f"vr_exp_{k}"] = v[f"exp_{k}"].to_numpy()
    if "closed" in fam:
        cols.update(closed_door_flags(m))
    if "corners" in fam and "home_corners" in m:
        c = adjusted_ratings(m, "corners", **BASE_PARAMS)
        for side in ("home", "away"):
            cols[f"{side}_cr_att"], cols[f"{side}_cr_def"] = c[f"{side}_att"].to_numpy(), c[f"{side}_def"].to_numpy()
        for k in ("home", "away", "total"):
            cols[f"cr_exp_{k}"] = c[f"exp_{k}"].to_numpy()
    if "gamestate" in fam and "home_ht_goals" in m:
        cols.update(gamestate_ratings(m))
    out = pd.DataFrame(cols)
    out.insert(0, "match_id", m["match_id"].to_numpy())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, default=IN_PATH)
    ap.add_argument("--extras", type=Path, default=EXTRAS_PATH)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--families", default=",".join(FAMILIES))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()
    extras = pd.read_parquet(args.extras) if args.extras.exists() else None
    out = build_research(pd.read_parquet(args.input), extras, families=args.families.split(","))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.output, index=False)
    log.info("wrote %d rows x %d cols to %s in %.1fs", len(out), out.shape[1], args.output, time.time() - t0)


if __name__ == "__main__":
    main()
