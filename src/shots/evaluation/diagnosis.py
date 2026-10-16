"""Error decomposition of the production model (reports/2026-10-15_diagnosis.md).

Usage:
    python -m shots.evaluation.diagnosis [--out reports/2026-10-15_diagnosis.json]

Needs ``data/processed/curves_negbin_pit_comp_season.parquet`` (``python -m shots.evaluation.curves``):
season-refit walk-forward curves of ``negbin_pit_comp`` (odds and no-odds variants) and the
climatology reference. Every number here is computed from those out-of-sample curves; nothing is
fitted on a test season except where stated ("discovery" seasons 2019/20-2022/23 fit, "confirmation"
seasons 2023/24-2025/26 evaluate).

Post-match quantities (goals, half-time score, red cards, corners, fouls, cards) are used ONLY to
explain residuals (what is irreducible pre-match), never as features. Pre-match candidates built here
(rolling corners / fouls / cards, referee tendency, AH line, residual-to-date) use strictly earlier
dates only.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from shots.evaluation import scoring as S

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "data" / "processed"
CURVES = PROC / "curves_negbin_pit_comp_season.parquet"
RAW = ROOT / "data" / "raw" / "football-data"
DISCOVERY = ("2019/2020", "2020/2021", "2021/2022", "2022/2023")
CONFIRM = ("2023/2024", "2024/2025", "2025/2026")
COMPLETE = DISCOVERY + CONFIRM
EXTRA_COLS = {"HTHG": "ht_home_goals", "HTAG": "ht_away_goals", "HC": "home_corners", "AC": "away_corners",
              "HF": "home_fouls", "AF": "away_fouls", "HY": "home_yellow", "AY": "away_yellow",
              "HR": "home_red", "AR": "away_red", "Referee": "referee", "Time": "kickoff"}
AH_LINE = ("AHh", "BbAHh")
AH_PRICE_H, AH_PRICE_A = ("AvgAHH", "BbAvAHH"), ("AvgAHA", "BbAvAHA")


# ----------------------------------------------------------------------------- data

def raw_extras(first: int = 2010) -> pd.DataFrame:
    """Unused football-data columns keyed by the canonical match_id (pre-match AH line and prices
    only; closing columns are never read)."""
    from shots.ingest.football_data import DIVISIONS, is_closing_odds_column, read_raw
    from shots.ingest.schema import make_match_id, normalize_team

    frames = []
    for sdir in sorted(RAW.iterdir()):
        if not sdir.is_dir() or not sdir.name.isdigit() or 2000 + int(sdir.name[:2]) < first:
            continue
        start = 2000 + int(sdir.name[:2])
        for path in sorted(sdir.glob("*.csv")):
            div = path.stem
            if div not in DIVISIONS:
                continue
            comp, country, _ = DIVISIONS[div]
            d = read_raw(path).dropna(subset=["HomeTeam", "AwayTeam", "Date"])
            out = pd.DataFrame(index=d.index)
            out["competition"] = f"{country} {comp}"
            out["date"] = pd.to_datetime(d["Date"], dayfirst=True, format="mixed", errors="coerce")
            out["home_team"] = d["HomeTeam"].map(lambda n: normalize_team("football-data", n))
            out["away_team"] = d["AwayTeam"].map(lambda n: normalize_team("football-data", n))
            for src, dst in EXTRA_COLS.items():
                out[dst] = d[src] if src in d else np.nan
            for name, cands in (("ah_line", AH_LINE), ("ah_price_home", AH_PRICE_H), ("ah_price_away", AH_PRICE_A)):
                col = next((c for c in cands if c in d), None)
                assert col is None or not is_closing_odds_column(col), col
                out[name] = pd.to_numeric(d[col], errors="coerce") if col else np.nan
            out = out.dropna(subset=["date"])
            out["match_id"] = [make_match_id(c, dt, h, a) for c, dt, h, a in
                               zip(out["competition"], out["date"], out["home_team"], out["away_team"])]
            frames.append(out)
    ex = pd.concat(frames, ignore_index=True).drop_duplicates("match_id", keep="last")
    for c in EXTRA_COLS.values():
        if c not in ("referee", "kickoff"):
            ex[c] = pd.to_numeric(ex[c], errors="coerce")
    return ex.drop(columns=["competition", "date", "home_team", "away_team"])


def load(curves_path: Path = CURVES) -> dict[str, pd.DataFrame]:
    """Per variant: curves + per-row scores + match context (one frame per variant, same rows)."""
    curves = pd.read_parquet(curves_path)
    feats = pd.read_parquet(PROC / "features.parquet")
    matches = pd.read_parquet(PROC / "matches.parquet")
    ctx_cols = ["match_id", "home_n_prior_season", "away_n_prior_season", "home_rest_days", "away_rest_days",
                "home_n_prior_matches", "away_n_prior_matches", "mkt_p_home", "mkt_p_draw", "mkt_p_away",
                "mkt_p_over25", "league_prior_shots", "home_tier_change", "away_tier_change",
                "days_since_season_start", "home_shots_for_shrunk", "away_shots_for_shrunk",
                "home_shots_against_shrunk", "away_shots_against_shrunk"]
    ctx = feats[ctx_cols].merge(matches[["match_id", "home_goals", "away_goals", "home_sot", "away_sot",
                                         "home_team", "away_team"]], on="match_id", how="left")
    ctx = ctx.merge(raw_extras(), on="match_id", how="left")
    out = {}
    for v, g in curves.groupby("variant"):
        g = g.reset_index(drop=True)
        r = S.row_scores(g)
        keep = [c for c in g.columns if not c.startswith("cdf_")]
        base = g[keep].join(r.drop(columns=[c for c in r.columns if c in keep]))
        base = base.merge(ctx, on="match_id", how="left")
        assert len(base) == len(g)
        base["sd_pred"] = np.sqrt(np.maximum(_var(S.curve_matrix(g)), 1e-9))
        base["resid"] = base["actual"] - base["mean"]
        base["u_mid"] = (base["pit_lo"] + base["pit_hi"]) / 2
        out[v] = (base, S.curve_matrix(g))
    return out


def _var(F: np.ndarray) -> np.ndarray:
    pmf = np.diff(np.hstack([np.zeros((len(F), 1)), F]), axis=1)
    k = np.arange(F.shape[1])
    m = (pmf * k).sum(1)
    return (pmf * k ** 2).sum(1) - m ** 2


# ----------------------------------------------------------------------------- helpers

def day_ci(x, dates, n_boot: int = 400, seed: int = 0):
    return S.block_bootstrap(np.asarray(x, float), dates, n_boot, seed)


def seg_stats(d: pd.DataFrame, ref: pd.DataFrame | None = None) -> dict:
    """Calibration direction and sharpness of one segment. ``ref`` = climatology rows (same index)."""
    n = len(d)
    if n == 0:
        return {"n": 0}
    pit = d["u_mid"].to_numpy() - 0.5
    rand80 = d["rand_80"].to_numpy() - 0.8
    out = {"n": n, "rps": float(d["rps"].mean()), "pin": float(d["pin_mean"].mean()),
           "resid": float(d["resid"].mean()), "resid_se": float(d["resid"].std(ddof=1) / np.sqrt(n)) if n > 1 else np.nan,
           "pit_off": float(pit.mean()), "pit_se": float(pit.std(ddof=1) / np.sqrt(n)) if n > 1 else np.nan,
           "rand80_gap_pp": 100 * float(rand80.mean()),
           "cov80": float(d["cov_80"].mean()), "stated80": float(d["stated_80"].mean()),
           "ucov80": float(d["ucov_80"].mean()),
           "z_var": float((d["resid"] / d["sd_pred"]).var(ddof=1)) if n > 1 else np.nan}
    if ref is not None:
        out["rps_clim"] = float(ref.loc[d.index, "rps"].mean())
        out["skill"] = 1 - out["rps"] / out["rps_clim"]
    return out


def shift_cdf(F: np.ndarray, delta: np.ndarray) -> np.ndarray:
    """Location-shifted curve: F'(k) = F(k - delta) with linear interpolation (delta per row)."""
    K = F.shape[1]
    k = np.arange(K)[None, :] - np.asarray(delta, float)[:, None]
    lo = np.floor(k).astype(int)
    w = k - lo
    def at(j):
        jj = np.clip(j, 0, K - 1)
        v = np.take_along_axis(F, jj, axis=1)
        return np.where(j < 0, 0.0, np.where(j >= K, 1.0, v))
    out = (1 - w) * at(lo) + w * at(lo + 1)
    return np.maximum.accumulate(np.clip(out, 0, 1), axis=1)


