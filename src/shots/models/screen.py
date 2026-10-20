"""Model-selection runner for the structural candidates (2026-10-15): walk-forward full curves + scores.

    python -m shots.models.screen run   --models negbin_pit_comp,cand_disp_total --variants odds,noodds \
        --seasons 2023/2024,2024/2025 [--refit season|month] [--jobs 6] [--cache DIR]
    python -m shots.models.screen score --models ... --seasons ... [--ref negbin_pit_comp] [--cache DIR]

Protocol = ``evaluation.backtest.walk_forward`` / ``evaluation.curves``: per test season (or month) a fresh
model is fitted on every row dated strictly before the block's first match (asserted); targets are dropped
from the rows passed to the model; ``noodds`` blanks every ``mkt_*`` column in training and scoring (the
production no-odds model). Each (model, variant, season, refit) job is cached as one parquet:
meta, bounds, the published-style CDF ``cdf_0..cdf_80`` = P(total <= k) (recalibrated), the recalibrated
per-team CDFs ``hcdf_0..40`` / ``acdf_0..40`` and the fit / predict seconds.

Model specs: a registry name, or ``label=name:json`` with constructor kwargs, e.g.
``hl730=cand_recency:{"base_kw": {"half_life_days": 730}}``.

Selection discipline: hyper-parameters and the structural choice are made on validation seasons
(2023/24, 2024/25); 2025/26 and 2026/27 (partial) are scored once for the chosen candidates.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
FEATURES_PATH = ROOT / "data" / "processed" / "features.parquet"
CANDIDATE_FEATURES = ROOT / "data" / "processed" / "features_candidates.parquet"
KMAX = 80
KSIDE = 40
HIDDEN = ("home_shots", "away_shots", "total_shots", "home_sot", "away_sot", "home_goals", "away_goals")
CONF = (0.7, 0.8, 0.9)


def parse_spec(spec: str) -> tuple[str, str, dict]:
    """'label=name:json' | 'name' -> (label, registry name, kwargs)."""
    head, _, kw = spec.partition(":")
    label, _, name = head.partition("=") if "=" in head else (head, "", head)
    return label, name, (json.loads(kw) if kw else {})


def no_odds(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    # every market input, including the candidate market-structure family (mktx_*): fixtures without odds
    out[[c for c in out.columns if c.startswith(("mkt_", "mktx_"))]] = float("nan")
    return out


def _pad(c: np.ndarray, width: int) -> np.ndarray:
    c = np.pad(c, ((0, 0), (0, max(0, width - c.shape[1]))), constant_values=1.0)[:, :width]
    return np.maximum.accumulate(np.clip(c, 0.0, 1.0), axis=1)


def total_curve(model, rows: pd.DataFrame, kmax: int = KMAX) -> np.ndarray:
    """Published CDF P(total <= k): G(F(k)) for the PIT-calibrated models (as ``prob_at_least``)."""
    from shots.models.calibrated import _cdf

    raw = _pad(_cdf(model.nb_, rows), kmax + 1)
    g = np.asarray(rows[model.group]) if model.group else None
    gg = np.repeat(g, kmax + 1) if g is not None else None
    return _pad(model.pit_map_(raw.ravel(), gg).reshape(raw.shape), kmax + 1)


def team_curve(model, rows: pd.DataFrame, side: str, kmax: int = KSIDE) -> np.ndarray:
    from shots.models.calibrated import _side_cdf

    raw = _pad(_side_cdf(model.nb_, rows, side), kmax + 1)
    g = np.asarray(rows[model.group]) if model.group else None
    gg = np.repeat(g, kmax + 1) if g is not None else None
    return _pad(model.team_pit_map_[side](raw.ravel(), gg).reshape(raw.shape), kmax + 1)


def _medians(model, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(raw NB median, median of the published curve) for PIT-calibrated models; NaN otherwise."""
    from shots.models.calibrated import _cdf, curve_median
    from shots.models.common import lb_from_cdf

    if not hasattr(model, "pit_map_") or not hasattr(model, "nb_"):
        nan = np.full(len(rows), np.nan)
        return nan, nan
    raw = _cdf(model.nb_, rows)
    g = rows[model.group] if model.group else None
    return lb_from_cdf(raw, 0.5).astype(float), curve_median(model.pit_map_, raw, g)


def _blocks(test: pd.DataFrame, refit: str):
    if refit == "season":
        return [test]
    return [g for _, g in test.groupby(test["date"].dt.to_period("M"), sort=True)]


