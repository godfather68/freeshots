"""Granularity-aware calibration checks and the ship rule (TASKS 3b.4).

Integer lower bounds of a discrete outcome cannot cover with rate exactly q without randomizing
or wasting pinball loss: the pinball-optimal bound k = min{k : F(k) > 1 - q} of a calibrated
predictive CDF F covers with probability 1 - F(k - 1) >= q, i.e. over-covers by up to the mass at
the bound. These helpers separate that granularity margin from miscalibration.

Inputs are backtest prediction frames (``backtest.walk_forward``) with ``actual``, ``lb_*`` and,
for models with a predictive distribution, the scoring-time diagnostics ``pit_lo`` = F(y - 1),
``pit_hi`` = F(y), ``p_<k>`` = stated P(total >= lb_q), ``pmf_<k>`` = stated P(total == lb_q).

- ``strict``  = share(actual > lb)   (<= q for the pinball-optimal bound of a calibrated F)
- ``cov``     = share(actual >= lb)  (>= q, idem)
- ``mid``     = (strict + cov) / 2   (model-free proxy; ~= q when the bound sits at the integer
                quantile of a calibrated distribution)
- ``rand``    = mean_i P(U_i > 1 - q), U_i ~ U(pit_lo_i, pit_hi_i): coverage of the randomized
                PIT (Czado, Gneiting & Held 2009). Equals q in expectation iff F is calibrated at
                level 1 - q; independent of how the bound is rounded.
- ``stated``  = mean p_q: the coverage the model itself expects from its integer bounds.
                cov - stated ~= 0 means the observed over-coverage is fully explained by
                granularity under the model's own distribution.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

from shots.models.base import CONFIDENCES, bound_column


def randomized_coverage(pit_lo, pit_hi, q: float) -> np.ndarray:
    """Per-row P(U > 1 - q) for U ~ Uniform(pit_lo, pit_hi) (point mass if equal)."""
    lo, hi = np.asarray(pit_lo, float), np.asarray(pit_hi, float)
    t = 1.0 - q
    den = hi - lo
    frac = np.where(den > 1e-12, (hi - t) / np.where(den > 1e-12, den, 1.0), (hi > t).astype(float))
    return np.clip(frac, 0.0, 1.0)


def pit_histogram(pit_lo, pit_hi, bins: int = 10) -> np.ndarray:
    """Expected share of the randomized PIT in each of ``bins`` equal bins (uniform = 1/bins)."""
    edges = np.linspace(0, 1, bins + 1)
    cdf = np.array([1.0 - randomized_coverage(pit_lo, pit_hi, 1.0 - e).mean() for e in edges])
    return np.diff(cdf)


def granularity_table(df: pd.DataFrame, confidences: Sequence[float] = CONFIDENCES) -> dict[str, float]:
    """strict / cov / mid (+ rand / stated / pmf when diagnostics are present) per q, one slice."""
    df = df[df["actual"].notna()]
    y = df["actual"].to_numpy(float)
    out: dict[str, float] = {"n": float(len(df))}
    has_pit = "pit_lo" in df and df["pit_lo"].notna().all()
    for q in confidences:
        k = round(q * 100)
        lb = df[bound_column(q)].to_numpy(float)
        cov, strict = float(np.mean(y >= lb)), float(np.mean(y > lb))
        out[f"cov_{k}"], out[f"strict_{k}"], out[f"mid_{k}"] = cov, strict, (cov + strict) / 2
        if has_pit:
            out[f"rand_{k}"] = float(randomized_coverage(df["pit_lo"], df["pit_hi"], q).mean())
            out[f"stated_{k}"] = float(df[f"p_{k}"].mean())
            out[f"pmf_{k}"] = float(df[f"pmf_{k}"].mean())
    return out


def granularity_breakdown(df: pd.DataFrame, by: str | None,
                          confidences: Sequence[float] = CONFIDENCES) -> pd.DataFrame:
    if by is None:
        return pd.DataFrame([granularity_table(df, confidences)], index=pd.Index(["all"], name="slice"))
    rows = {k: granularity_table(g, confidences) for k, g in df.groupby(by, sort=True, observed=True)}
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.name = by
    return out


def reliability_table(p, hit, edges: Sequence[float] = (0, .7, .75, .8, .85, .9, .95, 1.0001)) -> pd.DataFrame:
    """Stated probability vs realised frequency, binned on the stated probability."""
    p, hit = np.asarray(p, float), np.asarray(hit, float)
    b = pd.cut(p, list(edges), right=False)
    t = pd.DataFrame({"bin": b, "p": p, "hit": hit}).groupby("bin", observed=True).agg(
        n=("hit", "size"), stated=("p", "mean"), realised=("hit", "mean"))
    t["gap_pp"] = 100 * (t["realised"] - t["stated"])
    t["se_pp"] = 100 * np.sqrt(t["stated"] * (1 - t["stated"]) / t["n"])
    return t


def paired_bootstrap(a, b, n_boot: int = 500, seed: int = 0, block=None) -> tuple[float, float, float]:
    """Mean of (a - b) with a 95% bootstrap CI. ``block`` (e.g. match date) resamples whole blocks
    so same-day matches are not treated as independent."""
    d = np.asarray(a, float) - np.asarray(b, float)
    rng = np.random.default_rng(seed)
    if block is None:
        idx = rng.integers(0, len(d), size=(n_boot, len(d)))
        boots = d[idx].mean(axis=1)
    else:
        codes, uniq = pd.factorize(pd.Series(block))
        sums = np.bincount(codes, weights=d, minlength=len(uniq))
        cnts = np.bincount(codes, minlength=len(uniq)).astype(float)
        draw = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
        boots = sums[draw].sum(axis=1) / cnts[draw].sum(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(d.mean()), float(lo), float(hi)


def row_pinball(df: pd.DataFrame, confidences: Sequence[float] = CONFIDENCES) -> np.ndarray:
    """Per-row mean pinball loss over the confidences."""
    y = df["actual"].to_numpy(float)
    losses = []
    for q in confidences:
        u = y - df[bound_column(q)].to_numpy(float)
        tau = 1.0 - q
        losses.append(np.maximum(tau * u, (tau - 1.0) * u))
    return np.mean(losses, axis=0)


# --------------------------------------------------------------------------- ship rule

SHIP_RULE = {
    # CLAUDE.md rule 3 (rounding-aware ship rule, decided 2026-10-08) -- blocking checks:
    "cov_overall_min": 0.0,      # overall coverage >= q
    "cov_comp_min": 0.0,         # coverage >= q in every competition
    "mid_overall_tol": 2.0,      # |mid - q| <= 2pp overall, mid = (P(y >= N) + P(y > N)) / 2
    # pinball no worse than team_avg (point estimate <= 0; CI reported)
    # informational (non-blocking) checks, reported for monitoring:
    "cov_season_min": -1.5,      # every complete test season >= q - 1.5pp
    "mid_comp_tol": 4.0,         # |mid - q| <= 4pp per competition
    "comps_beating_baseline": 0.8,
}


def ship_check(pred: pd.DataFrame, baseline: pd.DataFrame, confidences: Sequence[float] = CONFIDENCES,
               rule: dict | None = None, min_comp_n: int = 0) -> tuple[bool, list[str]]:
    """Apply the CLAUDE.md rule-3 ship rule to ONE model's complete-season predictions.

    ``baseline`` = team_avg predictions on the same matches. Returns (passed, messages); only
    [PASS]/[FAIL] lines are blocking, [info ok]/[info !!] lines are informational.
    """
    r = {**SHIP_RULE, **(rule or {})}
    msgs, ok = [], True

    def check(cond: bool, text: str, blocking: bool = True):
        nonlocal ok
        if blocking:
            ok &= bool(cond)
            tag = "PASS" if cond else "FAIL"
        else:
            tag = "info ok" if cond else "info !!"
        msgs.append(f"[{tag}] {text}")

    o = granularity_table(pred, confidences)
    seas = granularity_breakdown(pred, "season", confidences)
    comp = granularity_breakdown(pred, "competition", confidences)
    comp = comp[comp["n"] >= min_comp_n]
    for q in confidences:
        k = round(q * 100)
        check(100 * (o[f"cov_{k}"] - q) >= r["cov_overall_min"],
              f"q={q}: overall coverage {100 * o[f'cov_{k}']:.1f}% >= {100 * q + r['cov_overall_min']:.1f}%")
        worst_c = comp[f"cov_{k}"].min()
        check(100 * (worst_c - q) >= r["cov_comp_min"],
              f"q={q}: worst competition coverage {100 * worst_c:.1f}% ({comp[f'cov_{k}'].idxmin()}) "
              f">= {100 * q + r['cov_comp_min']:.1f}%")
        check(abs(100 * (o[f"mid_{k}"] - q)) <= r["mid_overall_tol"],
              f"q={q}: overall mid-coverage {100 * o[f'mid_{k}']:.1f}% within +-{r['mid_overall_tol']:.0f}pp")
        worst_s = seas[f"cov_{k}"].min()
        check(100 * (worst_s - q) >= r["cov_season_min"],
              f"q={q}: worst season coverage {100 * worst_s:.1f}% ({seas[f'cov_{k}'].idxmin()}) "
              f">= {100 * q + r['cov_season_min']:.1f}%", blocking=False)
        dev = (100 * (comp[f"mid_{k}"] - q)).abs()
        check(dev.max() <= r["mid_comp_tol"],
              f"q={q}: worst competition |mid - q| {dev.max():.1f}pp ({dev.idxmax()}) <= {r['mid_comp_tol']:.0f}pp",
              blocking=False)
    m = pred[["match_id", "date", "competition", "actual", *[bound_column(q) for q in confidences]]]
    b = baseline[["match_id", *[bound_column(q) for q in confidences]]]
    j = m.merge(b, on="match_id", suffixes=("", "_b"))
    pm = row_pinball(j, confidences)
    pb = row_pinball(j.drop(columns=[bound_column(q) for q in confidences]).rename(
        columns={f"{bound_column(q)}_b": bound_column(q) for q in confidences}), confidences)
    d, lo, hi = paired_bootstrap(pm, pb, block=j["date"])
    check(d <= 0, f"pinball vs team_avg {d:+.4f} [{lo:+.4f}, {hi:+.4f}] (day-block bootstrap 95% CI) <= 0")
    per_comp = pd.Series(pm - pb).groupby(j["competition"].to_numpy()).mean()
    share = float((per_comp < 0).mean())
    check(share >= r["comps_beating_baseline"],
          f"beats team_avg pinball in {int((per_comp < 0).sum())}/{len(per_comp)} competitions",
          blocking=False)
    return ok, msgs


def monthly_drift(pred: pd.DataFrame, q: float = 0.8, z: float = 3.0, min_n: int = 150) -> pd.DataFrame:
    """Coverage per calendar month with a binomial lower control limit q - z * SE.

    Uses the model's stated P(total >= lb) as the expected coverage when available (so the
    granularity margin is not mistaken for headroom), else q. ``alert`` = below the limit.
    """
    k = round(q * 100)
    d = pred[pred["actual"].notna()].copy()
    d["month"] = pd.to_datetime(d["date"]).dt.to_period("M").astype(str)
    d["hit"] = d["actual"] >= d[bound_column(q)]
    exp_col = f"p_{k}" if f"p_{k}" in d and d[f"p_{k}"].notna().all() else None
    g = d.groupby("month").agg(n=("hit", "size"), coverage=("hit", "mean"),
                               expected=(exp_col, "mean") if exp_col else ("hit", lambda s: q),
                               bias=("median", "mean"))
    g["bias"] = g["bias"] - d.groupby("month")["actual"].mean()
    g["lcl"] = g["expected"] - z * np.sqrt(g["expected"] * (1 - g["expected"]) / g["n"])
    g["alert"] = (g["n"] >= min_n) & (g["coverage"] < g["lcl"])
    return g