def spread_cdf(F: np.ndarray, mean: np.ndarray, s: np.ndarray) -> np.ndarray:
    """Curve widened (s > 1) or narrowed around its mean: F'(k) = F(mean + (k - mean) / s)."""
    K = F.shape[1]
    mean = np.asarray(mean, float)[:, None]
    x = mean + (np.arange(K)[None, :] - mean) / np.asarray(s, float)[:, None]
    lo = np.floor(x).astype(int)
    w = x - lo

    def at(j):
        v = np.take_along_axis(F, np.clip(j, 0, K - 1), axis=1)
        return np.where(j < 0, 0.0, np.where(j >= K, 1.0, v))
    return np.maximum.accumulate(np.clip((1 - w) * at(lo) + w * at(lo + 1), 0, 1), axis=1)


def logs_of(F: np.ndarray, y: np.ndarray) -> np.ndarray:
    i = np.arange(len(y))
    p = F[i, y] - np.where(y > 0, F[i, np.maximum(y - 1, 0)], 0.0)
    return -np.log(np.maximum(p, S.LOG_FLOOR))


def rps_of(F: np.ndarray, y: np.ndarray) -> np.ndarray:
    ks = np.arange(F.shape[1])[None, :]
    return ((F - (y[:, None] <= ks)) ** 2).sum(axis=1)


def gain_from_shift(d: pd.DataFrame, F: np.ndarray, delta: np.ndarray, mask: np.ndarray) -> dict:
    """RPS change on rows ``mask`` when the curve is shifted by ``delta`` (negative = better)."""
    y = d["actual"].to_numpy().astype(int)
    base = d["rps"].to_numpy()[mask]
    new = rps_of(shift_cdf(F[mask], delta[mask]), y[mask])
    m, lo, hi = day_ci(new - base, d.loc[mask, "date"])
    return {"n": int(mask.sum()), "d_rps": m, "lo": lo, "hi": hi, "rel_pct": 100 * m / base.mean()}


def ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    return np.linalg.lstsq(X1, y, rcond=None)[0]


def ols_pred(beta, X):
    return np.column_stack([np.ones(len(X)), X]) @ beta


# ----------------------------------------------------------------------------- sections

def headline(V: dict) -> dict:
    out = {}
    clim = V["clim"][0]
    for v, (d, _) in V.items():
        for part, m in (("complete", d["season"].isin(COMPLETE)), ("2026/27", d["season"] == "2026/2027")):
            g = d[m]
            row = {"n": int(m.sum()), "rps": float(g["rps"].mean()), "logs": float(g["logs"].mean()),
                   "pin_mean": float(g["pin_mean"].mean()), "mae": float(g["abs_err"].mean()),
                   "bias": float(g["err"].mean()), "mean_bias": float(g["mean_err"].mean()),
                   "rmse_mean": float(np.sqrt((g["mean_err"] ** 2).mean()))}
            for q in (70, 80, 90):
                row[f"cov_{q}"] = float(g[f"cov_{q}"].mean())
                row[f"mid_{q}"] = float((g[f"cov_{q}"].mean() + g[f"strict_{q}"].mean()) / 2)
                row[f"rand_{q}"] = float(g[f"rand_{q}"].mean())
                row[f"ucov_{q}"] = float(g[f"ucov_{q}"].mean())
            row["skill_vs_clim"] = 1 - row["rps"] / float(clim.loc[m, "rps"].mean())
            out[f"{v}/{part}"] = row
    # odds vs no-odds paired
    d_o, d_n = V["odds"][0], V["noodds"][0]
    m = d_o["season"].isin(COMPLETE).to_numpy()
    out["odds_minus_noodds_rps"] = dict(zip(("diff", "lo", "hi"), day_ci(
        d_o.loc[m, "rps"].to_numpy() - d_n.loc[m, "rps"].to_numpy(), d_o.loc[m, "date"])))
    out["odds_minus_clim_rps"] = dict(zip(("diff", "lo", "hi"), day_ci(
        d_o.loc[m, "rps"].to_numpy() - V["clim"][0].loc[m, "rps"].to_numpy(), d_o.loc[m, "date"])))
    return out


def ceiling(V: dict) -> dict:
    """Variance budget of total shots (complete seasons, odds variant)."""
    d, F = V["odds"]
    m = d["season"].isin(COMPLETE).to_numpy()
    g = d[m]
    y, mu = g["actual"].to_numpy(), g["mean"].to_numpy()
    # within competition-season variance (the climatology already knows league level)
    within = g.groupby(["competition", "season"])["actual"].transform(lambda s: s - s.mean())
    var_y = float(y.var())
    var_within = float(within.var())
    mse = float(((y - mu) ** 2).mean())
    pred_var = float((g["sd_pred"] ** 2).mean())
    pois = float(mu.mean())
    # home / away components
    mh, ma = g["mu_home"].to_numpy(), g["mu_away"].to_numpy()
    rh, ra = g["home_actual"].to_numpy() - mh, g["away_actual"].to_numpy() - ma
    out = {"n": int(m.sum()), "var_total": var_y, "var_within_comp_season": var_within, "mse_mean": mse,
           "r2_total": 1 - mse / var_y, "r2_within": 1 - mse / var_within,
           "mean_pred_var": pred_var, "poisson_floor": pois,
           "share_explained_of_explainable_if_poisson": (var_within - mse) / (var_within - pois),
           "raw_nb_side_mse": {"home": float((rh ** 2).mean()), "away": float((ra ** 2).mean()),
                               "cov": float((rh * ra).mean()), "corr": float(np.corrcoef(rh, ra)[0, 1])},
           "mean_mu_home": float(mh.mean()), "mean_mu_away": float(ma.mean())}
    # Gaussian link between MSE and RPS: E[CRPS] = sigma / sqrt(pi) for a calibrated normal
    out["rps_obs"] = float(g["rps"].mean())
    out["rps_gauss_approx"] = float(np.sqrt(mse) / np.sqrt(np.pi))
    out["rps_if_poisson_floor"] = float(np.mean(np.sqrt(mu)) / np.sqrt(np.pi))
    return out


