"""Surge Watch (2026-10-27): the over-shoot patterns of ``reports/2026-10-26_overshoot_patterns.md`` as a small,
versioned signal catalogue, and the one rule that flags a fixture with them.

A **surge** is a match that ends 5+ shots above our published expected total (the integer median of the curve).
It happens in about 21% of matches, and the published curve says so too. The research found pre-match conditions
under which it happened more (boosts) or less (dampers) often than the curve stated. Surge Watch only *labels*
fixtures with those conditions: **no probability is changed**, the predictions stay exactly as published.

    python -m shots.evaluation.surge table [--report reports/2026-10-26_overshoot_patterns.json] [--preds DIR]
                                           [--out src/shots/evaluation/surge_signals.json]

``table`` writes the committed catalogue ``surge_signals.json``: per signal and per route (odds / no-odds) the
research's own numbers, copied from the report JSON (n, 5+ over rate, the curve's stated rate, the route's base rate,
the 95% CI of the excess, seasons in which it held) and whether the signal ``applies`` on that route. Nothing is
recomputed at publish time, so the published catalogue is deterministic and versioned with the code.

The segment cut points that are quantiles of the discovery period (low-goal run, rating momentum, rating gap) are
not in the report JSON: with ``--preds`` (the walk-forward predictions the study read, see ``overshoot``), they are
computed by the study's own code (``overshoot.conditioning_frame`` / ``segments``) and checked by re-deriving every
segment count of the report with the live rule (``flags``); without ``--preds`` the cut points already in the table
are kept.

Rules
- A signal **applies** on a route when the research excess (realised - stated) has the signal's sign with a 95% CI
  that excludes 0, and that sign held in at least ``MIN_SEASONS_HELD`` of the seasons with 20+ matches. Otherwise the
  catalogue still shows its numbers (transparency) but no fixture on that route is flagged with it.
- Pooled signals (December or January) combine their month segments: rates weighted by n; the CI of the pooled
  excess is a normal approximation from the month CIs (independent day blocks); per-season excess weighted by n.
- Season end is one group: a fixture gets at most one of ``dead_rubber``, ``dead_team``, ``last_three`` (the first
  in that order that applies), so overlapping definitions are not counted twice.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from shots.evaluation import surge_rules as sr

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
REPORT_JSON = ROOT / "reports" / "2026-10-26_overshoot_patterns.json"
REPORT_MD = "reports/2026-10-26_overshoot_patterns.md"
# league-table signals, season phase and stacking (2026-10-28, ``python -m shots.evaluation.surge_table study``)
TABLE_REPORT_JSON = ROOT / "reports" / "2026-10-28_surge_table_signals.json"
TABLE_REPORT_MD = "reports/2026-10-28_surge_table_signals.md"
TABLE_PATH = Path(__file__).with_name("surge_signals.json")
MARGIN = 5
MIN_SEASONS_HELD = 5
ROUTES = ("odds", "noodds")
# production models the research studied, by route (board `model` label prefix, before the "_<date>" suffix);
# a fixture scored by anything else (fallback model, candidate, experimental) is not assessed
ROUTE_MODELS = {"odds": "negbin_pit_comp", "noodds": "fin_c-noodds"}
SEASON_END = ("dead_rubber", "dead_team", "last_three")
LOW_GOAL_MIN_SHARE = 0.9  # see cut_points: the live low-goal rule is a strict subset of the study segment


@dataclass(frozen=True)
class Signal:
    id: str
    label: str
    direction: str            # "boost" (more surges than the curve said) | "damper" (fewer)
    segments: tuple[str, ...]  # research segment names (report JSON); more than one = pooled
    explanation: str
    source: str = "overshoot"  # which research report holds the segments: "overshoot" (REPORT_JSON) | "table"


SIGNALS: tuple[Signal, ...] = (
    Signal("dead_rubber", "Nothing to play for", "boost", ("dead rubber (both teams nothing to play for)",),
           "Neither team can still reach the title or drop into the relegation places, with 10 or fewer league "
           "games left. Matches with nothing at stake have ended 5+ shots above our expected total more often "
           "than our curve said."),
    Signal("dead_team", "One side with nothing to play for", "boost", ("one dead team",),
           "One of the two teams can no longer reach the title or the relegation places, with 10 or fewer "
           "league games left."),
    Signal("last_three", "Last three league games", "boost", ("last 3 matches (either team)",),
           "At least one of the teams is playing one of its last three league matches of the season."),
    Signal("h2h_above", "Head-to-head runs high", "boost", ("h2h mean total - our expected: [4, 99)",),
           "Earlier meetings of these two clubs averaged at least 4 more shots than our expected total for this "
           "one (recent meetings count more)."),
    Signal("low_goal_run", "Both in a low-scoring run", "boost", ("goals in both teams' last 5 (for + against): Q1",),
           "Both teams' last five matches were low-scoring (bottom fifth). With ordinary shot volume, "
           "low-scoring runs have meant more shooting, not less."),
    Signal("beating_ratings", "Recent games above ratings", "boost",
           ("rating-residual momentum (total - EKF exp), last 3: Q5",),
           "The two teams' last three matches averaged more shots than their opponent-adjusted ratings "
           "expected (top fifth)."),
    Signal("home_mismatch", "Strong home side, weak visitor", "boost",
           ("strong home vs weak away (rating gap top decile, home better)",),
           "The home team's rating is far above the visitor's (top tenth of rating gaps)."),
    Signal("top_vs_bottom", "Top of the table against the bottom", "boost",
           (sr.segment_name(*sr.SELECTED["top_vs_bottom"]),),
           "In the last third of the season, the two teams are far apart in the league table (at least 60% of the "
           "table between them), whichever side is at home.", source="table"),
    Signal("promoted_struggling", "Promoted side near the bottom", "boost",
           (sr.segment_name(*sr.SELECTED["promoted_struggling"]),),
           "In the last third of the season, a team promoted last summer sits in the bottom quarter of the table.",
           source="table"),
    Signal("winter", "December or January", "damper", ("month: 12", "month: 01"),
           "Matches in December and January have ended 5+ shots above our expected total less often than our "
           "curve said."),
    Signal("below_ratings", "Recent games below ratings", "damper",
           ("rating-residual momentum (total - EKF exp), last 3: Q1",),
           "The two teams' last three matches averaged fewer shots than their opponent-adjusted ratings "
           "expected (bottom fifth)."),
    Signal("title_race", "Title race", "damper", (sr.segment_name(*sr.SELECTED["title_race"]),),
           "In the last third of the season, both teams are within 6 points of first place. These matches have "
           "ended 5+ shots above our expected total much less often than our curve said.", source="table"),
)
TABLE_SIGNALS = tuple(s.id for s in SIGNALS if s.source == "table")
BY_ID = {s.id: s for s in SIGNALS}

# Signals kept in the catalogue (so the research numbers stay visible) but never applied to fixtures.
# beating_ratings (2026-10-27): weakest boost (+1.2pp, 5 of 7 seasons, no-odds route only), and its fixed
# discovery-period cut flags about a third of 2026/27 fixtures instead of a fifth, because this season's rating
# residuals run high, so a flag today would not mean what the research measured. Revisit with a per-season cut.
HELD_BACK: dict[str, str] = {
    "beating_ratings": "held back: this season's ratings run high, so the research cut would flag too many matches",
    # title_race (2026-10-28): strong on the no-odds route (-9pp, every season), but its discovery sample was below
    # the study's pre-set minimum (103 < 150 matches), so its definition was chosen after the fact. Shown, not used.
    "title_race": "held back: too few matches in the discovery seasons to choose its definition in advance",
}

METHOD = ("Out of sample: walk-forward predictions of our production models, refitted every month on earlier "
          "matches only, 2019/20 to 2025/26, 21 domestic leagues. 'Usual' is the share of all matches on that "
          "route that ended 5+ shots above our expected total; 'our curve' is what our own published curves "
          "gave for the same matches. Every condition is known before kick-off. A signal is used only on a "
          "route where its difference from our curve has a 95% interval that excludes zero and held in at "
          f"least {MIN_SEASONS_HELD} of the 7 seasons. It is a lean, not a promise: probabilities on the site "
          "are unchanged. A match carries at most one of the three season-end signals (the strongest that "
          "applies).")
ROUTE_LABELS = {"odds": "Scored with odds", "noodds": "Scored without odds"}


# --------------------------------------------------------------------------- catalogue from the report

def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else round(float(x), nd)


def _pool(parts: list[dict], direction: str) -> dict:
    """Combine disjoint segments (rates weighted by n; CI normal approximation from the parts' CIs)."""
    n = sum(p["n"] for p in parts)
    rate = sum(p["n"] * p["over5"]["rate"] for p in parts) / n
    stated = sum(p["n"] * p["over5"]["stated"] for p in parts) / n
    se = math.sqrt(sum((p["n"] / n * (p["over5"]["excess_ci"][1] - p["over5"]["excess_ci"][0]) / 3.92) ** 2
                       for p in parts))
    ex = rate - stated
    per: dict[str, list] = {}
    for p in parts:
        for season, (k, e, _) in p["per_season"].items():
            if e is None:
                continue
            w = per.setdefault(season, [0, 0.0])
            w[0] += k
            w[1] += k * e
    seasons = {s: (k, tot / k) for s, (k, tot) in per.items() if k}
    return {"n": n, "rate": rate, "stated": stated, "excess_ci": [ex - 1.96 * se, ex + 1.96 * se],
            "seasons": seasons, "ci_note": "approximate (pooled)"}


def _single(seg: dict) -> dict:
    o = seg["over5"]
    seasons = {s: (k, e) for s, (k, e, _) in seg["per_season"].items() if e is not None}
    return {"n": seg["n"], "rate": o["rate"], "stated": o["stated"], "excess_ci": list(o["excess_ci"]),
            "seasons": seasons, "ci_note": None}


def route_stats(report_route: dict, sig: Signal) -> dict:
    """The research numbers of one signal on one route, and whether it applies there."""
    segs = [report_route["segments"][name] for name in sig.segments]
    s = _single(segs[0]) if len(segs) == 1 else _pool(segs, sig.direction)
    sign = 1 if sig.direction == "boost" else -1
    counted = {k: v for k, v in s["seasons"].items() if v[0] >= 20}
    held = sum(1 for k, e in counted.values() if sign * e > 0)
    lo, hi = s["excess_ci"]
    ci_ok = lo > 0 if sign > 0 else hi < 0
    return {"n": int(s["n"]), "over_rate": _r(s["rate"]), "stated_rate": _r(s["stated"]),
            "base_rate": _r(report_route["base"]["over5"]), "excess_ci": [_r(lo), _r(hi)],
            "seasons_held": held, "seasons": len(counted),
            "applies": bool(ci_ok and held >= MIN_SEASONS_HELD and sig.id not in HELD_BACK)}


def load_table_report(path: Path | None = None) -> dict:
    return json.loads(Path(path or TABLE_REPORT_JSON).read_text(encoding="utf-8"))


def _stack_cell(c: dict) -> dict:
    """One stacking cell of the table report in catalogue form (rates, CI of the excess, seasons held)."""
    if "excess_ci" not in c:
        return {"n": int(c["n"])}
    return {"n": int(c["n"]), "over_rate": _r(c["rate"]), "stated_rate": _r(c["stated"]),
            "excess_ci": [_r(c["excess_ci"][0]), _r(c["excess_ci"][1])], "seasons_held": int(c["seasons_pos"]),
            "seasons": int(c["seasons_n"])}


STACK_NOTE = ("Matches by number of boost signals that apply (a match counts at most one season-end signal). "
              "'Strong surge candidate' = 2 or more. Out of sample, 2019/20 to 2025/26; the strong rule is used on a "
              "route only where 2+ signals beat our curve with a 95% interval above zero, in at least "
              f"{MIN_SEASONS_HELD} of 7 seasons and in the confirmation seasons 2023/24 to 2025/26.")


def stacking_table(table_report: dict) -> dict:
    """The page's stacking table (counts and rates by number of applying boosts, per route and season phase) and
    whether the "strong surge candidate" rule (``surge_rules.STRONG_MIN_BOOSTS``+ boosts) holds on each route."""
    out = {"report": TABLE_REPORT_MD, "note": STACK_NOTE, "min_boosts": sr.STRONG_MIN_BOOSTS,
           "groups": [*sr.STACK_GROUPS, "2+"], "phases": list(sr.PHASES), "routes": {}}
    for r in ROUTES:
        st = table_report[r]["stacking"]
        two = st["groups"]["2+"]
        oos = table_report[r]["oos"]["stacking_confirmation"]["groups"]["2+"]
        holds = bool("excess_ci" in two and two["excess_ci"][0] > 0 and two["seasons_pos"] >= MIN_SEASONS_HELD
                     and "conf" in two and two["conf"]["excess"] > 0 and "excess" in oos and oos["excess"] > 0)
        out["routes"][r] = {
            "signals": list(table_report[r]["used"]),
            "share": {g: _r(v) for g, v in st["share"].items()},
            "by_count": {g: _stack_cell(c) for g, c in st["groups"].items()},
            "by_phase": {ph: {g: _stack_cell(c) for g, c in cells.items()} for ph, cells in st["by_phase"].items()},
            "season_phase": {ph: _stack_cell(c) for ph, c in table_report[r]["phases"].items()},
            "calls_70": {g: {"n": int(c["n"]), "hit_rate": _r(c.get("rate")), "stated": _r(c.get("stated"))}
                         for g, c in st["calls_70"].items()},
            "strong_applies": holds,
        }
    return out


def build_table(report: dict, cuts: dict, table_report: dict | None = None) -> dict:
    """The committed catalogue (``surge_signals.json``) from the report JSON(s) and the discovery cut points.
    League-table signals (``source="table"``) and the stacking table come from ``table_report`` (default: the
    committed ``TABLE_REPORT_JSON``); the keys ``table_report`` and ``stacking`` are additive."""
    table_report = table_report if table_report is not None else load_table_report()
    src = {"overshoot": report, "table": table_report}
    comps = sorted(set(report["odds"]["competitions"]) & set(report["noodds"]["competitions"]))
    return {
        "report": REPORT_MD,
        "report_date": "2026-10-26",
        "margin": MARGIN,
        "matches": int(report["odds"]["n"]),
        "period": "2019/20 to 2025/26",
        "method": METHOD,
        "min_seasons_held": MIN_SEASONS_HELD,
        "route_models": dict(ROUTE_MODELS),
        "routes": {r: {"label": ROUTE_LABELS[r], "base_rate": _r(report[r]["base"]["over5"])} for r in ROUTES},
        "competitions": comps,
        "cuts": {k: _r(v, 6) for k, v in sorted(cuts.items())},
        "signals": [{"id": s.id, "label": s.label, "direction": s.direction, "group": "season_end" if s.id in SEASON_END else None,
                     "explanation": s.explanation, "segments": list(s.segments),
                     "routes": {r: route_stats(src[s.source][r], s) for r in ROUTES}} for s in SIGNALS],
        "table_report": TABLE_REPORT_MD,
        "stacking": stacking_table(table_report),
    }


def load_table(path: Path | None = None) -> dict:
    return json.loads(Path(path or TABLE_PATH).read_text(encoding="utf-8"))


def table_version(table: dict) -> str:
    """Short content hash of a catalogue: a board run records the version its fixture signals were assigned with,
    and the export publishes them only with that same catalogue."""
    import hashlib

    text = json.dumps(table, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- the rule (shared by research check and live)

INPUTS = ("dead_n", "games_left_min", "h2h_gap", "goals_l5", "res_l3", "rating_gap", "month")


def flags(inputs: pd.DataFrame, cuts: dict) -> pd.DataFrame:
    """Boolean column per signal from the match-level inputs (``INPUTS``; NaN = condition not met):
    dead_n = teams with nothing to play for (0-2); games_left_min = fewer of the two teams' league games left;
    h2h_gap = weighted head-to-head mean total - our expected (published median); goals_l5 = both teams' mean goals
    for + against over their last 5 matches, summed; res_l3 = mean of the two teams' last-3 mean (match total -
    rating expectation); rating_gap = (home att - home def) - (away att - away def), opponent-adjusted ratings;
    month = 1-12 of the match date.

    League-table signals (``TABLE_SIGNALS``, 2026-10-28) read the optional columns ``surge_rules.TABLE_INPUTS``
    (``surge_rules.table_inputs`` builds them from the candidate + form features); when a column is absent the
    condition is not met, so callers that do not pass them get exactly the earlier flags plus all-False columns."""
    f = inputs
    num = {c: pd.to_numeric(f[c], errors="coerce").to_numpy(float) for c in INPUTS}
    with np.errstate(invalid="ignore"):
        out = {
            "dead_rubber": num["dead_n"] >= 2,
            "dead_team": num["dead_n"] == 1,
            "last_three": num["games_left_min"] <= 3,
            "h2h_above": (num["h2h_gap"] >= 4) & (num["h2h_gap"] < 99),
            # goal averages are multiples of 0.2: compare at 6 decimals so float noise never decides a tie
            "low_goal_run": np.round(num["goals_l5"], 6) < round(float(cuts["goals_l5_q20"]), 6),
            "beating_ratings": num["res_l3"] >= cuts["res_l3_q80"],
            "home_mismatch": num["rating_gap"] >= cuts["rating_gap_q90"],
            "winter": np.isin(num["month"], (12, 1)),
            "below_ratings": num["res_l3"] < cuts["res_l3_q20"],
        }
    tf = sr.table_flags(f, {sid: sr.SELECTED[sid] for sid in TABLE_SIGNALS})
    out.update({sid: tf[sid].to_numpy(bool) for sid in TABLE_SIGNALS})
    return pd.DataFrame(out, index=f.index)


def assign(fl: pd.DataFrame, route: pd.Series, table: dict) -> pd.Series:
    """Per row: {"signals": [...boost ids], "against": [...damper ids]} for its route ("odds" / "noodds"), in
    catalogue order, only signals that apply on that route; season end reduced to its first applying member."""
    applies = {(s["id"], r): s["routes"][r]["applies"] for s in table["signals"] for r in ROUTES}
    order = [s["id"] for s in table["signals"]]
    direction = {s["id"]: s["direction"] for s in table["signals"]}
    out = []
    for i, r in zip(fl.index, route):
        row = fl.loc[i]
        on = [sid for sid in order if bool(row[sid]) and applies.get((sid, r), False)]
        ends = [sid for sid in on if sid in SEASON_END]
        on = [sid for sid in on if sid not in SEASON_END or sid == (ends[0] if ends else None)]
        out.append({"signals": [s for s in on if direction[s] == "boost"],
                    "against": [s for s in on if direction[s] == "damper"]})
    return pd.Series(out, index=fl.index, dtype=object)


# --------------------------------------------------------------------------- cut points from the study's own frame

def research_inputs(d: pd.DataFrame) -> pd.DataFrame:
    """The live rule's inputs on the study frame (after ``overshoot.segments``, which defines ``_goals5``,
    ``_ekf3`` and ``_rgap``)."""
    x = pd.DataFrame({
        "dead_n": d["ctx_dead_n"], "games_left_min": np.fmin(d["home_ctx_games_left"], d["away_ctx_games_left"]),
        "h2h_gap": d["h2h_total_wmean"] - d["expected"], "goals_l5": d["_goals5"], "res_l3": d["_ekf3"],
        "rating_gap": d["_rgap"], "month": d["month"]}, index=d.index)
    # table inputs: season context of features_candidates, promotion of overshoot._promoted (= form_promo)
    for side in ("home", "away"):
        for k in ("rank_frac", "gap_title", "gap_safety", "games_left"):
            x[f"{side}_{k}"] = d[f"{side}_ctx_{k}"]
        x[f"{side}_promo"] = d[f"{side}_promo"]
    x["progress"] = d["ctx_progress"]
    return x


def cut_points(preds: Path, report: dict, table_report: dict | None = None) -> dict:
    """Discovery quantiles of the study (``overshoot``) and a check that the live rule reproduces every segment
    count of the reports on the study's rows (league-table signals: counts of ``table_report``)."""
    from shots.evaluation import overshoot as ov

    cond = ov.conditioning_frame()
    teams = cond[["match_id", "home_team_id", "away_team_id"]]
    found = {}
    for route, (label, variant) in ov.ROUTES.items():
        d = ov.load_route(preds, label, variant)
        d = d.merge(cond.drop(columns=["date"]), on="match_id", how="left", validate="one_to_one")
        d = d.merge(ov.residual_momentum(d, teams), on="match_id", how="left")
        d["_mkt_gap"] = np.nan
        segs = ov.segments(d)
        disc = d["season"].isin(ov.DISCOVERY).to_numpy()

        def q(col: str, p: float) -> float:
            return float(np.nanquantile(d.loc[disc, col].to_numpy(float), p))

        cuts = {"goals_l5_q20": q("_goals5", .2), "res_l3_q20": q("_ekf3", .2), "res_l3_q80": q("_ekf3", .8),
                "rating_gap_q90": q("_rgap", .9)}
        x = research_inputs(d)
        fl = flags(x, cuts)
        for s in SIGNALS:
            if s.source == "table":
                if table_report is None:
                    continue
                fam, kw = sr.SELECTED[s.id]
                seg = np.asarray(sr.FAMILIES[fam](x, **kw), bool)
                reported = sum(table_report[route]["segments"][name]["n"] for name in s.segments)
            else:
                seg = np.logical_or.reduce([np.asarray(segs[name], bool) for name in s.segments])
                reported = sum(report[route]["segments"][name]["n"] for name in s.segments)
            got = fl[s.id].to_numpy(bool)
            if int(seg.sum()) != reported:
                raise AssertionError(f"{route} {s.id}: study {int(seg.sum())}, report {reported}")
            if s.id == "low_goal_run":
                # the study compared float sums with a cut that is itself a tied data value (4.2): part of the rows
                # at exactly 4.2 fell below it by rounding noise. The live rule is exact (strictly below 4.2): a
                # subset of the study segment, checked to hold almost all of it
                ok = not (got & ~seg).any() and got.sum() >= LOW_GOAL_MIN_SHARE * seg.sum()
            else:
                ok = bool((got == seg).all())
            if not ok:
                raise AssertionError(f"{route} {s.id}: live rule {int(got.sum())} rows, study {int(seg.sum())}")
            log.info("%s %s: live rule %d rows, study segment %d", route, s.id, int(got.sum()), int(seg.sum()))
        found[route] = cuts
    if found["odds"] != found["noodds"]:
        raise AssertionError(f"cut points differ between routes: {found}")
    return found["odds"]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("table", help="write the committed signal catalogue from the report JSON")
    t.add_argument("--report", type=Path, default=REPORT_JSON)
    t.add_argument("--preds", type=Path, default=None, help="walk-forward predictions of the study (recompute cuts)")
    t.add_argument("--table-report", type=Path, default=TABLE_REPORT_JSON,
                   help="league-table signals and stacking (python -m shots.evaluation.surge_table study)")
    t.add_argument("--out", type=Path, default=TABLE_PATH)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    report = json.loads(a.report.read_text(encoding="utf-8"))
    table_report = load_table_report(a.table_report)
    if a.preds is not None:
        cuts = cut_points(a.preds, report, table_report)
    else:
        cuts = load_table(a.out)["cuts"]
    table = build_table(report, cuts, table_report)
    a.out.write_text(json.dumps(table, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    log.info("wrote %s (%d signals)", a.out, len(table["signals"]))


if __name__ == "__main__":
    main()
