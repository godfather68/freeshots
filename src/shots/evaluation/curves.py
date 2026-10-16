"""Walk-forward FULL predictive curves for the common scoring protocol (docs/evaluation-protocol.md).

Usage:
    python -m shots.evaluation.curves [--model negbin_pit_comp] [--variants odds,noodds]
                                      [--seasons 2019/2020,...] [--refit season|month] [--jobs 4]

The stored backtest predictions (``backtest_predictions_*``) only keep the bounds and the PIT at
the realised count, which is enough for coverage / pinball but not for full-distribution scores
(RPS, log score, reliability at any line). This module re-runs the walk-forward of a model and
keeps, per match, the published-style CDF ``cdf_0 .. cdf_{K}`` = P(total <= k) (K = ``KMAX``),
plus the raw per-side NB means when the model exposes them (``mu_home`` / ``mu_away``).

Protocol (same as ``backtest.walk_forward``): for each test season (or calendar month with
``--refit month``) a fresh model is fitted on every row dated strictly before the first match
of the block; target columns are dropped from the rows passed to the model; ``variant=noodds``
blanks every ``mkt_*`` column in training and scoring (the production no-odds model).

A climatology reference is written too (``variant=clim``): the empirical distribution of match
totals in the same competition over its latest 3 training seasons (add-0.5 smoothed on 0..K),
fallback all competitions. It is the "league average" baseline in distribution form.

Output: ``data/processed/curves_<model>_<refit>.parquet`` (one row per (variant, match)).
"""

from __future__ import annotations

import argparse
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
FEATURES_PATH = ROOT / "data" / "processed" / "features.parquet"
OUT_DIR = ROOT / "data" / "processed"
KMAX = 80
SEASONS = tuple(f"{y}/{y + 1}" for y in range(2019, 2027))
HIDDEN = ("home_shots", "away_shots", "total_shots", "home_sot", "away_sot", "home_goals", "away_goals")
META = ("match_id", "date", "season", "competition", "tier", "home_team_id", "away_team_id")