def team_season_oracle(V: dict) -> dict:
    """Upper bound on what better team ratings could give: split-half within each competition-season,
    estimate each team's attack (shots for - mu) and defence (shots against - mu) residual on odd-
    numbered rounds, apply on even ones (and vice versa). The residual variance reduction measures
    persistent within-season team signal the model has not absorbed. Also the in-sample R2 (upper)."""
    d, F = V["odds"]
    d = d[d["season"].isin(COMPLETE)].copy()
    d = d.sort_values("date")
    d["half"] = d.groupby(["competition", "season"]).cumcount() % 2
    rh = d["home_actual"] - d["mu_home"]
    ra = d["away_actual"] - d["mu_away"]
    long = pd.concat([
        pd.DataFrame({"idx": d.index, "team": d["home_team_id"], "opp": d["away_team_id"], "side": "home",
                      "r": rh, "season": d["season"], "half": d["half"]}),
        pd.DataFrame({"idx": d.index, "team": d["away_team_id"], "opp": d["home_team_id"], "side": "away",
                      "r": ra, "season": d["season"], "half": d["half"]})])
    res = {}
    for k in (0, 5, 10, 20):
        pred = pd.Series(0.0, index=long.index)
        parts = []
        for h in (0, 1):
            tr = long[long["half"] == h]
            te = long[long["half"] != h].copy()
            att = tr.groupby(["season", "team"])["r"].agg(["sum", "count"])
            dfn = tr.groupby(["season", "opp"])["r"].agg(["sum", "count"])
            a = (att["sum"] / (att["count"] + k)).rename("a")
            b = (dfn["sum"] / (dfn["count"] + k)).rename("b")
            te = te.join(a, on=["season", "team"]).join(b, on=["season", "opp"])
            te["p"] = te["a"].fillna(0) + te["b"].fillna(0)
            parts.append(te)
        te = pd.concat(parts)
        mse0 = float((te["r"] ** 2).mean())
        mse1 = float(((te["r"] - te["p"]) ** 2).mean())
        res[f"k{k}"] = {"side_mse_before": mse0, "side_mse_after": mse1, "reduction_pct": 100 * (1 - mse1 / mse0)}
    return res


def team_persistence(V: dict) -> dict:
    """Per team-season mean side residuals (attack: own shots - mu; defence: conceded - mu of the
    opponent side). Correlation season s -> s+1 (same team, same competition, >= 15 matches each),
    and within-season first half -> second half."""
    d, _ = V["odds"]
    d = d[d["season"].isin(COMPLETE)].copy()
    rh = d["home_actual"] - d["mu_home"]
    ra = d["away_actual"] - d["mu_away"]
    long = pd.concat([
        pd.DataFrame({"team": d["home_team_id"], "comp": d["competition"], "season": d["season"], "date": d["date"],
                      "att": rh, "dfn": ra, "tot": d["resid"]}),
        pd.DataFrame({"team": d["away_team_id"], "comp": d["competition"], "season": d["season"], "date": d["date"],
                      "att": ra, "dfn": rh, "tot": d["resid"]})])
    out = {}
    ts = long.groupby(["team", "season"]).agg(att=("att", "mean"), dfn=("dfn", "mean"), tot=("tot", "mean"),
                                               n=("att", "size"), comp=("comp", "first")).reset_index()
    ts = ts[ts["n"] >= 15]
    seasons = sorted(ts["season"].unique())
    nxt = {s: seasons[i + 1] for i, s in enumerate(seasons[:-1])}
    ts["next"] = ts["season"].map(nxt)
    pair = ts.merge(ts, left_on=["team", "next", "comp"], right_on=["team", "season", "comp"], suffixes=("", "_n"))
    for c in ("att", "dfn", "tot"):
        out[f"season_to_next_corr_{c}"] = float(np.corrcoef(pair[c], pair[f"{c}_n"])[0, 1])
        out[f"season_sd_{c}"] = float(ts[c].std())
    out["n_pairs"] = int(len(pair))
    # sampling noise of a team-season mean: sd of side residual / sqrt(n)
    out["side_resid_sd"] = float(long["att"].std())
    out["expected_noise_sd_of_mean"] = float(long["att"].std() / np.sqrt(ts["n"].mean()))
    # within season halves
    long = long.sort_values("date")
    long["k"] = long.groupby(["team", "season"]).cumcount()
    long["nn"] = long.groupby(["team", "season"])["k"].transform("size")
    long["h"] = (long["k"] >= long["nn"] / 2).astype(int)
    hh = long[long["nn"] >= 20].groupby(["team", "season", "h"])[["att", "dfn", "tot"]].mean().unstack("h")
    for c in ("att", "dfn", "tot"):
        out[f"half_to_half_corr_{c}"] = float(np.corrcoef(hh[(c, 0)], hh[(c, 1)])[0, 1])
    # persistent teams: biggest |att| in both seasons (same sign)
    pair["both"] = pair["att"] * pair["att_n"]
    top = pair[(pair["both"] > 0)].assign(m=lambda x: (x["att"] + x["att_n"]) / 2).sort_values("m")
    out["examples_low_att"] = top.head(8)[["team", "season", "att", "att_n"]].round(2).to_dict("records")
    out["examples_high_att"] = top.tail(8)[["team", "season", "att", "att_n"]].round(2).to_dict("records")
    return out


def calibration_slope(V: dict) -> dict:
    """Regression of actual on predicted mean (total and per side), discovery vs confirmation.
    slope > 1: the model's means are too compressed (extremes under-predicted)."""
    d, _ = V["odds"]
    out = {}
    for name, seasons in (("discovery", DISCOVERY), ("confirm", CONFIRM)):
        g = d[d["season"].isin(seasons)]
        for tgt, pred in (("total", "mean"), ("home", "mu_home"), ("away", "mu_away")):
            y = g["actual" if tgt == "total" else f"{tgt}_actual"].to_numpy()
            x = g[pred].to_numpy()
            # within competition-season (removes the league level the model also knows)
            gx = x - g.groupby(["competition", "season"])[pred].transform("mean").to_numpy()
            gy = y - g.groupby(["competition", "season"])["actual" if tgt == "total" else f"{tgt}_actual"].transform("mean").to_numpy()
            b = ols(x[:, None], y)
            bw = ols(gx[:, None], gy)
            out[f"{name}/{tgt}"] = {"slope": float(b[1]), "intercept": float(b[0]), "slope_within": float(bw[1])}
    # binned
    g = d[d["season"].isin(COMPLETE)]
    bins = pd.qcut(g["mean"], 10)
    t = g.groupby(bins, observed=True).agg(n=("actual", "size"), pred=("mean", "mean"), act=("actual", "mean"),
                                           pit=("u_mid", "mean"), rand80=("rand_80", "mean"), ucov80=("ucov_80", "mean"),
                                           zvar=("resid", lambda s: float((s / g.loc[s.index, "sd_pred"]).var())))
    out["deciles"] = t.reset_index(drop=True).round(4).to_dict("records")
    return out


