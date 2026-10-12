"""Scan upcoming fixtures for matches likely to reach at least N total shots.

Usage:
    python -m shots.predict.scan                       # P(total >= 24) >= 70%, all leagues
    python -m shots.predict.scan --shots 26 --min-prob 0.6
    python -m shots.predict.scan --no-refresh          # skip re-pulling this season's results

Steps: refresh current-season results -> fetch football-data.co.uk fixtures.csv -> build
pre-match features for played + upcoming matches in one pass (upcoming rows have no shots, and
features only look strictly backwards, so played-match features are unchanged) -> load the
latest saved model (retrain if stale) -> calibrated P(total >= N) per fixture -> filter, rank.

Every scanned fixture is saved to reports/picks/ (not only the selected ones), so picks can be
settled against results later.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from shots.features.team_form import build_features
from shots.ingest import football_data as fd
from shots.models import MODELS
from shots.models.persist import load, load_metadata, save
from shots.models.train import STORE, train_and_save

log = logging.getLogger(__name__)

FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
FEATURES_PATH = fd.ROOT / "data" / "processed" / "features.parquet"
PICKS_DIR = fd.ROOT / "reports" / "picks"
CALENDAR_PATH = fd.ROOT / "data" / "processed" / "fixtures.parquet"  # built by shots.ingest.calendar_ics
DEFAULT_MODEL = "negbin_pit_comp"
COLD_START_MATCHES = 5


def fetch_fixtures(today: date) -> pd.DataFrame:
    """Download fixtures.csv (kept raw under data/raw) and convert supported divisions."""
    resp = requests.get(FIXTURES_URL, headers={"User-Agent": fd.USER_AGENT}, timeout=30)
    resp.raise_for_status()
    path = fd.RAW_DIR / "fixtures" / f"fixtures_{today:%Y-%m-%d}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)

    raw = fd.read_raw(path)
    season = fd.current_season_start(today)
    unsupported = sorted(set(raw["Div"]) - set(fd.DIVISIONS))
    if unsupported:
        log.info("skipping fixtures from divisions without shot history: %s", ", ".join(unsupported))
    frames = []
    for div, rows in raw[raw["Div"].isin(fd.DIVISIONS)].groupby("Div"):
        canon = fd.to_canonical(rows, div, season)
        canon["kickoff"] = rows["Time"].to_numpy() if "Time" in rows else ""
        frames.append(canon)
    fixtures = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return fixtures[fixtures["date"] >= pd.Timestamp(today)] if len(fixtures) else fixtures


def gather_fixtures(start: date, days: int | None, use_calendar: bool = True) -> pd.DataFrame:
    """Upcoming fixtures in [start, start + days]: the season calendars (all remaining matches of
    their leagues, no odds) merged with football-data's fixtures.csv (near-term, with odds). When a
    match is in both, the football-data row wins so its odds are used."""
    fd_fx = fetch_fixtures(start)
    frames = [fd_fx] if len(fd_fx) else []
    if use_calendar and CALENDAR_PATH.exists():
        cal = pd.read_parquet(CALENDAR_PATH)
        cal = cal[cal["date"] >= pd.Timestamp(start)]
        if len(fd_fx):
            cal = cal[~cal["match_id"].isin(fd_fx["match_id"])]
        frames.append(cal.drop(columns=["round", "status"], errors="ignore"))
    if not frames:
        return fd_fx
    fixtures = pd.concat(frames, ignore_index=True)
    if days is not None:
        fixtures = fixtures[fixtures["date"] <= pd.Timestamp(start) + pd.Timedelta(days=days)]
    return fixtures.reset_index(drop=True)


def no_odds_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Same rows with every market feature blanked: the no-odds model is trained this way."""
    out = frame.copy()
    out[[c for c in out.columns if c.startswith("mkt_")]] = float("nan")
    return out


def latest_no_odds_model(name: str, played: pd.DataFrame, max_age_days: int) -> tuple[object, str]:
    """No-odds variant of `name` (fitted with market features blanked), cached in models_store.
    Fixtures without published odds must use it: the odds model given missing odds overstates
    its picks (2025/26: 67% hit at 77% stated, vs 75.5% at 76.2% for this variant)."""
    label = f"{name}-noodds"
    last_played = pd.to_datetime(played["date"]).max()
    saved = sorted(STORE.glob(f"{label}_*"), key=lambda p: load_metadata(p)["until"])
    if saved and (last_played - pd.Timestamp(load_metadata(saved[-1])["until"])).days <= max_age_days:
        return load(saved[-1]), saved[-1].name
    train = no_odds_rows(played[played["total_shots"].notna()])
    model = MODELS[name]().fit(train)
    out = STORE / f"{label}_{last_played:%Y-%m-%d}"
    save(model, out, train, until=str(last_played.date()), variant="no market features")
    log.info("trained %s on %d rows -> %s", label, len(train), out)
    return model, out.name


