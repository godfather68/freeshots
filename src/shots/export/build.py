"""Build the four export documents (plain dicts) from the repository. No file access here.

Field names and shapes are the contract in docs/static-api.md; do not change them without bumping
SCHEMA_VERSION there. Values are the database's own (probabilities rounded to 4 decimals, never
recomputed); a curve that is not a valid non-increasing probability curve aborts the export rather
than being published or silently repaired.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from shots.db.access import FULL_ACCESS
from shots.db.repo import Repository
from shots.db.text import fold

SCHEMA_VERSION = 1
DEFAULT_TOTAL = 24  # default thresholds of the UI sliders (same as the server-rendered app)
DEFAULT_TEAM = 12
SELECTED_MIN_PROB = 0.70
RESULT_DAYS = 30
LIVE_LABEL = "Live picks (saved before kick-off)"


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


# ----------------------------------------------------------------------------- board / results

def build_board(repo: Repository, today: date) -> tuple[dict, dict]:
    """-> (board.json document, the repository's board_export dict)."""
    src = repo.board_export(today, FULL_ACCESS)
    if src is None:
        raise ExportError("no board run in the database")
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
    return {"schema": SCHEMA_VERSION, "fixtures": fixtures}, src


def build_results(repo: Repository, today: date, days: int = RESULT_DAYS) -> tuple[dict, list[dict]]:
    """-> (results.json document, the repository rows)."""
    rows = repo.official_results(today - timedelta(days=days), today, FULL_ACCESS)
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
        })
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


def build_track(repo: Repository, min_prob: float = SELECTED_MIN_PROB) -> tuple[dict, set[str]]:
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

    live = repo.track_records("live", FULL_ACCESS)
    counts = live["status"].value_counts().to_dict() if len(live) else {}
    settled = live[live["status"] == "settled"]
    records["live"] = {"label": LIVE_LABEL, "settled": int(counts.get("settled", 0)),
                       "pending": int(counts.get("pending", 0)), "void": int(counts.get("void", 0)),
                       "by_n": _by_n(repo, settled, min_prob) if len(settled) else {}}
    used |= set(settled.loc[settled["p"] >= min_prob, "competition"])
    return {"schema": SCHEMA_VERSION, "records": records}, used


# ----------------------------------------------------------------------------- meta

def build_meta(repo: Repository, built_at: datetime, board_src: dict, extra_leagues: set[str]) -> dict:
    """meta.json. `leagues` lists every league the other files refer to (fixtures = 0 when a league
    only appears in results or the track record), sorted by country, tier, name."""
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
                       "tier": comps[n]["tier"], "fixtures": per_league.get(n, 0)} for n in names),
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
                   "cold_start": sum(1 for r in rows if r["cold_start"])},
        "record": {"since": iso_z(repo.record_since())},
    }


def build_all(repo: Repository, built_at: datetime | None = None, today: date | None = None) -> dict[str, dict]:
    """{"meta": ..., "board": ..., "results": ..., "track": ...}. built_at: export time (UTC);
    today: the first day of the board, default the UTC date of built_at."""
    state = repo.state()
    if state != "ok":
        raise ExportError(f"the database is {state}: nothing to export (run `python -m shots update` first)")
    built_at = built_at or datetime.now(timezone.utc)
    if built_at.tzinfo is not None:
        built_at = built_at.astimezone(timezone.utc).replace(tzinfo=None)
    today = today or built_at.date()
    board, board_src = build_board(repo, today)
    results, result_rows = build_results(repo, today)
    track, used = build_track(repo)
    used |= {r["competition"] for r in result_rows}
    meta = build_meta(repo, built_at, board_src, used)
    return {"meta": meta, "board": board, "results": results, "track": track}
