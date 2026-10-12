"""Settle scanned picks against results, keep a track record, and render a static dashboard.

    python -m shots.predict.track              # settle + backfill (if missing) + reports/dashboard.html
    python -m shots.predict.track --rebuild    # refit the historical backfill
    python -m shots.predict.track --min-prob 0.7

Files: data/processed/track_record.parquet (every official pick, settled or not),
data/processed/track_backfill.parquet (out-of-sample season replay, one model fit before the season).
"""

from __future__ import annotations

import argparse
import html
import logging
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from shots.ingest import football_data as fd
from shots.models import MODELS

log = logging.getLogger(__name__)

PROCESSED = fd.ROOT / "data" / "processed"
PICKS_DIR = fd.ROOT / "reports" / "picks"
MATCHES_PATH = PROCESSED / "matches.parquet"
FEATURES_PATH = PROCESSED / "features.parquet"
RECORD_PATH = PROCESSED / "track_record.parquet"
BACKFILL_PATH = PROCESSED / "track_backfill.parquet"
DASHBOARD_PATH = fd.ROOT / "reports" / "dashboard.html"
DEFAULT_MODEL = "negbin_pit_comp"
BANDS = [(i / 10, (i + 1) / 10) for i in range(10)]

_NAME = re.compile(r"^(?:(?P<replay>replay_)(?P<rdate>\d{4}-\d{2}-\d{2})|(?P<ldate>\d{4}-\d{2}-\d{2})_(?P<ltime>\d{4}))"
                   r"_at_least_(?P<n>\d+)$")


# ----------------------------------------------------------------------------- settle

def parse_filename(path) -> dict | None:
    """-> {source, scan_ts, n} or None if the name is not a picks file."""
    m = _NAME.match(Path(path).stem)
    if not m:
        return None
    if m["replay"]:
        return dict(source="replay", scan_ts=pd.Timestamp(m["rdate"]), n=int(m["n"]))
    ts = pd.Timestamp(f"{m['ldate']} {m['ltime'][:2]}:{m['ltime'][2:]}")
    return dict(source="live", scan_ts=ts, n=int(m["n"]))