def latest_model(name: str, last_played: pd.Timestamp, max_age_days: int) -> Path:
    """Most recent saved model; retrain on all played matches if it is older than max_age_days."""
    saved = sorted(STORE.glob(f"{name}_*"), key=lambda p: load_metadata(p)["until"])
    if saved:
        until = pd.Timestamp(load_metadata(saved[-1])["until"])
        if (last_played - until).days <= max_age_days:
            return saved[-1]
        log.info("saved model trained until %s is stale; retraining", until.date())
    return train_and_save(name)


@dataclass
class Prepared:
    """Upcoming fixtures with features, odds mask and the models that score them."""
    upcoming: pd.DataFrame
    has_odds: pd.Series
    kickoff: pd.Series
    played: pd.DataFrame
    model: object
    model_label: str
    nomodel: object | None = None
    nolabel: str | None = None

    def routes(self):
        """Yield (mask, model, feature rows, label) per non-empty group: odds model for fixtures
        with published odds, no-odds model (market features blanked) for the rest."""
        if self.has_odds.any():
            yield self.has_odds, self.model, self.upcoming, self.model_label
        if (~self.has_odds).any():
            yield ~self.has_odds, self.nomodel, no_odds_rows(self.upcoming), self.nolabel


def prepare_upcoming(fixtures: pd.DataFrame, matches: pd.DataFrame, model_name: str = DEFAULT_MODEL,
                     as_of: date | None = None, max_model_age_days: int = 30) -> Prepared:
    """Build pre-match features for played + upcoming matches in one pass and load (or, in replay
    mode, fit) the odds and no-odds models. `fixtures` must carry a `kickoff` column."""
    t0 = time.time()
    kickoff = fixtures.set_index("match_id")["kickoff"]
    combined = pd.concat([matches, fixtures.drop(columns="kickoff")], ignore_index=True)
    feats = build_features(combined)
    played = feats[feats["match_id"].isin(matches["match_id"])]
    upcoming = feats[feats["match_id"].isin(fixtures["match_id"])].reset_index(drop=True)
    upcoming = upcoming.drop(columns=["home_shots", "away_shots", "total_shots"], errors="ignore")
    log.info("features for %d played + %d upcoming matches in %.0fs",
             len(played), len(upcoming), time.time() - t0)

    has_odds = upcoming[["mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25"]].notna().all(axis=1)
    nomodel = nolabel = None
    if as_of:
        model = MODELS[model_name]().fit(played[played["total_shots"].notna()])
        model_label = f"{model_name}_fit_before_{as_of}"
        if (~has_odds).any():
            nomodel = MODELS[model_name]().fit(no_odds_rows(played[played["total_shots"].notna()]))
            nolabel = f"{model_name}-noodds_fit_before_{as_of}"
    else:
        played.to_parquet(FEATURES_PATH, index=False)  # identical to a matches-only build
        model_dir = latest_model(model_name, pd.to_datetime(played["date"]).max(), max_model_age_days)
        model, model_label = load(model_dir), model_dir.name
        if (~has_odds).any():
            nomodel, nolabel = latest_no_odds_model(model_name, played, max_model_age_days)
    return Prepared(upcoming, has_odds, kickoff, played, model, model_label, nomodel, nolabel)


