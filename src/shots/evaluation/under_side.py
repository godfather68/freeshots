"""Out-of-sample check of the UPPER tail of the published curve ("under" probabilities).

Usage:
    python -m shots.evaluation.under_side [--seasons 2023/2024,2024/2025,2025/2026] [--rebuild] [--out FILE.json]

The site publishes P(total >= n). An over/under calculator reads the "under" side off the same
curve: P(under N.5) = P(total <= N) = 1 - P(total >= N + 1). Everything validated so far was the
lower tail (lower bounds, picks with P(total >= N) >= 70%); this module checks the other tail.

Two sources, both strictly out of sample:

1. Season replay (same protocol as the track-record backfill, ``predict.track.backfill``): for each
   test season the production model class is fitted once on every match dated before that season's
   first match and scores the whole season (2025/26 is the headline; the two seasons before it are
   replayed the same way to tell a pattern from one season's noise). Two variants: ``odds`` (market
   features as published) and ``noodds`` (market features blanked in training and scoring, the model
   used for fixtures without odds). The full curve is kept, so every line can be measured.
2. The existing monthly walk-forward predictions (``backtest_predictions_month_negbin_pit_comp``,
   2019/20 onwards, odds variant): they carry the PIT bounds F(y - 1), F(y) of the recalibrated
   distribution, from which coverage of any upper or lower bound follows exactly:
   y <= U_q  <=>  F(y - 1) < q   and   y >= L_q  <=>  F(y) > 1 - q.

Definitions
- upper bound U_q = min{n : P(total <= n) >= q} (mirror of the lower bound L_q). ``cov`` = share
  with y <= U_q, ``strict`` = share with y < U_q, ``mid`` = their average. Integers: cov >= q and
  mid ~= q for a calibrated distribution (the rounding-aware ship rule of CLAUDE.md, mirrored).
- reliability: rows binned on the stated probability; ``gap`` = realised - stated (negative = the
  event happened less often than stated, i.e. the stated probability was too high).
- picks: every (match, line) with a stated probability >= 70%, as track.json does for the over side.
  Their standard errors are clustered by match: the lines of one match are one outcome read at
  several thresholds, not independent picks.

Nothing here is used to fit or tune a model.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from shots.models import MODELS

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
FEATURES_PATH = ROOT / "data" / "processed" / "features.parquet"
CURVES_PATH = ROOT / "data" / "processed" / "under_side_curves.parquet"
WALK_PATH = ROOT / "data" / "processed" / "backtest_predictions_month_negbin_pit_comp.parquet"
MODEL = "negbin_pit_comp"
SEASONS = ("2023/2024", "2024/2025", "2025/2026")   # full out-of-sample seasons replayed
N_LO, N_HI = 8, 50                 # curve range kept: P(total >= n), n = N_LO..N_HI
LINES = tuple(range(16, 31))       # lines N of "under N.5" reported one by one
LINE_GROUPS = (("16-22", 16, 22), ("23-26", 23, 26), ("27-30", 27, 30))
QS = (0.7, 0.8, 0.9)
BANDS = ((0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8),
         (0.8, 0.9), (0.9, 1.0))
MKT = ("mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25")


def _no_odds(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out[[c for c in out.columns if c.startswith("mkt_")]] = float("nan")
    return out


def _score_season(feats: pd.DataFrame, season: str) -> pd.DataFrame:
    test = feats[feats["season"].astype(str) == season]
    start = test["date"].min()
    train = feats[feats["date"] < start]
    assert train["date"].max() < test["date"].min()
    parts = []
    for variant in ("odds", "noodds"):
        tr, te = (train, test) if variant == "odds" else (_no_odds(train), _no_odds(test))
        if variant == "odds":  # the odds model only ever scores fixtures with published odds
            te = te[te[list(MKT)].notna().all(axis=1)]
        log.info("%s %s: fit on %d matches before %s, score %d", season, variant, len(tr), start.date(), len(te))
        model = MODELS[MODEL]().fit(tr)
        rows = te.drop(columns=["home_shots", "away_shots", "total_shots"])
        out = pd.DataFrame({"variant": variant, "match_id": te["match_id"].to_numpy(),
                            "date": pd.to_datetime(te["date"]).to_numpy(), "season": season,
                            "competition": te["competition"].astype(str).to_numpy(),
                            "actual": te["total_shots"].to_numpy(float)})
        for n in range(N_LO, N_HI + 1):
            out[f"ge_{n}"] = model.prob_at_least(rows, n)
        parts.append(out)
    return pd.concat(parts, ignore_index=True)


def season_curves(seasons=SEASONS, features_path: Path = FEATURES_PATH, out_path: Path = CURVES_PATH,
                  rebuild: bool = False) -> pd.DataFrame:
    """One row per (season, variant, match) with ``ge_<n>`` = stated P(total >= n). Each season is
    scored by models fitted only on matches dated before its first match. Cached per season."""
    cached = pd.read_parquet(out_path) if Path(out_path).exists() and not rebuild else None
    have = set(cached["season"].astype(str)) if cached is not None else set()
    parts = [cached[cached["season"].astype(str).isin(seasons)]] if cached is not None else []
    todo = [x for x in seasons if x not in have]
    if todo:
        feats = pd.read_parquet(features_path)
        feats = feats[feats["total_shots"].notna()]
        parts += [_score_season(feats, season) for season in todo]
        keep = [cached[~cached["season"].astype(str).isin(seasons)]] if cached is not None else []
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        pd.concat([*keep, *parts], ignore_index=True).to_parquet(out_path, index=False)
    return pd.concat(parts, ignore_index=True)


# ----------------------------------------------------------------------------- metrics

def _se(p: float, n: int) -> float:
    return float(np.sqrt(max(p * (1 - p), 1e-12) / n)) if n else float("nan")


def long_events(curves: pd.DataFrame, lines=LINES) -> pd.DataFrame:
    """Long table, one row per (match, line N): stated ``p_under`` = P(total <= N), ``p_over`` =
    P(total >= N + 1) = 1 - p_under, and the outcome ``under`` (bool)."""
    match = curves["season"].astype(str).to_numpy() + ":" + curves["match_id"].astype(str).to_numpy()
    parts = []
    for n in lines:
        p_over = curves[f"ge_{n + 1}"].to_numpy(float)
        parts.append(pd.DataFrame({"competition": curves["competition"].to_numpy(), "match": match, "line": n,
                                   "p_under": 1.0 - p_over, "p_over": p_over,
                                   "under": curves["actual"].to_numpy() <= n}))
    return pd.concat(parts, ignore_index=True)


def reliability(p, hit, bands=BANDS) -> list[dict]:
    p, hit = np.asarray(p, float), np.asarray(hit, float)
    out = []
    for lo, hi in bands:
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        n = int(m.sum())
        if not n:
            continue
        stated, realised = float(p[m].mean()), float(hit[m].mean())
        out.append({"band": [lo, hi], "n": n, "stated": stated, "realised": realised,
                    "gap_pp": 100 * (realised - stated), "se_pp": 100 * _se(stated, n)})
    return out


def side_summary(p, hit, match=None, min_prob: float = 0.7) -> dict:
    """Headline of the picks at >= min_prob + Brier over all rows. ``se_pp``: standard error of the gap,
    clustered by match when ``match`` is given."""
    p, hit = np.asarray(p, float), np.asarray(hit, float)
    m = p >= min_prob
    n = int(m.sum())
    out = {"n_all": int(len(p)), "mean_stated_all": float(p.mean()), "realised_all": float(hit.mean()),
           "brier": float(((p - hit) ** 2).mean()), "n": n, "stated": None, "hit_rate": None, "gap_pp": None,
           "se_pp": None, "n_matches": None}
    if n:
        d = hit[m] - p[m]
        if match is not None:
            per = pd.Series(d).groupby(np.asarray(match)[m]).sum().to_numpy()
            se, out["n_matches"] = float(np.sqrt((per ** 2).sum()) / n), int(len(per))
        else:
            se = _se(float(p[m].mean()), n)
        out.update(stated=float(p[m].mean()), hit_rate=float(hit[m].mean()), gap_pp=100 * float(d.mean()),
                   se_pp=100 * se)
    return out


def bounds_from_curve(curves: pd.DataFrame, q: float) -> tuple[np.ndarray, np.ndarray]:
    """(lower bound L_q, upper bound U_q) read off the published-style curve.
    L_q = max{n : P(total >= n) >= q}; U_q = min{n : P(total <= n) >= q} = min{n : P(>= n + 1) <= 1 - q}."""
    ns = np.arange(N_LO, N_HI + 1)
    ge = curves[[f"ge_{n}" for n in ns]].to_numpy(float)
    lower = ns[np.maximum((ge >= q - 1e-12).sum(axis=1) - 1, 0)]  # the curve is non-increasing
    upper = ns[np.minimum((ge > 1 - q + 1e-12).sum(axis=1), len(ns) - 1)] - 1
    return lower, upper


def coverage(curves: pd.DataFrame, qs=QS) -> dict:
    y = curves["actual"].to_numpy(float)
    out = {"n": int(len(y))}
    for q in qs:
        lo, up = bounds_from_curve(curves, q)
        k = round(q * 100)
        out[f"upper_cov_{k}"] = float(np.mean(y <= up))
        out[f"upper_mid_{k}"] = float((np.mean(y <= up) + np.mean(y < up)) / 2)
        out[f"lower_cov_{k}"] = float(np.mean(y >= lo))
        out[f"lower_mid_{k}"] = float((np.mean(y >= lo) + np.mean(y > lo)) / 2)
    return out


def walk_forward_coverage(path: Path = WALK_PATH, qs=QS) -> dict:
    """Upper / lower bound coverage from the stored PIT bounds of the monthly walk-forward run."""
    if not Path(path).exists():
        return {}
    df = pd.read_parquet(path)
    df = df[(df["model"] == MODEL) & df["actual"].notna() & df["pit_lo"].notna()]

    def table(g: pd.DataFrame) -> dict:
        lo, hi = g["pit_lo"].to_numpy(float), g["pit_hi"].to_numpy(float)
        out = {"n": int(len(g))}
        for q in qs:
            k = round(q * 100)
            out[f"upper_cov_{k}"] = float(np.mean(lo < q))
            out[f"upper_mid_{k}"] = float((np.mean(lo < q) + np.mean(hi < q)) / 2)
            out[f"lower_cov_{k}"] = float(np.mean(hi > 1 - q))
            out[f"lower_mid_{k}"] = float((np.mean(hi > 1 - q) + np.mean(lo > 1 - q)) / 2)
        return out

    month = df["date"].dt.to_period("M").astype(str)
    latest = sorted(df["season"].astype(str).unique())[-1]
    cur = df[df["season"].astype(str) == latest]
    return {"overall": table(df),
            "by_season": {str(s): table(g) for s, g in df.groupby(df["season"].astype(str))},
            "by_competition": {str(c): table(g) for c, g in df.groupby(df["competition"].astype(str))},
            "latest_season": latest,
            "latest_by_month": {str(m): table(g) for m, g in cur.groupby(month[cur.index])}}


def _sides(e: pd.DataFrame) -> dict:
    return {"under": side_summary(e["p_under"], e["under"], e["match"]),
            "over": side_summary(e["p_over"], ~e["under"], e["match"])}


def block(cv: pd.DataFrame) -> dict:
    """All under / over numbers of one slice of curves (one variant)."""
    e = long_events(cv)
    out: dict = {"n_matches": int(len(cv)), "coverage": coverage(cv),
                 "coverage_by_competition": {c: coverage(g) for c, g in cv.groupby("competition")}}
    out.update(_sides(e))
    out["under_reliability"] = reliability(e["p_under"], e["under"])
    out["over_reliability"] = reliability(e["p_over"], ~e["under"])
    out["by_line"] = {int(n): _sides(g) for n, g in e.groupby("line")}
    out["by_line_group"] = {name: _sides(e[(e["line"] >= lo) & (e["line"] <= hi)]) for name, lo, hi in LINE_GROUPS}
    out["by_competition"] = {c: _sides(g) for c, g in e.groupby("competition")}
    return out


def evaluate(curves: pd.DataFrame) -> dict:
    res: dict = {"lines": list(LINES), "seasons": {}, "pooled": {}}
    for variant, cv in curves.groupby("variant"):
        res["pooled"][variant] = block(cv)
        for season, g in cv.groupby(cv["season"].astype(str)):
            res["seasons"].setdefault(season, {})[variant] = block(g)
    res["walk_forward"] = walk_forward_coverage()
    return res


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seasons", default=",".join(SEASONS), help="comma-separated test seasons")
    ap.add_argument("--rebuild", action="store_true", help="refit the models even if cached curves exist")
    ap.add_argument("--out", default=None, help="write the numbers as JSON to this path")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    seasons = tuple(x.strip() for x in args.seasons.split(",") if x.strip())
    res = evaluate(season_curves(seasons, rebuild=args.rebuild))
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1, sort_keys=True), encoding="utf-8")
    for season, variants in sorted(res["seasons"].items()):
        for variant, b in sorted(variants.items()):
            u, o = b["under"], b["over"]
            print(f"{season} {variant}: {b['n_matches']} matches; under picks >=70%: n={u['n']} "
                  f"stated={u['stated']:.3f} hit={u['hit_rate']:.3f}; over picks >=70%: n={o['n']} "
                  f"stated={o['stated']:.3f} hit={o['hit_rate']:.3f}")


if __name__ == "__main__":
    main()