def load_picks(picks_dir: Path = PICKS_DIR) -> pd.DataFrame:
    """All picks files stacked, with columns n, p, source, scan_ts, scan_file. Extra columns kept."""
    frames = []
    for path in sorted(Path(picks_dir).glob("*.csv")):
        info = parse_filename(path)
        if info is None:
            continue
        df = pd.read_csv(path)
        pcol = f"p_at_least_{info['n']}"
        if df.empty or pcol not in df or "match_id" not in df:
            continue
        df = df.assign(n=info["n"], p=df[pcol], source=info["source"], scan_ts=info["scan_ts"],
                       scan_file=path.name)
        df["date"] = pd.to_datetime(df["date"])
        frames.append(df.drop(columns=[c for c in df if re.fullmatch(r"p_at_least_\d+", c)]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _country(competition) -> str | None:
    from shots.ingest.calendar_ics import TIMEZONES

    comp = str(competition or "")
    return next((c for c in TIMEZONES if comp.startswith(c + " ")), None)


def before_kickoff(picks: pd.DataFrame) -> pd.Series:
    """True where the scan was taken before the match locked: the same lock moment as the database
    (shots.db.load.lock_moment: the row's "HH:MM" read as league-local and as UK time, the earlier
    one; no time -> 00:00 UTC of the match date). scan_ts is this machine's wall clock."""
    from shots.db.load import local_to_utc, lock_moment

    kick = picks["kickoff"] if "kickoff" in picks else pd.Series(None, index=picks.index)
    comp = picks["competition"] if "competition" in picks else pd.Series(None, index=picks.index)
    locks = {}
    out = []
    for day, ko, c, ts in zip(picks["date"], kick, comp, picks["scan_ts"]):
        ko = ko if isinstance(ko, str) else None
        key = (day, ko, c)
        if key not in locks:
            locks[key] = lock_moment(pd.Timestamp(day).date(), None, ko, _country(c))
        out.append(local_to_utc(pd.Timestamp(ts).to_pydatetime()) < locks[key])
    return pd.Series(out, index=picks.index, dtype=bool)


def dedupe(picks: pd.DataFrame) -> pd.DataFrame:
    """One official pick per (match_id, N): the last scan taken on or before the match date AND
    before kick-off (same rule as the database's v_official_picks). Scans made after kick-off are
    not pre-match predictions and are dropped."""
    if picks.empty:
        return picks
    pre = picks[(picks["scan_ts"].dt.normalize() <= picks["date"]) & before_kickoff(picks)]
    pre = pre.sort_values(["scan_ts", "scan_file"])
    return pre.drop_duplicates(["match_id", "n"], keep="last").reset_index(drop=True)


def settle(picks_dir: Path = PICKS_DIR, matches_path: Path = MATCHES_PATH,
           out_path: Path | None = RECORD_PATH) -> pd.DataFrame:
    """Official picks joined to results. status: settled | pending (no result row yet) |
    void (result present but no shots data). hit is NA unless settled."""
    picks = dedupe(load_picks(picks_dir))
    if picks.empty:
        return picks
    m = pd.read_parquet(matches_path, columns=["match_id", "home_shots", "away_shots"])
    m = m.assign(actual_shots=(m["home_shots"] + m["away_shots"])).set_index("match_id")
    picks = picks.drop(columns=["actual_shots", "hit"], errors="ignore")
    known = picks["match_id"].isin(m.index)
    picks["actual_shots"] = picks["match_id"].map(m["actual_shots"])
    picks["status"] = np.select([~known, picks["actual_shots"].isna()], ["pending", "void"], "settled")
    picks["hit"] = (picks["actual_shots"] >= picks["n"]).astype("boolean")
    picks.loc[picks["status"] != "settled", "hit"] = pd.NA
    picks["actual_shots"] = picks["actual_shots"].astype("Int64")
    picks = picks.sort_values(["date", "n", "match_id"]).reset_index(drop=True)
    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        picks.to_parquet(out_path, index=False)
    return picks


# ----------------------------------------------------------------------------- backfill

def backfill(season: str = "2025/2026", n_values=(22, 24, 26), model_name: str = DEFAULT_MODEL,
             rebuild: bool = False, features_path: Path = FEATURES_PATH,
             out_path: Path = BACKFILL_PATH) -> pd.DataFrame:
    """Out-of-sample record: fit once on matches before the season's first date, score the season."""
    if Path(out_path).exists() and not rebuild:
        return pd.read_parquet(out_path)
    feats = pd.read_parquet(features_path)
    feats = feats[feats["total_shots"].notna()]
    test = feats[feats["season"].astype(str) == season]
    start = test["date"].min()
    train = feats[feats["date"] < start]
    log.info("backfill %s: fit on %d matches before %s, score %d", season, len(train), start.date(), len(test))
    model = MODELS[model_name]().fit(train)
    parts = []
    for n in n_values:
        parts.append(pd.DataFrame({
            "match_id": test["match_id"].to_numpy(), "date": pd.to_datetime(test["date"]).to_numpy(),
            "competition": test["competition"].to_numpy(), "N": n,
            "p": model.prob_at_least(test, n), "actual": test["total_shots"].to_numpy()}))
    out = pd.concat(parts, ignore_index=True)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)
    return out


# ----------------------------------------------------------------------------- metrics

def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _hit_stats(df: pd.DataFrame) -> dict:
    n = len(df)
    k = int(df["hit"].sum()) if n else 0
    lo, hi = wilson(k, n)
    return dict(n=n, hits=k, hit_rate=k / n if n else float("nan"),
                mean_p=float(df["p"].mean()) if n else float("nan"), ci_lo=lo, ci_hi=hi)


def reliability(df: pd.DataFrame) -> pd.DataFrame:
    """Rows per 10%-wide probability band [lo, hi) (last band closed): n, mean stated p, realised rate."""
    rows = []
    for lo, hi in BANDS:
        mask = (df["p"] >= lo) & ((df["p"] < hi) if hi < 1 else (df["p"] <= hi))
        sub = df[mask]
        k = int(sub["hit"].sum())
        ci = wilson(k, len(sub))
        rows.append(dict(band=f"{lo:.0%}-{hi:.0%}", lo=lo, hi=hi, n=len(sub),
                         mean_p=float(sub["p"].mean()) if len(sub) else float("nan"),
                         realised=k / len(sub) if len(sub) else float("nan"), ci_lo=ci[0], ci_hi=ci[1]))
    return pd.DataFrame(rows)


def base_rates(feats: pd.DataFrame, n: int, before: pd.Timestamp) -> tuple[dict, float]:
    """Naive baseline: per-competition share of matches with >= n shots, history before `before`."""
    h = feats[(feats["date"] < before) & feats["total_shots"].notna()]
    flag = h["total_shots"] >= n
    return flag.groupby(h["competition"]).mean().to_dict(), float(flag.mean())


def brier(df: pd.DataFrame, baseline: pd.Series | None = None) -> dict:
    y = df["hit"].astype(float)
    out = dict(brier=float(((df["p"] - y) ** 2).mean()) if len(df) else float("nan"))
    if baseline is not None:
        out["brier_base"] = float(((baseline - y) ** 2).mean()) if len(df) else float("nan")
        out["skill"] = 1 - out["brier"] / out["brier_base"] if len(df) else float("nan")
    return out


def cumulative(sel: pd.DataFrame, freq: str = "W") -> pd.DataFrame:
    """Cumulative hit rate and cumulative mean stated p, sampled at the end of each week."""
    if sel.empty:
        return pd.DataFrame(columns=["period", "n", "hit_rate", "mean_p"])
    s = sel.sort_values("date")
    s = s.assign(period=s["date"].dt.to_period(freq).dt.end_time.dt.normalize(), h=s["hit"].astype(float))
    g = s.groupby("period").agg(n=("h", "size"), hits=("h", "sum"), psum=("p", "sum"))
    g[["n", "hits", "psum"]] = g[["n", "hits", "psum"]].cumsum()
    return pd.DataFrame(dict(period=g.index, n=g["n"].to_numpy(), hit_rate=(g["hits"] / g["n"]).to_numpy(),
                             mean_p=(g["psum"] / g["n"]).to_numpy()))


def per_competition(sel: pd.DataFrame) -> pd.DataFrame:
    cols = ["competition", "n", "hits", "hit_rate", "mean_p", "ci_lo", "ci_hi"]
    rows = [dict(competition=c, **_hit_stats(g)) for c, g in sel.groupby("competition")]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols).sort_values("n", ascending=False).reset_index(drop=True)