def segments(d: pd.DataFrame) -> dict[str, pd.Series]:
    """Candidate segment definitions (pre-match only unless noted 'post')."""
    fav = d[["mkt_p_home", "mkt_p_away"]].max(axis=1)
    close = (d["mkt_p_home"] - d["mkt_p_away"]).abs()
    phase = np.minimum(d["home_n_prior_season"].fillna(0), d["away_n_prior_season"].fillna(0))
    rest = np.minimum(d["home_rest_days"].fillna(99), d["away_rest_days"].fillna(99))
    month = d["date"].dt.month
    wd = d["date"].dt.dayofweek
    seg = {
        "competition": d["competition"],
        "tier": d["tier"].astype(str),
        "phase": pd.cut(phase, [-1, 4, 9, 19, 99], labels=["md1-5", "md6-10", "md11-20", "md21+"]).astype(str),
        "month": month.astype(str),
        "midweek": np.where(wd.isin([1, 2, 3]), "Tue-Thu", "other"),
        "fav_strength": pd.cut(fav, [0, .4, .5, .6, .7, .8, 1], labels=["<.4", ".4-.5", ".5-.6", ".6-.7", ".7-.8", ">.8"]).astype(str),
        "home_fav": np.where(d["mkt_p_home"] >= d["mkt_p_away"], "home fav", "away fav"),
        "closeness": pd.cut(close, [-.01, .1, .25, .45, 1], labels=["<.1", ".1-.25", ".25-.45", ">.45"]).astype(str),
        "p_over25": pd.cut(d["mkt_p_over25"], [0, .45, .5, .55, .6, .65, 1], labels=["<.45", ".45-.5", ".5-.55", ".55-.6", ".6-.65", ">.65"]).astype(str),
        "pred_level": pd.cut(d["mean"], [0, 20, 22, 24, 26, 28, 99], labels=["<20", "20-22", "22-24", "24-26", "26-28", "28+"]).astype(str),
        "min_rest": pd.cut(rest, [-1, 3, 5, 8, 14, 1000], labels=["<=3", "4-5", "6-8", "9-14", "15+"]).astype(str),
        "rest_diff": pd.cut(d["home_rest_days"].fillna(7) - d["away_rest_days"].fillna(7), [-99, -3, -1, 1, 3, 99],
                            labels=["away+3", "away+2", "even", "home+2", "home+3"]).astype(str),
        "promoted": np.where((d["home_tier_change"].fillna(0) < 0) | (d["away_tier_change"].fillna(0) < 0), "promoted side",
                             np.where((d["home_tier_change"].fillna(0) > 0) | (d["away_tier_change"].fillna(0) > 0), "relegated side", "none")),
    }
    lp = d["league_prior_shots"] * 2
    seg["league_level"] = pd.cut(lp, [0, 22, 24, 26, 99], labels=["<22", "22-24", "24-26", "26+"]).astype(str)
    seg["comp_x_phase"] = d["competition"].astype(str) + " | " + seg["phase"]
    seg["fav_x_level"] = seg["fav_strength"] + " | " + seg["pred_level"]
    seg["tier_x_phase"] = seg["tier"] + " | " + seg["phase"]
    seg["comp_x_fav"] = d["competition"].astype(str) + " | " + np.where(fav >= 0.6, "fav>=.6", "fav<.6")
    return {k: pd.Series(np.asarray(v), index=d.index) for k, v in seg.items()}


def segment_tables(V: dict, variant: str = "odds") -> dict:
    d, _ = V[variant]
    clim = V["clim"][0]
    segs = segments(d)
    out = {}
    for name, s in segs.items():
        rows = {}
        for key, idx in d.groupby(s.to_numpy()).groups.items():
            g = d.loc[idx]
            if len(g) < 150:
                continue
            row = seg_stats(g[g["season"].isin(COMPLETE)], clim)
            row["disc"] = seg_stats(g[g["season"].isin(DISCOVERY)])
            row["conf"] = seg_stats(g[g["season"].isin(CONFIRM)])
            row["s2627"] = seg_stats(g[g["season"] == "2026/2027"])
            rows[str(key)] = row
        out[name] = rows
    return out


def edges(tables: dict, z_disc: float = 3.0, z_conf: float = 2.0) -> list[dict]:
    """Segments miscalibrated in a direction: discovery |z| >= z_disc on the mean mid-PIT offset (or the
    mean residual), confirmation same sign with z >= z_conf. The z uses the iid SE inflated by 1.3 for
    same-day dependence (see report)."""
    infl = 1.3
    found = []
    for name, rows in tables.items():
        for key, r in rows.items():
            dd, cc = r["disc"], r["conf"]
            if dd.get("n", 0) < 200 or cc.get("n", 0) < 150:
                continue
            for stat, se in (("pit_off", "pit_se"), ("resid", "resid_se")):
                zd = dd[stat] / (dd[se] * infl)
                zc = cc[stat] / (cc[se] * infl)
                if abs(zd) >= z_disc:
                    found.append({"segment": name, "value": key, "stat": stat, "disc": dd[stat], "z_disc": zd,
                                  "conf": cc[stat], "z_conf": zc, "n_disc": dd["n"], "n_conf": cc["n"],
                                  "rand80_gap_disc": dd["rand80_gap_pp"], "rand80_gap_conf": cc["rand80_gap_pp"],
                                  "s2627": r["s2627"].get(stat), "n_2627": r["s2627"].get("n", 0),
                                  "confirmed": bool(np.sign(zd) == np.sign(zc) and abs(zc) >= z_conf)})
    return found


def tails(V: dict) -> dict:
    d, F = V["odds"]
    m = d["season"].isin(COMPLETE).to_numpy()
    g, Fm = d[m], F[m]
    y = g["actual"].to_numpy()
    out = {}
    for name, (lo, hi) in {"le11": (None, 11), "le12": (None, 12), "ge40": (40, None), "ge36": (36, None)}.items():
        if hi is not None:
            p = Fm[:, hi]
            hit = y <= hi
        else:
            p = 1 - Fm[:, lo - 1]
            hit = y >= lo
        from sklearn.metrics import roc_auc_score
        bins = pd.qcut(p, 5, duplicates="drop")
        rel = pd.DataFrame({"p": p, "hit": hit}).groupby(bins, observed=True).agg(n=("hit", "size"),
                                                                                 stated=("p", "mean"),
                                                                                 realised=("hit", "mean"))
        out[name] = {"n_events": int(hit.sum()), "rate": float(hit.mean()), "stated_mean": float(p.mean()),
                     "auc": float(roc_auc_score(hit, p)), "by_quintile": rel.round(4).reset_index(drop=True).to_dict("records")}
        # post-match: red cards and goals among the events vs others
        ev = g[hit]
        out[name]["red_card_share"] = float(((ev["home_red"].fillna(0) + ev["away_red"].fillna(0)) > 0).mean())
        out[name]["red_card_share_all"] = float(((g["home_red"].fillna(0) + g["away_red"].fillna(0)) > 0).mean())
        out[name]["mean_goals"] = float((ev["home_goals"] + ev["away_goals"]).mean())
        out[name]["mean_abs_gd"] = float((ev["home_goals"] - ev["away_goals"]).abs().mean())
        out[name]["mean_abs_gd_all"] = float((g["home_goals"] - g["away_goals"]).abs().mean())
        out[name]["by_competition_rate_vs_stated"] = (
            pd.DataFrame({"c": g["competition"], "p": p, "hit": hit}).groupby("c")[["p", "hit"]].mean()
            .assign(gap_pp=lambda t: 100 * (t["hit"] - t["p"])).round(4).to_dict("index"))
    return out


