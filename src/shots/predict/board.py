"""Build the board the web UI reads: every upcoming fixture with its predicted shots and the full
curve P(total shots >= n) for n = 10..45.

Usage:
    python -m shots.predict.board                  # next 30 days, refresh current-season results first
    python -m shots.predict.board --days 14 --no-refresh
    python -m shots.predict.board --season         # the whole remaining calendar

Output: data/processed/board.parquet (one row per fixture) + board.meta.json.

The curve is stored as wide columns p_10..p_45 (one row per fixture, 36 columns) rather than a long
table: the UI always filters/sorts fixtures first and then reads one or all n for a fixture, so one
row per fixture keeps the web side to a single DataFrame, and a single threshold lookup is a column
pick. Values are the models' calibrated `prob_at_least`, stored as-is.

`explain` (last column, JSON text; absent when no routed model supports it): why the expected total is
what it is, an additive decomposition computed by shots.models.explain from the same model and the same
feature rows that produced the prediction of that row. It never changes any other column.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from shots.ingest import football_data as fd
from shots.models import explain as ex
from shots.predict import scan as sc

log = logging.getLogger(__name__)

BOARD_PATH = fd.ROOT / "data" / "processed" / "board.parquet"
META_NAME = "board.meta.json"
N_MIN, N_MAX = 10, 45
NS = list(range(N_MIN, N_MAX + 1))
PCOLS = [f"p_{n}" for n in NS]
BOUNDS = (70, 80, 90)
TEAM_N_MIN, TEAM_N_MAX = 3, 25
TEAM_NS = list(range(TEAM_N_MIN, TEAM_N_MAX + 1))
SIDES = {"h": "home", "a": "away"}
# Per-team columns, appended after the total curve (experimental): home_/away_ mean, median, _lb_70/80/90 and the
# curve ph_3..ph_25 / pa_3..pa_25 = calibrated P(team shots >= n).
TEAM_SCALARS = ("mean", "median", "lb_70", "lb_80", "lb_90")


def board_path() -> Path:
    """Board location; SHOTS_BOARD overrides it (tests, alternative builds)."""
    return Path(os.environ.get("SHOTS_BOARD") or BOARD_PATH)


def meta_path(path: Path | None = None) -> Path:
    return (path or board_path()).with_name(META_NAME)


def _explanations(prep: sc.Prepared, info: pd.DataFrame, routes: list) -> list:
    """JSON text of the explanation per upcoming row (None where there is none). A failure here never
    costs the board its predictions: it is logged and the fixtures of that route go without `explain`."""
    up = prep.upcoming
    out = np.full(len(up), None, dtype=object)
    exp = prep.is_experimental()
    country = up["match_id"].map(info["country"])
    league = pd.Series([ex.league_label(str(c), k if isinstance(k, str) else None)
                        for c, k in zip(up["competition"], country)], index=up.index)
    for mask, mdl, rows, label in routes:
        if not ex.supports(mdl):
            continue
        sub = rows[mask]
        odds_used = bool(prep.has_odds[mask].all()) and not bool(exp[mask].any())
        gap = prep.nomodel if odds_used and prep.nomodel is not None and ex.supports(prep.nomodel) else None
        try:
            got = ex.explain_rows(mdl, sub, odds_used=odds_used, league=league[mask], gap_model=gap,
                                  gap_rows=sc.no_odds_rows(sub) if gap is not None else None)
        except Exception:  # noqa: BLE001 - explanations are an add-on; predictions must still be published
            log.exception("explanations failed for %s (%d fixtures); they are left out", label, len(sub))
            continue
        out[mask.to_numpy()] = [ex.dumps(e) for e in got]
    return list(out)


def assemble(prep: sc.Prepared, info: pd.DataFrame, ns=NS) -> pd.DataFrame:
    """Score `prep.upcoming` with the routed models: point/bound predictions + the full curve.
    info: indexed by match_id with country, tier and (optional) round; features do not carry them."""
    up = prep.upcoming
    n_rows = len(up)
    cols = {"median": np.full(n_rows, np.nan), **{f"lb_{q}": np.full(n_rows, np.nan) for q in BOUNDS}}
    curve = np.full((n_rows, len(ns)), np.nan)
    used = pd.Series(prep.model_label, index=up.index, dtype=object)
    routes = list(prep.routes())
    exp = prep.is_experimental()
    # team columns only if every routed production model carries team calibration (old pickles: skipped,
    # board still valid). The experimental route never decides this for the domestic rows.
    prod = [m for mask, m, _, _ in routes if not exp[mask].all()]
    team = bool(routes) and all(getattr(m, "has_team_calibration", False) for m in (prod or [r[1] for r in routes]))
    tcols = {f"{side}_{c}": np.full(n_rows, np.nan) for side in SIDES.values() for c in TEAM_SCALARS} if team else {}
    tcurve = {k: np.full((n_rows, len(TEAM_NS)), np.nan) for k in SIDES} if team else {}
    for mask, mdl, rows, label in routes:
        used[mask] = label
        sub = rows[mask]
        pr = mdl.predict(sub)
        for key in cols:
            cols[key][mask.to_numpy()] = pr[key].to_numpy(float)
        for j, n in enumerate(ns):
            curve[mask.to_numpy(), j] = mdl.prob_at_least(sub, n)
        if team and getattr(mdl, "has_team_calibration", False):
            m = mask.to_numpy()
            for k, side in SIDES.items():
                tp = mdl.team_predict(sub, side)
                for c in TEAM_SCALARS:
                    tcols[f"{side}_{c}"][m] = tp[c].to_numpy(float)
                for j, n in enumerate(TEAM_NS):
                    tcurve[k][m, j] = mdl.team_prob_at_least(sub, side, n)
    quality = None
    if exp.any():
        quality = up["data_quality"].where(exp, "full").fillna("full")
        if team:  # per-team numbers only where both clubs have data: a neutral stand-in has no "own" shots
            blank = (exp & (quality != "full")).to_numpy()
            for vals in (*tcols.values(), *tcurve.values()):
                vals[blank] = np.nan

    out = pd.DataFrame({
        "match_id": up["match_id"],
        "date": pd.to_datetime(up["date"]).dt.normalize(),
        "kickoff": up["match_id"].map(prep.kickoff).fillna(""),
        "competition": up["competition"],
        "country": up["match_id"].map(info["country"]),
        "tier": up["match_id"].map(info["tier"]).astype("Int64"),
        "round": up["match_id"].map(info["round"]).fillna("") if "round" in info else "",
        "home_team": up["home_team"],
        "away_team": up["away_team"],
        "expected_shots": cols["median"],
        **{f"lb_{q}": pd.array(cols[f"lb_{q}"], dtype="Float64").astype("Int64") for q in BOUNDS},
        "league_avg_shots": (2 * up["league_prior_shots"]).round(1),
        "odds": prep.has_odds,
        "mkt_p_over25": up["mkt_p_over25"].round(3),
        "cold_start": up[["home_n_prior_matches", "away_n_prior_matches"]].min(axis=1) < sc.COLD_START_MATCHES,
        "model": used,
    })
    out.insert(out.columns.get_loc("league_avg_shots") + 1, "above_league",
               (out["expected_shots"] - out["league_avg_shots"]).round(1))
    for j, c in enumerate(PCOLS if tuple(ns) == tuple(NS) else [f"p_{n}" for n in ns]):
        out[c] = curve[:, j].round(4)
    if team:
        for key, vals in tcols.items():
            if key.endswith("_mean"):
                out[key] = vals.round(2)
            else:  # median and bounds are whole numbers
                out[key] = pd.array(vals, dtype="Float64").astype("Int64")
        for k in SIDES:
            for j, n in enumerate(TEAM_NS):
                out[f"p{k}_{n}"] = tcurve[k][:, j].round(4)
    if quality is not None:  # only when the board has experimental fixtures: a domestic board keeps its columns
        out["experimental"] = exp.to_numpy()
        out["data_quality"] = quality.to_numpy()
    explain = _explanations(prep, info, routes)
    if any(e is not None for e in explain):  # only then: a board of models without explanations keeps its columns
        out["explain"] = pd.array(explain, dtype=object)
    return out.sort_values(["date", "kickoff", "competition", "home_team"]).reset_index(drop=True)


def build(days: int | None = 30, refresh: bool = True, model_name: str = sc.DEFAULT_MODEL,
          today: date | None = None, out_path: Path | None = None, max_model_age_days: int = 30) -> pd.DataFrame:
    today = today or date.today()
    out_path = out_path or board_path()
    if refresh:
        fd.build(refresh_current=True)
    matches = pd.read_parquet(fd.OUT_PATH)
    fixtures = sc.gather_fixtures(today, days)
    fixtures = fixtures[~fixtures["match_id"].isin(matches["match_id"])]
    if fixtures.empty:
        raise SystemExit("no upcoming fixtures in supported divisions")
    prep = sc.prepare_upcoming(fixtures, matches, model_name, None, max_model_age_days)
    info = fixtures.drop_duplicates("match_id").set_index("match_id")[["country", "tier"]]
    if sc.CALENDAR_PATH.exists():
        cal = pd.read_parquet(sc.CALENDAR_PATH, columns=["match_id", "round"])
        info = info.join(cal.drop_duplicates("match_id").set_index("match_id")["round"])
    board = assemble(prep, info)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    board.to_parquet(tmp, index=False)
    tmp.replace(out_path)  # atomic: the running web app never sees a half-written file
    meta = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "results_up_to": str(pd.to_datetime(matches["date"]).max().date()),
        "window_days": days,
        "window_from": str(today),
        "models": sorted(board["model"].unique()),
        "n_fixtures": len(board),
        "n_with_odds": int(board["odds"].sum()),
        "n_without_odds": int((~board["odds"]).sum()),
        "n_cold_start": int(board["cold_start"].sum()),
        "n_leagues": int(board["competition"].nunique()),
        "date_min": str(board["date"].min().date()),
        "date_max": str(board["date"].max().date()),
        "thresholds": [N_MIN, N_MAX],
        "team_predictions": "ph_3" in board.columns,
        "team_thresholds": [TEAM_N_MIN, TEAM_N_MAX],
    }
    if "explain" in board.columns:
        meta["n_explained"] = int(board["explain"].notna().sum())
    if "experimental" in board.columns:  # cross-border fixtures (experimental model, never settled)
        meta["n_experimental"] = int(board["experimental"].sum())
    meta_path(out_path).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("board: %d fixtures (%d with odds) -> %s", len(board), meta["n_with_odds"], out_path)
    return board


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30, help="window in days from today (default 30)")
    ap.add_argument("--season", action="store_true", help="the whole remaining calendar")
    ap.add_argument("--no-refresh", action="store_true", help="don't re-pull current-season results")
    ap.add_argument("--model", default=sc.DEFAULT_MODEL)
    ap.add_argument("--max-model-age", type=int, default=30)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    board = build(None if args.season else args.days, not args.no_refresh, args.model,
                  max_model_age_days=args.max_model_age)
    print(f"{len(board)} fixtures, {int(board['odds'].sum())} with odds, "
          f"{board['competition'].nunique()} leagues -> {board_path()}")


if __name__ == "__main__":
    main()
