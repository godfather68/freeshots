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

Cross-border fixtures (UEFA Champions League; shots.ingest.cross_border) never enter the domestic
feature pass: their features come from shots.features.cross_border and they are scored by the
EXPERIMENTAL model (shots.models.cross_border), label "<no-odds model>-ucl-exp". When a scan
contains such fixtures its output gains the columns `experimental` and `data_quality`; they can
never be settled and are excluded from the track record.
"""

from __future__ import annotations

import argparse
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from shots.features.cross_border import build_cross_border_features
from shots.features.team_form import build_features
from shots.ingest import cross_border as xb
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
# Competitions whose fixtures are scored but never published (picks, board): kept in training and history.
# England National League (football-data `EC`): no shot data 2016-2026, 2026/27 median off by ~2 shots and 80%
# coverage 68% (user decision 2026-10-15; reports/2026-10-15_model_candidates.md).
UNPUBLISHED_COMPETITIONS = frozenset({"England National League"})


def published(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows of ``frame`` that may be published (drops ``UNPUBLISHED_COMPETITIONS``); index reset. Applied
    after scoring, so every published row is computed exactly as before the exclusion."""
    return frame[~frame["competition"].isin(UNPUBLISHED_COMPETITIONS)].reset_index(drop=True)


def member(values: pd.Series, pool) -> pd.Series:
    """``values.isin(pool)`` for id columns (match_id): the same boolean Series, through a Python set.
    pandas' isin on Arrow-backed strings took ~3 s per call on 1e5 rows here."""
    allowed = set(pd.Series(pool).tolist())
    return pd.Series([v in allowed for v in values.tolist()], index=values.index, dtype=bool)


class SharedRun:
    """What one ``python -m shots update`` run computes once and shares between the picks scan and the
    board (see ``shared_run``). Everything here is a pure function of the run's inputs, so sharing it
    changes no output (tests/test_predict_shared.py; reports/2026-10-15_model_candidates.md):

    * fixtures.csv is downloaded once per date;
    * the domestic feature pass is built once, for the widest window of the run (``days``), and a
      narrower window is a slice of it. Features only look strictly backwards in time, so a fixture's
      row depends only on played matches and on fixtures dated on or before it; the slice is used only
      when the narrower window holds exactly the wider window's fixtures up to its last date, with
      identical rows (otherwise the features are rebuilt as before);
    * saved models are loaded once (per directory / label).
    Cross-border rows are always rebuilt: they depend on the whole fixture list.
    """

    def __init__(self, days: int | None = None):
        self.days = days
        self.fetched: dict = {}
        self.domestic: tuple | None = None   # (matches key, fixtures, played, upcoming)
        self.loaded: dict = {}
        self.stats = {"feature_builds": 0, "feature_slices": 0, "fetches": 0}

    @staticmethod
    def matches_key(matches: pd.DataFrame) -> tuple:
        h = pd.util.hash_pandas_object(matches, index=False).to_numpy()
        return (len(matches), tuple(matches.columns), int(h.sum(dtype=np.uint64)),
                int(h[::7].sum(dtype=np.uint64)))

    def slice(self, fixtures: pd.DataFrame, matches: pd.DataFrame):
        """(played, upcoming) for ``fixtures`` cut from the stored wider pass, or None."""
        if self.domestic is None:
            return None
        key, fc, played, up = self.domestic
        ids = fixtures["match_id"].tolist()
        pos = dict(zip(fc["match_id"].tolist(), range(len(fc))))
        if len(set(ids)) != len(ids) or any(i not in pos for i in ids) or key != self.matches_key(matches):
            return None
        if len(ids) and set(fc.loc[fc["date"] <= fixtures["date"].max(), "match_id"].tolist()) != set(ids):
            return None
        sub = fc.iloc[[pos[i] for i in ids]].reset_index(drop=True)
        if list(sub.columns) != list(fixtures.columns) or not sub.equals(fixtures.reset_index(drop=True)):
            return None
        upos = dict(zip(up["match_id"].tolist(), range(len(up))))
        self.stats["feature_slices"] += 1
        return played, up.iloc[[upos[i] for i in ids]].reset_index(drop=True)


