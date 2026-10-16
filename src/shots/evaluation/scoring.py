"""Common scoring for every model candidate (spec: docs/evaluation-protocol.md).

The site publishes the whole curve P(total >= n) and over/under at any line, so candidates are
compared on full-distribution scores, not only on the three lower bounds.

Input: one row per (match) with the predictive curve in either form
- ``cdf_0 .. cdf_K``  = P(total <= k)   (preferred; ``shots.evaluation.curves`` writes it), or
- ``ge_1 .. ge_K`` / ``p_<n>`` with n >= 1 = P(total >= n) (board style; must start at n = 1 for RPS),
and the realised total in ``actual`` (or passed separately as ``actuals``).

Per-row scores (``row_scores``):
- ``rps``     ranked probability score over total shots = discrete CRPS
              sum_{k=0..K} (F(k) - 1{y <= k})^2, in shots (not divided by K). Lower is better.
              Equal to the mean absolute error for a point forecast; strictly proper.
- ``logs``    log score -ln P(total = y) (probability floored at ``LOG_FLOOR``). Lower is better.
- ``pin_70/80/90``  pinball loss of the lower bound L_q = max{n : P(total >= n) >= q}
              (tau = 1 - q), the published bounds. Lower is better.
- ``cov_q`` = 1{y >= L_q}, ``strict_q`` = 1{y > L_q}; mid = average (rounding-aware, CLAUDE.md rule 3).
- ``stated_q`` = P(total >= L_q) as stated by the curve; ``rand_q`` randomized-PIT coverage.
- ``ucov_q`` = 1{y <= U_q}, U_q = min{n : P(total <= n) >= q} (upper bound, the "under" side).
- ``median`` = min{k : F(k) >= 0.5}; ``mean`` = sum_k (1 - F(k)); ``abs_err`` / ``err`` of the median.

``score(df)`` aggregates them (means, plus a reliability summary of P(total >= n) over lines
``LINES`` and the randomized PIT histogram). ``score_by(df, by)`` gives one row per group.
``compare(a, b)`` gives paired differences with day-block bootstrap CIs; ``competition_guard``
implements "no competition worse beyond noise".
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

QS = (0.7, 0.8, 0.9)
LINES = tuple(range(12, 41))          # lines n of P(total >= n) used in the reliability summary
LOG_FLOOR = 1e-6
REL_EDGES = tuple(np.round(np.linspace(0, 1, 11), 2))
_CDF_RE = re.compile(r"^cdf_(\d+)$")


# --------------------------------------------------------------------------- curves

def curve_matrix(df: pd.DataFrame, ge_prefix: str = "ge") -> np.ndarray:
    """CDF matrix F[i, k] = P(total_i <= k), k = 0..K, from ``cdf_k`` columns or, if there are none,
    from ``<ge_prefix>_n`` = P(total >= n) columns (``ge_prefix="p"`` reads a board's ``p_n``).

    ``ge`` columns must cover n = 1..K+1 contiguously (P(total >= 0) = 1 is implicit): the board's
    p_10..p_45 is NOT enough for RPS / log score (mass below 10 or above 45 is unknown). The result is
    clipped to [0, 1] and made non-decreasing (``check_curve`` reports curves that were not)."""
    cdf_cols = sorted((int(m.group(1)), c) for c in df.columns if (m := _CDF_RE.match(str(c))))
    if cdf_cols:
        ks = [k for k, _ in cdf_cols]
        if ks != list(range(len(ks))):
            raise ValueError("cdf_k columns must be contiguous from k = 0")
        F = df[[c for _, c in cdf_cols]].to_numpy(float)
    else:
        rx = re.compile(rf"^{re.escape(ge_prefix)}_(\d+)$")
        ge_cols = sorted((int(m.group(1)), c) for c in df.columns if (m := rx.match(str(c))))
        if not ge_cols:
            raise ValueError("no curve columns (cdf_k or ge_n) found")
        ns = [n for n, _ in ge_cols]
        if ns != list(range(1, len(ns) + 1)):
            raise ValueError("ge_n columns must cover n = 1..K contiguously to score the full curve "
                             f"(found {ns[0]}..{ns[-1]}); see docs/evaluation-protocol.md")
        F = 1.0 - df[[c for _, c in ge_cols]].to_numpy(float)   # P(total <= n - 1), n = 1..K+1
    F = np.clip(F, 0.0, 1.0)
    return np.maximum.accumulate(F, axis=1)


def check_curve(df: pd.DataFrame, tol: float = 1e-6) -> dict:
    """Sanity of raw curve columns: share of rows non-monotone, outside [0, 1], or with
    mass beyond the grid (F(K) < 1 - 1e-3)."""
    cdf_cols = [c for c in df.columns if _CDF_RE.match(str(c))]
    raw = (df[cdf_cols].to_numpy(float) if cdf_cols else 1.0 - df[[c for c in df.columns
                                                                     if str(c).startswith("ge_")]].to_numpy(float))
    return {"n": int(len(raw)),
            "non_monotone": float(np.mean((np.diff(raw, axis=1) < -tol).any(axis=1))),
            "out_of_range": float(np.mean(((raw < -tol) | (raw > 1 + tol)).any(axis=1))),
            "tail_mass_beyond_grid": float(np.mean(raw[:, -1] < 1 - 1e-3))}


def _at(F: np.ndarray, k: np.ndarray) -> np.ndarray:
    """F_i(k_i) with F(k) = 0 for k < 0 and 1 beyond the grid."""
    k = np.asarray(k).astype(int)
    out = F[np.arange(len(k)), np.clip(k, 0, F.shape[1] - 1)].astype(float)
    out[k < 0] = 0.0
    out[k >= F.shape[1]] = 1.0
    return out


def lower_bound(F: np.ndarray, q: float) -> np.ndarray:
    """L_q = max{n : P(total >= n) >= q} = #{k >= 0 : F(k) <= 1 - q} (F non-decreasing)."""
    return (F <= 1.0 - q + 1e-12).sum(axis=1)


def upper_bound(F: np.ndarray, q: float) -> np.ndarray:
    """U_q = min{n : P(total <= n) >= q}."""
    above = F >= q - 1e-12
    idx = above.argmax(axis=1)
    idx[~above.any(axis=1)] = F.shape[1]
    return idx


def _actual(df: pd.DataFrame, actuals) -> np.ndarray:
    y = np.asarray(df["actual"] if actuals is None else actuals, float)
    if np.isnan(y).any():
        raise ValueError("actuals contain NaN: drop unplayed matches before scoring")
    if not np.allclose(y, np.round(y)) or (y < 0).any():
        raise ValueError("actuals must be non-negative integers")
    return y.astype(int)


# --------------------------------------------------------------------------- per-row scores

def row_scores(df: pd.DataFrame, actuals=None, qs: Sequence[float] = QS) -> pd.DataFrame:
    """Per-row scores (see module docstring), indexed like ``df``."""
    F = curve_matrix(df)
    y = _actual(df, actuals)
    K = F.shape[1] - 1
    if (y > K).any():
        raise ValueError(f"actual > K = {K}: extend the curve grid")
    ks = np.arange(K + 1)[None, :]
    out = pd.DataFrame(index=df.index)
    out["rps"] = ((F - (y[:, None] <= ks)) ** 2).sum(axis=1)
    Fy, Fy1 = _at(F, y), _at(F, y - 1)
    p = Fy - Fy1
    out["p_y"] = p
    out["logs"] = -np.log(np.maximum(p, LOG_FLOOR))
    out["pit_lo"], out["pit_hi"] = Fy1, Fy
    for q in qs:
        k = round(q * 100)
        L = lower_bound(F, q)
        U = upper_bound(F, q)
        u = y - L
        tau = 1.0 - q
        out[f"lb_{k}"] = L
        out[f"pin_{k}"] = np.maximum(tau * u, (tau - 1.0) * u)
        out[f"cov_{k}"] = (y >= L).astype(float)
        out[f"strict_{k}"] = (y > L).astype(float)
        out[f"stated_{k}"] = 1.0 - _at(F, L - 1)
        t = 1.0 - q
        den = Fy - Fy1
        out[f"rand_{k}"] = np.clip(np.where(den > 1e-12, (Fy - t) / np.where(den > 1e-12, den, 1.0),
                                            (Fy > t).astype(float)), 0.0, 1.0)
        out[f"ub_{k}"] = U
        out[f"ucov_{k}"] = (y <= U).astype(float)
        out[f"ustrict_{k}"] = (y < U).astype(float)
    out["pin_mean"] = out[[f"pin_{round(q * 100)}" for q in qs]].mean(axis=1)
    med = (F < 0.5 - 1e-12).sum(axis=1)
    out["median"] = med
    out["mean"] = (1.0 - F).sum(axis=1)
    out["err"] = med - y
    out["abs_err"] = np.abs(med - y)
    out["mean_err"] = out["mean"] - y
    return out


# --------------------------------------------------------------------------- aggregates

def reliability(F: np.ndarray, y: np.ndarray, lines: Iterable[int] = LINES,
                edges: Sequence[float] = REL_EDGES) -> pd.DataFrame:
    """Stated P(total >= n) vs realised, pooled over ``lines`` and binned on the stated value.
    SE is clustered by match (lines of one match are one outcome read at several thresholds)."""
    lines = [n for n in lines if 1 <= n <= F.shape[1]]
    P = np.column_stack([1.0 - F[:, n - 1] for n in lines])
    H = np.column_stack([(y >= n).astype(float) for n in lines])
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (P >= lo) & ((P < hi) if hi < 1 else (P <= hi))
        n = int(m.sum())
        if not n:
            continue
        d = np.where(m, H - P, 0.0).sum(axis=1)
        se = float(np.sqrt((d ** 2).sum()) / n)
        rows.append({"lo": lo, "hi": hi, "n": n, "stated": float(P[m].mean()), "realised": float(H[m].mean()),
                     "gap_pp": 100 * float((H[m] - P[m]).mean()), "se_pp": 100 * se})
    return pd.DataFrame(rows)


def pit_histogram(pit_lo: np.ndarray, pit_hi: np.ndarray, bins: int = 10) -> np.ndarray:
    edges = np.linspace(0, 1, bins + 1)
    lo, hi = np.asarray(pit_lo, float), np.asarray(pit_hi, float)
    den = np.maximum(hi - lo, 1e-12)
    cdf = np.array([np.clip((e - lo) / den, 0, 1).mean() for e in edges])
    return np.diff(cdf)


def score(df: pd.DataFrame, actuals=None, qs: Sequence[float] = QS, lines: Iterable[int] = LINES,
          rows: pd.DataFrame | None = None) -> dict:
    """All headline numbers for one slice. ``rows`` = precomputed ``row_scores`` (optional)."""
    r = row_scores(df, actuals, qs) if rows is None else rows
    out: dict = {"n": int(len(r))}
    if not len(r):
        return out
    for c in ("rps", "logs", "pin_mean", "abs_err", "err", "mean_err"):
        out[c] = float(r[c].mean())
    out["mae"], out["bias"] = out.pop("abs_err"), out.pop("err")
    out["mean_bias"] = out.pop("mean_err")
    out["rmse_mean"] = float(np.sqrt((r["mean_err"] ** 2).mean()))
    out["p_floored"] = float((r["p_y"] < LOG_FLOOR).mean())
    for q in qs:
        k = round(q * 100)
        out[f"pin_{k}"] = float(r[f"pin_{k}"].mean())
        out[f"cov_{k}"] = float(r[f"cov_{k}"].mean())
        out[f"mid_{k}"] = float((r[f"cov_{k}"].mean() + r[f"strict_{k}"].mean()) / 2)
        out[f"stated_{k}"] = float(r[f"stated_{k}"].mean())
        out[f"rand_{k}"] = float(r[f"rand_{k}"].mean())
        out[f"sharp_{k}"] = float(r[f"lb_{k}"].mean())
        out[f"ucov_{k}"] = float(r[f"ucov_{k}"].mean())
        out[f"umid_{k}"] = float((r[f"ucov_{k}"].mean() + r[f"ustrict_{k}"].mean()) / 2)
    hist = pit_histogram(r["pit_lo"], r["pit_hi"])
    out["pit_hist"] = [float(h) for h in hist]
    out["pit_max_dev_pp"] = float(100 * np.max(np.abs(hist - 0.1)))
    F = curve_matrix(df)
    if True:  # reliability of P(total >= n) over the standard lines
        y = _actual(df, actuals)
        rel = reliability(F, y, lines)
        if len(rel):
            w = rel["n"] / rel["n"].sum()
            out["rel_ece_pp"] = float((w * rel["gap_pp"].abs()).sum())
            out["rel_max_gap_pp"] = float(rel.loc[rel["n"] >= 500, "gap_pp"].abs().max()) if (rel["n"] >= 500).any() else float("nan")
            tail = rel[rel["hi"] <= 0.2]           # upper tail: P(total >= n) <= 20%
            out["upper_tail_gap_pp"] = (float((tail["gap_pp"] * tail["n"]).sum() / tail["n"].sum())
                                        if len(tail) else float("nan"))
            head = rel[rel["lo"] >= 0.8]           # lower tail: P(total >= n) >= 80% (bounds region)
            out["lower_tail_gap_pp"] = (float((head["gap_pp"] * head["n"]).sum() / head["n"].sum())
                                        if len(head) else float("nan"))
    return out


def score_by(df: pd.DataFrame, by, actuals=None, qs: Sequence[float] = QS, min_n: int = 1) -> pd.DataFrame:
    """One ``score`` row per group (pit_hist dropped)."""
    if actuals is not None:
        df = df.assign(actual=np.asarray(actuals, float))
    rows = {}
    for key, g in df.groupby(by, sort=True, observed=True):
        if len(g) >= min_n:
            s = score(g, qs=qs)
            s.pop("pit_hist", None)
            rows[key] = s
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.name = by if isinstance(by, str) else "/".join(by)
    return out


# --------------------------------------------------------------------------- comparisons

def block_bootstrap(d, block, n_boot: int = 1000, seed: int = 0) -> tuple[float, float, float]:
    """Mean of d with a 95% CI from resampling whole blocks (e.g. match days)."""
    d = np.asarray(d, float)
    codes, uniq = pd.factorize(pd.Series(np.asarray(block)))
    sums = np.bincount(codes, weights=d, minlength=len(uniq))
    cnts = np.bincount(codes, minlength=len(uniq)).astype(float)
    rng = np.random.default_rng(seed)
    draw = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    boots = sums[draw].sum(axis=1) / cnts[draw].sum(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(d.mean()), float(lo), float(hi)


def _aligned(a: pd.DataFrame, b: pd.DataFrame, key: str = "match_id") -> tuple[pd.DataFrame, pd.DataFrame]:
    common = pd.Index(a[key]).intersection(pd.Index(b[key]))
    if len(common) < max(len(a), len(b)):
        raise ValueError(f"candidates score different matches ({len(a)} vs {len(b)}, {len(common)} common): "
                         "score both on identical rows")
    a = a.set_index(key).loc[common].reset_index()
    b = b.set_index(key).loc[common].reset_index()
    return a, b


def compare(a: pd.DataFrame, b: pd.DataFrame, metrics=("rps", "logs", "pin_mean", "abs_err"),
            n_boot: int = 1000, seed: int = 0, block_col: str = "date") -> dict:
    """Paired difference a - b (negative favours ``a``) per metric with a day-block bootstrap 95% CI.
    Both frames need ``match_id``, ``date``, ``actual`` and curves for the same matches."""
    a, b = _aligned(a, b)
    ra, rb = row_scores(a), row_scores(b)
    return {m: dict(zip(("diff", "lo", "hi"), block_bootstrap(ra[m].to_numpy() - rb[m].to_numpy(),
                                                              a[block_col], n_boot, seed)))
            for m in metrics}


def competition_guard(a: pd.DataFrame, b: pd.DataFrame, metric: str = "rps", by: str = "competition",
                      n_boot: int = 1000, seed: int = 0, block_col: str = "date") -> pd.DataFrame:
    """Per group: paired a - b of ``metric`` with day-block CI. ``worse`` = the CI lies entirely above 0
    (candidate ``a`` significantly worse than ``b`` in that group): the protocol's
    "must not get worse in any competition beyond noise" fails if any group is ``worse``."""
    a, b = _aligned(a, b)
    ra, rb = row_scores(a), row_scores(b)
    d = ra[metric].to_numpy() - rb[metric].to_numpy()
    rows = []
    for key, idx in pd.Series(np.arange(len(a))).groupby(a[by].to_numpy()):
        i = idx.to_numpy()
        m, lo, hi = block_bootstrap(d[i], a[block_col].to_numpy()[i], n_boot, seed)
        rows.append({by: key, "n": len(i), "diff": m, "lo": lo, "hi": hi, "worse": lo > 0, "better": hi < 0})
    return pd.DataFrame(rows).set_index(by)


__all__ = ["QS", "LINES", "curve_matrix", "check_curve", "lower_bound", "upper_bound", "row_scores", "score",
           "score_by", "reliability", "pit_histogram", "block_bootstrap", "compare", "competition_guard"]