def postmatch(V: dict) -> dict:
    """How much residual variance is explained by post-match events (irreducible pre-match):
    red cards, goal-state path (half-time and full-time score), and, for reference, corners/fouls.
    Cell means are fitted on discovery seasons and evaluated on confirmation seasons (out of sample)."""
    d, _ = V["odds"]
    d = d[d["season"].isin(COMPLETE)].copy()
    d["red_h"] = d["home_red"].fillna(0).clip(0, 2)
    d["red_a"] = d["away_red"].fillna(0).clip(0, 2)
    d["gd_ft"] = (d["home_goals"] - d["away_goals"]).clip(-3, 3)
    d["gd_ht"] = (d["ht_home_goals"] - d["ht_away_goals"]).clip(-2, 2)
    d["g_ft"] = (d["home_goals"] + d["away_goals"]).clip(0, 6)
    d["g_2h"] = (d["g_ft"] - (d["ht_home_goals"] + d["ht_away_goals"])).clip(0, 4)
    # expected goal difference from the market: who was "supposed" to lead
    d["fav_side"] = np.sign(d["mkt_p_home"] - d["mkt_p_away"]).fillna(0)
    d["surprise"] = np.sign(d["gd_ft"]) * d["fav_side"]   # +1 favourite won, -1 underdog won
    tr = d["season"].isin(DISCOVERY)
    te = ~tr
    base = float((d.loc[te, "resid"] ** 2).mean())

    def cells(cols, target="resid", k=20):
        key_tr = d.loc[tr, cols].astype(str).agg("|".join, axis=1)
        key_te = d.loc[te, cols].astype(str).agg("|".join, axis=1)
        st = d.loc[tr, target].groupby(key_tr).agg(["sum", "count"])
        mean = st["sum"] / (st["count"] + k)
        pred = key_te.map(mean).fillna(0).to_numpy()
        r = d.loc[te, target].to_numpy()
        mse = float(((r - pred) ** 2).mean())
        return {"cells": int(len(st)), "mse": mse, "explained_pct": 100 * (1 - mse / float((r ** 2).mean()))}

    out = {"resid_mse_confirm": base}
    out["red_cards"] = cells(["red_h", "red_a"])
    out["ft_score"] = cells(["gd_ft", "g_ft"])
    out["ht_and_ft"] = cells(["gd_ht", "gd_ft", "g_ft"])
    out["goal_path+fav+red"] = cells(["gd_ht", "gd_ft", "g_ft", "surprise", "red_h", "red_a"])
    # per side: trailing teams shoot more?
    for side, other in (("home", "away"), ("away", "home")):
        d[f"r_{side}"] = d[f"{side}_actual"] - d[f"mu_{side}"]
    d["gd_ft_h"] = d["gd_ft"]
    t = d.groupby("gd_ft")[["r_home", "r_away", "resid"]].mean().round(3)
    out["side_resid_by_ft_gd"] = t.to_dict("index")
    t = d.groupby("g_ft")[["resid"]].agg(["mean", "size"]).round(3)
    out["resid_by_total_goals"] = {int(k): {"mean": float(v[("resid", "mean")]), "n": int(v[("resid", "size")])} for k, v in t.iterrows()}
    t = d.groupby(["red_h", "red_a"])[["r_home", "r_away", "resid"]].agg(["mean"]).round(3)
    t.columns = ["r_home", "r_away", "resid"]
    t["n"] = d.groupby(["red_h", "red_a"]).size()
    out["resid_by_red"] = {f"{int(a)}-{int(b)}": v for (a, b), v in t.to_dict("index").items()}
    # corners / fouls (post-match, how tied to shots)
    for c in ("corners", "fouls", "yellow"):
        tot = d[f"home_{c}"] + d[f"away_{c}"]
        ok = tot.notna()
        out[f"corr_resid_{c}_postmatch"] = float(np.corrcoef(d.loc[ok, "resid"], tot[ok])[0, 1])
    # theoretical: residual variance not explainable even with perfect mean = mean predicted var
    out["mean_pred_var_confirm"] = float((d.loc[te, "sd_pred"] ** 2).mean())
    return out


def _rolling_team(df: pd.DataFrame, cols_for: dict, halflife: float = 5.0) -> pd.DataFrame:
    """EWM over each team's strictly-prior matches (one match per team per date, audited) of
    the given (home_col, away_col) pairs, as 'for' and 'against'. Returns home_/away_ prefixed columns."""
    recs = []
    for side, other in (("home", "away"), ("away", "home")):
        r = pd.DataFrame({"match_id": df["match_id"], "date": df["date"], "team": df[f"{side}_team_id"], "side": side})
        for name, (h, a) in cols_for.items():
            fc, ac = (h, a) if side == "home" else (a, h)
            r[f"{name}_for"] = df[fc].to_numpy()
            r[f"{name}_against"] = df[ac].to_numpy()
        recs.append(r)
    long = pd.concat(recs).sort_values(["team", "date"], kind="mergesort")
    vals = [c for c in long.columns if c.endswith("_for") or c.endswith("_against")]
    g = long.groupby("team", sort=False)
    for c in vals:
        shifted = g[c].shift(1)
        long[f"pre_{c}"] = (shifted.groupby(long["team"], sort=False).ewm(halflife=halflife, ignore_na=True).mean()
                            .reset_index(level=0, drop=True))
    long["prev_date"] = g["date"].shift(1)
    assert (long["prev_date"].isna() | (long["prev_date"] < long["date"])).all(), "same-day duplicate team rows"
    out = {}
    for side in ("home", "away"):
        s = long[long["side"] == side].set_index("match_id")
        for c in vals:
            out[f"{side}_pre_{c}"] = s[f"pre_{c}"]
    return pd.DataFrame(out)


def _elo(matches: pd.DataFrame, k: float = 20.0, home_adv: float = 60.0) -> pd.DataFrame:
    """Goal-based Elo, pre-match ratings only (ratings change after each date; same-day matches see
    the ratings from before that date). New teams start at 1500."""
    m = matches.sort_values("date")
    rating: dict[str, float] = {}
    out_h, out_a, ids = [], [], []
    for _, g in m.groupby("date", sort=True):
        upd: dict[str, float] = {}
        for row in g.itertuples(index=False):
            rh = rating.get(row.home_team_id, 1500.0)
            ra = rating.get(row.away_team_id, 1500.0)
            out_h.append(rh)
            out_a.append(ra)
            ids.append(row.match_id)
            e = 1 / (1 + 10 ** (-(rh + home_adv - ra) / 400))
            gd = row.home_goals - row.away_goals
            sc = 1.0 if gd > 0 else 0.5 if gd == 0 else 0.0
            mult = np.log1p(abs(gd)) + 1 if gd != 0 else 1.0
            delta = k * mult * (sc - e)
            upd[row.home_team_id] = upd.get(row.home_team_id, 0.0) + delta
            upd[row.away_team_id] = upd.get(row.away_team_id, 0.0) - delta
        for t, dlt in upd.items():
            rating[t] = rating.get(t, 1500.0) + dlt
    return pd.DataFrame({"match_id": ids, "elo_home": out_h, "elo_away": out_a})