_SHARED: SharedRun | None = None


@contextmanager
def shared_run(days: int | None = None):
    """Share fixtures, features and loaded models between the steps of one update run. ``days`` = the
    widest window any step asks for (the board's), so the picks scan can slice it."""
    global _SHARED
    prev, _SHARED = _SHARED, SharedRun(days)
    try:
        yield _SHARED
    finally:
        _SHARED = prev


def _load(path: Path):
    if _SHARED is None:
        return load(path)
    key = ("dir", str(path))
    if key not in _SHARED.loaded:
        _SHARED.loaded[key] = load(path)
    return _SHARED.loaded[key]


def fetch_fixtures(today: date) -> pd.DataFrame:
    """fixtures.csv for ``today`` (``_fetch_fixtures``); downloaded once per date inside a shared run."""
    if _SHARED is None:
        return _fetch_fixtures(today)
    if today not in _SHARED.fetched:
        _SHARED.fetched[today] = _fetch_fixtures(today)
        _SHARED.stats["fetches"] += 1
    return _SHARED.fetched[today].copy()


def _fetch_fixtures(today: date) -> pd.DataFrame:
    """Download fixtures.csv (kept raw under data/raw) and convert supported divisions."""
    path = fd.RAW_DIR / "fixtures" / f"fixtures_{today:%Y-%m-%d}.csv"
    try:
        resp = fd.http_get(FIXTURES_URL)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    except (requests.RequestException, OSError) as exc:
        # The season calendars still provide every fixture; only near-term odds are lost.
        cached = sorted(p for p in path.parent.glob("fixtures_*.csv") if p.stem <= path.stem) if path.parent.exists() else []
        if not cached:
            log.warning("fixtures.csv unavailable (%s) and no cached copy: calendars only", exc)
            return pd.DataFrame()
        path = cached[-1]
        log.warning("fixtures.csv unavailable (%s); using the cached %s", exc, path.name)

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
            cal = cal[~member(cal["match_id"], fd_fx["match_id"])]
        frames.append(cal.drop(columns=["round", "status", "kickoff_utc", "home_country", "away_country"],
                               errors="ignore"))
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
    key = ("noodds", name, last_played, max_age_days)
    if _SHARED is not None and key in _SHARED.loaded:
        return _SHARED.loaded[key]
    saved = sorted(STORE.glob(f"{label}_*"), key=lambda p: load_metadata(p)["until"])
    if saved and (last_played - pd.Timestamp(load_metadata(saved[-1])["until"])).days <= max_age_days:
        got = _load(saved[-1]), saved[-1].name
        if _SHARED is not None:
            _SHARED.loaded[key] = got
        return got
    train = no_odds_rows(played[played["total_shots"].notna()])
    model = MODELS[name]().fit(train)
    out = STORE / f"{label}_{last_played:%Y-%m-%d}"
    save(model, out, train, until=str(last_played.date()), variant="no market features")
    log.info("trained %s on %d rows -> %s", label, len(train), out)
    if _SHARED is not None:
        _SHARED.loaded[key] = (model, out.name)
    return model, out.name