def metrics(df: pd.DataFrame, min_prob: float = 0.70, feats: pd.DataFrame | None = None) -> dict:
    """df: settled rows with columns date, competition, n, p, hit (no NA)."""
    df = df.assign(hit=df["hit"].astype(bool))
    sel = df[df["p"] >= min_prob]
    res = dict(all=_hit_stats(df), selected=_hit_stats(sel), reliability=reliability(df),
               by_competition=per_competition(sel), cumulative=cumulative(sel))
    base = None
    if feats is not None and len(df):
        n = int(df["n"].iloc[0])
        rates, glob = base_rates(feats, n, df["date"].min())
        base = df["competition"].map(rates).fillna(glob)
    res["brier"] = brier(df, base)
    return res


# ----------------------------------------------------------------------------- dashboard

def _pct(x, d=1) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{d}f}%"


def _svg_reliability(rel: pd.DataFrame) -> str:
    W, H, L, B, T, R = 360, 340, 40, 36, 12, 12
    pw, ph = W - L - R, H - B - T
    X = lambda v: L + v * pw
    Y = lambda v: T + (1 - v) * ph
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Reliability chart">']
    for t in (0, .25, .5, .75, 1):
        s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                 f'<text class="ax" x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:.0%}</text>'
                 f'<text class="ax" x="{X(t):.1f}" y="{H - B + 16}" text-anchor="middle">{t:.0%}</text>')
    s.append(f'<line class="diag" x1="{X(0)}" y1="{Y(0)}" x2="{X(1)}" y2="{Y(1)}"/>')
    r = rel[rel["n"] > 0]
    for b in r.itertuples():
        s.append(f'<line class="ci" x1="{X(b.mean_p):.1f}" x2="{X(b.mean_p):.1f}" y1="{Y(b.ci_lo):.1f}" y2="{Y(b.ci_hi):.1f}"/>')
    pts = " ".join(f"{X(b.mean_p):.1f},{Y(b.realised):.1f}" for b in r.itertuples())
    s.append(f'<polyline class="line" points="{pts}"/>')
    for b in r.itertuples():
        rad = 3 + min(6, math.sqrt(b.n) / 6)
        s.append(f'<circle class="dot" cx="{X(b.mean_p):.1f}" cy="{Y(b.realised):.1f}" r="{rad:.1f}">'
                 f'<title>{b.band}: n={b.n}, stated {_pct(b.mean_p)}, realised {_pct(b.realised)}</title></circle>')
    s.append(f'<text class="ax" x="{L + pw / 2}" y="{H - 4}" text-anchor="middle">stated probability</text>'
             f'<text class="ax" transform="translate(11,{T + ph / 2}) rotate(-90)" text-anchor="middle">realised rate</text></svg>')
    return "".join(s)