def scan(shots: int = 24, min_prob: float = 0.70, model_name: str = DEFAULT_MODEL,
         refresh: bool = True, max_model_age_days: int = 30, today: date | None = None,
         as_of: date | None = None, days: int | None = 7, use_calendar: bool = True) -> pd.DataFrame:
    """as_of: replay mode. Pretend it is that date: use only results dated before it, fit the
    model in memory on them, score that day's fixtures file, and attach the actual totals.
    days: scan window from today (None = the whole remaining calendar)."""
    today = today or date.today()
    if refresh and not as_of:
        fd.build(refresh_current=True)
    all_matches = pd.read_parquet(fd.OUT_PATH)
    if as_of:
        matches = all_matches[all_matches["date"] < pd.Timestamp(as_of)]
        fixtures = fetch_fixtures(as_of)
    else:
        matches = all_matches
        fixtures = gather_fixtures(today, days, use_calendar)
        fixtures = fixtures[~fixtures["match_id"].isin(matches["match_id"])]
    if fixtures.empty:
        log.info("no upcoming fixtures in supported divisions")
        return pd.DataFrame()

    prep = prepare_upcoming(fixtures, matches, model_name, as_of, max_model_age_days)
    upcoming, has_odds, kickoff = prep.upcoming, prep.has_odds, prep.kickoff
    model_label = prep.model_label

    # Route each fixture to the model matching its inputs: odds model when odds are published,
    # no-odds model otherwise.
    p = pd.Series(float("nan"), index=upcoming.index)
    median = pd.Series(float("nan"), index=upcoming.index)
    lb80 = pd.Series(pd.NA, index=upcoming.index, dtype="Int64")
    used = pd.Series(model_label, index=upcoming.index)
    for mask, mdl, rows, label in prep.routes():
        used[mask] = label
        sub = rows[mask]
        pr = mdl.predict(sub)
        p[mask] = mdl.prob_at_least(sub, shots)
        median[mask] = pr["median"]
        lb80[mask] = pr["lb_80"].astype("Int64")
    pred = pd.DataFrame({"median": median, "lb_80": lb80})

    out = pd.DataFrame({
        "date": pd.to_datetime(upcoming["date"]).dt.date,
        "kickoff": upcoming["match_id"].map(kickoff).fillna(""),
        "competition": upcoming["competition"],
        "home_team": upcoming["home_team"],
        "away_team": upcoming["away_team"],
        f"p_at_least_{shots}": p.round(3),
        "expected_shots": pred["median"],
        # league_prior_shots is per team per match -> x2 for the match total.
        "league_avg_shots": (2 * upcoming["league_prior_shots"]).round(1),
        "lb_80": pred["lb_80"],
        "odds": has_odds.map({True: "yes", False: "no"}),
        "mkt_p_over25": upcoming["mkt_p_over25"].round(3),
        "cold_start": upcoming[["home_n_prior_matches", "away_n_prior_matches"]].min(axis=1)
        < COLD_START_MATCHES,
        "match_id": upcoming["match_id"],
        "model": used,
    })
    out.insert(out.columns.get_loc("league_avg_shots") + 1, "above_league",
               (out["expected_shots"] - out["league_avg_shots"]).round(1))
    out["selected"] = out[f"p_at_least_{shots}"] >= min_prob
    if as_of:
        actual = all_matches.set_index("match_id").eval("home_shots + away_shots")
        out["actual_shots"] = out["match_id"].map(actual).astype("Int64")
        out["hit"] = out["actual_shots"] >= shots
    out = out.sort_values(f"p_at_least_{shots}", ascending=False).reset_index(drop=True)

    PICKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"replay_{as_of}" if as_of else datetime.now().strftime("%Y-%m-%d_%H%M")
    path = PICKS_DIR / f"{stamp}_at_least_{shots}.csv"
    out.assign(min_prob=min_prob).to_csv(path, index=False)
    log.info("scored %d fixtures (%d with odds) -> %s", len(out), int(has_odds.sum()), path)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shots", type=int, default=24, help="total-shots threshold N (default 24)")
    ap.add_argument("--min-prob", type=float, default=0.70, help="minimum P(total >= N) to show")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-refresh", action="store_true", help="don't re-pull current-season results")
    ap.add_argument("--max-model-age", type=int, default=30, help="retrain if model is older (days)")
    ap.add_argument("--all", action="store_true", help="show every fixture, not only selected")
    ap.add_argument("--days", type=int, default=7, help="scan window in days from today (default 7)")
    ap.add_argument("--season", action="store_true", help="scan the whole remaining calendar")
    ap.add_argument("--no-calendar", action="store_true", help="use football-data fixtures.csv only")
    ap.add_argument("--rank-by", choices=["prob", "above_league"], default="prob",
                    help="above_league: matches expected to beat their own league's shot level most")
    ap.add_argument("--top", type=int, default=None, help="show at most this many rows")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None,
                    help="replay: score the latest fixtures file as if it were this date (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    out = scan(args.shots, args.min_prob, args.model, not args.no_refresh, args.max_model_age,
               as_of=args.as_of, days=None if args.season else args.days,
               use_calendar=not args.no_calendar)
    if out.empty:
        return
    if args.as_of and out["actual_shots"].notna().any():
        sel = out[out["selected"] & out["actual_shots"].notna()]
        print(f"\nreplay {args.as_of}: selected {len(sel)}, reached {args.shots}+ in {int(sel['hit'].sum())} "
              f"(expected {sel[f'p_at_least_{args.shots}'].sum():.1f}); "
              f"all fixtures: {out['hit'].mean():.0%} reached {args.shots}+")
    pcol = f"p_at_least_{args.shots}"
    if args.rank_by == "above_league":
        out = out.sort_values(["above_league", pcol], ascending=False)
    shown = out if args.all else out[out["selected"]]
    if args.top:
        shown = shown.head(args.top)
    print(f"\n{int(out['selected'].sum())} of {len(out)} upcoming matches with P(total shots >= {args.shots}) "
          f">= {args.min_prob:.0%}{' (showing all)' if args.all else ''}\n")
    if len(shown):
        view = shown.drop(columns=["match_id", "selected", "model"]).assign(**{pcol: shown[pcol].map("{:.0%}".format)})
        print(view.to_string(index=False))


if __name__ == "__main__":
    main()
