"""Repository: every read the web app needs, as plain Python data. No SQL outside this module
(and the loaders). Every method takes `entitlements` (default FULL_ACCESS) and enforces it in the
query itself, so access control holds server-side for any caller.

    repo = Repository(engine)
    repo.state()                                   'ok' | 'missing' | 'unreadable'
    repo.board_meta()                              latest board run's metadata, or None
    repo.board_leagues(ent)                        competitions on the latest board
    repo.latest_board(filters, today, ent)         BoardView (filtered + sorted rows, totals, league groups)
    repo.match_detail(match_id, today, ent)        one board row with its curves, or None
    repo.track_records(source, ent)                DataFrame of settled-or-not official picks / backfill rows
    repo.track_summary(source, n, min_prob, ent)   what the track page shows
    repo.data_status(today)                        counts and freshness for status pages / ops

See docs/platform.md for return shapes and invariants.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, case, func, literal, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DatabaseError, OperationalError

from shots.db.access import FULL_ACCESS, Entitlements
from shots.db.engine import make_engine
from shots.db.models import Competition, JobRun, Match, Prediction, PredictionRun
from shots.db.text import fold

log = logging.getLogger(__name__)

TOP5 = ("England Premier League", "Spain La Liga", "Italy Serie A", "Germany Bundesliga", "France Ligue 1")
WHEN_DAYS = {"today": 0, "3d": 3, "7d": 7, "30d": 30}
EPS = 1e-9
TEAM_KEYS = ("home_mean", "home_median", "home_lb_70", "home_lb_80", "home_lb_90",
             "away_mean", "away_median", "away_lb_70", "away_lb_80", "away_lb_90")


def _json_list(value) -> list:
    """A JSON array column read through raw SQL (a string) or the ORM (already a list)."""
    if value is None:
        return []
    return json.loads(value) if isinstance(value, (str, bytes)) else list(value)


def weekend(today: date) -> tuple[date, date]:
    wd = today.weekday()  # Monday = 0
    sat = today - timedelta(days=1) if wd == 6 else today + timedelta(days=(5 - wd) % 7)
    return sat, sat + timedelta(days=1)


@dataclass
class BoardView:
    """rows: dicts with the board's column names (match_id, kickoff, competition, country, tier, round,
    home_team, away_team, expected_shots, lb_70/80/90, league_avg_shots, above_league, odds,
    mkt_p_over25, cold_start, model, home_*/away_* team scalars) plus date (datetime.date), p
    (P(total >= filters.n)) and, with a team filter, tp / tside. total: upcoming matches visible to the
    caller before filters; league_groups: [{country, leagues: [{name, tier, count}]}] of those."""
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    league_groups: list[dict] = field(default_factory=list)
    has_team: bool = False
    run_id: int | None = None


class Repository:
    def __init__(self, engine: Engine | str | Path | None = None):
        self.engine = make_engine(engine, create_dir=False) if not isinstance(engine, Engine) else engine

    # ------------------------------------------------------------------ health

    def _file(self) -> Path | None:
        db = make_url(str(self.engine.url)).database
        return Path(db) if db and db != ":memory:" else None

    def state(self) -> str:
        """'ok', 'missing' (no DB file, not migrated, or no board run yet) or 'unreadable'.
        Never creates the DB file."""
        path = self._file()
        if path is not None and not path.exists():
            return "missing"
        try:
            return "ok" if self._latest_run("board") is not None else "missing"
        except OperationalError as exc:
            if "no such table" in str(exc):
                return "missing"
            log.warning("database unreadable: %s", exc)
            return "unreadable"
        except DatabaseError as exc:
            log.warning("database unreadable: %s", exc)
            return "unreadable"

    # ------------------------------------------------------------------ runs

    def _latest_run(self, kind: str) -> dict | None:
        path = self._file()
        if path is not None and not path.exists():  # never create the DB file from a read
            return None
        with self.engine.connect() as c:
            r = c.execute(select(PredictionRun).where(PredictionRun.kind == kind, PredictionRun.status == "complete")
                          .order_by(PredictionRun.built_at.desc(), PredictionRun.id.desc()).limit(1)).mappings().first()
        return dict(r) if r else None

    def board_meta(self) -> dict | None:
        """The latest board run's metadata: board.meta.json keys (built_at local ISO, results_up_to,
        n_fixtures, n_with_odds, n_without_odds, models, window_days, ...) plus run_id. None if none."""
        run = self._latest_run("board")
        if run is None:
            return None
        meta = dict(run["meta"] or {})
        meta.setdefault("built_at", run["built_at"].isoformat(timespec="seconds"))
        meta.setdefault("models", run["models"])
        meta.setdefault("n_fixtures", run["n_predictions"])
        meta["run_id"] = run["id"]
        return meta

    # ------------------------------------------------------------------ board

    @staticmethod
    def _visible(stmt, today: date, ent: Entitlements, upcoming: bool = True):
        if upcoming:  # a match whose result is loaded leaves the board even before the next board build
            stmt = stmt.where(Match.date >= today, Match.status != "played")
        if not ent.can_view_all_leagues:
            stmt = stmt.where(Competition.name.in_(sorted(ent.allowed_leagues)))
        if ent.max_days_ahead is not None:
            stmt = stmt.where(Match.date <= today + timedelta(days=ent.max_days_ahead))
        return stmt

    @staticmethod
    def _board_columns(team: bool) -> list:
        cols = [Prediction.match_id, Match.date, func.coalesce(Match.kickoff_local, "").label("kickoff"),
                Competition.name.label("competition"), Competition.country, Competition.tier,
                func.coalesce(Match.round, "").label("round"), Match.home_team, Match.away_team,
                Prediction.median.label("expected_shots"), Prediction.lb_70, Prediction.lb_80, Prediction.lb_90,
                Prediction.league_avg.label("league_avg_shots"), Prediction.above_league,
                Prediction.odds_available.label("odds"), Prediction.mkt_p_over25, Prediction.cold_start,
                Prediction.model, Prediction.experimental, Prediction.data_quality]
        if team:
            cols += [getattr(Prediction, k) for k in TEAM_KEYS]
        return cols

    @staticmethod
    def _base(run_id: int):
        return (select().select_from(Prediction).join(Match, Match.match_id == Prediction.match_id)
                .join(Competition, Competition.id == Match.competition_id).where(Prediction.run_id == run_id))

    def board_leagues(self, ent: Entitlements = FULL_ACCESS) -> tuple[str, ...]:
        """Competition names on the latest board (any date) that the caller may see."""
        run = self._latest_run("board")
        if run is None:
            return ()
        stmt = self._base(run["id"]).add_columns(Competition.name).distinct()
        if not ent.can_view_all_leagues:
            stmt = stmt.where(Competition.name.in_(sorted(ent.allowed_leagues)))
        with self.engine.connect() as c:
            return tuple(sorted(c.execute(stmt).scalars()))

    def latest_board(self, f, today: date, ent: Entitlements = FULL_ACCESS) -> BoardView:
        """Filtered, sorted rows of the latest board. `f` is a shots.web.filters.Filters (or anything
        with the same attributes: leagues, groups, n, smin, smax, pmin, when, odds, nocold, q, sort,
        side, tn, tp). Filtering, the probability lookup and sorting all run in SQL."""
        run = self._latest_run("board")
        if run is None:
            return BoardView()
        team = run["team_n_min"] is not None and ent.team_predictions
        base = self._visible(self._base(run["id"]), today, ent)

        with self.engine.connect() as c:
            total = c.execute(base.add_columns(func.count())).scalar_one()
            g = c.execute(base.add_columns(Competition.country, Competition.name, Competition.tier, func.count())
                          .group_by(Competition.country, Competition.name, Competition.tier)
                          .order_by(Competition.country, Competition.tier, Competition.name)).all()

            p = self._curve_at(Prediction.total_curve, f.n, run["curve_n_min"], run["curve_n_max"])
            stmt = base.add_columns(*self._board_columns(team), p.label("p"))
            if f.side and team:
                ph = self._curve_at(Prediction.home_curve, f.tn, run["team_n_min"], run["team_n_max"])
                pa = self._curve_at(Prediction.away_curve, f.tn, run["team_n_min"], run["team_n_max"])
                if f.side == "home":
                    tp, tside = ph, literal("home")
                elif f.side == "away":
                    tp, tside = pa, literal("away")
                else:  # either team: the better of the two chances (NULL-ignoring max, like np.fmax)
                    tp = case((or_(pa.is_(None), ph >= pa), ph), else_=pa)
                    tside = case((ph >= pa, "home"), else_="away")
                stmt = stmt.add_columns(tp.label("tp"), tside.label("tside"))
                if f.tp:
                    stmt = stmt.where(tp >= f.tp / 100 - EPS)
            if f.leagues or f.groups:
                keep = [Competition.name.in_(list(f.leagues))] if f.leagues else []
                if "top5" in f.groups:
                    keep.append(Competition.name.in_(TOP5))
                if "second" in f.groups:
                    keep.append(Competition.tier == 2)
                stmt = stmt.where(or_(*keep))
            if f.smin is not None:
                stmt = stmt.where(Prediction.median >= f.smin)
            if f.smax is not None:
                stmt = stmt.where(Prediction.median <= f.smax)
            if f.pmin:
                stmt = stmt.where(p >= f.pmin / 100 - EPS)
            if f.when == "weekend":
                stmt = stmt.where(Match.date.in_(weekend(today)))
            elif f.when in WHEN_DAYS:
                stmt = stmt.where(Match.date <= today + timedelta(days=WHEN_DAYS[f.when]))
            if f.odds:
                stmt = stmt.where(Prediction.odds_available.is_(True))
            if f.nocold:
                stmt = stmt.where(func.coalesce(Prediction.cold_start, False).is_(False))
            for term in fold(f.q).split():
                stmt = stmt.where(func.instr(Match.search_key, term) > 0)
            order = [Match.date, func.coalesce(Match.kickoff_local, ""), Competition.name, Match.home_team]
            lead = {"prob": p, "shots": Prediction.median, "above": Prediction.above_league}.get(f.sort)
            stmt = stmt.order_by(*([lead.desc()] if lead is not None else []), *order)
            rows = [dict(r) for r in c.execute(stmt).mappings()]

        groups = []
        for country, name, tier, count in g:
            if not groups or groups[-1]["country"] != country:
                groups.append({"country": country, "leagues": []})
            groups[-1]["leagues"].append({"name": name, "tier": int(tier) if tier is not None else None,
                                          "count": int(count)})
        return BoardView(rows=rows, total=int(total), league_groups=groups, has_team=team, run_id=run["id"])

    @staticmethod
    def _curve_at(col, n: int, n_min: int | None, n_max: int | None):
        """SQL expression: element n - n_min of a JSON curve (NULL outside the stored range)."""
        if n_min is None or n_max is None or not (n_min <= int(n) <= n_max):
            return func.json_extract(col, "$[999999]")  # always NULL, keeps the column typed
        return func.json_extract(col, f"$[{int(n) - n_min}]")

    def match_detail(self, match_id: str, today: date, ent: Entitlements = FULL_ACCESS) -> dict | None:
        """One row of the latest board (any date, as before) with its curves, or None when the match is
        not on the board or not visible to the caller. Extra keys: curve_ns + curve (P(total >= n)),
        and with team access team_ns + home_curve + away_curve (else those are None and the team
        scalars are dropped)."""
        run = self._latest_run("board")
        if run is None:
            return None
        team = run["team_n_min"] is not None and ent.team_predictions
        stmt = self._visible(self._base(run["id"]), today, ent, upcoming=False).where(Prediction.match_id == match_id)
        stmt = stmt.add_columns(*self._board_columns(team), Prediction.total_curve,
                                *([Prediction.home_curve, Prediction.away_curve] if team else []))
        with self.engine.connect() as c:
            r = c.execute(stmt).mappings().first()
        if r is None:
            return None
        r = dict(r)
        r["curve_ns"] = list(range(run["curve_n_min"], run["curve_n_max"] + 1)) if run["curve_n_min"] is not None else []
        r["curve"] = r.pop("total_curve") or []
        if team and r.get("home_curve") is not None:
            r["team_ns"] = list(range(run["team_n_min"], run["team_n_max"] + 1))
        else:
            r.update(team_ns=None, home_curve=None, away_curve=None)
        return r

    # ------------------------------------------------------------------ track record

    def track_records(self, source: str, ent: Entitlements = FULL_ACCESS) -> pd.DataFrame:
        """source 'live': official picks (per match and N the last live scan on or before match day
        that was made and inserted before kick-off), columns match_id, date, competition, n, p, status
        (settled | pending | void), actual, hit, scan_ts, source. source 'replay': the same for replay
        files (after-the-fact re-runs, never official). source 'experimental': the same rule for
        experimental predictions (v_experimental_picks; settled by manual results, never official).
        source 'backfill': the latest backfill run, same columns (status always settled or void). date is
        datetime64; hit is boolean (NA unless settled)."""
        cols = ["match_id", "date", "competition", "n", "p", "status", "actual", "hit", "scan_ts", "source"]
        path = self._file()
        if path is not None and not path.exists():  # never create the DB file from a read
            return pd.DataFrame(columns=cols).astype({"n": int, "p": float})
        if source in ("live", "replay", "experimental"):
            view = {"live": "v_official_picks", "replay": "v_replay_picks", "experimental": "v_experimental_picks"}[source]
            sql = text("SELECT match_id, date, competition, n, p, status, actual_total AS actual, hit, scan_ts, source "
                       f"FROM {view} ORDER BY date, n, match_id")
            params = {}
        else:
            run = self._latest_run("backfill")
            if run is None:
                return pd.DataFrame(columns=cols)
            sql = text("SELECT pt.match_id, o.date, o.competition, pt.n, pt.p, o.status, o.actual_total AS actual, "
                       "CASE WHEN o.status = 'settled' THEN o.actual_total >= pt.n END AS hit, "
                       "NULL AS scan_ts, 'backfill' AS source "
                       "FROM v_prediction_points pt JOIN v_prediction_outcomes o ON o.prediction_id = pt.prediction_id "
                       "WHERE pt.run_id = :run ORDER BY o.date, pt.n, pt.match_id")
            params = {"run": run["id"]}
        with self.engine.connect() as c:
            df = pd.DataFrame(c.execute(sql, params).all(), columns=cols)
        if not ent.can_view_all_leagues and len(df):
            df = df[df["competition"].isin(ent.allowed_leagues)].reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        df["n"] = df["n"].astype(int)
        df["p"] = df["p"].astype(float)
        df["actual"] = pd.array(df["actual"], dtype="Int64")
        df["hit"] = pd.array([None if pd.isna(h) else bool(h) for h in df["hit"]], dtype="boolean")
        return df

    def track_summary(self, source: str, n: int, min_prob: float, ent: Entitlements = FULL_ACCESS) -> dict:
        """-> {src, counts (live status counts), avail (Ns with settled rows), n (resolved N), has_data,
        settled (DataFrame of settled rows at n, or empty), metrics (shots.predict.track.metrics or None),
        total, n_comp}. Without track_detail only headline metrics are returned (no reliability bands,
        per-league or cumulative tables)."""
        from shots.predict import track

        src = source if source in ("backfill", "live") else "backfill"
        live = self.track_records("live", ent)
        counts = {k: int(v) for k, v in live["status"].value_counts().items()} if len(live) else {}
        df = live if src == "live" else self.track_records("backfill", ent)
        df = df[df["status"] == "settled"]
        avail = sorted(int(x) for x in df["n"].unique()) if len(df) else []
        n = n if n in avail else (24 if 24 in avail else (avail[0] if avail else n))
        out = dict(src=src, counts=counts, avail=avail, n=n, has_data=bool(avail), settled=df.iloc[0:0],
                   metrics=None, total=0, n_comp=0)
        if avail:
            sub = df[df["n"] == n].assign(hit=lambda d: d["hit"].astype(bool))
            m = track.metrics(sub, min_prob, None)
            if not ent.track_detail:
                m = {k: v for k, v in m.items() if k in ("all", "selected")}
            out.update(settled=sub, metrics=m, total=len(sub), n_comp=int(sub["competition"].nunique()))
        return out

    # ------------------------------------------------------------------ static export (shots.export)

    def board_export(self, today: date, ent: Entitlements = FULL_ACCESS) -> dict | None:
        """The upcoming rows of the latest board with their full curves, in display order (date,
        kick-off, competition, home). -> {run_id, curve_n_min/max, team_n_min/max (None without team
        access), rows}; rows carry match_id, date, kickoff ("" if unknown), kickoff_utc, competition,
        country, tier, round, teams, expected_shots, lb_70/80/90, league_avg_shots, above_league, odds
        (bool), cold_start, model, total_curve, explain (dict or None: the stored decomposition of the
        expected total, shots.models.explain) and, with team access, home_/away_ mean, median, lb_80,
        curve. No odds values and no source columns are selected. None when there is no board run."""
        run = self._latest_run("board")
        if run is None:
            return None
        team = run["team_n_min"] is not None and ent.team_predictions
        cols = [Prediction.match_id, Match.date, func.coalesce(Match.kickoff_local, "").label("kickoff"),
                Match.kickoff_utc, Competition.name.label("competition"), Competition.country, Competition.tier,
                func.coalesce(Match.round, "").label("round"), Match.home_team, Match.away_team,
                Prediction.median.label("expected_shots"), Prediction.lb_70, Prediction.lb_80, Prediction.lb_90,
                Prediction.league_avg.label("league_avg_shots"), Prediction.above_league,
                Prediction.odds_available.label("odds"), Prediction.cold_start, Prediction.model,
                Prediction.total_curve, Prediction.experimental, Prediction.data_quality, Prediction.explain]
        if team:
            cols += [Prediction.home_mean, Prediction.home_median, Prediction.home_lb_80, Prediction.home_curve,
                     Prediction.away_mean, Prediction.away_median, Prediction.away_lb_80, Prediction.away_curve]
        stmt = (self._visible(self._base(run["id"]), today, ent).add_columns(*cols)
                .order_by(Match.date, func.coalesce(Match.kickoff_local, ""), Competition.name, Match.home_team,
                          Prediction.match_id))
        with self.engine.connect() as c:
            rows = [dict(r) for r in c.execute(stmt).mappings()]
        return {"run_id": run["id"], "curve_n_min": run["curve_n_min"], "curve_n_max": run["curve_n_max"],
                "team_n_min": run["team_n_min"] if team else None, "team_n_max": run["team_n_max"] if team else None,
                "rows": rows}

    def official_results(self, since: date, until: date, ent: Entitlements = FULL_ACCESS) -> list[dict]:
        """Finished matches dated since..until that had an official board prediction: per match the
        last board run that is pre-match by the v_official_picks rule (v_prediction_outcomes.pre_match:
        built and inserted before the lock moment; board runs are always live builds, never replays),
        and whose result has shots data (status settled). Newest match first. Keys: match_id, date,
        competition, country, tier, home_team, away_team, actual_total/home/away, predicted_at (the
        prediction's insert time, DB clock, UTC), expected_shots, lb_70/80/90, odds (bool),
        total_curve, curve_n_min, curve_n_max, experimental (always False here), result_source
        ("football-data" | "manual"), source_url (manual results only, else None).
        Experimental predictions are excluded: see settled_results."""
        return self.settled_results(since, until, ent, experimental=False)

    def settled_results(self, since: date | None, until: date, ent: Entitlements = FULL_ACCESS,
                        experimental: bool | None = None) -> list[dict]:
        """official_results' rule applied to ordinary (experimental=False), experimental (True:
        cross-border competitions, settled by manual results, never part of the official record) or all
        (None) predictions. since=None: no lower date bound. Same keys and order as official_results;
        `experimental` tells the two kinds apart."""
        path = self._file()
        if path is not None and not path.exists():  # never create the DB file from a read
            return []
        flags = {None: "0, 1", False: "0", True: "1"}[experimental]
        sql = text(f"""
            SELECT match_id, date, competition, country, tier, home_team, away_team, actual_total, actual_home,
                   actual_away, predicted_at, expected_shots, lb_70, lb_80, lb_90, odds, total_curve,
                   curve_n_min, curve_n_max, experimental, manual, source_url
            FROM (SELECT p.match_id, o.date, o.competition, c.country, c.tier, m.home_team, m.away_team,
                         o.actual_total, o.actual_home, o.actual_away, p.created_at AS predicted_at,
                         p.median AS expected_shots, p.lb_70, p.lb_80, p.lb_90, p.odds_available AS odds,
                         p.total_curve, r.curve_n_min, r.curve_n_max, o.experimental,
                         mr.match_id IS NOT NULL AS manual, mr.source_url,
                         ROW_NUMBER() OVER (PARTITION BY p.match_id ORDER BY r.built_at DESC, r.id DESC) AS rn
                  FROM predictions p
                  JOIN prediction_runs r ON r.id = p.run_id
                  JOIN v_prediction_outcomes o ON o.prediction_id = p.id
                  JOIN matches m ON m.match_id = p.match_id
                  JOIN competitions c ON c.id = m.competition_id
                  LEFT JOIN manual_results mr ON mr.match_id = m.match_id
                  WHERE r.kind = 'board' AND r.status = 'complete' AND o.pre_match = 1 AND o.status = 'settled'
                    AND o.experimental IN ({flags})
                    AND m.date >= :since AND m.date <= :until)
            WHERE rn = 1
            ORDER BY date DESC, competition, home_team, match_id""")
        params = {"since": since.isoformat() if since else "0000-01-01", "until": until.isoformat()}
        with self.engine.connect() as c:
            rows = [dict(r) for r in c.execute(sql, params).mappings()]
        out = []
        for r in rows:
            if not ent.league_allowed(r["competition"]):
                continue
            r["date"] = date.fromisoformat(str(r["date"])[:10])
            r["predicted_at"] = pd.Timestamp(r["predicted_at"]).to_pydatetime()
            r["total_curve"] = _json_list(r["total_curve"])
            r["odds"] = None if r["odds"] is None else bool(r["odds"])
            r["experimental"] = bool(r["experimental"])
            r["result_source"] = "manual" if r.pop("manual") else "football-data"
            out.append(r)
        return out

    def base_rates(self, n: int, before: date) -> tuple[dict[str, float], float | None]:
        """Naive baseline of the track record: per competition, the share of played matches (with shots
        data) dated before `before` that reached at least n total shots; and the overall share (None
        without history). Same definition as shots.predict.track.base_rates, from the DB's results.
        The overall share counts automatic-source results only, so manual results (experimental
        competitions) never move a domestic baseline; they only form their own competition's share."""
        sql = text("SELECT c.name, sum(m.home_shots + m.away_shots >= :n), count(*), m.source = 'manual' "
                   "FROM matches m JOIN competitions c ON c.id = m.competition_id "
                   "WHERE m.status = 'played' AND m.home_shots IS NOT NULL AND m.away_shots IS NOT NULL "
                   "AND m.date < :before GROUP BY c.name, m.source = 'manual'")
        with self.engine.connect() as c:
            rows = c.execute(sql, {"n": int(n), "before": before.isoformat()}).all()
        per: dict[str, list[int]] = {}
        for name, h, k, _ in rows:
            acc = per.setdefault(name, [0, 0])
            acc[0] += int(h)
            acc[1] += int(k)
        auto = [(int(h), int(k)) for _, h, k, manual in rows if not manual]
        total = sum(k for _, k in auto)
        return ({name: h / k for name, (h, k) in per.items()},
                sum(h for h, _ in auto) / total if total else None)

    def competitions(self) -> dict[str, dict]:
        """{competition name: {country, tier}} for every competition in the DB."""
        with self.engine.connect() as c:
            return {name: {"country": country, "tier": int(tier) if tier is not None else None}
                    for name, country, tier in c.execute(select(Competition.name, Competition.country, Competition.tier))}

    def last_finished_job(self, job: str = "update") -> dict | None:
        """The most recent finished run of `job` (job_runs row as a dict), or None."""
        with self.engine.connect() as c:
            r = c.execute(select(JobRun).where(JobRun.job == job, JobRun.finished_at.is_not(None))
                          .order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1)).mappings().first()
        return dict(r) if r else None

    def record_since(self):
        """When this database's live record starts: the insert time (UTC) of its earliest pre-match
        prediction, or None. It moves forward if the database was ever rebuilt from scratch."""
        with self.engine.connect() as c:
            ts = c.execute(select(func.min(Prediction.created_at)).where(Prediction.pre_match.is_(True))).scalar()
        return ts

    # ------------------------------------------------------------------ status

    def data_status(self, today: date | None = None) -> dict:
        """Counts and freshness: {revision, matches_played, latest_result, upcoming_fixtures, competitions,
        teams, runs: {kind: {count, latest_built_at}}, predictions, latest_board_run, last_job}."""
        from shots.db import migrate

        today = today or date.today()
        with self.engine.connect() as c:
            played = c.execute(select(func.count(), func.max(Match.date)).where(Match.status == "played")).one()
            upcoming = c.execute(select(func.count()).where(Match.status == "scheduled", Match.date >= today)).scalar_one()
            runs = {k: {"count": int(n), "latest_built_at": b} for k, n, b in c.execute(
                select(PredictionRun.kind, func.count(), func.max(PredictionRun.built_at)).group_by(PredictionRun.kind))}
            preds = c.execute(select(func.count()).select_from(Prediction)).scalar_one()
            comps = c.execute(select(func.count()).select_from(Competition)).scalar_one()
            teams = c.execute(text("SELECT count(*) FROM teams")).scalar_one()
            job = c.execute(select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1)).mappings().first()
        board = self._latest_run("board")
        return {"revision": migrate.current(self.engine), "matches_played": int(played[0]), "latest_result": played[1],
                "upcoming_fixtures": int(upcoming), "competitions": int(comps), "teams": int(teams), "runs": runs,
                "predictions": int(preds), "latest_board_run": board["id"] if board else None,
                "last_job": dict(job) if job else None}