def _svg_cumulative(cum: pd.DataFrame, min_prob: float) -> str:
    if len(cum) < 2:
        return '<p class="muted">Not enough settled picks over time yet.</p>'
    W, H, L, B, T, R = 360, 240, 40, 30, 12, 12
    pw, ph = W - L - R, H - B - T
    lo = max(0.0, min(cum["hit_rate"].min(), cum["mean_p"].min()) - 0.05)
    hi = min(1.0, max(cum["hit_rate"].max(), cum["mean_p"].max(), min_prob) + 0.05)
    t0, t1 = cum["period"].min(), cum["period"].max()
    span = max((t1 - t0).days, 1)
    X = lambda d: L + (d - t0).days / span * pw
    Y = lambda v: T + (1 - (v - lo) / (hi - lo)) * ph
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Cumulative hit rate">']
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                 f'<text class="ax" x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:.0%}</text>')
    s.append(f'<text class="ax" x="{L}" y="{H - 10}">{t0:%d %b %y}</text>'
             f'<text class="ax" x="{W - R}" y="{H - 10}" text-anchor="end">{t1:%d %b %y}</text>')
    exp = " ".join(f"{X(r.period):.1f},{Y(r.mean_p):.1f}" for r in cum.itertuples())
    act = " ".join(f"{X(r.period):.1f},{Y(r.hit_rate):.1f}" for r in cum.itertuples())
    s.append(f'<polyline class="diag" points="{exp}"/><polyline class="line" points="{act}"/></svg>')
    return "".join(s)


def _kpi(label: str, value: str, sub: str = "") -> str:
    return f'<div class="kpi"><div class="kv">{value}</div><div class="kl">{html.escape(label)}</div><div class="ks">{sub}</div></div>'


def _kpis(m: dict, min_prob: float) -> str:
    s = m["selected"]
    if s["n"] == 0:
        return '<p class="muted">No settled selected picks yet.</p>'
    br = m["brier"]
    return '<div class="kpis">' + _kpi("picks settled", str(s["n"]), f"p &ge; {min_prob:.0%}") \
        + _kpi("hit rate", _pct(s["hit_rate"]), f"95% CI {_pct(s['ci_lo'])} to {_pct(s['ci_hi'])}") \
        + _kpi("mean stated P", _pct(s["mean_p"]), f"gap {100 * (s['hit_rate'] - s['mean_p']):+.1f} pp") \
        + _kpi("Brier (all fixtures)", f"{br['brier']:.3f}",
               f"naive league rate {br['brier_base']:.3f}" if "brier_base" in br else "") + "</div>"


def _rel_table(rel: pd.DataFrame) -> str:
    rows = "".join(f"<tr><td>{r.band}</td><td>{r.n}</td><td>{_pct(r.mean_p)}</td><td>{_pct(r.realised)}</td></tr>"
                   for r in rel.itertuples() if r.n)
    return f'<div class="tw"><table><tr><th>Band</th><th>n</th><th>Stated</th><th>Realised</th></tr>{rows}</table></div>'


def _comp_table(c: pd.DataFrame) -> str:
    rows = "".join(f"<tr><td>{html.escape(r.competition)}</td><td>{r.n}</td><td>{_pct(r.hit_rate)}</td>"
                   f"<td>{_pct(r.mean_p)}</td><td>{_pct(r.ci_lo, 0)} to {_pct(r.ci_hi, 0)}</td></tr>" for r in c.itertuples())
    return ('<div class="tw"><table><tr><th>League</th><th>n</th><th>Hit</th><th>Stated</th><th>95% CI</th></tr>'
            f"{rows}</table></div>") if rows else '<p class="muted">No picks.</p>'


