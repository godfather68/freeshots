"""Loaders: modelling-cache files (parquet / CSV) -> the DB. Every loader is idempotent.

    upsert_matches(engine, matches_df)            played matches with results (matches.parquet)
    upsert_fixtures(engine, fixtures_df, today)   upcoming fixtures (calendars + football-data fixtures.csv)
    import_board(engine, board_df, meta)          one prediction run from the UI board
    import_picks_file / import_picks_dir          one picks run per scan CSV in reports/picks/
    import_backfill(engine, backfill_df)          the out-of-sample season replay as a run
    sync_all(engine)                              all of the above from the default file locations

Predictions are only ever inserted (append-only, enforced by DB triggers). Outcomes are not copied:
the views v_prediction_outcomes / v_official_picks derive them from the current match results.
Each prediction gets `pre_match` at insert: the run was built AND the row is inserted (DB clock, which
is also its created_at) before the match's lock moment (lock_moment()). Replays (`replay_*` picks files,
params.source = 'replay') are inserted after their matches, so they are never pre-match; the official
live record (v_official_picks) counts live scans only (migration 0003).

Kick-off times: the canonical `kickoff` / DB `kickoff_local` is the league's local "HH:MM" and
`kickoff_utc` the same instant in UTC. Season calendars are UTC at the source (converted to local by
shots.ingest.calendar_ics). football-data's fixtures.csv `Time` is UK time: read_fixtures converts it
UK -> UTC -> league local. Board / picks frames do not say which zone their `kickoff` is in (they mix
both sources), so they never overwrite a known kick-off (see _ensure_matches).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Engine, bindparam, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from shots.db import migrate
from shots.db.engine import make_engine, session_scope
from shots.db.models import Competition, Match, Prediction, PredictionRun, Team, utcnow
from shots.db.text import search_key
from shots.ingest import football_data as fd
from shots.ingest.schema import make_team_id

log = logging.getLogger(__name__)

PROCESSED = fd.ROOT / "data" / "processed"
MATCHES_PATH = fd.OUT_PATH
CALENDAR_PATH = PROCESSED / "fixtures.parquet"
FD_FIXTURES_DIR = fd.RAW_DIR / "fixtures"
PICKS_DIR = fd.ROOT / "reports" / "picks"
BACKFILL_PATH = PROCESSED / "track_backfill.parquet"
CHUNK = 2000

RESULT_COLS = ["home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"]
ODDS_COLS = ["odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"]
DIVISION_BY_NAME = {f"{country} {comp}": (div, country, tier) for div, (comp, country, tier) in fd.DIVISIONS.items()}
TEAM_SCALARS = ("mean", "median", "lb_70", "lb_80", "lb_90")
UK_TZ = "Europe/London"  # football-data.co.uk publishes kick-off times in UK time


# ----------------------------------------------------------------------------- value helpers

def _v(x):
    """pandas/numpy scalar -> plain Python, NA/NaN -> None."""
    if x is None or x is pd.NA or x is pd.NaT:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    if hasattr(x, "item"):  # numpy scalar
        x = x.item()
        if isinstance(x, float) and math.isnan(x):
            return None
    return x


def _int(x):
    x = _v(x)
    return None if x is None else int(x)


def _float(x):
    x = _v(x)
    return None if x is None else float(x)


def _bool(x):
    x = _v(x)
    if x is None:
        return None
    if isinstance(x, str):
        return {"yes": True, "true": True, "1": True, "no": False, "false": False, "0": False}.get(x.strip().lower())
    return bool(x)


def _str(x):
    x = _v(x)
    return None if x is None or (isinstance(x, str) and not x.strip()) else str(x)


def _date(x) -> date | None:
    x = _v(x)
    return None if x is None else pd.Timestamp(x).date()


def local_to_utc(ts: datetime) -> datetime:
    """Naive local wall-clock time (pipeline convention) -> naive UTC."""
    if ts.tzinfo is None:
        ts = ts.astimezone()  # interpret as this machine's local time
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def league_tz(country: str | None) -> str | None:
    from shots.ingest.calendar_ics import TIMEZONES

    return TIMEZONES.get(country) if country else None


def uk_kickoffs(dates: pd.Series, times: pd.Series, country: str | None) -> tuple[pd.Series, pd.Series]:
    """football-data `Time` (UK wall clock) on `dates` -> (league-local "HH:MM", naive UTC timestamp).
    Unparseable times give (None, NaT); an unknown league zone gives (None, UTC) rather than a
    wrong-zone local time. The match date itself is kept as published (it is part of match_id)."""
    idx = dates.index
    local_out = pd.Series([None] * len(idx), index=idx, dtype=object)
    utc_out = pd.Series(pd.NaT, index=idx, dtype="datetime64[us]")
    t = times.astype(object).where(times.notna(), "").astype(str).str.strip()
    ok = t.str.fullmatch(r"\d{1,2}:\d{2}")
    if not ok.any():
        return local_out, utc_out
    wall = pd.to_datetime(pd.to_datetime(dates[ok]).dt.strftime("%Y-%m-%d") + " " + t[ok].str.zfill(5))
    utc = wall.dt.tz_localize(UK_TZ, ambiguous=True, nonexistent="shift_forward").dt.tz_convert("UTC")
    utc_out[ok] = utc.dt.tz_localize(None).astype("datetime64[us]")
    tz = league_tz(country)
    if tz:
        local_out[ok] = utc.dt.tz_convert(tz).dt.strftime("%H:%M")
    return local_out, utc_out


def lock_moment(day: date, kickoff_utc: datetime | None, kickoff: str | None = None,
                country: str | None = None) -> datetime:
    """When a match locks (naive UTC): its kickoff_utc when known. Else, if the source row gives an
    "HH:MM" of unknown zone (board / picks frames mix league-local and UK times), the earliest of the
    league-local and UK readings. Else 00:00 UTC of the match date (before any European kick-off).
    A prediction is pre-match when its run was built strictly before this moment."""
    if kickoff_utc is not None and not pd.isna(kickoff_utc):
        return pd.Timestamp(kickoff_utc).to_pydatetime().replace(tzinfo=None)
    k = kickoff.strip() if isinstance(kickoff, str) else ""
    if ":" in k:
        try:
            hh, mm = (int(x) for x in k.split(":"))
            wall = datetime.combine(day, time(hh, mm))
        except ValueError:
            wall = None
        if wall is not None:
            zones = {UK_TZ} | ({league_tz(country)} if league_tz(country) else set())
            return min(wall.replace(tzinfo=ZoneInfo(z)).astimezone(timezone.utc) for z in zones).replace(tzinfo=None)
    return datetime.combine(day, time(0, 0))


def _curve(row, cols) -> list | None:
    vals = [_float(row.get(c)) for c in cols]
    return None if all(v is None for v in vals) else vals


def _chunks(rows: list, n: int = CHUNK):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


# ----------------------------------------------------------------------------- reference data

def _competition_ids(session: Session, frame: pd.DataFrame) -> dict[str, int]:
    """Upsert competitions named in frame (columns competition, country, tier) -> {name: id}."""
    comps = frame[["competition", "country", "tier"]].drop_duplicates("competition", keep="last")
    rows = []
    for r in comps.itertuples(index=False):
        div = DIVISION_BY_NAME.get(r.competition)
        rows.append(dict(name=r.competition, country=r.country, tier=_int(r.tier), code=div[0] if div else None))
    if rows:
        stmt = sqlite_insert(Competition)
        session.execute(stmt.on_conflict_do_update(
            index_elements=[Competition.name],
            set_=dict(country=stmt.excluded.country, tier=func.coalesce(stmt.excluded.tier, Competition.tier),
                      code=func.coalesce(stmt.excluded.code, Competition.code))), rows)
    return dict(session.execute(select(Competition.name, Competition.id)).all())


def _upsert_teams(session: Session, frame: pd.DataFrame) -> None:
    """frame: country, home_team, away_team, home_team_id, away_team_id; ordered so the latest name wins."""
    parts = [frame[["home_team_id", "home_team", "country"]].set_axis(["team_id", "name", "country"], axis=1),
             frame[["away_team_id", "away_team", "country"]].set_axis(["team_id", "name", "country"], axis=1)]
    teams = pd.concat(parts, ignore_index=True).drop_duplicates("team_id", keep="last")
    rows = [dict(team_id=r.team_id, name=r.name, country=r.country) for r in teams.itertuples(index=False)]
    if rows:
        stmt = sqlite_insert(Team)
        session.execute(stmt.on_conflict_do_update(
            index_elements=[Team.team_id], set_=dict(name=stmt.excluded.name, country=stmt.excluded.country),
            where=(Team.name.is_not(stmt.excluded.name)) | (Team.country.is_not(stmt.excluded.country))), rows)


def _with_team_ids(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for side in ("home", "away"):
        col = f"{side}_team_id"
        ids = frame[col] if col in frame else pd.Series(pd.NA, index=frame.index, dtype=object)
        missing = ids.isna()
        if missing.any():
            ids = ids.astype(object)
            ids[missing] = [make_team_id(c, t) for c, t in zip(frame.loc[missing, "country"], frame.loc[missing, f"{side}_team"])]
        frame[col] = ids
    return frame


def _match_rows(frame: pd.DataFrame, comp_ids: dict[str, int], status: str, source: str | None = None) -> list[dict]:
    """Vectorised frame -> row dicts of plain Python values (None for missing)."""
    now = utcnow()
    n = len(frame)

    def col(name, default=None):
        return frame[name] if name in frame else pd.Series([default] * n, index=frame.index, dtype=object)

    def text(name):
        s = col(name).astype(object)
        return s.where(s.notna() & (s.astype(str).str.strip() != ""), None)

    keys = {(h, a): search_key(h, a) for h, a in set(zip(frame["home_team"], frame["away_team"]))}
    out = pd.DataFrame({
        "match_id": frame["match_id"].astype(object),
        "source": text("source").fillna(source or "unknown") if source or "source" in frame else "unknown",
        "competition_id": frame["competition"].map(comp_ids).astype(object),
        "season": text("season"),
        "date": pd.to_datetime(frame["date"]).dt.date.astype(object),
        "kickoff_local": text("kickoff"),
        "kickoff_utc": (pd.to_datetime(frame["kickoff_utc"]).dt.to_pydatetime() if "kickoff_utc" in frame
                        else [None] * n),
        "round": text("round"),
        "status": status,
        "home_team_id": frame["home_team_id"].astype(object), "away_team_id": frame["away_team_id"].astype(object),
        "home_team": frame["home_team"].astype(object), "away_team": frame["away_team"].astype(object),
        "search_key": [keys[(h, a)] for h, a in zip(frame["home_team"], frame["away_team"])],
    }, index=frame.index)
    for c in RESULT_COLS:
        out[c] = pd.to_numeric(col(c), errors="coerce").astype("Int64").astype(object) if c in frame else None
    for c in ODDS_COLS:
        out[c] = pd.to_numeric(col(c), errors="coerce").astype(float).astype(object) if c in frame else None
    rows = out.astype(object).where(out.notna(), None).to_dict("records")
    for r in rows:
        r["created_at"] = r["updated_at"] = now
    return rows


_DATA_COLS = ["source", "competition_id", "season", "date", "status", "home_team_id", "away_team_id", "home_team",
              "away_team", "search_key", *RESULT_COLS, *ODDS_COLS]


def _upsert(session: Session, rows: list[dict], *, played: bool) -> int:
    """INSERT ... ON CONFLICT DO UPDATE only when something changed (no churn on unchanged rows).
    Played rows overwrite fixture rows; fixture rows never overwrite a played match."""
    # Plain INSERT for new rows + UPDATE ... WHERE <something changed> for known ones. (A single
    # INSERT ... ON CONFLICT DO UPDATE is ~10x slower in SQLite with foreign keys on.)
    if not rows:
        return 0
    conn = session.connection()
    ids = [r["match_id"] for r in rows]
    existing: set[str] = set()
    for chunk in _chunks(ids, 30000):
        existing.update(conn.execute(select(Match.match_id).where(Match.match_id.in_(chunk))).scalars())
    new = [r for r in rows if r["match_id"] not in existing]
    if new:
        conn.execute(sqlite_insert(Match), new)
    old = [{f"b_{k}": v for k, v in r.items()} for r in rows if r["match_id"] in existing]
    if not old:
        return len(new)
    b = {c: bindparam(f"b_{c}", type_=Match.__table__.c[c].type)
         for c in [*_DATA_COLS, "kickoff_local", "kickoff_utc", "round", "updated_at"]}
    set_ = {c: b[c] for c in _DATA_COLS}
    # a missing kick-off / round never erases a known one
    set_.update({c: func.coalesce(b[c], getattr(Match, c)) for c in ("kickoff_local", "kickoff_utc", "round")})
    if not played:  # odds appear on fixtures over time; results never come from fixture sources
        for c in RESULT_COLS:
            set_.pop(c)
        set_.update({c: func.coalesce(b[c], getattr(Match, c)) for c in ODDS_COLS})
    changed = or_(*(getattr(Match, c).is_not(v) for c, v in set_.items()))
    cond = [Match.match_id == bindparam("b_match_id"), changed]
    if not played:
        cond.append(Match.status != "played")
    stmt = update(Match).where(*cond).values(**set_, updated_at=b["updated_at"])
    if not played:  # unused bind keys are fine for executemany, but keep the dicts minimal
        old = [{k: v for k, v in r.items() if k[2:] in set_ or k in ("b_match_id", "b_updated_at")} for r in old]
    n = len(new)
    for chunk in _chunks(old, 5000):
        n += conn.execute(stmt, chunk).rowcount or 0
    return n


def upsert_matches(engine: Engine, matches: pd.DataFrame, since: date | None = None) -> dict:
    """Played matches (canonical schema, with results). Returns {rows, written}.
    since: only rows dated on/after it (past seasons never change upstream; see incremental_since)."""
    if since is not None:
        matches = matches[pd.to_datetime(matches["date"]) >= pd.Timestamp(since)]
    matches = _with_team_ids(matches.sort_values("date"))
    with session_scope(engine) as s:
        comp = _competition_ids(s, matches)
        _upsert_teams(s, matches)
        written = _upsert(s, _match_rows(matches, comp, "played"), played=True)
    return {"rows": len(matches), "written": written}


def read_fixtures(today: date | None = None, calendar_path: Path | None = None,
                  fd_dir: Path | None = None) -> pd.DataFrame:
    """Upcoming fixtures from the cached files only (no network): the season calendars plus the most
    recent football-data fixtures.csv download, which wins on odds and adds near-term fixtures of
    leagues without a calendar. Every row gets `kickoff` (league-local "HH:MM") and `kickoff_utc`:
    calendar rows from their UTC source, football-data rows from UK time (uk_kickoffs)."""
    from shots.ingest.calendar_ics import TIMEZONES

    today = today or date.today()
    calendar_path = Path(calendar_path or CALENDAR_PATH)
    fd_dir = Path(fd_dir or FD_FIXTURES_DIR)
    frames = []
    if calendar_path.exists():
        cal = pd.read_parquet(calendar_path)
        cal = cal[cal["date"] >= pd.Timestamp(today)].copy()
        cal["kickoff_utc"] = pd.Series(pd.NaT, index=cal.index, dtype="datetime64[us]")
        for country, g in cal.groupby("country"):
            tz = TIMEZONES.get(country)
            ok = g["kickoff"].fillna("").str.fullmatch(r"\d{2}:\d{2}")
            if not tz or not ok.any():
                continue
            local = pd.to_datetime(g.loc[ok, "date"].dt.strftime("%Y-%m-%d") + " " + g.loc[ok, "kickoff"])
            utc = local.dt.tz_localize(tz, ambiguous=True, nonexistent="shift_forward").dt.tz_convert("UTC")
            cal.loc[utc.index, "kickoff_utc"] = utc.dt.tz_localize(None).astype("datetime64[us]")
        frames.append(cal)
    files = sorted(Path(fd_dir).glob("fixtures_*.csv")) if Path(fd_dir).exists() else []
    if files:
        raw = fd.read_raw(files[-1])
        season = fd.current_season_start(today)
        parts = []
        for div, rows in raw[raw["Div"].isin(fd.DIVISIONS)].groupby("Div"):
            canon = fd.to_canonical(rows, div, season)
            times = pd.Series(rows["Time"].to_numpy() if "Time" in rows else [None] * len(rows), index=canon.index)
            canon["kickoff"], canon["kickoff_utc"] = uk_kickoffs(canon["date"], times, fd.DIVISIONS[div][1])
            parts.append(canon)
        if parts:
            fx = pd.concat(parts, ignore_index=True)
            fx = fx[fx["date"] >= pd.Timestamp(today)]
            if frames:  # the calendar row wins (its kick-off was UTC at the source); football-data adds odds
                base = frames[0]
                fxi = fx.drop_duplicates("match_id").set_index("match_id")
                both = base["match_id"].isin(fxi.index)
                for c in ODDS_COLS:
                    base.loc[both, c] = base.loc[both, "match_id"].map(fxi[c])
                no_ko = both & base["kickoff_utc"].isna()  # calendar row without a time: take football-data's
                if no_ko.any():
                    base.loc[no_ko, "kickoff_utc"] = base.loc[no_ko, "match_id"].map(fxi["kickoff_utc"])
                    base.loc[no_ko, "kickoff"] = base.loc[no_ko, "match_id"].map(fxi["kickoff"])
                fx = fx[~fx["match_id"].isin(base["match_id"])]
            frames.append(fx)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates("match_id", keep="first").reset_index(drop=True)


def upsert_fixtures(engine: Engine, fixtures: pd.DataFrame, today: date | None = None) -> dict:
    """Upcoming fixtures -> status 'scheduled' (never touching played matches). Scheduled matches from
    today on that no fixture source lists any more become 'unlisted' (postponed / moved)."""
    today = today or date.today()
    if fixtures.empty:
        return {"rows": 0, "written": 0, "unlisted": 0}
    fixtures = _with_team_ids(fixtures)
    with session_scope(engine) as s:
        comp = _competition_ids(s, fixtures)
        _upsert_teams(s, fixtures)
        written = _upsert(s, _match_rows(fixtures, comp, "scheduled"), played=False)
        res = s.execute(update(Match).where(Match.status == "scheduled", Match.date >= today,
                                            Match.match_id.not_in(list(fixtures["match_id"])))
                        .values(status="unlisted", updated_at=utcnow()))
    return {"rows": len(fixtures), "written": written, "unlisted": res.rowcount or 0}


def _ensure_matches(s: Session, frame: pd.DataFrame, source: str, refresh_meta: bool) -> set[str]:
    """Make sure every match_id in frame exists (stub rows for fixtures we have not loaded). With
    refresh_meta, non-played matches also take this frame's kick-off / round / 'scheduled' status
    (the board is the freshest merged fixture view). Returns the match_ids that could be ensured."""
    frame = frame.drop_duplicates("match_id", keep="first").copy()
    if "country" not in frame or frame["country"].isna().any() or "tier" not in frame:
        known = dict(s.execute(select(Competition.name, Competition.country)).all())
        tiers = dict(s.execute(select(Competition.name, Competition.tier)).all())
        country = frame["competition"].map(lambda c: known.get(c) or (DIVISION_BY_NAME.get(c) or (None, None, None))[1])
        tier = frame["competition"].map(lambda c: tiers.get(c) or (DIVISION_BY_NAME.get(c) or (None, None, None))[2])
        frame["country"] = frame["country"].fillna(country) if "country" in frame else country
        frame["tier"] = frame["tier"].fillna(tier) if "tier" in frame else tier
    unknown = frame["country"].isna()
    if unknown.any():
        log.warning("skipping %d rows from unknown competitions: %s", int(unknown.sum()),
                    ", ".join(sorted(frame.loc[unknown, "competition"].unique())))
        frame = frame[~unknown]
    if frame.empty:
        return set()
    frame = _with_team_ids(frame)
    comp = _competition_ids(s, frame)
    existing = {m for (m,) in s.execute(select(Match.match_id).where(Match.match_id.in_(list(frame["match_id"]))))}
    new = frame[~frame["match_id"].isin(existing)]
    if len(new):
        _upsert_teams(s, new)
        s.execute(sqlite_insert(Match).on_conflict_do_nothing(), _match_rows(new, comp, "scheduled", source))
    if refresh_meta:
        old = frame[frame["match_id"].isin(existing)]
        now = utcnow()
        params = [{"b_id": r.match_id, "b_round": _str(getattr(r, "round", None)),
                   "b_ko": _str(getattr(r, "kickoff", None)), "b_now": now} for r in old.itertuples(index=False)]
        if params:
            new_round = func.coalesce(bindparam("b_round"), Match.round)
            s.connection().execute(
                update(Match).where(Match.match_id == bindparam("b_id"), Match.status != "played",
                                    or_(Match.status != "scheduled", Match.round.is_not(new_round)))
                .values(status="scheduled", round=new_round, updated_at=bindparam("b_now")),
                [{k: v for k, v in p.items() if k != "b_ko"} for p in params])
            # The frame's kick-off zone is unknown (league-local for calendar rows, UK time for
            # football-data rows): it only fills a match that has no kick-off at all, never replaces one.
            fill = [p for p in params if p["b_ko"]]
            if fill:
                s.connection().execute(
                    update(Match).where(Match.match_id == bindparam("b_id"), Match.status != "played",
                                        Match.kickoff_local.is_(None), Match.kickoff_utc.is_(None))
                    .values(kickoff_local=bindparam("b_ko"), updated_at=bindparam("b_now")),
                    [{k: v for k, v in p.items() if k != "b_round"} for p in fill])
    return set(frame["match_id"])


# ----------------------------------------------------------------------------- prediction runs

def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def board_prediction_rows(board: pd.DataFrame) -> tuple[list[dict], dict]:
    """Board frame -> prediction dicts (without run_id) + curve ranges."""
    pcols = sorted((c for c in board.columns if c.startswith("p_") and c[2:].isdigit()), key=lambda c: int(c[2:]))
    hcols = sorted((c for c in board.columns if c.startswith("ph_") and c[3:].isdigit()), key=lambda c: int(c[3:]))
    acols = sorted((c for c in board.columns if c.startswith("pa_") and c[3:].isdigit()), key=lambda c: int(c[3:]))
    ns = [int(c[2:]) for c in pcols]
    tns = [int(c[3:]) for c in hcols]
    if not ns or ns != list(range(ns[0], ns[-1] + 1)):
        raise ValueError("board curve columns p_N must be contiguous")
    if tns and (tns != list(range(tns[0], tns[-1] + 1)) or [int(c[3:]) for c in acols] != tns):
        raise ValueError("board team curve columns ph_N / pa_N must be contiguous and aligned")
    team = bool(tns) and {"home_mean", "away_mean"} <= set(board.columns)
    rows = []
    for r in board.drop_duplicates("match_id", keep="first").to_dict("records"):
        d = dict(match_id=r["match_id"], model=_str(r.get("model")), odds_available=_bool(r.get("odds")),
                 cold_start=_bool(r.get("cold_start")), median=_float(r.get("expected_shots")),
                 lb_70=_int(r.get("lb_70")), lb_80=_int(r.get("lb_80")), lb_90=_int(r.get("lb_90")),
                 league_avg=_float(r.get("league_avg_shots")), above_league=_float(r.get("above_league")),
                 mkt_p_over25=_float(r.get("mkt_p_over25")), total_curve=_curve(r, pcols))
        if team:
            for side in ("home", "away"):
                d[f"{side}_mean"] = _float(r.get(f"{side}_mean"))
                for c in TEAM_SCALARS[1:]:
                    d[f"{side}_{c}"] = _int(r.get(f"{side}_{c}"))
            d["home_curve"], d["away_curve"] = _curve(r, hcols), _curve(r, acols)
        rows.append(d)
    ranges = dict(curve_n_min=ns[0] if ns else None, curve_n_max=ns[-1] if ns else None,
                  team_n_min=tns[0] if team else None, team_n_max=tns[-1] if team else None)
    return rows, ranges


def _lock_moments(s: Session, match_ids: list[str], frame: pd.DataFrame | None = None) -> dict[str, datetime]:
    """{match_id: lock_moment} from the DB (date, kickoff_utc, country), with the frame's `kickoff`
    as the fallback hint where the DB has no kickoff_utc."""
    hint = {}
    if frame is not None and "kickoff" in frame:
        hint = {m: _str(k) for m, k in zip(frame["match_id"], frame["kickoff"])}
    out = {}
    for chunk in _chunks(list(match_ids), 30000):
        q = (select(Match.match_id, Match.date, Match.kickoff_utc, Competition.country)
             .join(Competition, Competition.id == Match.competition_id).where(Match.match_id.in_(chunk)))
        for mid, day, ko_utc, country in s.execute(q):
            out[mid] = lock_moment(day, ko_utc, hint.get(mid), country)
    return out


def db_now(s: Session) -> datetime:
    """The database clock (naive UTC). Prediction insert times come from it: the DB rejects a
    created_at more than 5 minutes away from its own clock (migration 0003)."""
    return datetime.fromisoformat(s.scalar(text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")))


def _insert_predictions(s: Session, run_id: int, rows: list[dict], allowed: set[str], built_at: datetime,
                        frame: pd.DataFrame | None = None) -> int:
    """Insert the rows whose match exists. pre_match = the run was built AND the row is inserted
    (DB clock) before the match locks, and the match has no result yet."""
    now = db_now(s)
    rows = [r for r in rows if r["match_id"] in allowed]
    ids = [r["match_id"] for r in rows]
    locks = _lock_moments(s, ids, frame)
    played = set()
    for chunk in _chunks(ids, 30000):
        played |= {m for (m,) in s.execute(select(Match.match_id).where(Match.match_id.in_(chunk),
                                                                       Match.status == "played"))}
    rows = [dict(r, run_id=run_id, created_at=now,
                 pre_match=bool(built_at < locks[r["match_id"]] and now < locks[r["match_id"]]
                                and r["match_id"] not in played)) for r in rows]
    if rows:
        s.execute(sqlite_insert(Prediction), rows)
    return len(rows)


def import_board(engine: Engine, board: pd.DataFrame, meta: dict) -> dict:
    """Record a freshly built board as a 'board' run. Idempotent on meta.built_at; a rebuild whose
    predictions are identical to the latest board run only bumps that run's confirmed_at and meta.
    -> {status: inserted | exists | unchanged, run_id, predictions}."""
    built_local = datetime.fromisoformat(meta["built_at"])
    built_utc = local_to_utc(built_local)
    key = f"board:{meta['built_at']}"
    rows, ranges = board_prediction_rows(board)
    content = _hash({"rows": sorted(rows, key=lambda r: r["match_id"]), "ranges": ranges,
                     "results_up_to": meta.get("results_up_to"), "window_days": meta.get("window_days")})
    with session_scope(engine) as s:
        found = s.scalar(select(PredictionRun).where(PredictionRun.source_key == key))
        if found:
            return {"status": "exists", "run_id": found.id, "predictions": found.n_predictions}
        latest = s.scalar(select(PredictionRun).where(PredictionRun.kind == "board")
                          .order_by(PredictionRun.built_at.desc(), PredictionRun.id.desc()).limit(1))
        if latest is not None and latest.content_hash == content and built_utc >= latest.built_at:
            latest.confirmed_at = built_utc
            latest.meta = dict(meta)
            return {"status": "unchanged", "run_id": latest.id, "predictions": latest.n_predictions}
        ensured = _ensure_matches(s, board, "board", refresh_meta=True)
        run = PredictionRun(
            kind="board", source_key=key, status="complete", built_at=built_utc, confirmed_at=built_utc,
            results_up_to=_date(meta.get("results_up_to")), window_from=_date(meta.get("window_from")),
            window_days=meta.get("window_days"), models=list(meta.get("models") or sorted(board["model"].dropna().unique())),
            params={"window_days": meta.get("window_days"), "window_from": meta.get("window_from")},
            meta=dict(meta), content_hash=content, **ranges)
        s.add(run)
        s.flush()
        run.n_predictions = _insert_predictions(s, run.id, rows, ensured, built_utc, board)
        return {"status": "inserted", "run_id": run.id, "predictions": run.n_predictions}


def import_board_file(engine: Engine, path: Path | None = None) -> dict:
    from shots.predict import board as boardmod

    path = Path(path or boardmod.board_path())
    mpath = boardmod.meta_path(path)
    if not path.exists() or not mpath.exists():
        return {"status": "missing", "run_id": None, "predictions": 0}
    meta = json.loads(mpath.read_text(encoding="utf-8"))
    return import_board(engine, pd.read_parquet(path), meta)


def import_picks_file(engine: Engine, path: Path) -> dict:
    """One scan CSV (reports/picks/<stamp>_at_least_<N>.csv) -> a 'picks' run. Idempotent on file name."""
    from shots.predict.track import parse_filename

    path = Path(path)
    info = parse_filename(path)
    if info is None:
        return {"status": "skipped", "run_id": None, "predictions": 0}
    key = f"picks:{path.name}"
    with session_scope(engine) as s:
        found = s.scalar(select(PredictionRun.id).where(PredictionRun.source_key == key))
        if found:
            return {"status": "exists", "run_id": found, "predictions": None}
    df = pd.read_csv(path, dtype={"kickoff": str})
    n = info["n"]
    pcol = f"p_at_least_{n}"
    if df.empty or pcol not in df or "match_id" not in df:
        return {"status": "skipped", "run_id": None, "predictions": 0}
    df["date"] = pd.to_datetime(df["date"])
    scan_ts = info["scan_ts"].to_pydatetime()
    min_prob = _float(df["min_prob"].iloc[0]) if "min_prob" in df else None
    rows = []
    for r in df.drop_duplicates("match_id", keep="first").to_dict("records"):
        rows.append(dict(match_id=r["match_id"], model=_str(r.get("model")), odds_available=_bool(r.get("odds")),
                         cold_start=_bool(r.get("cold_start")), median=_float(r.get("expected_shots")),
                         lb_80=_int(r.get("lb_80")), league_avg=_float(r.get("league_avg_shots")),
                         above_league=_float(r.get("above_league")), mkt_p_over25=_float(r.get("mkt_p_over25")),
                         points={str(n): _float(r[pcol])}, selected=_bool(r.get("selected"))))
    with session_scope(engine) as s:
        ensured = _ensure_matches(s, df, "picks", refresh_meta=False)
        run = PredictionRun(
            kind="picks", source_key=key, status="complete", built_at=local_to_utc(scan_ts),
            models=sorted(df["model"].dropna().unique().tolist()) if "model" in df else [],
            params={"n": n, "min_prob": min_prob, "source": info["source"], "scan_ts": scan_ts.isoformat(),
                    "file": path.name},
            meta={})
        s.add(run)
        s.flush()
        run.n_predictions = _insert_predictions(s, run.id, rows, ensured, run.built_at, df)
        return {"status": "inserted", "run_id": run.id, "predictions": run.n_predictions}


def import_picks_dir(engine: Engine, picks_dir: Path | None = None) -> dict:
    out = {"files": 0, "inserted": 0, "predictions": 0}
    for path in sorted(Path(picks_dir or PICKS_DIR).glob("*.csv")):
        res = import_picks_file(engine, path)
        out["files"] += 1
        if res["status"] == "inserted":
            out["inserted"] += 1
            out["predictions"] += res["predictions"]
    return out


def import_backfill(engine: Engine, back: pd.DataFrame, model: str | None = None) -> dict:
    """Out-of-sample season replay (match_id, date, competition, N, p[, actual]) -> a 'backfill' run.
    Idempotent on content; a rebuilt backfill becomes a new run (the repository reads the latest).
    Rows whose match is not in the DB are skipped (outcomes come from match results)."""
    from shots.predict.track import DEFAULT_MODEL

    if back.empty:
        return {"status": "skipped", "run_id": None, "predictions": 0}
    back = back.sort_values(["match_id", "N"])
    points = {m: {str(int(n)): round(float(p), 6) for n, p in zip(g["N"], g["p"])} for m, g in back.groupby("match_id")}
    content = _hash(points)
    key = f"backfill:{content[:20]}"
    start = pd.to_datetime(back["date"]).min()
    label = model or f"{DEFAULT_MODEL}_fit_before_{start:%Y-%m-%d}"
    with session_scope(engine) as s:
        found = s.scalar(select(PredictionRun.id).where(PredictionRun.source_key == key))
        if found:
            return {"status": "exists", "run_id": found, "predictions": None}
        known = {m for (m,) in s.execute(select(Match.match_id).where(Match.match_id.in_(list(points))))}
        missing = len(points) - len(known)
        if missing:
            log.warning("backfill: %d of %d matches are not in the DB; skipped", missing, len(points))
        run = PredictionRun(kind="backfill", source_key=key, status="complete", built_at=utcnow(), models=[label],
                            params={"n_values": sorted(int(x) for x in back["N"].unique()),
                                    "date_from": str(start.date()), "date_to": str(pd.to_datetime(back["date"]).max().date())},
                            meta={}, content_hash=content)
        s.add(run)
        s.flush()
        rows = [dict(match_id=m, model=label, points=pts) for m, pts in points.items()]
        run.n_predictions = _insert_predictions(s, run.id, rows, known, run.built_at)
        return {"status": "inserted", "run_id": run.id, "predictions": run.n_predictions}


def delete_run(engine: Engine, run_id: int) -> int:
    """Remove a run and its predictions, allowed only while none of its matches has kicked off
    (the DB trigger raises otherwise). For purging a broken import before anything is locked."""
    with session_scope(engine) as s:
        n = s.query(Prediction).filter(Prediction.run_id == run_id).delete(synchronize_session=False)
        s.query(PredictionRun).filter(PredictionRun.id == run_id).delete(synchronize_session=False)
    return n


# ----------------------------------------------------------------------------- everything

def incremental_since(engine: Engine, days: int = 400) -> date | None:
    """Start date for an incremental results upsert: `days` before the latest played match in the DB
    (covers the current and previous season, which football-data may still correct). None = empty DB."""
    with session_scope(engine) as s:
        last = s.scalar(select(func.max(Match.date)).where(Match.status == "played"))
    return None if last is None else last - pd.Timedelta(days=days).to_pytimedelta()


def sync_results(engine: Engine, today: date | None = None, full: bool = False) -> dict:
    """Results (matches.parquet) + upcoming fixtures (calendars + latest fixtures.csv download)."""
    out: dict = {}
    if Path(MATCHES_PATH).exists():
        since = None if full else incremental_since(engine)
        out["matches"] = upsert_matches(engine, pd.read_parquet(MATCHES_PATH), since)
    out["fixtures"] = upsert_fixtures(engine, read_fixtures(today), today)
    return out


def sync_predictions(engine: Engine, backfill: bool = True) -> dict:
    """Picks CSVs (one run per scan file) + the season backfill, then outcome counts. Outcomes are
    views over current results, so 'settling' needs no writes: this only reports them."""
    out: dict = {"picks": import_picks_dir(engine)}
    if backfill and Path(BACKFILL_PATH).exists():
        out["backfill"] = import_backfill(engine, pd.read_parquet(BACKFILL_PATH))
    with session_scope(engine) as s:
        out["official_picks"] = dict(s.execute(text("SELECT status, count(*) FROM v_official_picks GROUP BY status")).all())
    return out


def sync_all(target=None, today: date | None = None, full: bool = False) -> dict:
    """Migrate, then load every cached file into the DB: matches, fixtures, board, picks, backfill.
    Safe to run any number of times. full=False upserts only recent results once the DB has some.
    Returns per-step counts."""
    engine = make_engine(target)
    try:
        migrate.upgrade(engine)
        out = sync_results(engine, today, full)
        out["board"] = import_board_file(engine)
        out.update(sync_predictions(engine))
        return out
    finally:
        if not isinstance(target, Engine):
            engine.dispose()