def job_path(cache: Path, label: str, variant: str, season: str, refit: str) -> Path:
    return cache / f"{label}__{variant}__{season.replace('/', '-')}__{refit}.parquet"


def run_job(args) -> str:
    spec, variant, season, refit, cache, features_path = args
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    label, name, kw = parse_spec(spec)
    path = job_path(Path(cache), label, variant, season, refit)
    if path.exists():
        return str(path)
    from shots.models import MODELS

    feats = pd.read_parquet(features_path)
    if os.environ.get("SHOTS_SCREEN_EXTRA"):  # phase 2: candidate feature families, merged on match_id
        extra = pd.read_parquet(os.environ["SHOTS_SCREEN_EXTRA"])
        assert extra["match_id"].is_unique and not set(extra.columns) & (set(feats.columns) - {"match_id"})
        feats = feats.merge(extra, on="match_id", how="left", validate="one_to_one")
    feats["date"] = pd.to_datetime(feats["date"])
    trainable = feats[feats["total_shots"].notna()]
    test_all = trainable[trainable["season"].astype(str) == season]
    parts = []
    for test in _blocks(test_all, refit):
        start = test["date"].min()
        train = trainable[trainable["date"] < start]
        if train["date"].max() >= start or train.index.intersection(test.index).size:
            raise AssertionError("training rows not strictly before the block")
        rows = test.drop(columns=[c for c in HIDDEN if c in test.columns])
        tr, te = (train, rows) if variant == "odds" else (no_odds(train), no_odds(rows))
        t0 = time.time()
        model = MODELS[name](**kw).fit(tr.copy())
        t_fit = time.time() - t0
        t0 = time.time()
        pred = model.predict(te.copy())
        cdf = total_curve(model, te.copy())
        t_pred = time.time() - t0
        out = pd.DataFrame({c: test[c].to_numpy() for c in ("match_id", "date", "season", "competition", "tier")})
        out["actual"] = test["total_shots"].to_numpy(float)
        out["home_actual"] = test["home_shots"].to_numpy(float)
        out["away_actual"] = test["away_shots"].to_numpy(float)
        out["n_prior_min"] = np.fmin(test["home_n_prior_matches"], test["away_n_prior_matches"]).to_numpy()
        out["median"] = pred["median"].to_numpy(float)
        # both readings of the integer median (shots.config.MEDIAN_FROM_CURVE off / on), from the same fit
        out["median_raw"], out["median_curve"] = _medians(model, te)
        for q in CONF:
            out[f"lb_{round(q * 100)}"] = pred[f"lb_{round(q * 100)}"].to_numpy(int)
        out["train_end"] = train["date"].max()
        out["t_fit"], out["t_pred"] = t_fit, t_pred
        frames = [out, pd.DataFrame(cdf, columns=[f"cdf_{k}" for k in range(cdf.shape[1])])]
        if getattr(model, "has_team_calibration", False):
            for side, p in (("home", "h"), ("away", "a")):
                tc = team_curve(model, te.copy(), side)
                frames.append(pd.DataFrame(tc, columns=[f"{p}cdf_{k}" for k in range(tc.shape[1])]))
        parts.append(pd.concat([f.reset_index(drop=True) for f in frames], axis=1))
        log.info("%s %s %s block %s: fit %.0fs, predict %.1fs, %d rows", label, variant, season,
                 start.date(), t_fit, t_pred, len(test))
    res = pd.concat(parts, ignore_index=True)
    res.insert(0, "model", label)
    res.insert(1, "variant", variant)
    path.parent.mkdir(parents=True, exist_ok=True)
    res.to_parquet(path, index=False)
    return str(path)


def run(specs, variants, seasons, refit="season", jobs=6, cache: Path | None = None,
        features_path: Path = FEATURES_PATH) -> list[str]:
    cache = Path(cache)
    tasks = [(s, v, se, refit, str(cache), str(features_path)) for s in specs for v in variants for se in seasons]
    tasks = [t for t in tasks if not job_path(cache, parse_spec(t[0])[0], t[1], t[2], refit).exists()]
    log.info("%d jobs to run", len(tasks))
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            return list(ex.map(run_job, tasks))
    return [run_job(t) for t in tasks]


