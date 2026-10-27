"""Over-shoot study (2026-10-26): which pre-match conditions make a match exceed our published expected shots
by 5+ (or 6+), out of sample, beyond what the published curve already states.

    python -m shots.evaluation.overshoot study --preds <dir> [--out reports/2026-10-26_overshoot_patterns.json]
    python -m shots.evaluation.overshoot compare --cache <dir> --pairs fin_h:fin_c:noodds,... \
        [--refit season|month] [--segments] --out <json>

Inputs
- Walk-forward predictions of the PRODUCTION routing, monthly refit, 2019/20-2025/26, as written by
  ``python -m shots.models.screen run --refit month`` (``<label>__<variant>__<season>__month.parquet``):
  odds route = ``cand_nbmle`` / ``odds`` (production ``negbin_pit_comp`` re-estimated with nbmle, identical to
  1e-4 RPS), no-odds route = ``fin_c`` / ``noodds``. Each block is fitted on rows dated strictly before it.
- ``features.parquet``, ``features_candidates.parquet``, ``match_extras.parquet`` (kickoff time),
  ``matches.parquet`` (football-data only; provisional / manual results are never read).

Definitions
- expected = the published integer median of the curve (``median_curve``; ``shots.config.MEDIAN_FROM_CURVE``).
- over-shoot k = actual - expected >= k (k = 5, 6); under-shoot = actual - expected <= -5.
- stated P(over-shoot 5) = P(total >= expected + 5) read off the same published curve. The model "already
  captures" a pattern when the realised rate in the segment equals the stated rate (excess = realised - stated
  close to 0): a segment can have many over-shoots simply because the curve there is wide or high.
- Every conditioning variable is pre-match: features use only matches dated strictly before the match date
  (same-day matches excluded) and pre-match odds; the model-residual momentum uses out-of-sample residuals of
  earlier dates only.
- CIs: day-block bootstrap (all matches of a date resampled together), 1,000 resamples.
- Discovery = 2019/20-2022/23, confirmation = 2023/24-2025/26 (docs/evaluation-protocol.md). Segment cut points
  are fixed in advance or are quantiles of the discovery period.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "data" / "processed"
SEASONS = [f"{y}/{y + 1}" for y in range(2019, 2026)]
DISCOVERY = SEASONS[:4]
CONFIRMATION = SEASONS[4:]
ROUTES = {"odds": ("cand_nbmle", "odds"), "noodds": ("fin_c", "noodds")}
N_BOOT = 1000


# --------------------------------------------------------------------------- predictions

def load_route(preds: Path, label: str, variant: str, refit: str = "month") -> pd.DataFrame:
    fs = sorted(glob.glob(str(Path(preds) / f"{label}__{variant}__*__{refit}.parquet")))
    if not fs:
        raise FileNotFoundError(f"no {label}/{variant} predictions in {preds}")
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d[d["season"].isin(SEASONS)].reset_index(drop=True)
    cdf_cols = [f"cdf_{k}" for k in range(81)]
    F = d[cdf_cols].to_numpy(float)
    med = d["median_curve"].to_numpy(int)
    y = d["actual"].to_numpy(float)

    def ge(n):  # P(total >= n) on the published curve
        n = np.clip(n, 0, F.shape[1])
        out = np.ones(len(n))
        pos = n > 0
        out[pos] = 1.0 - F[np.arange(len(n))[pos], n[pos] - 1]
        return out

    out = d[["match_id", "date", "season", "competition", "tier", "actual", "home_actual", "away_actual"]].copy()
    out["expected"] = med
    out["mean"] = (1.0 - F).sum(axis=1)
    out["resid"] = y - med
    out["resid_mean"] = y - out["mean"]
    out["over5"] = (out["resid"] >= 5).astype(float)
    out["over6"] = (out["resid"] >= 6).astype(float)
    out["under5"] = (out["resid"] <= -5).astype(float)
    out["st_over5"] = ge(med + 5)
    out["st_over6"] = ge(med + 6)
    out["st_under5"] = 1.0 - ge(med - 4)          # P(total <= expected - 5)
    out["p24"] = ge(np.full(len(d), 24))
    out["hit24"] = (y >= 24).astype(float)
    out["lb_70"], out["lb_80"] = d["lb_70"].to_numpy(), d["lb_80"].to_numpy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    return out


# --------------------------------------------------------------------------- conditioning variables

def _team_long(m: pd.DataFrame, cols: dict[str, tuple[str, str]]) -> pd.DataFrame:
    """One row per (match, side): team id, date and per-side values (home_col, away_col)."""
    parts = []
    for side in ("home", "away"):
        d = {"match_id": m["match_id"].to_numpy(), "side": side, "team": m[f"{side}_team_id"].to_numpy(),
             "date": m["date"].to_numpy()}
        for name, (hc, ac) in cols.items():
            d[name] = m[hc if side == "home" else ac].to_numpy(float)
        parts.append(pd.DataFrame(d))
    return pd.concat(parts, ignore_index=True).sort_values(["team", "date", "match_id"], kind="mergesort")


def prior_rolling(long: pd.DataFrame, col: str, window: int, how: str = "mean", min_periods: int = 1) -> pd.Series:
    """Statistic of ``col`` over the team's previous ``window`` matches dated strictly before the row's date
    (NaN values skipped; same-day matches never see each other). Index = ``long.index``."""
    g = long.groupby("team", sort=False)[col]
    shifted = g.shift(1)
    r = shifted.groupby(long["team"], sort=False).rolling(window, min_periods=min_periods)
    val = getattr(r, how)().droplevel(0).reindex(long.index)
    # same-day exclusion: every row of a (team, date) takes the value of its first row
    first = ~long.duplicated(["team", "date"])
    return val.where(first).groupby([long["team"], long["date"]], sort=False).transform("max")


def _to_match(long: pd.DataFrame, s: pd.Series, name: str) -> pd.DataFrame:
    t = long[["match_id", "side"]].assign(v=s.to_numpy())
    w = t.pivot(index="match_id", columns="side", values="v")
    return w.rename(columns={"home": f"home_{name}", "away": f"away_{name}"})


def _promoted(m: pd.DataFrame) -> pd.DataFrame:
    """Per match and side: +1 promoted (previous season's last tier higher number), -1 relegated, 0 same tier,
    NaN = no previous season in the data. Uses the previous season's matches only (all dated before)."""
    long = _team_long(m, {"tier": ("tier", "tier")}).assign(season=lambda x: x["match_id"].map(
        m.set_index("match_id")["season"]))
    last = long.groupby(["team", "season"], sort=True)["tier"].last().reset_index()
    last["season_next"] = last["season"].map(lambda s: f"{int(s[:4]) + 1}/{int(s[:4]) + 2}")
    prev = last.set_index(["team", "season_next"])["tier"]
    cur = long.set_index(["team", "season"])["tier"]
    prev_t = prev.reindex(cur.index).to_numpy()
    move = np.sign(prev_t - cur.to_numpy())  # previous tier 2, now 1 -> +1 (promoted)
    out = long[["match_id", "side"]].assign(v=move)
    return out.pivot(index="match_id", columns="side", values="v").rename(
        columns={"home": "home_promo", "away": "away_promo"})


def _derby(m: pd.DataFrame) -> pd.Series:
    """Approximate local derby: both team names start with the same word of 4+ letters, excluding generic
    words (a stand-in for "same city", which the data do not carry). Examples: Sheffield Utd / Sheffield Weds,
    Bristol City / Bristol Rvs, Dundee / Dundee United."""
    generic = {"real", "sporting", "atletico", "athletic", "union", "racing", "inter", "dynamo", "royal",
               "olympique", "stade", "queens", "west", "east", "north", "south"}

    def first(n):
        w = str(n).lower().replace(".", " ").split()
        return w[0] if w and len(w[0]) >= 4 and w[0] not in generic else None

    h = m["home_team"].map(first)
    a = m["away_team"].map(first)
    return ((h == a) & h.notna()).astype(float)


def conditioning_frame() -> pd.DataFrame:
    """Pre-match conditioning variables, one row per match_id (all matches)."""
    m = pd.read_parquet(PROC / "matches.parquet")
    m = m[m["source"] == "football-data"].copy()
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    f = pd.read_parquet(PROC / "features.parquet")
    c = pd.read_parquet(PROC / "features_candidates.parquet")
    ex = pd.read_parquet(PROC / "match_extras.parquet", columns=["match_id", "kickoff_time", "referee"])
    keep_f = ["match_id", "home_rest_days", "away_rest_days", "home_n_prior_season", "away_n_prior_season",
              "home_tier_change", "away_tier_change", "league_prior_shots", "league_level_drift",
              "league_recent28_drift", "mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25",
              *[f"{s}_shots_{d}_{w}" for s in ("home", "away") for d in ("for", "against") for w in ("l5", "l10")]]
    out = m[["match_id", "date", "home_team", "away_team", "home_team_id", "away_team_id"]].merge(
        f[keep_f], on="match_id", how="left").merge(c, on="match_id", how="left").merge(ex, on="match_id", how="left")
    # goals form (last 5, strictly before date)
    long = _team_long(m, {"gf": ("home_goals", "away_goals"), "ga": ("away_goals", "home_goals"),
                          "sf": ("home_shots", "away_shots"), "sa": ("away_shots", "home_shots")})
    for col in ("gf", "ga"):
        out = out.merge(_to_match(long, prior_rolling(long, col, 5), f"{col}_l5"), left_on="match_id",
                        right_index=True, how="left")
    # EKF-residual momentum: team's recent (match total - rating expectation at that time), last 3 and 5
    exp = c.set_index("match_id")["adj_exp_total_shots"]
    m2 = m.assign(ekf_res=(m["home_shots"] + m["away_shots"]) - m["match_id"].map(exp))
    long2 = _team_long(m2, {"er": ("ekf_res", "ekf_res")})
    for w in (3, 5):
        out = out.merge(_to_match(long2, prior_rolling(long2, "er", w), f"ekfres_l{w}"), left_on="match_id",
                        right_index=True, how="left")
    out = out.merge(_promoted(m), left_on="match_id", right_index=True, how="left")
    out["derby"] = _derby(out).to_numpy()
    k = pd.to_datetime(out["kickoff_time"].astype("string"), format="%H:%M", errors="coerce")
    out["kick_hour"] = k.dt.hour + k.dt.minute / 60
    out["dow"] = out["date"].dt.dayofweek
    out["month"] = out["date"].dt.month
    return out


def residual_momentum(route: pd.DataFrame, teams: pd.DataFrame) -> pd.DataFrame:
    """Per match: each team's mean out-of-sample residual (actual - expected) and number of over-shoots (5+)
    over its last 3 / 5 predicted matches dated strictly before the match (NaN without any)."""
    r = route[["match_id", "date", "resid", "over5"]].merge(teams, on="match_id")
    long = _team_long(r.rename(columns={}), {"res": ("resid", "resid"), "o5": ("over5", "over5")})
    out = pd.DataFrame(index=pd.Index(r["match_id"], name="match_id"))
    for w in (3, 5):
        out = out.join(_to_match(long, prior_rolling(long, "res", w), f"mres_l{w}"))
        out = out.join(_to_match(long, prior_rolling(long, "o5", w, "sum"), f"mo5_l{w}"))
    return out.reset_index()


# --------------------------------------------------------------------------- statistics

class DayBoot:
    """Day-block bootstrap of ratios of sums: resampled day indices are drawn once and reused, so every segment
    and the overall base rate are resampled together (lift CIs are paired)."""

    def __init__(self, days: np.ndarray, n_boot: int = N_BOOT, seed: int = 0):
        self.codes, uniq = pd.factorize(days)
        self.nd = len(uniq)
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, self.nd, (n_boot, self.nd))
        self.W = np.zeros((n_boot, self.nd))
        for b in range(n_boot):
            self.W[b] = np.bincount(idx[b], minlength=self.nd)

    def sums(self, values: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """Bootstrap replicates of sum(values[mask]) (shape n_boot)."""
        per_day = np.bincount(self.codes[mask], weights=values[mask], minlength=self.nd)
        return self.W @ per_day


def _ci(x: np.ndarray) -> list[float]:
    lo, hi = np.nanpercentile(x, [2.5, 97.5])
    return [float(lo), float(hi)]


def segment_stats(df: pd.DataFrame, mask: np.ndarray, boot: DayBoot, base: dict) -> dict:
    """Over-shoot rate, stated rate, excess, lift vs the route's base rate, mean residual, per period / season."""
    mask = np.asarray(mask, bool)
    n = int(mask.sum())
    res = {"n": n}
    if n < 30:
        return res
    one = np.ones(len(df))
    nb = boot.sums(one, mask)
    for k in ("over5", "over6", "under5"):
        y = df[k].to_numpy(float)
        st = df[f"st_{k}"].to_numpy(float)
        rate = y[mask].mean()
        b_rate = base[k]
        yb = boot.sums(y, mask) / nb
        sb = boot.sums(st, mask) / nb
        res[k] = {"rate": float(rate), "stated": float(st[mask].mean()), "excess": float(rate - st[mask].mean()),
                  "excess_ci": _ci(yb - sb), "lift": float(rate / b_rate), "lift_ci": _ci(yb / base[f"{k}_boot"])}
    r = df["resid_mean"].to_numpy(float)
    res["resid_mean"] = float(r[mask].mean())
    res["resid_mean_ci"] = _ci(boot.sums(r, mask) / nb)
    res["expected"] = float(df["expected"].to_numpy(float)[mask].mean())
    res["actual"] = float(df["actual"].to_numpy(float)[mask].mean())
    # stability
    ex = (df["over5"] - df["st_over5"]).to_numpy(float)
    per = {}
    for s in SEASONS:
        mm = mask & (df["season"] == s).to_numpy()
        per[s] = [int(mm.sum()), float(ex[mm].mean()) if mm.sum() else None,
                  float(df["over5"].to_numpy(float)[mm].mean()) if mm.sum() else None]
    res["per_season"] = per
    res["seasons_pos"] = int(sum(1 for v in per.values() if v[1] is not None and v[0] >= 20 and v[1] > 0))
    res["seasons_n"] = int(sum(1 for v in per.values() if v[1] is not None and v[0] >= 20))
    for lab, ss in (("disc", DISCOVERY), ("conf", CONFIRMATION)):
        mm = mask & df["season"].isin(ss).to_numpy()
        if mm.sum() >= 30:
            nbb = boot.sums(one, mm)
            res[lab] = {"n": int(mm.sum()), "rate": float(df["over5"].to_numpy(float)[mm].mean()),
                        "excess": float(ex[mm].mean()), "excess_ci": _ci(boot.sums(ex, mm) / nbb),
                        "resid_mean": float(r[mm].mean())}
    return res


def segments(d: pd.DataFrame) -> dict[str, np.ndarray]:
    """Named boolean segments (pre-match conditions). Cut points fixed in advance or discovery quantiles."""
    S: dict[str, np.ndarray] = {}
    disc = d["season"].isin(DISCOVERY)

    def q(col, qs):
        return np.nanquantile(d.loc[disc, col].to_numpy(float), qs)

    def add_bins(name, col, edges, labels=None):
        v = d[col].to_numpy(float)
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            lab = labels[i] if labels else f"[{lo:.3g}, {hi:.3g})"
            S[f"{name}: {lab}"] = (v >= lo) & (v < hi)

    def add_quint(name, col, k=5):
        e = q(col, np.linspace(0, 1, k + 1))
        e[0], e[-1] = -np.inf, np.inf
        add_bins(name, col, e, [f"Q{i + 1}" for i in range(k)])

    # 1. strength mismatch
    fav = np.fmax(d["mkt_p_home"], d["mkt_p_away"])
    d["_fav"] = fav
    add_bins("market favourite p", "_fav", [0, .45, .55, .65, .75, .85, 1.01])
    S["strong home favourite (p_home >= .70)"] = (d["mkt_p_home"] >= .70).to_numpy()
    S["strong away favourite (p_away >= .60)"] = (d["mkt_p_away"] >= .60).to_numpy()
    S["even market (|pH - pA| < .10)"] = ((d["mkt_p_home"] - d["mkt_p_away"]).abs() < .10).to_numpy()
    d["_rgap"] = (d["home_adj_att"] - d["home_adj_def"]) - (d["away_adj_att"] - d["away_adj_def"])
    d["_absrgap"] = d["_rgap"].abs()
    add_quint("rating gap |home - away| (EKF)", "_absrgap")
    d["_dom"] = np.abs(np.log(d["adj_exp_home_shots"] / d["adj_exp_away_shots"]))
    add_quint("expected shot dominance |log(exp_h / exp_a)|", "_dom")
    S["strong home vs weak away (rating gap top decile, home better)"] = (d["_rgap"] >= q("_rgap", [.9])[0]).to_numpy()
    S["strong away vs weak home (rating gap bottom decile)"] = (d["_rgap"] <= q("_rgap", [.1])[0]).to_numpy()
    # 2. market goal expectation
    add_bins("market total goals", "mktx_total_goals", [0, 2.3, 2.55, 2.8, 3.05, 3.3, 9])
    add_quint("market goals vs our expected (gap)", "_mkt_gap")
    # 3. residual momentum / volatility
    d["_mres5"] = d[["home_mres_l5", "away_mres_l5"]].mean(axis=1)
    d["_mres3"] = d[["home_mres_l3", "away_mres_l3"]].mean(axis=1)
    d["_mo5"] = d["home_mo5_l5"] + d["away_mo5_l5"]
    d["_ekf5"] = d[["home_ekfres_l5", "away_ekfres_l5"]].mean(axis=1)
    d["_ekf3"] = d[["home_ekfres_l3", "away_ekfres_l3"]].mean(axis=1)
    add_quint("model residual momentum, both teams last 5", "_mres5")
    add_quint("model residual momentum, both teams last 3", "_mres3")
    add_bins("over-shoots (5+) in both teams' last 5", "_mo5", [0, 1, 2, 3, 4, 11], ["0", "1", "2", "3", "4+"])
    add_quint("rating-residual momentum (total - EKF exp), last 5", "_ekf5")
    add_quint("rating-residual momentum (total - EKF exp), last 3", "_ekf3")
    d["_sd10"] = d["home_style_total_sd10"] + d["away_style_total_sd10"]
    add_quint("match-total volatility sd10 (both teams)", "_sd10")
    # 4. form trend / goals
    l5 = d[[f"{s}_shots_{x}_l5" for s in ("home", "away") for x in ("for", "against")]].sum(axis=1, min_count=4)
    l10 = d[[f"{s}_shots_{x}_l10" for s in ("home", "away") for x in ("for", "against")]].sum(axis=1, min_count=4)
    d["_trend"] = np.log(l5 / l10)
    add_quint("shot-volume trend log(l5 / l10), both teams", "_trend")
    d["_goals5"] = d[["home_gf_l5", "home_ga_l5", "away_gf_l5", "away_ga_l5"]].sum(axis=1, min_count=4)
    add_quint("goals in both teams' last 5 (for + against)", "_goals5")
    d["_gfmax"] = np.fmax(d["home_gf_l5"], d["away_gf_l5"])
    add_bins("best recent scoring rate (max gf_l5)", "_gfmax", [0, 1, 1.5, 2, 2.5, 9])
    # 5. season phase / promotion
    S["promoted team involved"] = ((d["home_promo"] == 1) | (d["away_promo"] == 1)).to_numpy()
    S["relegated team involved"] = ((d["home_promo"] == -1) | (d["away_promo"] == -1)).to_numpy()
    S["newcomer (no previous season in data)"] = (d["home_promo"].isna() | d["away_promo"].isna()).to_numpy()
    npm = np.fmin(d["home_n_prior_season"], d["away_n_prior_season"])
    S["early season (either team < 5 matches)"] = (npm < 5).to_numpy()
    gl = np.fmin(d["home_ctx_games_left"], d["away_ctx_games_left"])
    S["last 3 matches (either team)"] = (gl <= 3).to_numpy()
    S["4-8 matches left"] = ((gl > 3) & (gl <= 8)).to_numpy()
    S["dead rubber (both teams nothing to play for)"] = (d["ctx_dead_n"] >= 2).to_numpy()
    S["one dead team"] = (d["ctx_dead_n"] == 1).to_numpy()
    late = gl <= 8
    title = (np.fmin(d["home_ctx_gap_title"], d["away_ctx_gap_title"]) <= 3) & late
    drop = ((d["home_ctx_gap_safety"].abs() <= 3) | (d["away_ctx_gap_safety"].abs() <= 3)) & late
    S["late title race (gap <= 3, <= 8 left)"] = title.to_numpy()
    S["late relegation fight (|gap| <= 3, <= 8 left)"] = drop.to_numpy()
    # 6. rest / congestion
    S["away short rest (<= 3 days)"] = (d["away_rest_days"] <= 3).to_numpy()
    S["home short rest (<= 3 days)"] = (d["home_rest_days"] <= 3).to_numpy()
    S["both long rest (>= 7 days)"] = ((d["home_rest_days"] >= 7) & (d["away_rest_days"] >= 7)).to_numpy()
    S["rest gap >= 3 days (home fresher)"] = ((d["home_rest_days"] - d["away_rest_days"]) >= 3).to_numpy()
    S["congested (either team >= 4 matches in 14 d)"] = (np.fmax(d["home_sched_n14"], d["away_sched_n14"]) >= 4).to_numpy()
    # 7. calendar
    for i, lab in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        S[f"weekday: {lab}"] = (d["dow"] == i).to_numpy()
    add_bins("kickoff hour (44% of rows)", "kick_hour", [0, 14, 16, 18, 19.5, 24],
             ["< 14:00", "14:00-15:59", "16:00-17:59", "18:00-19:29", ">= 19:30"])
    for mo in (8, 9, 10, 11, 12, 1, 2, 3, 4, 5):
        S[f"month: {mo:02d}"] = (d["month"] == mo).to_numpy()
    # 8. head to head / derby
    S["approximate derby (shared city word)"] = (d["derby"] == 1).to_numpy()
    add_bins("h2h deviation vs ratings (shrunk)", "h2h_dev_shrunk", [-99, -2, -0.5, 0.5, 2, 99])
    d["_h2hgap"] = d["h2h_total_wmean"] - d["expected"]
    add_bins("h2h mean total - our expected", "_h2hgap", [-99, -4, -1, 1, 4, 99])
    # 9. referee
    add_bins("referee shots deviation (UK only)", "ref_shots_dev", [-99, -1, -0.3, 0.3, 1, 99])
    # 10. league drift
    add_quint("league drift last 28 days", "league_recent28_drift")
    add_quint("league level drift vs previous season", "league_level_drift")
    # model level (known, for reference)
    add_bins("our expected", "expected", [0, 20, 22, 24, 26, 28, 99])
    S["all"] = np.ones(len(d), bool)
    return S


def run(preds: Path) -> dict:
    cond = conditioning_frame()
    teams = cond[["match_id", "home_team_id", "away_team_id"]]
    # trailing competition mean of market total goals (300 matches strictly before the date), for the gap
    cm = cond.merge(pd.read_parquet(PROC / "matches.parquet", columns=["match_id", "competition"]), on="match_id")
    cm = cm.sort_values(["date", "match_id"])
    out = {}
    for route, (label, variant) in ROUTES.items():
        d = load_route(preds, label, variant)
        d = d.merge(cond.drop(columns=["date"]), on="match_id", how="left", validate="one_to_one")
        d = d.merge(residual_momentum(d, teams), on="match_id", how="left")
        # market goals relative to the competition's trailing level vs our expected relative to the league level
        mg = cm[["match_id", "competition", "date", "mktx_total_goals"]].copy()
        daily = mg.groupby(["competition", "date"])["mktx_total_goals"].agg(["sum", "count"]).reset_index()
        daily = daily.sort_values(["competition", "date"])
        daily["csum"] = daily.groupby("competition")["sum"].transform(lambda s: s.rolling(60, min_periods=10).sum().shift(1))
        daily["ccnt"] = daily.groupby("competition")["count"].transform(lambda s: s.rolling(60, min_periods=10).sum().shift(1))
        daily["mg_level"] = daily["csum"] / daily["ccnt"]
        d = d.merge(daily[["competition", "date", "mg_level"]], on=["competition", "date"], how="left")
        d["_mkt_gap"] = np.log(d["mktx_total_goals"] / d["mg_level"]) - np.log(d["expected"] / (2 * d["league_prior_shots"]))
        boot = DayBoot(d["date"].to_numpy())
        one = np.ones(len(d))
        all_mask = np.ones(len(d), bool)
        nb = boot.sums(one, all_mask)
        base = {}
        for k in ("over5", "over6", "under5"):
            base[k] = float(d[k].mean())
            base[f"{k}_boot"] = boot.sums(d[k].to_numpy(float), all_mask) / nb
        segs = segments(d)
        res = {name: segment_stats(d, mask, boot, base) for name, mask in segs.items()}
        comp = {c: segment_stats(d, (d["competition"] == c).to_numpy(), boot, base)
                for c in sorted(d["competition"].unique())}
        out[route] = {"base": {k: v for k, v in base.items() if not k.endswith("_boot")}, "segments": res,
                      "competitions": comp, "n": len(d)}
        log.info("%s: %d rows, over5 %.3f", route, len(d), base["over5"])
    return out


# --------------------------------------------------------------------------- candidate evaluation

SLICES = {"all 2019/20-2025/26": SEASONS, "discovery 2019/20-2022/23": DISCOVERY,
          "confirmation 2023/24-2025/26": CONFIRMATION, "2025/26": SEASONS[-1:]}
KEY_SEGMENTS = ("last 3 matches (either team)", "dead rubber (both teams nothing to play for)", "one dead team",
                "h2h mean total - our expected: [4, 99)", "h2h mean total - our expected: [1, 4)",
                "rating-residual momentum (total - EKF exp), last 3: Q1",
                "rating-residual momentum (total - EKF exp), last 3: Q5",
                "goals in both teams' last 5 (for + against): Q1", "goals in both teams' last 5 (for + against): Q5",
                "shot-volume trend log(l5 / l10), both teams: Q5", "promoted team involved",
                "league level drift vs previous season: Q1", "league level drift vs previous season: Q5",
                "strong home vs weak away (rating gap top decile, home better)",
                "our expected: [0, 20)", "our expected: [28, 99)")


def high_threshold(df: pd.DataFrame, line: int = 24, min_prob: float = 0.7) -> dict:
    """Calibration of P(total >= line) and the hit rate of confident calls (P >= min_prob), as the scan selects."""
    F = df[[f"cdf_{k}" for k in range(81)]].to_numpy(float)
    p = 1.0 - F[:, line - 1]
    y = (df["actual"].to_numpy(float) >= line).astype(float)
    out = {"stated": float(p.mean()), "realised": float(y.mean())}
    sel = p >= min_prob
    out["calls"] = {"n": int(sel.sum()), "stated": float(p[sel].mean()) if sel.any() else None,
                    "hit": float(y[sel].mean()) if sel.any() else None}
    bins = [0, .3, .5, .6, .7, .8, .9, 1.01]
    b = np.digitize(p, bins) - 1
    out["bins"] = {f"[{bins[i]:.1f}, {min(bins[i + 1], 1):.1f})": [int((b == i).sum()), float(p[b == i].mean()),
                                                                    float(y[b == i].mean())]
                   for i in range(len(bins) - 1) if (b == i).sum() >= 30}
    return out


def compare_candidates(cache: Path, pairs, refit: str, team_avg: Path | None = None) -> dict:
    """``pairs``: (candidate label, reference label, variant). Scores per docs/evaluation-protocol.md."""
    from shots.evaluation import scoring as sc
    from shots.models.screen import load

    team = pd.read_parquet(team_avg) if team_avg and Path(team_avg).exists() else None
    out = {}
    for cand, ref, variant in pairs:
        a = load(cache, [cand], [variant], SEASONS, refit)
        b = load(cache, [ref], [variant], SEASONS, refit)
        r: dict = {}
        for name, seas in SLICES.items():
            x, y = a[a.season.isin(seas)], b[b.season.isin(seas)]
            ids = sorted(set(x.match_id) & set(y.match_id))
            if not ids:
                continue
            x = x.set_index("match_id").loc[ids].reset_index()
            y = y.set_index("match_id").loc[ids].reset_index()
            s = {"n": len(x), "cand": {}, "ref": {}}
            for lab, d in (("cand", x), ("ref", y)):
                sc_ = sc.score(d)
                s[lab] = {k: sc_[k] for k in ("rps", "logs", "pin_mean", "mae", "cov_70", "cov_80", "cov_90",
                                             "mid_70", "mid_80", "mid_90", "rel_ece_pp", "upper_tail_gap_pp")}
                comp = sc.score_by(d, "competition")
                s[lab]["worst_cov"] = {q: [float(comp[f"cov_{q}"].min()), str(comp[f"cov_{q}"].idxmin())]
                                       for q in (70, 80, 90)}
                s[lab]["cells_below_q"] = int(sum((comp[f"cov_{q}"] < q / 100).sum() for q in (70, 80, 90)))
                s[lab]["p24"] = high_threshold(d)
                s[lab]["over5"] = float((d["actual"] - d["median_curve"] >= 5).mean())
            s["delta"] = sc.compare(x, y)
            for metric in ("rps", "pin_mean"):
                g = sc.competition_guard(x, y, metric)
                s[f"guard_{metric}"] = {"worse": g.index[g.worse].tolist(), "better": int(g.better.sum()),
                                        "n": len(g), "max": [float(g["diff"].max()), str(g["diff"].idxmax())]}
            if team is not None:
                t = team.set_index("match_id").reindex(ids)
                if t["actual"].notna().all():
                    yy = t["actual"].to_numpy(float)
                    pin = []
                    for q in (70, 80, 90):
                        u = yy - t[f"lb_{q}"].to_numpy(float)
                        tau = 1 - q / 100
                        pin.append(np.maximum(tau * u, (tau - 1) * u).mean())
                    s["team_avg_pin"] = float(np.mean(pin))
            r[name] = s
        out[f"{cand} vs {ref} ({variant}, {refit})"] = r
        log.info("%s vs %s %s: dRPS %s", cand, ref, variant, next(iter(r.values()))["delta"]["rps"])
    return out


def segment_check(cache: Path, labels, variant: str, refit: str) -> dict:
    """Over-shoot excess in the key segments of the study, for each candidate's predictions (did the feature
    absorb the pattern?)."""
    cond = conditioning_frame()
    teams = cond[["match_id", "home_team_id", "away_team_id"]]
    out = {}
    for lab in labels:
        d = load_route(cache, lab, variant, refit)
        d = d.merge(cond.drop(columns=["date"]), on="match_id", how="left", validate="one_to_one")
        d = d.merge(residual_momentum(d, teams), on="match_id", how="left")
        d["_mkt_gap"] = np.nan
        boot = DayBoot(d["date"].to_numpy(), n_boot=300)
        allm = np.ones(len(d), bool)
        nb = boot.sums(np.ones(len(d)), allm)
        base = {}
        for k in ("over5", "over6", "under5"):
            base[k] = float(d[k].mean())
            base[f"{k}_boot"] = boot.sums(d[k].to_numpy(float), allm) / nb
        segs = segments(d)
        out[lab] = {k: segment_stats(d, segs[k], boot, base) for k in KEY_SEGMENTS + ("all",) if k in segs}
    return out


def _round(x, nd: int = 5):
    if isinstance(x, dict):
        return {k: _round(v, nd) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_round(v, nd) for v in x]
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else round(float(x), nd)
    return x


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("study", help="residual study of the production routing")
    st.add_argument("--preds", type=Path, required=True)
    st.add_argument("--out", type=Path, default=ROOT / "reports" / "2026-10-26_overshoot_patterns.json")
    cp = sub.add_parser("compare", help="candidate vs reference per docs/evaluation-protocol.md")
    cp.add_argument("--cache", type=Path, required=True)
    cp.add_argument("--pairs", required=True, help="cand:ref:variant,...")
    cp.add_argument("--refit", default="season")
    cp.add_argument("--segments", action="store_true", help="also re-run the key segments on each candidate")
    cp.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if a.cmd == "compare":
        pairs = [tuple(p.split(":")) for p in a.pairs.split(",")]
        res = {"compare": compare_candidates(a.cache, pairs, a.refit,
                                             PROC / "backtest_predictions_month_team_avg.parquet")}
        if a.segments:
            for variant in sorted({v for _, _, v in pairs}):
                labs = sorted({c for c, _, v in pairs if v == variant} | {r for _, r, v in pairs if v == variant})
                res[f"segments_{variant}"] = segment_check(a.cache, labs, variant, a.refit)
    else:
        res = run(a.preds)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(_round(res), indent=1, default=float))
    log.info("wrote %s", a.out)


if __name__ == "__main__":
    main()