def cross_border_model(nomodel, nolabel: str, played: pd.DataFrame, persist: bool = True):
    """The experimental cross-border model on top of the no-odds model `nolabel` (saved under
    models_store/cross_border/, so its team profiles stay fixed for the life of that model)."""
    from shots.models import cross_border as xbm
    from shots.models.cross_border import CrossBorderModel

    path = STORE / "cross_border" / f"{nolabel}-exp"
    key = ("cross_border", nolabel)
    if _SHARED is not None and persist and key in _SHARED.loaded:
        return _SHARED.loaded[key]
    if persist and (path / "model.joblib").exists():
        saved = _load(path)
        current = (xbm.LEVEL_SD, xbm.SPLIT_SD, xbm.N_PROFILES, xbm.PROFILE_SEED)
        if (saved.level_sd, saved.split_sd, saved.n_profiles, saved.seed) == current:
            if _SHARED is not None:
                _SHARED.loaded[key] = saved
            return saved
        log.info("saved cross-border model has other parameters than the code; rebuilding")
    train = no_odds_rows(played[played["total_shots"].notna()])
    model = CrossBorderModel.from_base(nomodel, train)
    if persist:
        save(model, path, train, base_model=nolabel, status="experimental",
             validation="reports/2026-10-13_champions_league.md",
             note="not fitted or calibrated on any cross-border competition; global PIT fallback")
        log.info("built the experimental cross-border model on %s -> %s", nolabel, path)
        if _SHARED is not None:
            _SHARED.loaded[key] = model
    return model


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
    experimental: pd.Series | None = None  # True for cross-border fixtures (None = there are none)
    xbmodel: object | None = None

    def is_experimental(self) -> pd.Series:
        if self.experimental is None:
            return pd.Series(False, index=self.upcoming.index)
        return self.experimental

    def routes(self):
        """Yield (mask, model, feature rows, label) per non-empty group: odds model for fixtures
        with published odds, no-odds model (market features blanked) for the rest, and the
        experimental cross-border model (one label per competition) for cross-border fixtures."""
        exp = self.is_experimental()
        if not exp.any():  # the domestic-only path, exactly as before cross-border fixtures existed
            if self.has_odds.any():
                yield self.has_odds, self.model, self.upcoming, self.model_label
            if (~self.has_odds).any():
                yield ~self.has_odds, self.nomodel, no_odds_rows(self.upcoming), self.nolabel
            return
        odds, plain = self.has_odds & ~exp, ~self.has_odds & ~exp
        if odds.any():
            yield odds, self.model, self.upcoming, self.model_label
        if plain.any():
            yield plain, self.nomodel, no_odds_rows(self.upcoming), self.nolabel
        for comp in sorted(self.upcoming.loc[exp, "competition"].unique()):
            yield (exp & (self.upcoming["competition"] == comp), self.xbmodel, no_odds_rows(self.upcoming),
                   f"{self.nolabel}-{xb.BY_NAME[comp].label}")