def _referee_tendency(allm: pd.DataFrame, k: float = 20.0) -> pd.DataFrame:
    """Per referee (keyed by country + name), shrunk mean of (total shots - 2 x league prior) and of
    excess total fouls over the referee's matches on strictly earlier dates (same-date matches of one
    name, e.g. two officials sharing a name, never see each other)."""
    r = allm.dropna(subset=["referee"]).copy()
    r["ref"] = r["country"].astype(str) + ":" + r["referee"].astype(str).str.strip().str.lower()
    r["dev"] = r["total_shots"] - 2 * r["league_prior_shots"]
    r["fouls"] = r["home_fouls"] + r["away_fouls"]
    r = r.sort_values("date", kind="mergesort")
    lf = r.groupby(["competition", "date"])["fouls"].agg(["sum", "count"]).groupby(level=0).cumsum()
    lf = lf.groupby(level=0).shift(1)
    r = r.join((lf["sum"] / lf["count"]).rename("lf"), on=["competition", "date"])
    r["fdev"] = r["fouls"] - r["lf"]
    daily = r.groupby(["ref", "date"]).agg(dev_s=("dev", "sum"), dev_n=("dev", "count"),
                                           fdev_s=("fdev", "sum"), fdev_n=("fdev", "count"))
    cum = daily.groupby(level=0).cumsum().groupby(level=0).shift(1).fillna(0)
    r = r.join(cum, on=["ref", "date"])
    r["ref_dev"] = r["dev_s"] / (r["dev_n"] + k)
    r["ref_fdev"] = r["fdev_s"] / (r["fdev_n"] + k)
    r["ref_n"] = r["dev_n"]
    return r[["match_id", "ref_dev", "ref_fdev", "ref_n"]]


def candidate_frame(V: dict) -> pd.DataFrame:
    """Pre-match candidate signals per match (all from strictly earlier dates)."""
    feats = pd.read_parquet(PROC / "features.parquet")
    matches = pd.read_parquet(PROC / "matches.parquet")
    allm = matches.merge(raw_extras(), on="match_id", how="left").merge(
        feats[["match_id", "league_prior_shots", "total_shots"]], on="match_id", how="left")
    roll = _rolling_team(allm, {"corners": ("home_corners", "away_corners"), "fouls": ("home_fouls", "away_fouls"),
                                "yellow": ("home_yellow", "away_yellow"), "goals": ("home_goals", "away_goals")})
    elo = _elo(matches)
    ref = _referee_tendency(allm)
    # team residual history from the walk-forward curves (out-of-sample residuals of the odds model)
    d = V["odds"][0]
    rh = d["home_actual"] - d["mu_home"]
    ra = d["away_actual"] - d["mu_away"]
    long = pd.concat([
        pd.DataFrame({"match_id": d["match_id"], "date": d["date"], "season": d["season"], "team": d["home_team_id"],
                      "side": "home", "att": rh, "dfn": ra}),
        pd.DataFrame({"match_id": d["match_id"], "date": d["date"], "season": d["season"], "team": d["away_team_id"],
                      "side": "away", "att": ra, "dfn": rh})]).sort_values(["team", "date"], kind="mergesort")
    g = long.groupby(["team", "season"], sort=False)
    for c in ("att", "dfn"):
        long[f"{c}_sum"] = g[c].transform(lambda x: x.shift(1).cumsum()).fillna(0)
        long[f"{c}_n"] = g[c].transform(lambda x: x.shift(1).notna().cumsum())
    ts = long.groupby(["team", "season"])[["att", "dfn"]].mean()
    seasons = sorted(long["season"].unique())
    prev = {s: seasons[i - 1] for i, s in enumerate(seasons) if i > 0}
    long["prev_season"] = long["season"].map(prev)
    long = long.join(ts.rename(columns={"att": "att_prev", "dfn": "dfn_prev"}), on=["team", "prev_season"])
    out = d[["match_id"]].copy().set_index("match_id")
    for side in ("home", "away"):
        s = long[long["side"] == side].set_index("match_id")
        for c in ("att", "dfn"):
            out[f"{side}_{c}_todate_sum"] = s[f"{c}_sum"]
            out[f"{side}_{c}_todate_n"] = s[f"{c}_n"]
            out[f"{side}_{c}_prev"] = s[f"{c}_prev"]
    out = out.join(roll).join(elo.set_index("match_id")).join(ref.set_index("match_id"))
    # schedule position: matches the team still has to play in this competition-season (the fixture
    # list is known before the season; computed here from the season's played matches)
    sched = pd.concat([feats[["match_id", "season", "competition", "date"]].assign(team=feats["home_team_id"]),
                       feats[["match_id", "season", "competition", "date"]].assign(team=feats["away_team_id"])],
                      ignore_index=True).sort_values("date", kind="mergesort")
    sched["left"] = sched.groupby(["team", "season", "competition"]).cumcount(ascending=False)
    out = out.join(sched.groupby("match_id")["left"].min().rename("matches_left"))
    return out.reset_index()


def _centre(X: np.ndarray) -> np.ndarray:
    X = np.asarray(X, float)
    return np.nan_to_num(X - np.nanmean(X, axis=0))


def _design(d: pd.DataFrame, c: pd.DataFrame, name: str) -> np.ndarray:
    """Match-level design of one candidate (NaN -> column mean)."""
    lvl = (d["mean"] - 2 * d["league_prior_shots"]).to_numpy()
    fav = d[["mkt_p_home", "mkt_p_away"]].max(axis=1)
    if name == "intercept":
        return np.zeros((len(d), 0))
    if name == "month":
        mo = d["date"].dt.month
        return np.column_stack([(mo == m).astype(float) for m in (8, 9, 10, 11, 12, 1, 2, 3, 4, 5)])
    if name == "season_end":
        left = c["matches_left"].fillna(99).to_numpy()
        return np.column_stack([(left == 0).astype(float), ((left >= 1) & (left <= 2)).astype(float)])
    if name == "winter":
        return np.column_stack([d["date"].dt.month.isin([12, 1, 2]).astype(float)])
    if name == "level_slope":
        return np.column_stack([lvl])
    if name == "mismatch":
        dif = (d["mu_home"] - d["mu_away"]).to_numpy()
        return np.column_stack([np.abs(dif), dif, lvl])
    if name == "market_mismatch":
        return _centre(np.column_stack([fav, (d["mkt_p_home"] - d["mkt_p_away"]).abs(), d["mkt_p_over25"]]))
    if name in ("elo", "elo+level"):
        e = (c["elo_home"] - c["elo_away"]).to_numpy() / 100
        cols = [np.abs(e), e] + ([lvl] if name == "elo+level" else [])
        return np.column_stack(cols)
    if name == "corners":
        return _centre(c[["home_pre_corners_for", "home_pre_corners_against", "away_pre_corners_for",
                          "away_pre_corners_against"]].to_numpy(float))
    if name == "fouls_cards":
        return _centre(c[["home_pre_fouls_for", "home_pre_fouls_against", "away_pre_fouls_for",
                          "away_pre_fouls_against", "home_pre_yellow_for", "away_pre_yellow_for"]].to_numpy(float))
    if name == "goals_form":
        return _centre(c[["home_pre_goals_for", "home_pre_goals_against", "away_pre_goals_for",
                          "away_pre_goals_against"]].to_numpy(float))
    if name == "referee":
        has = c["ref_n"].fillna(0).to_numpy() >= 1
        return np.column_stack([np.where(has, c["ref_dev"].fillna(0), 0.0), np.where(has, c["ref_fdev"].fillna(0), 0.0)])
    if name == "ah_line":
        ah = d["ah_line"].fillna(0).to_numpy()
        ph, pa = 1 / d["ah_price_home"], 1 / d["ah_price_away"]
        pah = (ph / (ph + pa)).fillna(0.5).to_numpy()
        return np.column_stack([np.abs(ah), ah, pah - 0.5])
    if name == "resid_todate":
        cols = []
        for side in ("home", "away"):
            for cc in ("att", "dfn"):
                cols.append((c[f"{side}_{cc}_todate_sum"] / (c[f"{side}_{cc}_todate_n"].fillna(0) + 10.0)).fillna(0).to_numpy())
        return np.column_stack(cols)
    if name == "resid_prev_season":
        return np.column_stack([c[f"{s}_{cc}_prev"].fillna(0).to_numpy() for s in ("home", "away") for cc in ("att", "dfn")])
    if name == "division_change":
        cols = []
        for side in ("home", "away"):
            tc = d[f"{side}_tier_change"].fillna(0)
            early = (d[f"{side}_n_prior_season"].fillna(0) < 10).astype(float)
            cols += [(early * (tc < 0)).to_numpy(), (early * (tc > 0)).to_numpy()]
        return np.column_stack(cols)
    raise KeyError(name)