def _block(df: pd.DataFrame, key: str, min_prob: float, feats: pd.DataFrame | None, ns) -> str:
    """One tab per N for a settled dataset."""
    tabs, panes = [], []
    for n in ns:
        sub = df[df["n"] == n]
        if sub.empty:
            continue
        m = metrics(sub, min_prob, feats)
        pid = f"{key}_{n}"
        tabs.append(f'<button data-pane="{pid}" class="{"on" if not tabs else ""}">{n}+</button>')
        panes.append(f'<div class="pane" id="{pid}" {"" if not panes else " hidden"}>'
                     f'{_kpis(m, min_prob)}<div class="grid2"><div><h3>Reliability</h3>{_svg_reliability(m["reliability"])}'
                     f'{_rel_table(m["reliability"])}</div><div><h3>Cumulative hit rate (selected)</h3>'
                     f'{_svg_cumulative(m["cumulative"], min_prob)}<p class="muted">Solid: realised. Dashed: mean stated P.</p>'
                     f'<h3>By league (selected)</h3>{_comp_table(m["by_competition"])}</div></div></div>')
    if not panes:
        return '<p class="muted">No settled picks yet.</p>'
    return f'<div><div class="tabs">Total shots threshold: {"".join(tabs)}</div>{"".join(panes)}</div>'


def _upcoming(record: pd.DataFrame, min_prob: float) -> str:
    live = record[(record["source"] == "live") & (record["status"] == "pending")]
    if live.empty:
        return '<p class="muted">No unsettled live picks.</p>'
    last = live["scan_ts"].max()
    up = live[(live["scan_ts"] == last) & (live["p"] >= min_prob)].sort_values(["date", "p"], ascending=[True, False])
    if up.empty:
        return f'<p class="muted">Latest scan ({last:%d %b %H:%M}) had no picks above {min_prob:.0%}.</p>'
    extra = [c for c in ("league_avg_shots", "above_league") if c in up]
    head = "".join(f"<th>{c.replace('_', ' ')}</th>" for c in extra)
    rows = "".join(
        f"<tr><td>{r.date:%a %d %b} {html.escape(str(r.kickoff) if pd.notna(r.kickoff) else '')}</td>"
        f"<td>{html.escape(r.home_team)} - {html.escape(r.away_team)}</td><td>{html.escape(r.competition)}</td>"
        f"<td>{r.n}+</td><td>{_pct(r.p, 0)}</td><td>{r.expected_shots:.0f}</td>"
        + "".join(f"<td>{getattr(r, c):.1f}</td>" if pd.notna(getattr(r, c)) else "<td>-</td>" for c in extra) + "</tr>"
        for r in up.itertuples())
    return (f'<p class="muted">Scan of {last:%d %b %Y %H:%M}, P &ge; {min_prob:.0%}.</p><div class="tw"><table>'
            f'<tr><th>When</th><th>Match</th><th>League</th><th>N</th><th>P</th><th>Exp.</th>{head}</tr>{rows}</table></div>')


CSS = """
:root{--bg:#fafaf8;--fg:#1c1c1a;--mut:#6b6b66;--card:#fff;--bd:#e3e2dc;--acc:#2a6f97;--on-acc:#fff}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161715;--fg:#ecebe6;--mut:#9a9990;--card:#1f201e;--bd:#33342f;--acc:#6bb3dd;--on-acc:#10212b;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#161715;--fg:#ecebe6;--mut:#9a9990;--card:#1f201e;--bd:#33342f;--acc:#6bb3dd;--on-acc:#10212b;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding-block:16px;padding-inline:16px}h1{font-size:1.4rem;margin:.2em 0}h2{font-size:1.15rem;margin:1.6em 0 .4em;border-bottom:1px solid var(--bd);padding-bottom:4px}
h3{font-size:.95rem;margin:1em 0 .3em}.muted{color:var(--mut);font-size:.85rem}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:10px 0}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:10px 12px}.kv{font-size:1.5rem;font-weight:600}.kl{color:var(--mut);font-size:.8rem}.ks{font-size:.8rem}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.85rem}th,td{padding:4px 8px;border-bottom:1px solid var(--bd);text-align:left;white-space:nowrap}th{color:var(--mut);font-weight:500}td,.kv{font-variant-numeric:tabular-nums}
svg{width:100%;height:auto;max-width:420px}.grid{stroke:var(--bd)}.ax{fill:var(--mut);font-size:10px}.diag{stroke:var(--mut);stroke-dasharray:4 3;fill:none}
.line{stroke:var(--acc);stroke-width:2;fill:none}.dot{fill:var(--acc)}.ci{stroke:var(--acc);stroke-opacity:.45;stroke-width:2}
.tabs{font-size:.85rem;color:var(--mut)}.tabs button{border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:6px;padding:3px 10px;margin-left:4px;cursor:pointer}.tabs button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}.tabs button.on{background:var(--acc);color:var(--on-acc);border-color:var(--acc)}
"""
JS = """document.querySelectorAll('.tabs').forEach(t=>t.querySelectorAll('button').forEach(b=>b.onclick=()=>{
const root=t.parentElement;t.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
root.querySelectorAll('.pane').forEach(p=>p.hidden=p.id!==b.dataset.pane);}));"""

