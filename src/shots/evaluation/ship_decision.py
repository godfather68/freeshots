"""Tables for the 3b.4 ship decision (season vs monthly refit, granularity-aware calibration).

Usage:
    python -m shots.evaluation.ship_decision --out <tables.md>

Reads the per-model monthly-refit prediction files ``data/processed/backtest_predictions_month_<m>.parquet``,
the season-refit predictions with distribution diagnostics
``data/processed/backtest_predictions_season_diag.parquet`` (NB models) and
``data/processed/backtest_predictions_3b.parquet`` (team_avg, season refit), and writes markdown
tables. The narrative report (reports/<date>_ship_decision.md) is written by the evaluator from
these tables.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from shots.evaluation import calibration_checks as C
from shots.evaluation import metrics as M
from shots.evaluation.backtest import DEFAULT_TEST_SEASONS, PARTIAL_SEASONS, ROOT

PROC = ROOT / "data" / "processed"
MODELS = ("team_avg", "negbin", "negbin_cal_comp", "negbin_pit_comp")
QS = (0.7, 0.8, 0.9)


def load() -> pd.DataFrame:
    parts = []
    for m in MODELS:
        p = PROC / f"backtest_predictions_month_{m}.parquet"
        if p.exists():
            parts.append(pd.read_parquet(p).assign(refit="month"))
    s = pd.read_parquet(PROC / "backtest_predictions_season_diag.parquet").assign(refit="season")
    t = pd.read_parquet(PROC / "backtest_predictions_3b.parquet")
    t = t[t["model"] == "team_avg"].assign(refit="season")
    df = pd.concat([*parts, s, t], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return M.add_slices(df)


def pct(x, signed=False):
    return "" if pd.isna(x) else (f"{100 * x:+.1f}" if signed else f"{100 * x:.1f}")


def overall_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for (refit, m), g in df.groupby(["refit", "model"], sort=False):
        s, gt = M.score(g), C.granularity_table(g)
        r = {"n": int(s["n"])}
        for k in (70, 80, 90):
            r[f"cov{k}"] = pct(s[f"cov_{k}"])
        r["mid80"] = pct(gt["mid_80"])
        r["rand80"] = pct(gt.get("rand_80", np.nan))
        r["stated80"] = pct(gt.get("stated_80", np.nan))
        r["pinball"] = f"{s['pinball_mean']:.4f}"
        r["sharp80"] = f"{s['sharp_80']:.2f}"
        r["mae"] = f"{s['mae']:.3f}"
        r["bias"] = f"{s['bias']:+.2f}"
        rows[(refit, m)] = r
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.names = ["refit", "model"]
    return out


def granularity_overall(df: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for (refit, m), g in df.groupby(["refit", "model"], sort=False):
        if "pit_lo" not in g or g["pit_lo"].isna().any():
            gt = C.granularity_table(g)
            rows[(refit, m)] = {f"{c}{k}": pct(gt[f"{c}_{k}"]) for k in (70, 80, 90) for c in ("strict", "cov", "mid")}
            continue
        gt = C.granularity_table(g)
        rows[(refit, m)] = {f"{c}{k}": pct(gt.get(f"{c}_{k}", np.nan))
                            for k in (70, 80, 90) for c in ("strict", "cov", "mid", "rand", "stated", "pmf")}
    out = pd.DataFrame.from_dict(rows, orient="index").fillna("")
    out.index.names = ["refit", "model"]
    return out


def worst_competition(df: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for (refit, m), g in df.groupby(["refit", "model"], sort=False):
        gb = C.granularity_breakdown(g, "competition")
        r = {}
        for k in (70, 80, 90):
            q = k / 100
            c = gb[f"cov_{k}"]
            r[f"min cov{k}"] = f"{pct(c.min())} ({c.idxmin()})"
            r[f"comps < q-2pp @{k}"] = int((c < q - 0.02).sum())
        mid = (gb["mid_80"] - 0.8).abs()
        r["max |mid80-q|"] = f"{100 * mid.max():.1f} ({mid.idxmax()})"
        if "rand_80" in gb:
            rd = (gb["rand_80"] - 0.8).abs()
            r["max |rand80-q|"] = f"{100 * rd.max():.1f} ({rd.idxmax()})"
        r["sd cov80 (pp)"] = f"{100 * gb['cov_80'].std():.2f}"
        rows[(refit, m)] = r
    out = pd.DataFrame.from_dict(rows, orient="index").fillna("")
    out.index.names = ["refit", "model"]
    return out


def per_comp_cov80(df: pd.DataFrame, refit: str) -> pd.DataFrame:
    d = df[df["refit"] == refit]
    w = {m: C.granularity_breakdown(g, "competition") for m, g in d.groupby("model", sort=False)}
    out = pd.DataFrame({"n": next(iter(w.values()))["n"].astype(int)})
    for m, gb in w.items():
        out[f"{m} cov80"] = gb["cov_80"].map(pct)
        if "rand_80" in gb:
            out[f"{m} rand80"] = gb["rand_80"].map(pct)
    return out


def per_season(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    out = {}
    for (refit, m), g in df.groupby(["refit", "model"], sort=False):
        b = M.breakdown(g, "season")
        out[f"{m} ({refit[0]})"] = b[metric].map(pct) if metric.startswith("cov") else b[metric].map(
            lambda v: f"{v:.3f}" if metric != "bias" else f"{v:+.2f}")
    return pd.DataFrame(out)


def per_month(df: pd.DataFrame, season: str) -> pd.DataFrame:
    d = df[df["season"] == season].copy()
    d["month"] = d["date"].dt.to_period("M").astype(str)
    out = {}
    for (refit, m), g in d.groupby(["refit", "model"], sort=False):
        b = M.breakdown(g, "month")
        out[(f"{m} ({refit[0]})", "cov80")] = b["cov_80"].map(pct)
        out[(f"{m} ({refit[0]})", "bias")] = b["bias"].map(lambda v: f"{v:+.2f}")
    t = pd.DataFrame(out)
    t.insert(0, "n", d[d["model"] == "team_avg"].groupby("month").size().reindex(t.index))
    t.columns = [c if isinstance(c, str) else " ".join(c) for c in t.columns]
    return t


def bootstrap_table(df: pd.DataFrame, refit: str, ref: str = "negbin_pit_comp") -> pd.DataFrame:
    d = df[(df["refit"] == refit) & df["season"].isin(DEFAULT_TEST_SEASONS)]
    base = d[d["model"] == ref].set_index("match_id")
    rows = {}
    if base.empty:
        return pd.DataFrame()
    for m, g in d.groupby("model", sort=False):
        if m == ref:
            continue
        g = g.set_index("match_id").loc[base.index]
        mean, lo, hi = C.paired_bootstrap(C.row_pinball(g), C.row_pinball(base), block=base["date"].to_numpy())
        rows[m] = {f"pinball(model) - pinball({ref})": f"{mean:+.4f}", "95% CI (day blocks)": f"[{lo:+.4f}, {hi:+.4f}]"}
    return pd.DataFrame.from_dict(rows, orient="index")


def reliability(df: pd.DataFrame, model: str, refit: str, seasons) -> pd.DataFrame:
    d = df[(df["refit"] == refit) & (df["model"] == model) & df["season"].isin(seasons)]
    parts = []
    for k in (70, 80, 90):
        t = C.reliability_table(d[f"p_{k}"], d["actual"] >= d[f"lb_{k}"])
        t.insert(0, "q", k / 100)
        parts.append(t)
    allp = pd.concat([d[f"p_{k}"] for k in (70, 80, 90)])
    allh = pd.concat([d["actual"] >= d[f"lb_{k}"] for k in (70, 80, 90)])
    t = C.reliability_table(allp, allh, edges=(0, .7, .72, .74, .76, .78, .8, .82, .84, .86, .88, .9, .92, .94, .96, 1.0001))
    t.insert(0, "q", "pooled")
    parts.append(t)
    out = pd.concat(parts).reset_index()
    for c in ("stated", "realised"):
        out[c] = out[c].map(pct)
    for c in ("gap_pp", "se_pp"):
        out[c] = out[c].map(lambda v: f"{v:+.1f}" if c == "gap_pp" else f"{v:.1f}")
    return out


def pit_hist(df: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for (refit, m), g in df.groupby(["refit", "model"], sort=False):
        g = g[g["season"].isin(DEFAULT_TEST_SEASONS)]
        if "pit_lo" in g and g["pit_lo"].notna().all() and len(g):
            out[f"{m} ({refit[0]})"] = [f"{100 * v:.1f}" for v in C.pit_histogram(g["pit_lo"], g["pit_hi"])]
    return pd.DataFrame(out, index=[f"{d / 10:.1f}-{(d + 1) / 10:.1f}" for d in range(10)])


def slice_table(df: pd.DataFrame, mask_fn, title_col="slice") -> pd.DataFrame:
    rows = {}
    for (refit, m), g in df.groupby(["refit", "model"], sort=False):
        g = g[mask_fn(g)]
        s, gt = M.score(g), C.granularity_table(g)
        rows[(refit, m)] = {"n": int(s["n"]), **{f"cov{k}": pct(s[f"cov_{k}"]) for k in (70, 80, 90)},
                            "mid80": pct(gt["mid_80"]), "rand80": pct(gt.get("rand_80", np.nan)),
                            "pinball": f"{s['pinball_mean']:.4f}", "mae": f"{s['mae']:.3f}",
                            "bias": f"{s['bias']:+.2f}"}
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.names = ["refit", "model"]
    return out


def ranks(df: pd.DataFrame, seasons) -> pd.DataFrame:
    out = {}
    for refit, d in df[df["season"].isin(seasons)].groupby("refit"):
        pin = d.groupby("model").apply(lambda g: M.score(g)["pinball_mean"])
        out[f"{refit} pinball"] = pin.map(lambda v: f"{v:.4f}")
        out[f"{refit} rank"] = pin.rank().astype(int)
    return pd.DataFrame(out)


def drift(df: pd.DataFrame, model: str, refit: str = "month") -> pd.DataFrame:
    d = df[(df["refit"] == refit) & (df["model"] == model)]
    return C.monthly_drift(d)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    df = load()
    main_df = df[df["season"].isin(DEFAULT_TEST_SEASONS)]
    part = df[df["season"].isin(PARTIAL_SEASONS)]
    L = []

    def add(title, t):
        L.extend([f"## {title}", "", t.to_markdown(), ""])

    add("Overall, complete seasons 2019/20-2025/26", overall_table(main_df))
    add("Granularity decomposition (complete seasons)", granularity_overall(main_df))
    add("Worst competition (complete seasons)", worst_competition(main_df))
    add("Per competition q=0.8 (monthly refit)", per_comp_cov80(main_df, "month"))
    add("Per competition q=0.8 (season refit)", per_comp_cov80(main_df, "season"))
    for metric in ("cov_70", "cov_80", "cov_90", "pinball_mean", "bias"):
        add(f"Per season: {metric}", per_season(main_df, metric))
    add("2026/27 partial", overall_table(part))
    add("2026/27 by month", per_month(df, "2026/2027"))
    add("2025/26 by month", per_month(df, "2025/2026"))
    add("Cold start (either team < 5 prior matches), complete seasons",
        slice_table(main_df, lambda g: g["cold_start"]))
    add("Ranking by pinball, complete seasons", ranks(df, DEFAULT_TEST_SEASONS))
    add("Ranking by pinball, 2026/27", ranks(df, PARTIAL_SEASONS))
    for refit in ("month", "season"):
        add(f"Paired bootstrap vs negbin_pit_comp ({refit} refit)", bootstrap_table(df, refit))
    add("PIT histogram (randomized PIT, % per decile; uniform = 10.0)", pit_hist(df))
    for refit in ("month", "season"):
        for m in ("negbin_pit_comp", "negbin", "negbin_cal_comp"):
            if ((df["refit"] == refit) & (df["model"] == m)).any():
                add(f"Reliability of stated P(total >= lb): {m}, {refit} refit, complete seasons",
                    reliability(df, m, refit, DEFAULT_TEST_SEASONS))
    add("Reliability: negbin_pit_comp, monthly refit, 2026/27",
        reliability(df, "negbin_pit_comp", "month", PARTIAL_SEASONS))
    for refit in ("month", "season"):
        d = df[(df["refit"] == refit) & df["season"].isin(DEFAULT_TEST_SEASONS)]
        base = d[d["model"] == "team_avg"]
        for m in ("negbin", "negbin_cal_comp", "negbin_pit_comp"):
            g = d[d["model"] == m]
            if g.empty:
                continue
            ok, msgs = C.ship_check(g, base)
            L += [f"## Ship rule: {m} ({refit} refit) -> {'PASS' if ok else 'FAIL'}", "", *[f"- {x}" for x in msgs], ""]
    for m in ("negbin_pit_comp", "negbin_cal_comp", "team_avg"):
        if ((df["refit"] == "month") & (df["model"] == m)).any():
            dr = drift(df, m)
            fl = dr[dr["alert"]]
            L += [f"## Monthly drift alerts (q=0.8, 3 SE below expected, n >= 150): {m}, monthly refit", "",
                  f"{len(fl)} alert months of {int((dr['n'] >= 150).sum())} with n >= 150.", "",
                  dr.assign(coverage=dr["coverage"].map(pct), expected=dr["expected"].map(pct),
                            lcl=dr["lcl"].map(pct), bias=dr["bias"].map(lambda v: f"{v:+.2f}"))
                  .to_markdown(), ""]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