CANDIDATES = ("intercept", "month", "season_end", "winter", "level_slope", "mismatch", "market_mismatch", "elo", "elo+level",
              "corners", "fouls_cards", "goals_form", "referee", "ah_line", "resid_todate", "resid_prev_season",
              "division_change")


def candidate_signals(V: dict, C: pd.DataFrame, variant: str = "odds", candidates=CANDIDATES,
                      mask_fn=None) -> dict:
    """Each candidate: OLS of the total residual (actual - curve mean) on its design, fitted on the
    discovery seasons (plus intercept), applied as a location shift of the curve on the confirmation
    seasons and on 2026/27. Reported: d_rps vs the unshifted curve and vs the intercept-only shift
    (negative = better, day-block CI), residual-MSE change, coefficients. Same rows for every
    candidate. ``mask_fn(d)`` restricts the rows (e.g. referee data available)."""
    d, F = V[variant]
    c = d[["match_id"]].merge(C, on="match_id", how="left")
    assert len(c) == len(d)
    keep = np.ones(len(d), bool) if mask_fn is None else np.asarray(mask_fn(d, c), bool)
    tr = d["season"].isin(DISCOVERY).to_numpy() & keep
    r = d["resid"].to_numpy()
    y = d["actual"].to_numpy().astype(int)
    out, base = {}, {}
    for name in ("intercept", *[x for x in candidates if x != "intercept"]):
        X = np.asarray(_design(d, c, name), float)
        beta = ols(X[tr], r[tr])
        delta = ols_pred(beta, X)
        row = {"coef": [float(b) for b in beta]}
        for part, m in (("confirm", d["season"].isin(CONFIRM).to_numpy() & keep),
                        ("s2627", (d["season"] == "2026/2027").to_numpy() & keep)):
            g = gain_from_shift(d, F, delta, m)
            mse0 = float((r[m] ** 2).mean())
            mse1 = float(((r[m] - delta[m]) ** 2).mean())
            row[part] = {**g, "mse_change_pct": 100 * (mse1 / mse0 - 1)}
            if name == "intercept":
                base[part] = delta
            else:
                new = rps_of(shift_cdf(F[m], delta[m]), y[m])
                ref = rps_of(shift_cdf(F[m], base[part][m]), y[m])
                mm, lo, hi = day_ci(new - ref, d.loc[m, "date"])
                row[part]["vs_intercept"] = {"d_rps": mm, "lo": lo, "hi": hi}
        out[name] = row
    return out


def oracle_level_gains(V: dict, seed: int = 0) -> dict:
    """Hindsight upper bounds: RPS change if every match's curve were shifted by the in-sample mean
    residual of its group (competition, competition-season, competition-month, calendar month, date,
    predicted decile), and the same with group labels permuted (the pure-noise gain of fitting that
    many means). ``net`` = gain - null is the signal a perfect level estimate would recover."""
    rng = np.random.default_rng(seed)
    out = {}
    for var in ("odds", "noodds"):
        d, F = V[var]
        m = d["season"].isin(COMPLETE).to_numpy()
        d, F = d[m], F[m]
        y = d["actual"].to_numpy().astype(int)
        base = float(d["rps"].mean())
        mo = d["date"].dt.to_period("M").astype(str)
        keys = {"competition": d["competition"], "comp_season": d["competition"] + d["season"],
                "comp_month": d["competition"] + mo, "month": mo, "date": d["date"].astype(str),
                "pred_decile": pd.qcut(d["mean"], 10, labels=False).astype(str)}
        for k, key in keys.items():
            delta = d["resid"].groupby(key).transform("mean").to_numpy()
            gain = float(rps_of(shift_cdf(F, delta), y).mean() - base)
            kp = pd.Series(rng.permutation(key.to_numpy()), index=d.index)
            null = float(rps_of(shift_cdf(F, d["resid"].groupby(kp).transform("mean").to_numpy()), y).mean() - base)
            out[f"{var}/{k}"] = {"groups": int(key.nunique()), "gain": gain, "null": null, "net": gain - null}
    return out


def dispersion_check(V: dict) -> dict:
    """Is the spread right? sd of standardized residuals per competition / predicted level, fitted on
    discovery, applied as a widening on confirmation: change in RPS and log score."""
    out = {}
    for var in ("odds", "noodds"):
        d, F = V[var]
        tr = d["season"].isin(DISCOVERY).to_numpy()
        te = d["season"].isin(CONFIRM).to_numpy()
        z = d["resid"] / d["sd_pred"]
        y = d["actual"].to_numpy().astype(int)
        for grp in ("competition", "level"):
            key = d["competition"] if grp == "competition" else pd.cut(d["mean"], [0, 20, 22, 24, 26, 28, 99]).astype(str)
            tab = z[tr].groupby(key[tr]).std()
            sc = key.map(tab).fillna(1.0).to_numpy()
            G = spread_cdf(F[te], d["mean"].to_numpy()[te], sc[te])
            out[f"{var}/{grp}"] = {
                "sd_z_discovery": {str(k): float(v) for k, v in tab.items()},
                "d_rps": dict(zip(("diff", "lo", "hi"), day_ci(rps_of(G, y[te]) - d["rps"].to_numpy()[te], d.loc[te, "date"]))),
                "d_logs": dict(zip(("diff", "lo", "hi"), day_ci(logs_of(G, y[te]) - d["logs"].to_numpy()[te], d.loc[te, "date"])))}
    return out


def season_end_table(V: dict, C: pd.DataFrame) -> dict:
    out = {}
    for var in ("odds", "noodds"):
        d = V[var][0]
        d = d[d["season"].isin(COMPLETE)].merge(C[["match_id", "matches_left"]], on="match_id", how="left")
        grp = pd.cut(d["matches_left"], [-1, 0, 2, 5, 10, 1000], labels=["last", "2-3 left", "4-6 left", "7-11 left", "12+ left"])
        rows = {}
        for g, x in d.groupby(grp, observed=True):
            rows[str(g)] = {"n": int(len(x)), "resid": float(x["resid"].mean()),
                            "resid_disc": float(x.loc[x["season"].isin(DISCOVERY), "resid"].mean()),
                            "resid_conf": float(x.loc[x["season"].isin(CONFIRM), "resid"].mean()),
                            "rand80": float(x["rand_80"].mean()), "ucov80": float(x["ucov_80"].mean())}
        out[var] = rows
        mo = d["date"].dt.month
        out[f"{var}_month_by_season"] = {f"{s}/{m}": float(v) for (s, m), v in d.groupby([d["season"], mo])["resid"].mean().items()}
    return out