METHODS = """<p>P is the model's estimated probability that a match produces at least N total shots (both teams; on
and off target, blocked included), from a negative-binomial model of pre-match team shot rates whose distribution is
recalibrated per competition on recent seasons. Calibration means that among picks stated at about 75%, about 75% should
reach N: the reliability chart plots stated against realised rates; the dashed diagonal is perfect calibration.</p>
<p><b>Live record</b> = picks saved by scans, including dated replays of past rounds (fitted only on earlier results).
Where a match was scanned repeatedly, the last scan on or before match day counts. Matches with no shot data are void.
<b>Backfill</b> = one model fitted on everything before the season, scoring every match of that season: out of sample,
but not rolling. Brier baseline: the per-league share of matches reaching N in earlier history.</p>
<p>Small samples are noisy: every hit rate carries a 95% Wilson interval, and a gap between hit rate and stated P
inside that interval is not evidence of miscalibration. Integer bounds make the model slightly conservative, so
a realised rate a little above stated P is expected.</p>"""


def render(record: pd.DataFrame, back: pd.DataFrame, feats: pd.DataFrame | None, min_prob: float = 0.70,
           standalone: bool = True) -> str:
    """standalone=False returns the page body only (title + style + content), the form the
    Artifact publisher expects; it wraps the document itself."""
    has = len(record) > 0
    live = record[record["status"] == "settled"].copy() if has else record
    counts = record["status"].value_counts().to_dict() if has else {}
    b = back.rename(columns={"N": "n"}).assign(hit=lambda d: d["actual"] >= d["n"]) if len(back) else back
    live_html = (_block(live, "live", min_prob, feats, sorted(live["n"].unique()))
                 if has and len(live) else "<p class='muted'>No settled picks yet.</p>")
    back_html = _block(b, "back", min_prob, feats, sorted(b["n"].unique())) if len(b) else "<p class='muted'>Not built.</p>"
    sections = [
        f"<h2>Live record</h2><p class='muted'>{counts.get('settled', 0)} settled, {counts.get('pending', 0)} pending, "
        f"{counts.get('void', 0)} void (all picks, any probability).</p>{live_html}",
        f"<h2>Historical backfill (2025/26, out of sample)</h2>{back_html}",
        f"<h2>Upcoming picks</h2>{_upcoming(record, min_prob) if has else '<p class=muted>None.</p>'}",
        f"<h2>Methods</h2>{METHODS}"]
    page = (f"<title>Shots track record</title><style>{CSS}</style><main><h1>Shots track record</h1>"
            f'<p class="muted">Selected = stated P &ge; {min_prob:.0%}. Generated {pd.Timestamp.now():%Y-%m-%d %H:%M}.</p>'
            f"{''.join(sections)}</main><script>{JS}</script>")
    if not standalone:
        return page
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>' + page + "</body></html>")


def build_dashboard(min_prob: float = 0.70, rebuild: bool = False, out: Path = DASHBOARD_PATH) -> Path:
    record = settle()
    back = backfill(rebuild=rebuild)
    feats = pd.read_parquet(FEATURES_PATH, columns=["date", "competition", "total_shots"])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(render(record, back, feats, min_prob), encoding="utf-8")
    # Body-only copy for publishing as an Artifact (the publisher adds the document wrapper).
    Path(out).with_name(Path(out).stem + "_publish.html").write_text(
        render(record, back, feats, min_prob, standalone=False), encoding="utf-8")
    return Path(out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-prob", type=float, default=0.70)
    ap.add_argument("--rebuild", action="store_true", help="refit the historical backfill")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(build_dashboard(args.min_prob, args.rebuild))


if __name__ == "__main__":
    main()
