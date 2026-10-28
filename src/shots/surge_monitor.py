"""Monthly monitor of "Surge" (the odds-route model ``cand_odds_surge``, adopted 2026-10-27) against production's
shadow numbers. Verification condition 4 of reports/2026-10-27_surge_verification.md; rule and revert procedure in
reports/2026-10-27_surge_adoption.md.

    python -m shots.surge_monitor [--months 12] [--as-of YYYY-MM-DD] [--out-dir reports/surge_monitor]

Lives outside ``shots.evaluation`` on purpose: it reads settled outcomes from the database (which may include
provisional results), and the modelling packages must never touch the database (tests/test_db_provisional.py).

Reads the database (``SHOTS_DB``) only:
- **Surge picks**: the official live record (``v_official_picks``, N = 24, settled, pre-match) whose prediction was
  made by Surge (``predictions.model`` starting with ``cand_odds_surge``).
- **Shadow**: for each of those matches, production's (``negbin_pit_comp``) P(total >= 24) recorded at board time in
  ``board.meta.json["odds_route"]["shadow"]`` (``shots.predict.board.shadow_production``): same rows, same moment,
  the previous production model. The board run used is the pre-match one (built before the match date) closest in
  time to the pick's scan. Causal by construction: both numbers were stored before kick-off.
- **Fallback share**: ``odds_route.share_non_cold`` of the board runs in the window.

Reported (window = the last ``--months`` complete months up to ``--as-of``):
- 70%+ calls (P >= 0.7) of Surge and of the shadow on the same matches: count, mean stated P, hit rate, and the
  hit-rate difference with a day-block bootstrap CI;
- reliability of the 0.8-0.9 band (stated vs realised) for both;
- fallback share (mean and max over board runs).

**Revert rule.** Over the last 12 months, with at least ``MIN_CALLS`` 70%+ calls for BOTH Surge and the shadow:
if Surge's hit rate trails the shadow's by more than ``REVERT_PP`` (1.5pp), the report says REVERT and the command
exits with status 2. Fewer calls: "not enough calls yet", exit 0. ``MIN_CALLS = 300``: a 75% hit rate has a
standard error of 2.5pp at n = 300, and the paired difference is far tighter because both models call mostly the
same matches (about 90% overlap in the backtest), so 1.5pp is then resolvable at roughly the 1.5-2 SE level.
Writes ``reports/surge_monitor/<YYYY-MM>.md`` (the month of ``--as-of``). Nothing is scheduled by this module.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports" / "surge_monitor"
N = 24
CALL = 0.7
BAND = (0.8, 0.9)
MIN_CALLS = 300
REVERT_PP = 1.5
SURGE_PREFIX = "cand_odds_surge"


# --------------------------------------------------------------------------- data

def load(engine, since: date, until: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(picks, boards). picks: match_id, date, competition, p (Surge), hit, actual, model, scan_ts.
    boards: run built_at, shadow {match_id: p}, share_non_cold, n_odds, n_fallback."""
    from sqlalchemy import text

    sql = text("""
        SELECT v.match_id, v.date, v.competition, v.p, v.hit, v.actual_total AS actual, p.model, v.scan_ts
        FROM v_official_picks v JOIN predictions p ON p.id = v.prediction_id
        WHERE v.n = :n AND v.status = 'settled' AND v.date >= :since AND v.date <= :until""")
    bsql = text("""SELECT id, built_at, meta FROM prediction_runs
                   WHERE kind = 'board' AND status = 'complete' AND built_at >= :since AND built_at <= :until2""")
    with engine.connect() as c:
        picks = pd.DataFrame(c.execute(sql, {"n": N, "since": since.isoformat(), "until": until.isoformat()}).all(),
                             columns=["match_id", "date", "competition", "p", "hit", "actual", "model", "scan_ts"])
        runs = c.execute(bsql, {"since": (since - timedelta(days=40)).isoformat(),
                                "until2": (until + timedelta(days=1)).isoformat()}).all()
    rows = []
    for rid, built, meta in runs:
        meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
        route = meta.get("odds_route") or {}
        shadow = (route.get("shadow") or {})
        rows.append({"run_id": rid, "built_at": pd.Timestamp(built), "shadow": shadow.get("p") or {},
                     "shadow_n": shadow.get("n"), "share_non_cold": route.get("share_non_cold"),
                     "n_odds": route.get("n_odds"), "n_fallback": route.get("n_fallback"),
                     "has_route": bool(route)})
    boards = pd.DataFrame(rows, columns=["run_id", "built_at", "shadow", "shadow_n", "share_non_cold", "n_odds",
                                         "n_fallback", "has_route"])
    if len(picks):
        picks["date"] = pd.to_datetime(picks["date"])
        picks["scan_ts"] = pd.to_datetime(picks["scan_ts"], errors="coerce")
        picks["hit"] = picks["hit"].astype(bool)
        picks["p"] = picks["p"].astype(float)
    return picks, boards


