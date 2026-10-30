"""Build the export documents (plain dicts) from the repository. No file access here, except the committed Surge
Watch catalogue (shots.evaluation.surge, versioned with the code).

Field names and shapes are the contract in docs/static-api.md; do not change them without bumping
SCHEMA_VERSION there. Values are the database's own (probabilities rounded to 4 decimals, never
recomputed); a curve that is not a valid non-increasing probability curve aborts the export rather
than being published or silently repaired.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from shots.db.access import FULL_ACCESS
from shots.db.repo import Repository
from shots.db.text import fold
from shots.evaluation import surge as sv
from shots.export.history import model_history
from shots.ingest.cross_border import BY_NAME as CROSS_BORDER

FEATURED = frozenset(CROSS_BORDER)  # competitions the site pins first (meta.leagues[].featured)

log = logging.getLogger("shots.export")

SCHEMA_VERSION = 1
DEFAULT_TOTAL = 24  # default thresholds of the UI sliders (same as the server-rendered app)
DEFAULT_TEAM = 12
SELECTED_MIN_PROB = 0.70
RESULT_DAYS = 30
LIVE_LABEL = "Live picks (saved before kick-off)"
EXPERIMENTAL_LABEL = "European cups (experimental, manual results)"
DAILY_POINTS = 30  # the most recent days with at least one settled match
WEEKLY_POINTS = 52  # the most recent weeks (Monday to Sunday) with at least one settled match
DATA_QUALITY = ("full", "partial", "none")


class ExportError(RuntimeError):
    """The database cannot be exported (no board, or data that violates the contract)."""


# ----------------------------------------------------------------------------- small helpers

def league_id(competition: str) -> str:
    """'England Premier League' -> 'england-premier-league' (stable slug; accents folded)."""
    return re.sub(r"[^a-z0-9]+", "-", fold(competition)).strip("-")


def league_name(competition: str, country: str | None) -> str:
    """Display name without the country prefix: 'England Premier League' -> 'Premier League'."""
    if country and competition.startswith(country + " ") and len(competition) > len(country) + 1:
        return competition[len(country) + 1:]
    return competition


def iso_z(ts: datetime | None) -> str | None:
    """Naive UTC (database convention) or aware datetime -> '2026-10-12T09:00:00Z'."""
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts).to_pydatetime()
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.replace(microsecond=0).isoformat() + "Z"


def _num(x, digits: int):
    """Rounded float, or None for missing / NaN."""
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else round(x, digits)


def _count(x):
    """Shot counts and bounds: an int when the value is integral (the usual case), else the number."""
    if x is None:
        return None
    x = float(x)
    if math.isnan(x):
        return None
    return int(x) if x == int(x) else round(x, 2)


def _curve(values, n_min: int | None, n_max: int | None, what: str) -> list[float]:
    """Validate and round one curve: aligned with its threshold range, within [0, 1], non-increasing."""
    if n_min is None or n_max is None:
        raise ExportError(f"{what}: the run has no threshold range")
    out = [round(float(v), 4) for v in values or []]
    if len(out) != n_max - n_min + 1:
        raise ExportError(f"{what}: curve has {len(out)} points, thresholds {n_min}..{n_max} need {n_max - n_min + 1}")
    if any(not (0.0 <= v <= 1.0) for v in out):
        raise ExportError(f"{what}: probability outside [0, 1]")
    if any(b > a for a, b in zip(out, out[1:])):
        raise ExportError(f"{what}: curve is not non-increasing")
    return out


EXPLAIN_SIDES = ("home", "away", "match")
VOLATILITY = ("low", "average", "high")


def _explain(src: dict, what: str) -> dict:
    """The stored explanation, re-emitted key by key in the contract's order (nothing else can pass
    through) and checked: the published one-decimal numbers must add up exactly, in tenths."""
    try:
        tenths = lambda x: int(round(float(x) * 10))  # noqa: E731
        factors = [{"id": str(f["id"]), "side": str(f["side"]), "label": str(f["label"]), "detail": str(f["detail"]),
                    "effect": tenths(f["effect"]) / 10} for f in src["factors"]]
        sig = src["signals"]
        out = {
            "expected_mean": tenths(src["expected_mean"]) / 10,
            "baseline": {"value": tenths(src["baseline"]["value"]) / 10, "label": str(src["baseline"]["label"])},
            "factors": factors,
            "residual": tenths(src["residual"]) / 10,
            "signals": {"home_matches": int(sig["home_matches"]), "away_matches": int(sig["away_matches"]),
                        "odds_used": bool(sig["odds_used"]),
                        "home_volatility": sig.get("home_volatility"), "away_volatility": sig.get("away_volatility"),
                        "model_gap": _num(sig.get("model_gap"), 1)},
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ExportError(f"{what}: malformed explanation ({type(exc).__name__}: {exc})") from exc
    total = tenths(out["baseline"]["value"]) + sum(tenths(f["effect"]) for f in factors) + tenths(out["residual"])
    if total != tenths(out["expected_mean"]):
        raise ExportError(f"{what}: explanation does not add up (baseline + effects + residual != expected_mean)")
    if any(f["side"] not in EXPLAIN_SIDES for f in factors):
        raise ExportError(f"{what}: explanation factor with an unknown side")
    if any(v is not None and v not in VOLATILITY for v in (out["signals"]["home_volatility"], out["signals"]["away_volatility"])):
        raise ExportError(f"{what}: unknown volatility label")
    return out


# ----------------------------------------------------------------------------- board / results

def build_board(repo: Repository, today: date, table: dict | None = None) -> tuple[dict, dict]:
    """-> (board.json document, the repository's board_export dict). table: the Surge Watch catalogue
    (default: the committed one) that a fixture's `surge` labels are checked against."""
    src = repo.board_export(today, FULL_ACCESS)
    if src is None:
        raise ExportError("no board run in the database")
    surge = _surge_labels(repo, src, table or sv.load_table())
    ctx = repo.board_context()
    context = ctx["fixtures"] if ctx is not None and ctx["run_id"] == src["run_id"] else {}
    has_team = src["team_n_min"] is not None
    fixtures = []
    for r in src["rows"]:
        what = f"board fixture {r['match_id']}"
        team = None
        if has_team and r.get("home_curve") is not None and r.get("away_curve") is not None:
            team = {side: {"mean": _num(r[f"{side}_mean"], 2), "median": _count(r[f"{side}_median"]),
                           "lb80": _count(r[f"{side}_lb_80"]),
                           "curve": _curve(r[f"{side}_curve"], src["team_n_min"], src["team_n_max"], f"{what} {side}")}
                    for side in ("home", "away")}
        fixtures.append({
            "id": r["match_id"],
            "league": league_id(r["competition"]),
            "round": r["round"] or None,
            "date": r["date"].isoformat(),
            "kickoff_local": r["kickoff"] or None,
            "kickoff_utc": iso_z(r["kickoff_utc"]),
            "home": r["home_team"],
            "away": r["away_team"],
            "expected": _count(r["expected_shots"]),
            "lb": {"70": _count(r["lb_70"]), "80": _count(r["lb_80"]), "90": _count(r["lb_90"])},
            "league_avg": _num(r["league_avg_shots"], 1),
            "above_league": _num(r["above_league"], 1),
            "odds": bool(r["odds"]),
            "cold_start": bool(r["cold_start"]),
            "model": r["model"],
            "curve": _curve(r["total_curve"], src["curve_n_min"], src["curve_n_max"], what),
            "team": team,
        })
        if isinstance(r.get("explain"), dict):  # additive; absent when the prediction has no stored explanation
            fixtures[-1]["explain"] = _explain(r["explain"], what)
        if r.get("experimental"):  # additive; absent for ordinary (domestic) fixtures
            fixtures[-1]["experimental"] = True
            if r.get("data_quality") in DATA_QUALITY:  # unknown quality is left out, never reported as "full"
                fixtures[-1]["data_quality"] = r["data_quality"]
        if r["match_id"] in surge:  # additive; absent when the fixture was not assessed (see _surge_labels)
            fixtures[-1]["surge"] = surge[r["match_id"]](bool(r["odds"]), what)
        if r["match_id"] in context and not r.get("experimental"):  # additive; assessed fixtures only
            fixtures[-1]["context"] = _context(context[r["match_id"]], what)
    return {"schema": SCHEMA_VERSION, "fixtures": fixtures}, src


# ----------------------------------------------------------------------------- Surge Watch

SURGE_STATS = ("n", "over_rate", "stated_rate", "base_rate", "excess_ci", "seasons_held", "seasons", "applies")


def _surge_labels(repo: Repository, src: dict, table: dict) -> dict:
    """{match_id: check(odds, what) -> {"signals", "against"}} of the board run's Surge Watch labels, or {} when the
    run has none or recorded them with another catalogue version than `table` (then no fixture is labelled: the
    published catalogue would not describe them). The check re-emits the two lists and aborts the export on a
    label the catalogue does not allow on the fixture's route, never repairing it."""
    rs = repo.board_surge()
    if rs is None or rs["run_id"] != src["run_id"]:
        return {}
    version = sv.table_version(table)
    if rs["version"] != version:
        log.warning("surge labels of board run %s use catalogue %s, not %s: published without labels",
                    src["run_id"], rs["version"], version)
        return {}
    by_id = {s["id"]: s for s in table["signals"]}
    stacking = table.get("stacking") or {}
    min_boosts = int(stacking.get("min_boosts", 2))

    def checker(entry):
        def check(odds: bool, what: str) -> dict:
            route = "odds" if odds else "noodds"
            try:
                out = {"signals": [str(x) for x in entry["signals"]], "against": [str(x) for x in entry["against"]]}
            except (KeyError, TypeError) as exc:
                raise ExportError(f"{what}: malformed surge labels ({type(exc).__name__}: {exc})") from exc
            for key, direction in (("signals", "boost"), ("against", "damper")):
                for sid in out[key]:
                    s = by_id.get(sid)
                    if s is None or s["direction"] != direction or not s["routes"][route]["applies"]:
                        raise ExportError(f"{what}: surge label {sid!r} is not a {direction} that applies on the "
                                          f"{route} route")
                if len(set(out[key])) != len(out[key]):
                    raise ExportError(f"{what}: duplicate surge label")
            if sum(1 for sid in out["signals"] if sid in sv.SEASON_END) > 1:
                raise ExportError(f"{what}: more than one season-end surge label")
            # "strong surge candidate": exactly when the run says so, it has min_boosts+ boosts and the catalogue
            # allows the label on this route (stacking.routes.<route>.strong_applies)
            allowed = bool((stacking.get("routes") or {}).get(route, {}).get("strong_applies", False))
            want = allowed and len(out["signals"]) >= min_boosts
            got = entry.get("strong", False)
            if got not in (True, False) or bool(got) != want:
                raise ExportError(f"{what}: surge `strong` = {got!r}, the catalogue gives {want} on the {route} route")
            if want:
                out["strong"] = True
            return out
        return check

    return {mid: checker(entry) for mid, entry in rs["fixtures"].items()}


CONTEXT_PHASES = ("early", "middle", "late", "run_in")
CONTEXT_KEYS = ("pos", "of", "pts", "gp", "left", "to_top", "to_safety")


def _context(entry, what: str) -> dict:
    """A fixture's table context as recorded with the board run (shots.predict.surge.table_context), re-emitted key
    by key in the contract's order and checked (whole numbers, 1 <= pos <= of, leader gap >= 0, phase defined
    whenever games left are); anything else aborts the export, never repaired."""
    def whole(v, nullable: bool = False):
        if v is None and nullable:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
            raise ExportError(f"{what}: table context value {v!r} is not a whole number")
        return int(v)

    try:
        phase = entry["phase"]
        sides = {}
        for side in ("home", "away"):
            t = entry[side]
            sides[side] = {"pos": whole(t["pos"], True), "of": whole(t["of"]), "pts": whole(t["pts"]),
                           "gp": whole(t["gp"]), "left": whole(t["left"], True), "to_top": whole(t["to_top"]),
                           "to_safety": whole(t["to_safety"])}
    except (KeyError, TypeError) as exc:
        raise ExportError(f"{what}: malformed table context ({type(exc).__name__}: {exc})") from exc
    if phase is not None and phase not in CONTEXT_PHASES:
        raise ExportError(f"{what}: unknown season phase {phase!r}")
    for t in sides.values():
        if t["of"] < 2 or (t["pos"] is not None and not 1 <= t["pos"] <= t["of"]) or t["to_top"] < 0                 or t["pts"] < 0 or t["gp"] < 0 or (t["left"] is not None and t["left"] < 0):
            raise ExportError(f"{what}: table context out of range")
        if t["left"] is not None and phase is None:
            raise ExportError(f"{what}: games left known but no season phase")
    return {"phase": phase, "home": sides["home"], "away": sides["away"]}


def _recorded_labels(entry, what: str) -> dict:
    """Labels a settled match carried in its pre-match board run, as recorded then (with the catalogue of that day,
    so they are not re-checked against today's route rules; clients ignore ids they do not know)."""
    try:
        out = {"signals": [str(x) for x in entry["signals"]], "against": [str(x) for x in entry["against"]]}
    except (KeyError, TypeError) as exc:
        raise ExportError(f"{what}: malformed surge labels ({type(exc).__name__}: {exc})") from exc
    for key in ("signals", "against"):
        if len(set(out[key])) != len(out[key]) or any(not re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*", x) for x in out[key]):
            raise ExportError(f"{what}: malformed surge label in {key}")
    if "strong" in entry:
        if entry["strong"] is not True or not out["signals"]:
            raise ExportError(f"{what}: malformed surge `strong`")
        out["strong"] = True
    return out


def build_surge(table: dict | None = None) -> dict:
    """surge.json: the Surge Watch catalogue (the committed table of shots.evaluation.surge, generated from the
    over-shoot report), re-emitted key by key in the contract's order. Deterministic: no timestamp."""
    t = table or sv.load_table()
    try:
        doc = {
            "schema": SCHEMA_VERSION,
            "report_date": str(t["report_date"]),
            "margin": int(t["margin"]),
            "matches": int(t["matches"]),
            "period": str(t["period"]),
            "method": str(t["method"]),
            "routes": {r: {"label": str(t["routes"][r]["label"]), "base_rate": _num(t["routes"][r]["base_rate"], 4)}
                       for r in sv.ROUTES},
            "signals": [{"id": str(s["id"]), "label": str(s["label"]), "direction": str(s["direction"]),
                         "explanation": str(s["explanation"]),
                         "routes": {r: {k: s["routes"][r][k] for k in SURGE_STATS} for r in sv.ROUTES}}
                        for s in t["signals"]],
        }
        if isinstance(t.get("stacking"), dict):  # additive (2026-10-28)
            doc["stacking"] = _stacking(t["stacking"])
        return doc
    except (KeyError, TypeError, ValueError) as exc:
        raise ExportError(f"surge catalogue: malformed ({type(exc).__name__}: {exc})") from exc


STACK_CELL = ("n", "over_rate", "stated_rate", "excess_ci", "seasons_held", "seasons")


def _stack_cell(c: dict) -> dict:
    """One stacking cell in the contract's order; an empty group has only ``n``."""
    return {k: c[k] for k in STACK_CELL if k in c}


def _stacking(st: dict) -> dict:
    """surge.json `stacking`: matches by number of applying boosts per route (the research's numbers, copied) and
    whether the "strong surge candidate" label is used on the route."""
    return {"min_boosts": int(st["min_boosts"]), "note": str(st["note"]), "groups": [str(g) for g in st["groups"]],
            "phases": [str(p) for p in st["phases"]],
            "routes": {r: {"signals": [str(x) for x in v["signals"]],
                           "share": {g: _num(x, 4) for g, x in v["share"].items()},
                           "by_count": {g: _stack_cell(c) for g, c in v["by_count"].items()},
                           "season_phase": {ph: _stack_cell(c) for ph, c in v["season_phase"].items()},
                           "strong_applies": bool(v["strong_applies"])}
                       for r, v in sorted(st["routes"].items())}}


def build_results(repo: Repository, today: date, days: int = RESULT_DAYS) -> tuple[dict, list[dict]]:
    """-> (results.json document, the repository rows). Ordinary predictions by the official rule, plus
    experimental ones (cross-border competitions settled by manual results) flagged `experimental`. Every
    row has result_status: "provisional" (settled by a hand-entered stand-in football-data has not
    published yet; result_source = the provider, source_url = its page) or "confirmed"."""
    rows = repo.settled_results(today - timedelta(days=days), today, FULL_ACCESS, experimental=None)
    labels = repo.run_surge_labels({r["run_id"] for r in rows})
    results = []
    for r in rows:
        results.append({
            "id": r["match_id"],
            "league": league_id(r["competition"]),
            "date": r["date"].isoformat(),
            "home": r["home_team"],
            "away": r["away_team"],
            "actual": {"total": int(r["actual_total"]), "home": int(r["actual_home"]), "away": int(r["actual_away"])},
            "predicted_at": iso_z(r["predicted_at"]),
            "expected": _count(r["expected_shots"]),
            "lb": {"70": _count(r["lb_70"]), "80": _count(r["lb_80"]), "90": _count(r["lb_90"])},
            "odds": bool(r["odds"]),
            "curve": _curve(r["total_curve"], r["curve_n_min"], r["curve_n_max"], f"result {r['match_id']}"),
            "result_source": r["result_source"],  # additive: where the actual counts come from
            "result_status": r["result_status"],  # additive: provisional (hand-entered stand-in) | confirmed
        })
        if r["source_url"] is not None:  # manual and provisional results
            results[-1]["source_url"] = r["source_url"]
        if r["experimental"]:  # additive; absent for ordinary predictions
            results[-1]["experimental"] = True
        run = labels.get(r["run_id"], {})
        got = run.get("fixtures", {}).get(r["match_id"])
        if got is not None:  # additive; only when the pre-match board run assessed this fixture
            results[-1]["surge"] = _recorded_labels(got, f"result {r['match_id']}")
        ctx = (run.get("context") or {}).get(r["match_id"])
        if ctx is not None and not r["experimental"]:  # additive; the table before the match, as recorded
            results[-1]["context"] = _context(ctx, f"result {r['match_id']}")
    return {"schema": SCHEMA_VERSION, "results": results}, rows


# ----------------------------------------------------------------------------- track record

def _season_label(dates: pd.Series) -> str:
    lo, hi = dates.min(), dates.max()
    if hi.year == lo.year + 1:
        return f"{lo.year}/{hi.year % 100:02d} season (out of sample)"
    return f"{lo.date()} to {hi.date()} (out of sample)"


def _by_n(repo: Repository, settled: pd.DataFrame, min_prob: float) -> dict:
    """Per threshold N: headline numbers of the selected picks (p >= min_prob), calibration bands and
    Brier score over all settled rows, and the per-league breakdown of the selected picks."""
    from shots.predict import track

    out = {}
    for n in sorted(int(x) for x in settled["n"].unique()):
        sub = settled[settled["n"] == n]
        m = track.metrics(sub, min_prob, None)
        sel = m["selected"]
        hit = sub["hit"].astype(float)
        rates, overall = repo.base_rates(n, sub["date"].min().date())
        baseline = None
        if overall is not None:
            base = sub["competition"].map(rates).astype(float).fillna(overall)
            baseline = _num(((base - hit) ** 2).mean(), 4)
        by_league = [{"league": league_id(r.competition), "n": int(r.n), "hit_rate": _num(r.hit_rate, 4),
                      "stated": _num(r.mean_p, 4)} for r in m["by_competition"].itertuples()]
        out[str(n)] = {
            "selected_min_prob": min_prob,
            "n": int(sel["n"]),
            "hits": int(sel["hits"]),
            "hit_rate": _num(sel["hit_rate"], 4),
            "mean_stated": _num(sel["mean_p"], 4),
            "wilson95": [_num(sel["ci_lo"], 4), _num(sel["ci_hi"], 4)] if sel["n"] else None,
            "brier": _num(m["brier"]["brier"], 4),
            "brier_baseline": baseline,
            "reliability": [{"band": [round(float(b.lo), 1), round(float(b.hi), 1)], "n": int(b.n),
                             "stated": _num(b.mean_p, 4), "realised": _num(b.realised, 4)}
                            for b in m["reliability"].itertuples() if b.n],
            "by_league": sorted(by_league, key=lambda d: (-d["n"], d["league"])),
        }
    return out


def accuracy_point(key: date, rows: list[dict], picks: list[tuple[int, float, bool]], ns: list[int],
                   min_prob: float = SELECTED_MIN_PROB) -> dict:
    """Accuracy of one day / week.
    rows: settled_results rows of the group (one per match, the results.json rule): matches, mae = mean
    |actual - expected|, bias = mean (expected - actual) (positive: more shots expected than played),
    floor80_held = share of matches with actual >= lb 80 (None when no row has the value).
    picks: (n, p, hit) of the group's settled picks of the record (v_official_picks / v_experimental_picks),
    so by_n uses the record's own pick definition (p >= min_prob) and has exactly the record's
    thresholds `ns` (0 / 0 for a threshold without picks in the group).
    provisional = how many of the group's matches (rows) are settled by a provisional result."""
    exp = [(int(r["actual_total"]), float(r["expected_shots"])) for r in rows if r["expected_shots"] is not None]
    floor = [(int(r["actual_total"]), float(r["lb_80"])) for r in rows if r["lb_80"] is not None]
    by_n = {str(n): {"picks": 0, "hits": 0} for n in ns}
    for n, p, hit in picks:
        if p >= min_prob and str(n) in by_n:
            by_n[str(n)]["picks"] += 1
            by_n[str(n)]["hits"] += int(bool(hit))
    return {
        "date": key.isoformat(),
        "matches": len(rows),
        "mae": _num(sum(abs(a - e) for a, e in exp) / len(exp), 2) if exp else None,
        "bias": _num(sum(e - a for a, e in exp) / len(exp), 2) if exp else None,
        "floor80_held": _num(sum(a >= lb for a, lb in floor) / len(floor), 4) if floor else None,
        "by_n": by_n,
        "provisional": sum(1 for r in rows if r.get("result_status") == "provisional"),
    }


def _period_key(d: date, period: str) -> date:
    return d if period == "day" else d - timedelta(days=d.weekday())  # week: its Monday


def accuracy_series(rows: list[dict], picks: list[tuple[date, int, float, bool]], ns: list[int], period: str,
                    limit: int, min_prob: float = SELECTED_MIN_PROB) -> list[dict]:
    """rows (settled_results) and picks ((match date, n, p, hit) of settled picks) grouped by match date
    ("day") or week ("week": key = the Monday of the match dates' week); the most recent `limit` groups
    with at least one settled match or pick, oldest first."""
    groups: dict[date, list[dict]] = {}
    pgroups: dict[date, list[tuple[int, float, bool]]] = {}
    for r in rows:
        groups.setdefault(_period_key(r["date"], period), []).append(r)
    for d, n, p, hit in picks:
        pgroups.setdefault(_period_key(d, period), []).append((n, p, hit))
    keys = sorted(set(groups) | set(pgroups))[-limit:]
    return [accuracy_point(k, groups.get(k, []), pgroups.get(k, []), ns, min_prob) for k in keys]


def _picks_record(repo: Repository, source: str, label: str, today: date, experimental: bool,
                  min_prob: float) -> tuple[dict, set[str]]:
    """records.live (source live) / records.experimental: counts and by_n of the picks view, plus the
    daily / weekly accuracy series."""
    df = repo.track_records(source, FULL_ACCESS)
    counts = df["status"].value_counts().to_dict() if len(df) else {}
    settled = df[df["status"] == "settled"]
    by_n = _by_n(repo, settled, min_prob) if len(settled) else {}
    rows = repo.settled_results(None, today, FULL_ACCESS, experimental=experimental)
    sp = settled[settled["date"].dt.date <= today]
    picks = [(d.date(), int(n), float(p), bool(h)) for d, n, p, h in zip(sp["date"], sp["n"], sp["p"], sp["hit"])]
    ns = sorted(int(n) for n in by_n)
    rec = {"label": label, "settled": int(counts.get("settled", 0)), "pending": int(counts.get("pending", 0)),
           "void": int(counts.get("void", 0)),
           # settled picks whose result is provisional (football-data has not confirmed it yet)
           "provisional": int(settled["provisional"].sum()) if len(settled) else 0,
           "by_n": by_n,
           "daily": accuracy_series(rows, picks, ns, "day", DAILY_POINTS, min_prob),
           "weekly": accuracy_series(rows, picks, ns, "week", WEEKLY_POINTS, min_prob)}
    return rec, set(settled.loc[settled["p"] >= min_prob, "competition"])


def build_track(repo: Repository, min_prob: float = SELECTED_MIN_PROB, today: date | None = None) -> tuple[dict, set[str]]:
    """-> (track.json document, competition names it refers to)."""
    records: dict = {}
    used: set[str] = set()

    back = repo.track_records("backfill", FULL_ACCESS)
    back = back[back["status"] == "settled"]
    if len(back):
        records["backfill"] = {"label": _season_label(back["date"]), "by_n": _by_n(repo, back, min_prob)}
        used |= set(back.loc[back["p"] >= min_prob, "competition"])
    else:
        records["backfill"] = None

    today = today or date.today()
    # live = the official record (v_official_picks); experimental = v_experimental_picks (cross-border
    # competitions settled by manual results), kept apart and never mixed into the live numbers.
    for key, source, label, experimental in (("live", "live", LIVE_LABEL, False),
                                             ("experimental", "experimental", EXPERIMENTAL_LABEL, True)):
        records[key], comps = _picks_record(repo, source, label, today, experimental, min_prob)
        used |= comps
    return {"schema": SCHEMA_VERSION, "records": records}, used


# ----------------------------------------------------------------------------- meta

def build_meta(repo: Repository, built_at: datetime, board_src: dict, extra_leagues: set[str],
               result_rows: list[dict] | None = None) -> dict:
    """meta.json. `leagues` lists every league the other files refer to (fixtures = 0 when a league
    only appears in results or the track record), sorted by country, tier, name. counts.results /
    counts.provisional_results: results.json rows and how many of them are provisional."""
    bm = repo.board_meta() or {}
    rows = board_src["rows"]
    comps = repo.competitions()
    per_league: dict[str, int] = {}
    for r in rows:
        per_league[r["competition"]] = per_league.get(r["competition"], 0) + 1
    names = set(per_league) | {c for c in extra_leagues if c in comps}
    ids = {}
    for name in names:
        if ids.setdefault(league_id(name), name) != name:
            raise ExportError(f"two competitions share the league id {league_id(name)!r}")
    leagues = sorted(({"id": league_id(n), "name": league_name(n, comps[n]["country"]), "country": comps[n]["country"],
                       "tier": comps[n]["tier"], "fixtures": per_league.get(n, 0),
                       **({"featured": True} if n in FEATURED else {})} for n in names),
                     key=lambda d: (d["country"] or "", d["tier"] if d["tier"] is not None else 99, d["name"], d["id"]))
    job = repo.last_finished_job("update")
    has_team = board_src["team_n_min"] is not None
    results_up_to = bm.get("results_up_to")
    return {
        "schema": SCHEMA_VERSION,
        "built_at": iso_z(built_at),
        "results_up_to": str(results_up_to)[:10] if results_up_to else None,
        "last_job": {"status": job["status"], "finished_at": iso_z(job["finished_at"])} if job else None,
        "models": sorted(bm.get("models") or []),
        "thresholds": {"total": [board_src["curve_n_min"], board_src["curve_n_max"]],
                       "team": [board_src["team_n_min"], board_src["team_n_max"]] if has_team else None,
                       "default_total": DEFAULT_TOTAL, "default_team": DEFAULT_TEAM},
        "leagues": leagues,
        "days": sorted({r["date"].isoformat() for r in rows}),
        "counts": {"fixtures": len(rows), "with_odds": sum(1 for r in rows if r["odds"]),
                   "cold_start": sum(1 for r in rows if r["cold_start"]),
                   "results": len(result_rows or []),
                   "provisional_results": sum(1 for r in result_rows or [] if r.get("result_status") == "provisional")},
        "record": {"since": iso_z(repo.record_since())},
        "awaiting_results": _awaiting(repo),
        "model_history": model_history(built_at.date()),
    }


def _awaiting(repo: Repository) -> dict:
    a = repo.awaiting_results()
    return {"official": a["official"], "experimental": a["experimental"],
            "oldest": a["oldest"].isoformat() if a["oldest"] else None}


def build_all(repo: Repository, built_at: datetime | None = None, today: date | None = None) -> dict[str, dict]:
    """{"meta": ..., "board": ..., "results": ..., "track": ..., "surge": ...}. built_at: export time (UTC);
    today: the first day of the board, default the UTC date of built_at."""
    state = repo.state()
    if state != "ok":
        raise ExportError(f"the database is {state}: nothing to export (run `python -m shots update` first)")
    built_at = built_at or datetime.now(timezone.utc)
    if built_at.tzinfo is not None:
        built_at = built_at.astimezone(timezone.utc).replace(tzinfo=None)
    today = today or built_at.date()
    table = sv.load_table()
    board, board_src = build_board(repo, today, table)
    results, result_rows = build_results(repo, today)
    track, used = build_track(repo, today=today)
    used |= {r["competition"] for r in result_rows}
    meta = build_meta(repo, built_at, board_src, used, result_rows)
    return {"meta": meta, "board": board, "results": results, "track": track, "surge": build_surge(table)}
