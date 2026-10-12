"""Walk-forward (expanding window) backtest harness (TASKS 4.1).

Usage:
    python -m shots.evaluation.backtest --models league_avg,team_avg,negbin,qgbm
    python -m shots.evaluation.backtest --models qgbm --seasons 2023/2024,2024/2025 --refit month

For each test season S (default 2019/2020 .. 2025/2026, plus the in-progress 2026/2027 reported
separately), a fresh model is fit on every row dated strictly before the first date of S and
predicts all rows of S. With ``--refit month`` the season is split into calendar months and the
model is refit before each month on everything dated strictly before that month's first match
(closer to production, ~10x more fits).

Leakage guards (the harness does not trust the model):
- training rows are dated strictly before every row of the block they predict (asserted);
- target columns (home_shots, away_shots, total_shots) are dropped from the rows passed to
  ``predict`` so a model cannot read the answer;
- the model receives copies, so it cannot mutate the frames the harness scores against.

Outputs: ``reports/<today>_backtest.md`` and ``data/processed/backtest_predictions.parquet``
(long format, one row per (model, match)).
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from shots.evaluation import metrics as M
from shots.models.base import CONFIDENCES, TARGET, ShotModel, bound_column

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
FEATURES_PATH = ROOT / "data" / "processed" / "features.parquet"
PRED_PATH = ROOT / "data" / "processed" / "backtest_predictions.parquet"
REPORT_DIR = ROOT / "reports"

DEFAULT_TEST_SEASONS = tuple(f"{y}/{y + 1}" for y in range(2019, 2026))
PARTIAL_SEASONS = ("2026/2027",)
HIDDEN_FROM_PREDICT = ("home_shots", "away_shots", "total_shots",
                       "home_sot", "away_sot", "home_goals", "away_goals")
BASELINE = "league_avg"


class LeakageError(AssertionError):
    pass


def _blocks(rows: pd.DataFrame, refit: str) -> list[tuple[str, pd.DataFrame]]:
    if refit == "season":
        return [("season", rows)]
    if refit == "month":
        key = rows["date"].dt.to_period("M")
        return [(str(p), g) for p, g in rows.groupby(key, sort=True)]
    raise ValueError(f"refit must be 'season' or 'month', got {refit!r}")


def _check_output(out: pd.DataFrame, rows: pd.DataFrame, confidences: Sequence[float], name: str) -> pd.DataFrame:
    cols = ["median", *[bound_column(q) for q in confidences]]
    missing = [c for c in cols if c not in out.columns]
    if missing:
        raise ValueError(f"{name}.predict is missing columns {missing}")
    if len(out) != len(rows) or not out.index.equals(rows.index):
        raise ValueError(f"{name}.predict must return a frame indexed like its input")
    out = out[cols].copy()
    for q in confidences:
        c = bound_column(q)
        v = out[c].to_numpy(dtype=float)
        if np.isnan(v).any():
            raise ValueError(f"{name}.predict returned NaN in {c}")
        if not np.allclose(v, np.round(v)):
            raise ValueError(f"{name}.predict returned non-integer bounds in {c}")
    return out


def _surv_fn(model) -> Callable[[pd.DataFrame, np.ndarray], np.ndarray] | None:
    """Model's stated P(total >= n_i) per row, if it exposes a predictive distribution.

    Duck-typed (no model code is changed): ``prob_at_least(rows, n)`` (recalibrated CDF of the
    calibrated NB models) or ``total_pmf(rows)`` (raw NB). None for quantile-only models.
    """
    if hasattr(model, "prob_at_least"):
        return lambda rows, n: np.asarray(model.prob_at_least(rows, n), float)
    if hasattr(model, "total_pmf"):
        def surv(rows, n):
            cdf = np.cumsum(model.total_pmf(rows), axis=1)
            n = np.asarray(n).astype(int) - 1
            k = np.clip(n, 0, cdf.shape[1] - 1)
            F = cdf[np.arange(len(n)), k]
            F = np.where(n < 0, 0.0, np.where(n >= cdf.shape[1], 1.0, F))
            return 1.0 - F
        return surv
    return None


def distribution_diagnostics(model, rows: pd.DataFrame, actual: np.ndarray, pred: pd.DataFrame,
                             confidences: Sequence[float]) -> pd.DataFrame | None:
    """Scoring-time diagnostics for models with a predictive distribution (evaluation only; the
    actuals are used after ``predict`` has returned, on target-free rows):

    - ``pit_lo`` = F(y - 1), ``pit_hi`` = F(y): the randomized PIT of row i is U(pit_lo, pit_hi);
    - ``p_<k>``   = stated P(total >= lb_q) for the model's own bound;
    - ``pmf_<k>`` = stated P(total == lb_q) (mass at the bound: the integer-granularity margin).
    """
    surv = _surv_fn(model)
    if surv is None:
        return None
    y = np.asarray(actual, float)
    ok = ~np.isnan(y)
    yi = np.where(ok, y, 0).astype(int)
    out = pd.DataFrame(index=rows.index)
    out["pit_lo"] = np.where(ok, 1.0 - surv(rows, yi), np.nan)
    out["pit_hi"] = np.where(ok, 1.0 - surv(rows, yi + 1), np.nan)
    for q in confidences:
        k = round(q * 100)
        lb = pred[bound_column(q)].to_numpy().astype(int)
        p_lb = surv(rows, lb)
        out[f"p_{k}"] = p_lb
        out[f"pmf_{k}"] = p_lb - surv(rows, lb + 1)
    return out


def walk_forward(
    model_factory: Callable[[], ShotModel],
    features: pd.DataFrame,
    test_seasons: Iterable[str] = DEFAULT_TEST_SEASONS,
    confidences: Sequence[float] = CONFIDENCES,
    refit: str = "season",
    min_train_rows: int = 1000,
    diagnostics: bool = True,
) -> pd.DataFrame:
    """Expanding-window backtest. Returns one row per predicted match.

    Columns: match_id, date, season, competition, tier, n_prior_min, actual, median, lb_*,
    block (season or YYYY-MM), train_end (last training date), n_train; plus, for models with a
    predictive distribution and ``diagnostics=True``, pit_lo / pit_hi / p_* / pmf_*
    (see ``distribution_diagnostics``).
    """
    confidences = tuple(confidences)
    f = features.copy()
    f["date"] = pd.to_datetime(f["date"])
    trainable = f[f[TARGET].notna()]
    hidden = [c for c in HIDDEN_FROM_PREDICT if c in f.columns]
    outs = []
    for season in test_seasons:
        season_rows = f[f["season"] == season]
        if season_rows.empty:
            log.warning("no rows for test season %s, skipped", season)
            continue
        for block, rows in _blocks(season_rows, refit):
            cutoff = rows["date"].min()
            train = trainable[trainable["date"] < cutoff]
            if len(train) < min_train_rows:
                log.warning("season %s block %s: only %d training rows, skipped", season, block, len(train))
                continue
            if train["date"].max() >= rows["date"].min():  # strict temporal split, never relaxed
                raise LeakageError(f"training row dated {train['date'].max()} >= test row {rows['date'].min()}")
            if train.index.intersection(rows.index).size:
                raise LeakageError("training and prediction rows overlap")
            model = model_factory()
            t0 = time.time()
            model.fit(train.copy())
            pred = model.predict(rows.drop(columns=hidden).copy(), confidences)
            pred = _check_output(pred, rows, confidences, getattr(model, "name", type(model).__name__))
            if diagnostics:
                diag = distribution_diagnostics(model, rows.drop(columns=hidden).copy(),
                                                rows[TARGET].to_numpy(float), pred, confidences)
                if diag is not None:
                    pred = pred.join(diag)
            log.info("%s %s %s: train=%d (<%s) test=%d in %.1fs", getattr(model, "name", "?"), season,
                     block, len(train), cutoff.date(), len(rows), time.time() - t0)
            meta = pd.DataFrame({
                "match_id": rows["match_id"], "date": rows["date"], "season": rows["season"],
                "competition": rows["competition"], "tier": rows["tier"],
                "n_prior_min": np.fmin(rows["home_n_prior_matches"], rows["away_n_prior_matches"])
                if "home_n_prior_matches" in rows else np.nan,
                "actual": rows[TARGET],
            }, index=rows.index)
            out = meta.join(pred)
            out["block"] = block if refit == "month" else season
            out["train_end"] = train["date"].max()
            out["n_train"] = len(train)
            outs.append(out)
    if not outs:
        raise ValueError("nothing was predicted")
    return pd.concat(outs).reset_index(drop=True)


# --------------------------------------------------------------------------- report

def _fmt(df: pd.DataFrame, pct_cols=(), digits: int = 2) -> str:
    df = df.copy()
    for c in df.columns:
        if c in pct_cols or c.startswith(("cov_", "calib_")):
            df[c] = df[c].map(lambda v: "" if pd.isna(v) else f"{100 * v:+.1f}" if c.startswith("calib_") else f"{100 * v:.1f}")
        elif c == "n":
            df[c] = df[c].map(lambda v: "" if pd.isna(v) else f"{int(v)}")
        elif pd.api.types.is_float_dtype(df[c]):
            df[c] = df[c].map(lambda v: "" if pd.isna(v) else f"{v:.{digits}f}")
    return df.to_markdown()


def _wide(results: dict[str, dict[str, pd.DataFrame]], slice_name: str, metric: str) -> pd.DataFrame:
    return pd.DataFrame({m: r[slice_name][metric] for m, r in results.items()})


def verdict(results: dict[str, dict[str, pd.DataFrame]], confidences: Sequence[float]) -> list[str]:
    """Automatic, rule-based verdict lines (a human still reads the tables)."""
    lines = []
    overall = pd.concat({m: r["overall"].iloc[0] for m, r in results.items()}, axis=1).T
    best = overall["pinball_mean"].idxmin()
    lines.append(f"- Lowest mean pinball loss: **{best}** ({overall.loc[best, 'pinball_mean']:.3f}).")
    if BASELINE in overall.index:
        b = overall.loc[BASELINE]
        for m, r in overall.iterrows():
            if m == BASELINE:
                continue
            gain = 1 - r["pinball_mean"] / b["pinball_mean"]
            mae_gain = 1 - r["mae"] / b["mae"]
            flag = " **SUSPICIOUS: too good, check for leakage**" if gain > 0.25 or mae_gain > 0.35 else ""
            lines.append(f"- {m}: {100 * gain:+.1f}% lower mean pinball and {100 * mae_gain:+.1f}% lower MAE "
                         f"than {BASELINE} (positive = better).{flag}")
    for m, r in results.items():
        o = r["overall"].iloc[0]
        worst_overall = max(abs(o[f"calib_{round(q * 100)}"]) for q in confidences)
        comp = r["competition"]
        comp = comp[comp["n"] >= 200]
        worst_comp = max(comp[f"calib_{round(q * 100)}"].abs().max() for q in confidences)
        seas = r["season"]
        worst_season = max(seas[f"calib_{round(q * 100)}"].abs().max() for q in confidences)
        ok = worst_overall <= 0.02 and worst_comp <= 0.06 and worst_season <= 0.04
        lines.append(
            f"- {m}: calibration overall worst |cov-q| = {100 * worst_overall:.1f}pp, worst season "
            f"{100 * worst_season:.1f}pp, worst competition {100 * worst_comp:.1f}pp -> "
            f"{'calibrated' if ok else 'NOT calibrated to tolerance (2pp overall / 4pp season / 6pp competition)'}."
        )
    return lines


def write_report(preds: pd.DataFrame, path: Path, confidences: Sequence[float], test_seasons: Sequence[str],
                 partial_seasons: Sequence[str], refit: str, runtimes: dict[str, float]) -> None:
    confidences = tuple(confidences)
    ks = [round(q * 100) for q in confidences]
    main = preds[preds["season"].isin(test_seasons)]
    part = preds[preds["season"].isin(partial_seasons)]
    results = {m: M.all_breakdowns(g, confidences) for m, g in main.groupby("model", sort=False)}
    lines = [
        f"# Walk-forward backtest, {date.today():%Y-%m-%d}",
        "",
        f"- Models: {', '.join(results)}",
        f"- Test seasons (complete): {', '.join(test_seasons)}; partial: {', '.join(partial_seasons) or 'none'}",
        f"- Refit: {refit} (expanding window; train = all rows dated strictly before the block's first match)",
        f"- Bounds: lb_q = (1-q) quantile; coverage target = q. Pinball at tau = 1-q (lower is better). "
        f"Sharpness = mean bound (higher is better at equal coverage). calib = coverage - q in pp.",
        *([f"- Runtime (s): " + ", ".join(f"{m} {t:.0f}" for m, t in runtimes.items())] if runtimes else []),
        "",
        "## Verdict (automatic checks)",
        "",
        *verdict(results, confidences),
        "",
        "## Overall (complete test seasons)",
        "",
    ]
    overall = pd.concat({m: r["overall"].iloc[0] for m, r in results.items()}, axis=1).T
    overall.index.name = "model"
    cols = ["n", *[f"cov_{k}" for k in ks], *[f"pinball_{k}" for k in ks], "pinball_mean",
            *[f"sharp_{k}" for k in ks], "mae", "bias"]
    lines += [_fmt(overall[cols], digits=3), ""]

    lines += ["## Per season", ""]
    for metric in [f"cov_{k}" for k in ks] + ["pinball_mean", "mae"]:
        lines += [f"**{metric}**", "", _fmt(_wide(results, "season", metric), pct_cols=list(results) if metric.startswith("cov") else (), digits=3), ""]

    lines += ["## Per competition", ""]
    k_mid = ks[len(ks) // 2]
    for metric in [f"cov_{k_mid}", "pinball_mean", "mae"]:
        w = _wide(results, "competition", metric)
        w.insert(0, "n", next(iter(results.values()))["competition"]["n"])
        lines += [f"**{metric}**", "", _fmt(w, pct_cols=list(results) if metric.startswith("cov") else (), digits=3), ""]

    for sl, title in (("tier", "Per tier"), ("cold_start", "Cold start (either team < 5 prior matches)")):
        lines += [f"## {title}", ""]
        for m, r in results.items():
            lines += [f"**{m}**", "", _fmt(r[sl][cols], digits=3), ""]

    if not part.empty:
        lines += [f"## Partial season {', '.join(partial_seasons)} (in progress, small n; not in verdict)", ""]
        po = pd.concat({m: M.breakdown(g, None, confidences).iloc[0] for m, g in part.groupby("model", sort=False)}, axis=1).T
        po.index.name = "model"
        lines += [_fmt(po[cols], digits=3), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- CLI

def load_registry() -> dict:
    from shots.models import MODELS  # noqa: WPS433 (registry owned by the modeler)
    return MODELS


def run(models: Sequence[str], test_seasons: Sequence[str], partial_seasons: Sequence[str], refit: str,
        features_path: Path = FEATURES_PATH, pred_path: Path = PRED_PATH, report_path: Path | None = None,
        confidences: Sequence[float] = CONFIDENCES) -> pd.DataFrame:
    registry = load_registry()
    unknown = [m for m in models if m not in registry]
    if unknown:
        raise SystemExit(f"unknown models {unknown}; registry has {sorted(registry)}")
    features = pd.read_parquet(features_path)
    all_preds, runtimes = [], {}
    for name in models:
        t0 = time.time()
        p = walk_forward(registry[name], features, [*test_seasons, *partial_seasons], confidences, refit)
        p.insert(0, "model", name)
        all_preds.append(p)
        runtimes[name] = time.time() - t0
    preds = pd.concat(all_preds, ignore_index=True)
    pred_path.parent.mkdir(parents=True, exist_ok=True)
    preds.to_parquet(pred_path, index=False)
    report_path = report_path or REPORT_DIR / f"{date.today():%Y-%m-%d}_backtest.md"
    write_report(preds, report_path, confidences, test_seasons, partial_seasons, refit, runtimes)
    log.info("wrote %s and %s", pred_path, report_path)
    return preds


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", default="league_avg,team_avg,negbin,qgbm")
    ap.add_argument("--seasons", default=",".join(DEFAULT_TEST_SEASONS), help="complete test seasons, comma-separated")
    ap.add_argument("--partial", default=",".join(PARTIAL_SEASONS), help="in-progress seasons reported separately ('' for none)")
    ap.add_argument("--refit", choices=["season", "month"], default="season")
    ap.add_argument("--features", type=Path, default=FEATURES_PATH)
    ap.add_argument("--predictions", type=Path, default=PRED_PATH)
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--report-only", action="store_true", help="rebuild the report from saved predictions")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
    if args.report_only:
        preds = pd.read_parquet(args.predictions)
        preds = preds[preds["model"].isin(split(args.models))]
        out = args.report or REPORT_DIR / f"{date.today():%Y-%m-%d}_backtest.md"
        write_report(preds, out, CONFIDENCES, split(args.seasons), split(args.partial), args.refit, {})
        log.info("wrote %s", out)
        return
    run(split(args.models), split(args.seasons), split(args.partial), args.refit,
        args.features, args.predictions, args.report)


if __name__ == "__main__":
    main()