def attach_shadow(picks: pd.DataFrame, boards: pd.DataFrame) -> pd.DataFrame:
    """Surge picks with ``shadow`` = production's P(>= 24) from the pre-match board run closest to the scan."""
    sp = picks[picks["model"].fillna("").str.startswith(SURGE_PREFIX)].copy()
    sp["shadow"] = np.nan
    if sp.empty or boards.empty:
        return sp
    b = boards[boards["shadow_n"].fillna(N).eq(N)]
    for i, r in sp.iterrows():
        cand = b[(b["built_at"] < r["date"] + pd.Timedelta(days=1))
                 & b["shadow"].map(lambda d, m=r["match_id"]: m in d)]
        if cand.empty:
            continue
        ref = r["scan_ts"] if pd.notna(r["scan_ts"]) else r["date"]
        j = (cand["built_at"] - ref).abs().idxmin()
        sp.at[i, "shadow"] = float(cand.at[j, "shadow"][r["match_id"]])
    return sp


# --------------------------------------------------------------------------- metrics

def _calls(p: np.ndarray, hit: np.ndarray) -> dict:
    m = p >= CALL
    return {"n": int(m.sum()), "stated": float(p[m].mean()) if m.any() else None,
            "hit": float(hit[m].mean()) if m.any() else None}


def _band(p: np.ndarray, hit: np.ndarray) -> dict:
    m = (p >= BAND[0]) & (p < BAND[1])
    return {"n": int(m.sum()), "stated": float(p[m].mean()) if m.any() else None,
            "realised": float(hit[m].mean()) if m.any() else None}


def hit_gap_ci(df: pd.DataFrame, n_boot: int = 1000, seed: int = 0) -> tuple[float, float] | None:
    """Day-block bootstrap 95% CI of hit(Surge calls) - hit(shadow calls)."""
    if df.empty:
        return None
    days = pd.factorize(df["date"].dt.normalize())[0]
    nd = days.max() + 1
    hs = np.where(df["p"] >= CALL, df["hit"], 0).astype(float)
    cs = (df["p"] >= CALL).astype(float).to_numpy()
    ho = np.where(df["shadow"] >= CALL, df["hit"], 0).astype(float)
    co = (df["shadow"] >= CALL).astype(float).to_numpy()
    sums = [np.bincount(days, weights=w, minlength=nd) for w in (hs, cs, ho, co)]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_boot):
        idx = rng.integers(0, nd, nd)
        a, b, c, d = (s[idx].sum() for s in sums)
        if b > 0 and d > 0:
            out.append(a / b - c / d)
    if not out:
        return None
    lo, hi = np.percentile(out, [2.5, 97.5])
    return float(lo), float(hi)


def assess(sp: pd.DataFrame, boards: pd.DataFrame) -> dict:
    """All monitor numbers and the verdict (``"REVERT"``, ``"OK"`` or ``"NOT ENOUGH CALLS"``)."""
    both = sp[sp["shadow"].notna()]
    hit = both["hit"].to_numpy(bool).astype(float)
    res = {"n_surge_picks": int(len(sp)), "n_with_shadow": int(len(both)),
           "surge": {"calls": _calls(both["p"].to_numpy(float), hit), "band": _band(both["p"].to_numpy(float), hit)},
           "shadow": {"calls": _calls(both["shadow"].to_numpy(float), hit),
                      "band": _band(both["shadow"].to_numpy(float), hit)}}
    fb = boards[boards["has_route"]]
    res["fallback"] = {"runs": int(len(fb)),
                       "mean_share": float(fb["share_non_cold"].astype(float).mean()) if len(fb) else None,
                       "max_share": float(fb["share_non_cold"].astype(float).max()) if len(fb) else None}
    s, o = res["surge"]["calls"], res["shadow"]["calls"]
    if s["n"] < MIN_CALLS or o["n"] < MIN_CALLS:
        res["verdict"], res["gap_pp"], res["gap_ci_pp"] = "NOT ENOUGH CALLS", None, None
        if s["hit"] is not None and o["hit"] is not None:
            res["gap_pp"] = 100 * (s["hit"] - o["hit"])
        return res
    gap = 100 * (s["hit"] - o["hit"])
    ci = hit_gap_ci(both)
    res["gap_pp"] = gap
    res["gap_ci_pp"] = [100 * ci[0], 100 * ci[1]] if ci else None
    res["verdict"] = "REVERT" if gap < -REVERT_PP else "OK"
    return res