def no_odds(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out[[c for c in out.columns if c.startswith("mkt_")]] = float("nan")
    return out


def model_cdf(model, rows: pd.DataFrame, kmax: int = KMAX) -> np.ndarray:
    """Published-style CDF P(total <= k), k = 0..kmax, rows = matches. Uses the model's
    ``prob_at_least`` (calibrated models) or ``total_pmf`` (raw NB); forced monotone in [0, 1]."""
    n = len(rows)
    if hasattr(model, "pit_map_") and hasattr(model, "nb_"):
        from shots.models.calibrated import _cdf
        raw = _cdf(model.nb_, rows)
        raw = np.pad(raw, ((0, 0), (0, max(0, kmax + 1 - raw.shape[1]))), constant_values=1.0)[:, :kmax + 1]
        g = rows[model.group] if model.group else None
        cdf = np.column_stack([model.pit_map_(raw[:, k], g) for k in range(kmax + 1)])
    elif hasattr(model, "prob_at_least"):
        cdf = np.column_stack([1.0 - np.asarray(model.prob_at_least(rows, np.full(n, k + 1)), float)
                               for k in range(kmax + 1)])
    elif hasattr(model, "total_pmf"):
        cdf = np.cumsum(model.total_pmf(rows), axis=1)
        cdf = np.pad(cdf, ((0, 0), (0, max(0, kmax + 1 - cdf.shape[1]))), constant_values=1.0)[:, :kmax + 1]
    else:
        raise TypeError(f"{type(model).__name__} exposes no predictive distribution")
    cdf = np.maximum.accumulate(np.clip(cdf, 0.0, 1.0), axis=1)
    return cdf


def side_means(model, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    nb = getattr(model, "nb_", model)
    if hasattr(nb, "side_mu"):
        return nb.side_mu(rows, "home"), nb.side_mu(rows, "away")
    nan = np.full(len(rows), np.nan)
    return nan, nan


def climatology_cdf(train: pd.DataFrame, rows: pd.DataFrame, n_seasons: int = 3, kmax: int = KMAX,
                    min_rows: int = 200) -> np.ndarray:
    def pmf(y):
        h = np.bincount(np.clip(np.asarray(y, int), 0, kmax), minlength=kmax + 1).astype(float) + 0.5
        return h / h.sum()

    seasons = sorted(train["season"].astype(str).unique())[-n_seasons:]
    glob = pmf(train.loc[train["season"].astype(str).isin(seasons), "total_shots"])
    table = {}
    for comp, g in train.groupby("competition"):
        keep = sorted(g["season"].astype(str).unique())[-n_seasons:]
        y = g.loc[g["season"].astype(str).isin(keep), "total_shots"]
        if len(y) >= min_rows:
            table[comp] = pmf(y)
    p = np.vstack([table.get(c, glob) for c in rows["competition"]])
    return np.cumsum(p, axis=1)


def _blocks(test: pd.DataFrame, refit: str):
    if refit == "season":
        return [test]
    return [g for _, g in test.groupby(test["date"].dt.to_period("M"), sort=True)]


def _job(args) -> pd.DataFrame:
    model_name, variant, season, refit, features_path = args
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    from shots.models import MODELS

    feats = pd.read_parquet(features_path)
    feats = feats[feats["total_shots"].notna()]
    test_all = feats[feats["season"].astype(str) == season]
    parts = []
    for test in _blocks(test_all, refit):
        start = test["date"].min()
        train = feats[feats["date"] < start]
        if train["date"].max() >= start:
            raise AssertionError("training rows not strictly before the block")
        rows = test.drop(columns=[c for c in HIDDEN if c in test.columns])
        out = pd.DataFrame({c: test[c].to_numpy() for c in META})
        out["variant"] = variant
        out["actual"] = test["total_shots"].to_numpy(float)
        out["home_actual"] = test["home_shots"].to_numpy(float)
        out["away_actual"] = test["away_shots"].to_numpy(float)
        out["train_end"] = train["date"].max()
        if variant == "clim":
            cdf = climatology_cdf(train, rows)
            mh = ma = np.full(len(rows), np.nan)
        else:
            tr, te = (train, rows) if variant == "odds" else (no_odds(train), no_odds(rows))
            model = MODELS[model_name]().fit(tr.copy())
            cdf = model_cdf(model, te.copy())
            mh, ma = side_means(model, te.copy())
        out["mu_home"], out["mu_away"] = mh, ma
        cdf_df = pd.DataFrame(cdf, columns=[f"cdf_{k}" for k in range(cdf.shape[1])])
        parts.append(pd.concat([out.reset_index(drop=True), cdf_df], axis=1))
        log.info("%s %s %s block from %s: %d rows", model_name, variant, season, start.date(), len(test))
    return pd.concat(parts, ignore_index=True)


def build(model_name: str = "negbin_pit_comp", variants=("odds", "noodds", "clim"), seasons=SEASONS,
          refit: str = "season", jobs: int = 4, features_path: Path = FEATURES_PATH,
          out_path: Path | None = None) -> pd.DataFrame:
    out_path = out_path or OUT_DIR / f"curves_{model_name}_{refit}.parquet"
    tasks = [(model_name, v, s, refit, features_path) for v in variants for s in seasons]
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            parts = list(ex.map(_job, tasks))
    else:
        parts = [_job(t) for t in tasks]
    res = pd.concat(parts, ignore_index=True)
    res["model"] = model_name
    res.to_parquet(out_path, index=False)
    log.info("wrote %s (%d rows)", out_path, len(res))
    return res


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default="negbin_pit_comp")
    ap.add_argument("--variants", default="odds,noodds,clim")
    ap.add_argument("--seasons", default=",".join(SEASONS))
    ap.add_argument("--refit", default="season", choices=("season", "month"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    build(a.model, tuple(a.variants.split(",")), tuple(a.seasons.split(",")), a.refit, a.jobs,
          out_path=Path(a.out) if a.out else None)


if __name__ == "__main__":
    main()
