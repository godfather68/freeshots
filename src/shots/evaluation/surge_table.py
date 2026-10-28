"""League-table surge signals, season phase and signal stacking (2026-10-28), out of sample.

    python -m shots.evaluation.surge_table study --preds <dir with cand_nbmle/odds + fin_c/noodds, month refit>
        --surge-preds <dir with cand_odds_surge/odds, month refit> [--out reports/2026-10-28_surge_table_signals.json]

Same setup as the over-shoot study (``shots.evaluation.overshoot``, ``reports/2026-10-26_overshoot_patterns.md``):
walk-forward predictions refitted every month on earlier matches only, 2019/20-2025/26; a "surge" = total >= the
published expected (integer curve median) + 5; "stated" = P(total >= expected + 5) on the same curve; CIs by
day-block bootstrap (1,000). Three residual sets:
- ``noodds``: fin_c (production no-odds route) - the one Surge Watch labels most;
- ``odds``: cand_nbmle (= production ``negbin_pit_comp``), the curves the catalogue's odds numbers describe;
- ``odds_surge``: cand_odds_surge (Surge, production on the odds route since 2026-10-27): what is left of each
  pattern after the Surge correction (Surge-scored fixtures are never labelled, so this is for information).

Sections
1. Season phase (``surge_rules.season_phase``): surge rate vs stated per phase, per residual set.
2. Table signals: a small grid of definitions per family (``surge_rules.FAMILIES``). The definition of each family
   is chosen on the discovery seasons 2019/20-2022/23 only, on the no-odds route (largest |z| of the excess,
   discovery n >= 150); then reported on the whole period and on the confirmation seasons 2023/24-2025/26.
   A selected signal applies on a route by the catalogue's rule (95% CI of the excess excludes 0 with the
   signal's sign, sign held in >= 5 of 7 seasons) and, in addition, its confirmation excess has the same sign.
3. Stacking: per fixture, the number of applying boost signals (catalogue + applying table signals, season end
   reduced to one) and the realised surge rate vs stated per count 0 / 1 / 2 / 3+, overall, per phase, per
   period and per season; additivity (realised excess vs the sum of the single-signal excesses), pairwise overlap,
   and the hit rate of 70%+ calls of P(total >= 24) by count.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from shots.evaluation import overshoot as ov
from shots.evaluation import surge as sv
from shots.evaluation import surge_rules as sr

log = logging.getLogger(__name__)

OUT = ov.ROOT / "reports" / "2026-10-28_surge_table_signals.json"
SETS = {"noodds": ("fin_c", "noodds", "preds"), "odds": ("cand_nbmle", "odds", "preds"),
        "odds_surge": ("cand_odds_surge", "odds", "surge")}
CATALOGUE_ROUTE = {"noodds": "noodds", "odds": "odds", "odds_surge": "odds"}
SELECT_ON = "noodds"
MIN_DISC_N = 150
RELAXED_DISC_N = 100
LATE = ("late", "run_in")
NOT_EARLY = ("middle", "late", "run_in")


def grid() -> dict[str, tuple[str, dict]]:
    """Candidate definitions: segment name -> (family, keyword arguments). Fixed before looking at any result."""
    g: dict[str, tuple[str, dict]] = {}

    def add(fam, **kw):
        g[sr.segment_name(fam, kw)] = (fam, kw)

    for pts in (3, 6):
        for ph in (LATE, ("run_in",)):
            add("title_race", pts=pts, phases=ph)
            add("six_pointer", pts=pts, phases=ph)
    for gap in (0.6, 0.75):
        for ph in (NOT_EARLY, LATE):
            add("top_bottom", gap=gap, phases=ph)
        add("top_bottom", gap=gap, phases=NOT_EARLY, home_top=True)
        add("top_bottom", gap=gap, phases=NOT_EARLY, home_top=False)
    for k in (1.0, 1.5, 2.0):
        add("mid_table", k=k, phases=LATE)
    for pts in (1, 3):
        add("needs_win", pts=pts, both=False, phases=("run_in",))
        add("needs_win", pts=pts, both=False, phases=LATE)
    for where in ("top", "bottom", "any"):
        add("promoted", where=where, phases=NOT_EARLY)
    add("promoted", where="bottom", phases=LATE)
    return g


# --------------------------------------------------------------------------- frames

def table_x(d: pd.DataFrame) -> pd.DataFrame:
    """``surge_rules.TABLE_INPUTS`` on a study frame (ctx columns of features_candidates; promotion from
    ``overshoot._promoted``, the same definition as ``recent_form.form_promo``)."""
    out = {}
    for s in ("home", "away"):
        for k in ("rank_frac", "gap_title", "gap_safety", "games_left"):
            out[f"{s}_{k}"] = d[f"{s}_ctx_{k}"]
        out[f"{s}_promo"] = d[f"{s}_promo"]
    out["progress"] = d["ctx_progress"]
    return pd.DataFrame(out, index=d.index)


def load_frames(preds: Path, surge_preds: Path) -> dict[str, pd.DataFrame]:
    cond = ov.conditioning_frame()
    teams = cond[["match_id", "home_team_id", "away_team_id"]]
    out = {}
    for name, (label, variant, where) in SETS.items():
        d = ov.load_route(preds if where == "preds" else surge_preds, label, variant)
        d = d.merge(cond.drop(columns=["date"]), on="match_id", how="left", validate="one_to_one")
        d = d.merge(ov.residual_momentum(d, teams), on="match_id", how="left")
        d["_mkt_gap"] = np.nan
        ov.segments(d)  # defines _goals5 / _ekf3 / _rgap used by the catalogue's rule
        d["phase"] = sr.phase_of(table_x(d))
        out[name] = d
        log.info("%s: %d rows", name, len(d))
    return out


# --------------------------------------------------------------------------- statistics

def _base(d: pd.DataFrame, boot: ov.DayBoot) -> dict:
    allm = np.ones(len(d), bool)
    nb = boot.sums(np.ones(len(d)), allm)
    base = {}
    for k in ("over5", "over6", "under5"):
        base[k] = float(d[k].mean())
        base[f"{k}_boot"] = boot.sums(d[k].to_numpy(float), allm) / nb
    return base


def cell(d: pd.DataFrame, mask: np.ndarray, boot: ov.DayBoot, y: str = "over5", st: str = "st_over5") -> dict:
    """n, realised, stated, excess with its 95% CI, seasons held (excess > 0, seasons with 20+ rows), and the
    discovery / confirmation excess."""
    mask = np.asarray(mask, bool)
    n = int(mask.sum())
    out: dict = {"n": n}
    if n < 30:
        return out
    yy, ss = d[y].to_numpy(float), d[st].to_numpy(float)
    nb = boot.sums(np.ones(len(d)), mask)
    ex = boot.sums(yy - ss, mask) / nb
    out.update(rate=float(yy[mask].mean()), stated=float(ss[mask].mean()), excess=float((yy - ss)[mask].mean()),
               excess_ci=ov._ci(ex))
    seas = d["season"].to_numpy()
    per = {}
    for s in ov.SEASONS:
        mm = mask & (seas == s)
        if mm.sum():
            per[s] = [int(mm.sum()), float((yy - ss)[mm].mean())]
    out["per_season"] = per
    out["seasons_pos"] = sum(1 for k, e in per.values() if k >= 20 and e > 0)
    out["seasons_neg"] = sum(1 for k, e in per.values() if k >= 20 and e < 0)
    out["seasons_n"] = sum(1 for k, _ in per.values() if k >= 20)
    for lab, ss_ in (("disc", ov.DISCOVERY), ("conf", ov.CONFIRMATION)):
        mm = mask & np.isin(seas, ss_)
        if mm.sum() >= 30:
            nbb = boot.sums(np.ones(len(d)), mm)
            out[lab] = {"n": int(mm.sum()), "rate": float(yy[mm].mean()), "stated": float(ss[mm].mean()),
                        "excess": float((yy - ss)[mm].mean()), "excess_ci": ov._ci(boot.sums(yy - ss, mm) / nbb)}
    return out


def applies(c: dict, direction: str, need_conf: bool = True) -> bool:
    """The catalogue's rule (CI excludes 0 with the sign, held in >= MIN_SEASONS_HELD seasons) [+ confirmation
    excess of the same sign]."""
    if "excess_ci" not in c:
        return False
    sign = 1 if direction == "boost" else -1
    lo, hi = c["excess_ci"]
    held = c["seasons_pos"] if sign > 0 else c["seasons_neg"]
    ok = (lo > 0 if sign > 0 else hi < 0) and held >= sv.MIN_SEASONS_HELD
    if need_conf:
        ok = ok and "conf" in c and sign * c["conf"]["excess"] > 0
    return bool(ok)


def _z(c: dict) -> float:
    lo, hi = c["excess_ci"]
    se = (hi - lo) / 3.92
    return c["excess"] / se if se > 0 else 0.0


# --------------------------------------------------------------------------- study

def select(frames: dict, boots: dict, g: dict, min_n: int = MIN_DISC_N) -> tuple[dict, dict]:
    """Per family, the definition with the largest discovery |z| on ``SELECT_ON`` (discovery n >= ``min_n``).
    Returns ({family: {"segment", "kwargs", "direction", "z"}}, {segment: discovery cell})."""
    d = frames[SELECT_ON]
    disc_rows = d["season"].isin(ov.DISCOVERY).to_numpy()
    x = table_x(d)
    scores, chosen = {}, {}
    for name, (fam, kw) in g.items():
        m = np.asarray(sr.FAMILIES[fam](x, **kw), bool)
        c = cell(d, m & disc_rows, boots[SELECT_ON])
        scores[name] = {k: c.get(k) for k in ("n", "rate", "stated", "excess", "excess_ci")}
        if c["n"] < min_n or "excess_ci" not in c:
            continue
        z = _z(c)
        scores[name]["z"] = z
        best = chosen.get(fam)
        if best is None or abs(z) > abs(best["z"]):
            chosen[fam] = {"segment": name, "kwargs": kw, "direction": "boost" if z > 0 else "damper", "z": z}
    return chosen, scores


def catalogue_flags(d: pd.DataFrame, cuts: dict) -> pd.DataFrame:
    """Every catalogue signal (``sv.flags``, the live rule) on a study frame, table signals included."""
    return sv.flags(sv.research_inputs(d), cuts)


def reduce_season_end(fl: pd.DataFrame, use: dict[str, bool]) -> pd.DataFrame:
    """Applying boost flags per row, season end reduced to its first applying member (``sv.SEASON_END`` order)."""
    b = fl[[c for c, on in use.items() if on]].astype(bool).copy()
    taken = np.zeros(len(b), bool)
    for c in (c for c in sv.SEASON_END if c in b.columns):
        v = b[c].to_numpy() & ~taken
        taken |= v
        b[c] = v
    return b


def stacking(d: pd.DataFrame, boot: ov.DayBoot, B: pd.DataFrame, single_excess: dict, rows=None) -> dict:
    """Realised vs stated surge rate by number of applying boosts (``B``), overall, per phase, 70%+ calls of
    P(total >= 24), additivity and pairwise overlap. ``rows``: restrict to these rows (bool)."""
    rows = np.ones(len(d), bool) if rows is None else np.asarray(rows, bool)
    k = B.sum(axis=1).to_numpy(int)
    grp = sr.stack_group(k)
    add = np.zeros(len(d))
    for c in B.columns:
        add += B[c].to_numpy(float) * single_excess.get(c, 0.0)
    out: dict = {"groups": {}, "by_phase": {}, "calls_70": {}, "additive": {}, "overlap": {}}
    two = (k >= 2) & rows
    masks = {g: (grp == g) & rows for g in sr.STACK_GROUPS}
    masks["2+"] = two
    for gname, m in masks.items():
        out["groups"][gname] = cell(d, m, boot)
        out["additive"][gname] = float(add[m].mean()) if m.any() else None
    ph_all = d["phase"].to_numpy().astype(str)
    for ph in sr.PHASES:
        out["by_phase"][ph] = {gname: cell(d, m & (ph_all == ph), boot) for gname, m in masks.items()}
    calls = d["p24"].to_numpy(float) >= 0.7
    for gname, m in (("0", (k == 0) & rows), ("1", (k == 1) & rows), ("2+", two), ("all", rows)):
        out["calls_70"][gname] = cell(d, calls & m, boot, y="hit24", st="p24")
    cols = list(B.columns)
    for a, b in itertools.combinations(cols, 2):
        both = B[a].to_numpy() & B[b].to_numpy() & rows
        if both.sum() >= 100:
            c = cell(d, both, boot)
            out["overlap"][f"{a} + {b}"] = {"n": c["n"], "excess": c["excess"], "excess_ci": c["excess_ci"],
                                            "additive": single_excess.get(a, 0) + single_excess.get(b, 0),
                                            "single": [single_excess.get(a), single_excess.get(b)]}
    out["co_occurrence"] = {a: {b: int((B[a] & B[b] & rows).sum()) for b in cols} for a in cols}
    out["share"] = {gname: float(m.sum() / max(rows.sum(), 1)) for gname, m in masks.items()}
    return out


def strong_rules(d: pd.DataFrame, boot: ov.DayBoot, k: np.ndarray) -> dict:
    """Candidate "strong surge candidate" rules (fixed in advance): 2+ boosts, 3+ boosts, 2+ outside mid-season."""
    mid = d["phase"].to_numpy().astype(str) == "middle"
    rules = {"2+": k >= 2, "3+": k >= 3, "2+ not mid-season": (k >= 2) & ~mid}
    return {name: cell(d, m, boot) for name, m in rules.items()}


def run(preds: Path, surge_preds: Path) -> dict:
    table = sv.load_table()
    cuts = table["cuts"]
    frames = load_frames(preds, surge_preds)
    boots = {k: ov.DayBoot(d["date"].to_numpy()) for k, d in frames.items()}
    g = grid()
    chosen, disc_scores = select(frames, boots, g)
    # families without any definition at the pre-set minimum n: the relaxed choice (n >= 100), recorded only;
    # signals defined this way are held back in the catalogue (chosen after the fact)
    relaxed = {f: c for f, c in select(frames, boots, g, min_n=RELAXED_DISC_N)[0].items() if f not in chosen}
    for sid, (fam, kw) in sr.SELECTED.items():
        if fam in relaxed and sr.segment_name(fam, kw) != relaxed[fam]["segment"]:
            raise AssertionError(f"{sid}: live {sr.segment_name(fam, kw)} != relaxed {relaxed[fam]['segment']}")
        if fam in relaxed and sid not in sv.HELD_BACK:
            raise AssertionError(f"{sid}: chosen after the fact, must be held back")
    log.info("selected: %s", {f: c["segment"] for f, c in chosen.items()})
    for sid, (fam, kw) in sr.SELECTED.items():  # the live definitions are the research's choices
        if fam in chosen and sr.segment_name(fam, kw) != chosen[fam]["segment"]:
            raise AssertionError(f"{sid}: live {sr.segment_name(fam, kw)} != selected {chosen[fam]['segment']}")
    table_ids = list(sr.SELECTED)
    direction = {s.id: s.direction for s in sv.SIGNALS}
    cat = {s["id"]: s for s in table["signals"]}
    res: dict = {"selection": {"on": SELECT_ON, "seasons": ov.DISCOVERY, "min_n": MIN_DISC_N,
                               "chosen": chosen, "relaxed_min_n": RELAXED_DISC_N, "relaxed_chosen": relaxed,
                               "discovery_scores": disc_scores,
                               "live": {sid: {"family": fam, "kwargs": kw, "segment": sr.segment_name(fam, kw)}
                                        for sid, (fam, kw) in sr.SELECTED.items()}}}
    for name, d in frames.items():
        boot = boots[name]
        base = _base(d, boot)
        x = table_x(d)
        segs = {}
        for seg, (fam, kw) in g.items():  # every grid cell, report format (sv.route_stats reads these)
            segs[seg] = ov.segment_stats(d, np.asarray(sr.FAMILIES[fam](x, **kw), bool), boot, base)
        phases = {ph: cell(d, d["phase"].to_numpy().astype(str) == ph, boot) for ph in sr.PHASES}
        fl = catalogue_flags(d, cuts)
        signals = {sid: cell(d, fl[sid].to_numpy(bool), boot) for sid in fl.columns}
        croute = CATALOGUE_ROUTE[name]
        table_applies = {sid: applies(signals[sid], direction[sid]) and sid not in sv.HELD_BACK for sid in table_ids}
        # catalogue signals that count: applying boosts of the committed catalogue (route of this residual set)
        use_cat = {sid: bool(cat[sid]["routes"][croute]["applies"]) and cat[sid]["direction"] == "boost"
                   for sid in fl.columns if sid in cat and sid not in table_ids}
        use = dict(use_cat)
        use.update({sid: table_applies[sid] and direction[sid] == "boost" for sid in table_ids})
        single = {sid: signals[sid].get("excess", 0.0) for sid in fl.columns}
        B = reduce_season_end(fl, use)
        k = B.sum(axis=1).to_numpy(int)
        ph_all = d["phase"].to_numpy().astype(str)
        sig_phase = {sid: {ph: cell(d, fl[sid].to_numpy(bool) & (ph_all == ph), boot) for ph in sr.PHASES}
                     for sid in fl.columns}
        # out of sample: the boost set chosen on the discovery seasons alone (CI of the discovery excess above 0,
        # positive in >= 3 of the 4 discovery seasons), counted and scored on the confirmation seasons only
        disc_use = {}
        for sid in fl.columns:
            c = signals[sid]
            dd = c.get("disc")
            held = sum(1 for s_, (n_, e_) in c.get("per_season", {}).items()
                       if s_ in ov.DISCOVERY and n_ >= 20 and e_ > 0)
            disc_use[sid] = bool(direction[sid] == "boost" and dd is not None and dd["excess_ci"][0] > 0
                                 and held >= 3 and sid not in sv.HELD_BACK)
        Bd = reduce_season_end(fl, disc_use)
        conf_rows = d["season"].isin(ov.CONFIRMATION).to_numpy()
        res[name] = {"n": len(d), "base": {k_: v for k_, v in base.items() if not k_.endswith("_boot")},
                     "segments": segs, "phases": phases, "signals": signals, "table_applies": table_applies,
                     "used": list(B.columns), "signal_phase": sig_phase,
                     "stacking": stacking(d, boot, B, single),
                     "stacking_catalogue_only": stacking(d, boot, reduce_season_end(fl, use_cat), single),
                     "strong_rules": strong_rules(d, boot, k),
                     "oos": {"used": list(Bd.columns),
                             "stacking_confirmation": stacking(d, boot, Bd, single, rows=conf_rows)},
                     "competitions": sorted(d["competition"].unique().tolist())}
        log.info("%s: phases %s", name, {p: round(v.get("excess", 0), 4) for p, v in phases.items()})
    return res


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("study")
    st.add_argument("--preds", type=Path, required=True)
    st.add_argument("--surge-preds", type=Path, required=True)
    st.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    res = run(a.preds, a.surge_preds)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(ov._round(res), indent=1, default=float) + "\n", encoding="utf-8", newline="\n")
    log.info("wrote %s", a.out)


if __name__ == "__main__":
    main()