def _domestic_pass(matches: pd.DataFrame, fixtures: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(played, upcoming) feature rows from one feature pass over played matches + domestic fixtures."""
    combined = pd.concat([matches, fixtures.drop(columns="kickoff")], ignore_index=True)
    feats = build_features(combined)
    played = feats[member(feats["match_id"], matches["match_id"])]
    upcoming = feats[member(feats["match_id"], fixtures["match_id"])].reset_index(drop=True)
    upcoming = upcoming.drop(columns=["home_shots", "away_shots", "total_shots"], errors="ignore")
    return played, upcoming


def _warm_shared(today: date, days: int | None, use_calendar: bool, matches: pd.DataFrame) -> None:
    """Inside a shared run whose widest window is longer than this scan's, build the domestic features
    for the widest window first, so that this scan and the board slice the same pass."""
    if (_SHARED is None or _SHARED.domestic is not None or not use_calendar or days is None
            or _SHARED.days is None or _SHARED.days <= days):
        return
    wide = gather_fixtures(today, _SHARED.days, use_calendar)
    wide = wide[~member(wide["match_id"], matches["match_id"])]
    wide = wide[~wide["competition"].map(xb.is_cross_border).astype(bool)]
    if wide.empty:
        return
    played, upcoming = _domestic_pass(matches, wide)
    played.to_parquet(FEATURES_PATH, index=False)  # identical to a matches-only build
    _SHARED.domestic = (SharedRun.matches_key(matches), wide.reset_index(drop=True), played, upcoming)
    _SHARED.stats["feature_builds"] += 1


def prepare_upcoming(fixtures: pd.DataFrame, matches: pd.DataFrame, model_name: str = DEFAULT_MODEL,
                     as_of: date | None = None, max_model_age_days: int = 30) -> Prepared:
    """Build pre-match features for played + upcoming matches in one pass and load (or, in replay
    mode, fit) the odds and no-odds models. `fixtures` must carry a `kickoff` column."""
    t0 = time.time()
    kickoff = fixtures.set_index("match_id")["kickoff"]
    # Cross-border fixtures are kept out of the domestic feature pass: domestic features, league
    # priors and team histories are exactly what they are without them.
    is_xb = fixtures["competition"].map(xb.is_cross_border).astype(bool)
    xb_fixtures, fixtures = fixtures[is_xb], fixtures[~is_xb]
    shared = _SHARED is not None and not as_of
    hit = _SHARED.slice(fixtures, matches) if shared else None
    if hit is not None:
        played, upcoming = hit
    else:
        played, upcoming = _domestic_pass(matches, fixtures)
        if shared:
            _SHARED.domestic = (SharedRun.matches_key(matches), fixtures.reset_index(drop=True), played, upcoming)
            _SHARED.stats["feature_builds"] += 1
    experimental = None
    if len(xb_fixtures):
        xrows = build_cross_border_features(matches, xb_fixtures.drop(columns="kickoff"), columns=upcoming.columns)
        n_dom = len(upcoming)
        upcoming = pd.concat([upcoming, xrows], ignore_index=True) if n_dom else xrows
        experimental = pd.Series(np.arange(len(upcoming)) >= n_dom, index=upcoming.index)
    log.info("features for %d played + %d upcoming matches in %.0fs",
             len(played), len(upcoming), time.time() - t0)

    has_odds = upcoming[["mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25"]].notna().all(axis=1)
    nomodel = nolabel = xbmodel = None
    need_noodds = bool((~has_odds).any())  # cross-border fixtures never have odds, so this covers them
    if as_of:
        model = MODELS[model_name]().fit(played[played["total_shots"].notna()])
        model_label = f"{model_name}_fit_before_{as_of}"
        if (~has_odds).any():
            nomodel = MODELS[model_name]().fit(no_odds_rows(played[played["total_shots"].notna()]))
            nolabel = f"{model_name}-noodds_fit_before_{as_of}"
    else:
        if hit is None:  # a slice comes from a pass of this run that already wrote the file
            played.to_parquet(FEATURES_PATH, index=False)  # identical to a matches-only build
        model_dir = latest_model(model_name, pd.to_datetime(played["date"]).max(), max_model_age_days)
        model, model_label = _load(model_dir), model_dir.name
        if (~has_odds).any():
            nomodel, nolabel = latest_no_odds_model(model_name, played, max_model_age_days)
    if experimental is not None and need_noodds:
        xbmodel = cross_border_model(nomodel, nolabel, played, persist=not as_of)
    return Prepared(upcoming, has_odds, kickoff, played, model, model_label, nomodel, nolabel, experimental, xbmodel)


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
        fixtures = fixtures[~member(fixtures["match_id"], matches["match_id"])]
        _warm_shared(today, days, use_calendar, matches)
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
    if prep.is_experimental().any():  # only then: a domestic-only scan keeps its exact columns
        out["experimental"] = prep.is_experimental().to_numpy()
        out["data_quality"] = upcoming["data_quality"].where(prep.is_experimental(), "full").to_numpy()
    if as_of:
        actual = all_matches.set_index("match_id").eval("home_shots + away_shots")
        out["actual_shots"] = out["match_id"].map(actual).astype("Int64")
        out["hit"] = out["actual_shots"] >= shots
    out = out.sort_values(f"p_at_least_{shots}", ascending=False).reset_index(drop=True)
    out = published(out)

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
        view = shown.drop(columns=["match_id", "selected", "model", "experimental"], errors="ignore").assign(**{pcol: shown[pcol].map("{:.0%}".format)})
        print(view.to_string(index=False))


if __name__ == "__main__":
    main()