def load(cache: Path, labels, variants, seasons, refit="season") -> pd.DataFrame:
    parts = []
    for lab in labels:
        for v in variants:
            for s in seasons:
                p = job_path(Path(cache), lab, v, s, refit)
                if p.exists():
                    parts.append(pd.read_parquet(p))
                else:
                    log.warning("missing %s", p.name)
    return pd.concat(parts, ignore_index=True)


# --------------------------------------------------------------------------- scores

def _cdf_matrix(df: pd.DataFrame, prefix: str = "cdf_") -> np.ndarray:
    cols = sorted([c for c in df.columns if c.startswith(prefix) and c[len(prefix):].isdigit()],
                  key=lambda c: int(c[len(prefix):]))
    return df[cols].to_numpy(float)


def row_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Per-row scores: rps, logs (total), pinball_q, cov_q, strict_q, team rps (if present)."""
    F = _cdf_matrix(df)
    y = df["actual"].to_numpy(float).astype(int)
    K = F.shape[1]
    ind = (np.arange(K)[None, :] >= y[:, None]).astype(float)
    out = pd.DataFrame(index=df.index)
    out["rps"] = ((F - ind) ** 2).sum(axis=1)
    yi = np.clip(y, 0, K - 1)
    Fy = F[np.arange(len(y)), yi]
    Fy1 = np.where(yi > 0, F[np.arange(len(y)), np.maximum(yi - 1, 0)], 0.0)
    out["logs"] = -np.log(np.clip(Fy - Fy1, 1e-12, None))
    for q in CONF:
        k = round(q * 100)
        lb = df[f"lb_{k}"].to_numpy(float)
        tau = 1 - q
        u = y - lb
        out[f"pin_{k}"] = np.maximum(tau * u, (tau - 1) * u)
        out[f"cov_{k}"] = (y >= lb).astype(float)
        out[f"strict_{k}"] = (y > lb).astype(float)
    out["pin_mean"] = out[[f"pin_{round(q * 100)}" for q in CONF]].mean(axis=1)
    out["ae"] = np.abs(df["median"].to_numpy(float) - y)
    out["err"] = df["median"].to_numpy(float) - y
    out["mean_pred"] = (1.0 - F).sum(axis=1)
    for side, p in (("home", "h"), ("away", "a")):
        if f"{p}cdf_0" in df:
            T = _cdf_matrix(df, f"{p}cdf_")
            ys = df[f"{side}_actual"].to_numpy(float).astype(int)
            ind = (np.arange(T.shape[1])[None, :] >= ys[:, None]).astype(float)
            out[f"rps_{p}"] = ((T - ind) ** 2).sum(axis=1)
    return out


def under_over_reliability(df: pd.DataFrame, lines=range(16, 31), min_p: float = 0.7) -> dict:
    """Stated vs realised for confident picks on lines N.5: under = P(total <= N), over = P(total >= N + 1);
    plus the mean |gap| over 10% bands of P(under) (pooled lines)."""
    F = _cdf_matrix(df)
    y = df["actual"].to_numpy(float)
    st_u, re_u, st_o, re_o, allp, allr = [], [], [], [], [], []
    for n in lines:
        pu = F[:, n]
        ru = (y <= n).astype(float)
        allp.append(pu)
        allr.append(ru)
        m = pu >= min_p
        st_u.append(pu[m]); re_u.append(ru[m])
        m = (1 - pu) >= min_p
        st_o.append(1 - pu[m]); re_o.append(1 - ru[m])
    su, ru, so, ro = map(np.concatenate, (st_u, re_u, st_o, re_o))
    ap, ar = np.concatenate(allp), np.concatenate(allr)
    bands = np.clip((ap * 10).astype(int), 0, 9)
    gaps = [abs(ar[bands == b].mean() - ap[bands == b].mean()) for b in range(10) if (bands == b).sum() > 200]
    return {"under_stated": su.mean(), "under_real": ru.mean(), "under_gap": ru.mean() - su.mean(), "n_under": len(su),
            "over_stated": so.mean(), "over_real": ro.mean(), "over_gap": ro.mean() - so.mean(), "n_over": len(so),
            "band_mae": float(np.mean(gaps))}


def summary(df: pd.DataFrame) -> dict:
    s = row_scores(df)
    out = {"n": len(df), "rps": s["rps"].mean(), "logs": s["logs"].mean()}
    for q in CONF:
        k = round(q * 100)
        out[f"pin_{k}"] = s[f"pin_{k}"].mean()
    out["pin_mean"] = s["pin_mean"].mean()
    for q in CONF:
        k = round(q * 100)
        out[f"cov_{k}"] = s[f"cov_{k}"].mean()
        out[f"mid_{k}"] = 0.5 * (s[f"cov_{k}"].mean() + s[f"strict_{k}"].mean())
    out["mae"] = s["ae"].mean()
    out["bias"] = s["err"].mean()
    out["mean_bias"] = (s["mean_pred"] - df["actual"]).mean()
    if "rps_h" in s:
        out["rps_team"] = 0.5 * (s["rps_h"].mean() + s["rps_a"].mean())
    comp = df.assign(**{f"c{k}": s[f"cov_{round(q * 100)}"] for k, q in zip((70, 80, 90), CONF)})
    by = comp.groupby("competition")[["c70", "c80", "c90"]].mean()
    for k in (70, 80, 90):
        out[f"worst_comp_{k}"] = by[f"c{k}"].min()
    out["worst_comp_rps_ratio"] = None
    aug = df["date"].dt.month.isin([8, 9]).to_numpy()
    out["rps_augsep"] = s.loc[aug, "rps"].mean() if aug.any() else np.nan
    out["t_fit"] = df.groupby(["season", "train_end"])["t_fit"].first().mean()
    out["t_pred_per_1k"] = (df.groupby(["season", "train_end"])["t_pred"].first().sum() / len(df) * 1000)
    out.update({f"uo_{k}": v for k, v in under_over_reliability(df).items()})
    return out


def paired_ci(a: pd.DataFrame, b: pd.DataFrame, col: str, n_boot: int = 1000, seed: int = 0) -> tuple[float, float, float]:
    """Mean of per-row score difference a - b with a day-block bootstrap 95% CI (rows matched on match_id)."""
    sa = row_scores(a).assign(match_id=a["match_id"].to_numpy(), date=a["date"].to_numpy())
    sb = row_scores(b).assign(match_id=b["match_id"].to_numpy())
    m = sa.merge(sb[["match_id", col]], on="match_id", suffixes=("", "_b"))
    d = (m[col] - m[f"{col}_b"]).to_numpy()
    days = pd.factorize(pd.to_datetime(m["date"]).dt.normalize())[0]
    sums = np.bincount(days, weights=d)
    cnts = np.bincount(days).astype(float)
    rng = np.random.default_rng(seed)
    nd = len(sums)
    boots = []
    for _ in range(n_boot):
        idx = rng.integers(0, nd, nd)
        boots.append(sums[idx].sum() / cnts[idx].sum())
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(d.mean()), float(lo), float(hi)


def score_table(df: pd.DataFrame, ref: str | None = None, ci_cols=("rps", "logs", "pin_mean")) -> pd.DataFrame:
    rows = []
    for (m, v), g in df.groupby(["model", "variant"], sort=False):
        r = {"model": m, "variant": v, **summary(g)}
        if ref and m != ref:
            base = df[(df["model"] == ref) & (df["variant"] == v)]
            if len(base):
                for c in ci_cols:
                    mean, lo, hi = paired_ci(g, base, c)
                    r[f"d_{c}"], r[f"d_{c}_lo"], r[f"d_{c}_hi"] = mean, lo, hi
                if "rps_team" in r and f"hcdf_0" in base:
                    for c in ("rps_h", "rps_a"):
                        mean, lo, hi = paired_ci(g, base, c)
                        r[f"d_{c}"], r[f"d_{c}_lo"], r[f"d_{c}_hi"] = mean, lo, hi
        rows.append(r)
    return pd.DataFrame(rows)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "score"])
    ap.add_argument("--models", required=True)
    ap.add_argument("--variants", default="odds,noodds")
    ap.add_argument("--seasons", default="2023/2024,2024/2025")
    ap.add_argument("--refit", default="season", choices=("season", "month"))
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--ref", default="negbin_pit_comp")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    specs = [s for s in a.models.split(";" if (";" in a.models or "{" in a.models) else ",") if s]
    variants, seasons = a.variants.split(","), a.seasons.split(",")
    if a.cmd == "run":
        run(specs, variants, seasons, a.refit, a.jobs, a.cache)
        return
    labels = [parse_spec(s)[0] for s in specs]
    df = load(a.cache, labels, variants, seasons, a.refit)
    tab = score_table(df, a.ref)
    pd.set_option("display.width", 250, "display.max_columns", 80)
    print(tab.round(4).to_string())
    if a.out:
        tab.to_csv(a.out, index=False)


if __name__ == "__main__":
    main()