# --------------------------------------------------------------------------- report

def _pct(v) -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def render(res: dict, since: date, until: date) -> str:
    s, o = res["surge"], res["shadow"]
    lines = [f"# Surge monitor, {until:%Y-%m} (window {since} to {until})", "",
             f"**Verdict: {res['verdict']}**", ""]
    if res["verdict"] == "REVERT":
        lines += [f"Surge's 70%+ calls hit {res['gap_pp']:+.1f}pp vs production's shadow (rule: revert below "
                  f"-{REVERT_PP}pp with at least {MIN_CALLS} calls each). Revert procedure: "
                  "reports/2026-10-27_surge_adoption.md (set `ODDS_MODEL = \"negbin_pit_comp\"` in "
                  "src/shots/config.py after the user's approval).", ""]
    elif res["verdict"] == "NOT ENOUGH CALLS":
        lines += [f"Fewer than {MIN_CALLS} 70%+ calls for Surge or the shadow in the window: no decision yet.", ""]
    lines += ["| | Surge | production shadow |", "|---|---|---|",
              f"| settled official N=24 picks by Surge (with shadow) | {res['n_surge_picks']} ({res['n_with_shadow']}) | same matches |",
              f"| 70%+ calls: n | {s['calls']['n']} | {o['calls']['n']} |",
              f"| 70%+ calls: mean stated | {_pct(s['calls']['stated'])} | {_pct(o['calls']['stated'])} |",
              f"| 70%+ calls: hit rate | {_pct(s['calls']['hit'])} | {_pct(o['calls']['hit'])} |",
              f"| band 0.8-0.9: n | {s['band']['n']} | {o['band']['n']} |",
              f"| band 0.8-0.9: stated / realised | {_pct(s['band']['stated'])} / {_pct(s['band']['realised'])} | "
              f"{_pct(o['band']['stated'])} / {_pct(o['band']['realised'])} |", ""]
    if res.get("gap_pp") is not None:
        ci = res.get("gap_ci_pp")
        lines.append(f"Hit-rate gap Surge - shadow: {res['gap_pp']:+.1f}pp"
                     + (f" [95% day-block CI {ci[0]:+.1f}, {ci[1]:+.1f}]" if ci else "") + ".")
    fb = res["fallback"]
    lines += ["", f"Fallback share (odds route, non-cold-start; failure threshold 2%): mean "
              f"{_pct(fb['mean_share'])}, max {_pct(fb['max_share'])} over {fb['runs']} board runs.", "",
              f"Rule: revert when Surge trails the shadow by more than {REVERT_PP}pp over the last 12 months with at "
              f"least {MIN_CALLS} calls each (shots.surge_monitor)."]
    return "\n".join(lines) + "\n"


def window(as_of: date, months: int) -> tuple[date, date]:
    start = (pd.Timestamp(as_of) - pd.DateOffset(months=months)).date()
    return start, as_of


def run(as_of: date, months: int = 12, engine=None, out_dir: Path = OUT_DIR) -> tuple[dict, Path]:
    from shots.db.engine import make_engine

    eng = engine if engine is not None else make_engine()
    since, until = window(as_of, months)
    picks, boards = load(eng, since, until)
    res = assess(attach_shadow(picks, boards), boards)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{until:%Y-%m}.md"
    path.write_text(render(res, since, until), encoding="utf-8")
    return res, path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res, path = run(a.as_of or date.today(), a.months, out_dir=a.out_dir)
    print(f"{res['verdict']}: {path}")
    return 2 if res["verdict"] == "REVERT" else 0


if __name__ == "__main__":
    sys.exit(main())