def newcomers(V: dict) -> dict:
    """Matches with a team that has < 5 matches in our data before this season (promoted from a
    league we do not cover): calibration of those rows."""
    out = {}
    for var in ("odds", "noodds"):
        d = V[var][0]
        d = d[d["season"].isin(COMPLETE)]
        new = (((d["home_n_prior_matches"] - d["home_n_prior_season"]) < 5)
               | ((d["away_n_prior_matches"] - d["away_n_prior_season"]) < 5))
        byc = {}
        for c, g in d.groupby("competition"):
            nm = new[g.index]
            byc[c] = {"share": float(nm.mean()), "rand80_new": float(g.loc[nm, "rand_80"].mean()) if nm.any() else None,
                      "resid_new": float(g.loc[nm, "resid"].mean()) if nm.any() else None}
        out[var] = {"share": float(new.mean()), "rps_new": float(d.loc[new, "rps"].mean()),
                    "rps_rest": float(d.loc[~new, "rps"].mean()), "rand80_new": float(d.loc[new, "rand_80"].mean()),
                    "rand80_rest": float(d.loc[~new, "rand_80"].mean()), "resid_new": float(d.loc[new, "resid"].mean()),
                    "by_competition": byc}
    return out


def euro_proxy(V: dict) -> dict:
    """European-congestion proxy: tier-1 teams that finished top 5 the previous season; their own-shot
    residual in European months (Sep-Dec, Feb-Mar) vs other months, difference-in-differences against
    the other tier-1 teams."""
    m = pd.read_parquet(PROC / "matches.parquet")
    m1 = m[m["tier"] == 1]
    rows = []
    for side, o in (("home", "away"), ("away", "home")):
        gf, ga = m1[f"{side}_goals"], m1[f"{o}_goals"]
        rows.append(pd.DataFrame({"team": m1[f"{side}_team_id"], "season": m1["season"], "comp": m1["competition"],
                                  "pts": np.where(gf > ga, 3, np.where(gf == ga, 1, 0)), "gd": gf - ga}))
    pts = pd.concat(rows).groupby(["comp", "season", "team"]).agg(pts=("pts", "sum"), gd=("gd", "sum")).reset_index()
    pts["rank"] = pts.sort_values(["pts", "gd"], ascending=False).groupby(["comp", "season"]).cumcount() + 1
    seasons = sorted(pts["season"].unique())
    pts["next"] = pts["season"].map({s: seasons[i + 1] for i, s in enumerate(seasons[:-1])})
    euro = pts[pts["rank"] <= 5][["team", "next"]].rename(columns={"next": "season"}).assign(euro=1)
    out = {}
    for var in ("odds", "noodds"):
        d = V[var][0]
        d = d[d["season"].isin(COMPLETE) & (d["tier"] == 1)]
        L = pd.concat([pd.DataFrame({"team": d[f"{s}_team_id"], "season": d["season"], "date": d["date"],
                                     "own": d[f"{s}_actual"] - d[f"mu_{s}"], "conc": d[f"{o}_actual"] - d[f"mu_{o}"]})
                       for s, o in (("home", "away"), ("away", "home"))])
        L = L.merge(euro, on=["team", "season"], how="left").fillna({"euro": 0})
        L["euro_months"] = L["date"].dt.month.isin([9, 10, 11, 12, 2, 3])
        t = L.groupby(["euro", "euro_months"])[["own", "conc"]].mean()
        did = ((t.loc[(1, True), "own"] - t.loc[(1, False), "own"])
               - (t.loc[(0, True), "own"] - t.loc[(0, False), "own"]))
        out[var] = {"cells": {f"euro={int(e)}/euro_months={bool(w)}": {k: float(v) for k, v in r.items()}
                              for (e, w), r in t.iterrows()},
                    "did_own_shots": float(did), "n_euro_team_matches": int((L["euro"] == 1).sum())}
    return out


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 6)
    return o


def run(curves_path: Path = CURVES) -> dict:
    V = load(curves_path)
    res = {"headline": headline(V), "ceiling": ceiling(V), "oracle_team_season": team_season_oracle(V),
           "oracle_level_gains": oracle_level_gains(V), "team_persistence": team_persistence(V),
           "calibration_slope_odds": calibration_slope(V),
           "calibration_slope_noodds": calibration_slope({"odds": V["noodds"]}),
           "dispersion": dispersion_check(V)}
    res["segments_odds"] = segment_tables(V, "odds")
    res["segments_noodds"] = segment_tables(V, "noodds")
    res["edges_odds"] = edges(res["segments_odds"])
    res["edges_noodds"] = edges(res["segments_noodds"])
    res["tails"] = tails(V)
    res["postmatch"] = postmatch(V)
    res["newcomers"] = newcomers(V)
    res["euro_proxy"] = euro_proxy(V)
    C = candidate_frame(V)
    res["season_end"] = season_end_table(V, C)
    res["candidates_odds"] = candidate_signals(V, C, "odds")
    res["candidates_noodds"] = candidate_signals(V, C, "noodds", tuple(c for c in CANDIDATES if c != "ah_line"))

    def has_ref(d, c):
        return c["ref_n"].fillna(0).to_numpy() >= 10

    res["candidates_referee_rows"] = {v: candidate_signals(V, C, v, ("intercept", "referee"), has_ref)
                                      for v in ("odds", "noodds")}
    combos = {"odds": ("season_end", "referee", "goals_form"),
              "noodds": ("elo+level", "season_end", "goals_form", "referee")}
    res["combined"] = {}
    for v, combo in combos.items():
        d, F = V[v]
        c = d[["match_id"]].merge(C, on="match_id", how="left")
        X = np.column_stack([_design(d, c, n) for n in combo])
        tr = d["season"].isin(DISCOVERY).to_numpy()
        r = d["resid"].to_numpy()
        delta = ols_pred(ols(X[tr], r[tr]), X)
        d0 = np.full(len(d), ols(np.zeros((int(tr.sum()), 0)), r[tr])[0])
        y = d["actual"].to_numpy().astype(int)
        row = {"features": list(combo)}
        for part, m in (("confirm", d["season"].isin(CONFIRM).to_numpy()),
                        ("s2627", (d["season"] == "2026/2027").to_numpy())):
            new = rps_of(shift_cdf(F[m], delta[m]), y[m])
            ref = rps_of(shift_cdf(F[m], d0[m]), y[m])
            row[part] = dict(zip(("d_rps", "lo", "hi"), day_ci(new - ref, d.loc[m, "date"])))
        res["combined"][v] = row
    return _jsonable(res)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Error decomposition of the production model.")
    ap.add_argument("--curves", default=str(CURVES))
    ap.add_argument("--out", default=str(ROOT / "reports" / "2026-10-15_diagnosis.json"))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    res = run(Path(a.curves))
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    h = res["headline"]
    for k in ("odds/complete", "noodds/complete", "clim/complete"):
        print(k, {m: h[k][m] for m in ("n", "rps", "logs", "pin_mean", "cov_80", "mid_80")})


if __name__ == "__main__":
    main()
