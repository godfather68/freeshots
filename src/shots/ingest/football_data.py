"""football-data.co.uk adapter: season CSVs -> canonical match schema.

Usage:
    python -m shots.ingest.football_data              # fetch missing files + build parquet
    python -m shots.ingest.football_data --refresh-current
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from shots.ingest.schema import CANONICAL_COLUMNS, INT_COLUMNS, make_match_id, make_team_id, normalize_team

log = logging.getLogger(__name__)

SOURCE = "football-data"
BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
USER_AGENT = "shots-research/0.1 (personal research project)"
REQUEST_DELAY_S = 1.5
NOT_MODIFIED_DELAY_S = 0.2
# football-data.co.uk is sometimes slow or drops connections (seen 2026-10-19: SSL EOF, 30 s+ per file).
RETRY_DELAYS_S = (3, 10, 30)          # waits between attempts; len + 1 attempts in total
HTTP_TIMEOUT = (15, 90)               # (connect, read) seconds
_TRANSIENT = (requests.ConnectionError, requests.Timeout)


def http_get(url: str, session: requests.Session | None = None, headers: dict | None = None) -> requests.Response:
    """GET with retries on dropped connections, timeouts, 429 and 5xx. Other HTTP errors (404...)
    are returned to the caller unchanged; the last transient error is re-raised."""
    getter = session.get if session is not None else requests.get
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt, delay in enumerate((*RETRY_DELAYS_S, None), start=1):
        try:
            resp = getter(url, timeout=HTTP_TIMEOUT, headers=hdrs)
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            err: Exception = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
        except _TRANSIENT as exc:
            err = exc
        if delay is None:
            raise err
        log.warning("%s: attempt %d failed (%s); retrying in %ss", url, attempt, type(err).__name__, delay)
        time.sleep(delay)
    raise AssertionError("unreachable")


# Stop calling the server for the rest of a fetch() after this many files fail in a row.
MAX_CONSECUTIVE_FAILURES = 3

# Files that could not be refreshed in the last fetch() (cached copies were used instead).
LAST_FETCH_FAILURES: list[str] = []

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / SOURCE
OUT_PATH = ROOT / "data" / "processed" / "matches.parquet"

# div -> (competition, country, tier)
DIVISIONS: dict[str, tuple[str, str, int]] = {
    "E0": ("Premier League", "England", 1),
    "E1": ("Championship", "England", 2),
    "E2": ("League One", "England", 3),
    "E3": ("League Two", "England", 4),
    "EC": ("National League", "England", 5),
    "SC0": ("Premiership", "Scotland", 1),
    "SC1": ("Championship", "Scotland", 2),
    "SC2": ("League One", "Scotland", 3),
    "SC3": ("League Two", "Scotland", 4),
    "D1": ("Bundesliga", "Germany", 1),
    "D2": ("2. Bundesliga", "Germany", 2),
    "I1": ("Serie A", "Italy", 1),
    "I2": ("Serie B", "Italy", 2),
    "SP1": ("La Liga", "Spain", 1),
    "SP2": ("Segunda Division", "Spain", 2),
    "F1": ("Ligue 1", "France", 1),
    "F2": ("Ligue 2", "France", 2),
    "N1": ("Eredivisie", "Netherlands", 1),
    "B1": ("Pro League", "Belgium", 1),
    "P1": ("Primeira Liga", "Portugal", 1),
    "T1": ("Super Lig", "Turkey", 1),
    "G1": ("Super League", "Greece", 1),
}

# Pre-match odds, first column present wins (names changed over the years).
ODDS_COLUMNS = {
    "odds_home": ["AvgH", "BbAvH", "B365H"],
    "odds_draw": ["AvgD", "BbAvD", "B365D"],
    "odds_away": ["AvgA", "BbAvA", "B365A"],
    "odds_over25": ["Avg>2.5", "BbAv>2.5", "B365>2.5"],
    "odds_under25": ["Avg<2.5", "BbAv<2.5", "B365<2.5"],
}

# Closing-odds columns in football-data headers: a "C" right after the bookmaker prefix
# (B365CH, PSCH, MaxCH, AvgCH, BbAvC..., BFECH, PC>2.5, MaxC<2.5, AvgCAHH, ...).
_CLOSING_RE = re.compile(
    r"^(?:B365|BFD|BFE|BMGM|BV|BW|CL|LB|PS|P|Max|Avg|BbMx|BbAv|Bb|GB|IW|SB|SJ|SO|VC|WH|Sy|1XB)C(?:[HDA]|>|<|AH|\d|$)"
    r"|^AHC"  # closing Asian handicap line (AHCh); the pre-match line is AHh
)


def is_closing_odds_column(name: str) -> bool:
    return bool(_CLOSING_RE.match(str(name)))


def _check_no_closing_odds() -> None:
    bad = [c for cands in ODDS_COLUMNS.values() for c in cands if is_closing_odds_column(c)]
    if bad:
        raise AssertionError(f"closing-odds columns configured as pre-match odds: {bad}")


_check_no_closing_odds()  # import-time guard (CLAUDE.md: pre-match odds only)

RAW_RENAME = {
    "HomeTeam": "home_team", "AwayTeam": "away_team",
    "HS": "home_shots", "AS": "away_shots", "HST": "home_sot", "AST": "away_sot",
    "FTHG": "home_goals", "FTAG": "away_goals",
}


def season_code(start_year: int) -> str:
    """2023 -> '2324'."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def current_season_start(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def fetch(first_season: int = 2010, last_season: int | None = None, divisions=None,
          refresh_current: bool = False) -> list[Path]:
    """Download missing season files. Raw files are never modified once written,
    except the in-progress current season when refresh_current=True."""
    last_season = last_season or current_season_start()
    divisions = divisions or list(DIVISIONS)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    paths = []
    LAST_FETCH_FAILURES.clear()
    consecutive = 0
    for start in range(first_season, last_season + 1):
        code = season_code(start)
        for div in divisions:
            path = RAW_DIR / code / f"{div}.csv"
            is_current = start == current_season_start()
            if path.exists() and not (refresh_current and is_current):
                paths.append(path)
                continue
            url = BASE_URL.format(season=code, div=div)
            # Conditional GET: an unchanged file costs a 304 and no re-download.
            etag_path = path.with_suffix(".etag")
            headers = {"If-None-Match": etag_path.read_text()} if path.exists() and etag_path.exists() else {}
            try:
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    raise requests.ConnectionError("server unreachable this run (circuit breaker open)")
                resp = http_get(url, session, headers)
                consecutive = 0
            except (requests.RequestException, OSError) as exc:
                consecutive += 1
                # Keep going with what we have: a stale current-season file is better than no run.
                LAST_FETCH_FAILURES.append(url)
                if path.exists():
                    log.warning("could not refresh %s (%s); using the cached copy", url, exc)
                    paths.append(path)
                else:
                    log.warning("could not download %s (%s); skipped", url, exc)
                continue
            if resp.status_code == 304:
                time.sleep(NOT_MODIFIED_DELAY_S)
                paths.append(path)
                continue
            time.sleep(REQUEST_DELAY_S)
            if resp.status_code == 404:
                log.info("not available: %s", url)
                continue
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
            if resp.headers.get("ETag"):
                etag_path.write_text(resp.headers["ETag"])
            log.info("downloaded %s (%d bytes)", url, len(resp.content))
            paths.append(path)
    return paths


def read_raw(path: Path) -> pd.DataFrame:
    """Read one season file; tolerant of BOMs, latin-1 bytes and ragged trailing columns."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    df = pd.read_csv(io.StringIO(text), on_bad_lines="skip", dtype=str)
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    return df.dropna(how="all")


def to_canonical(raw: pd.DataFrame, div: str, season_start: int) -> pd.DataFrame:
    competition, country, tier = DIVISIONS[div]
    df = raw.rename(columns=RAW_RENAME)
    df = df.dropna(subset=["home_team", "away_team", "Date"])

    out = pd.DataFrame(index=df.index)
    out["source"] = SOURCE
    out["competition"] = f"{country} {competition}"
    out["country"] = country
    out["tier"] = tier
    out["season"] = f"{season_start}/{season_start + 1}"
    out["date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    out["home_team"] = df["home_team"].map(lambda n: normalize_team(SOURCE, n))
    out["away_team"] = df["away_team"].map(lambda n: normalize_team(SOURCE, n))
    out["home_team_id"] = out["home_team"].map(lambda n: make_team_id(country, n))
    out["away_team_id"] = out["away_team"].map(lambda n: make_team_id(country, n))
    for col in ["home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals"]:
        out[col] = pd.to_numeric(df[col], errors="coerce") if col in df else pd.NA
    for col, candidates in ODDS_COLUMNS.items():
        present = next((c for c in candidates if c in df), None)
        out[col] = pd.to_numeric(df[present], errors="coerce") if present else float("nan")

    out = out.dropna(subset=["date"])
    out["match_id"] = [
        make_match_id(c, d, h, a)
        for c, d, h, a in zip(out["competition"], out["date"], out["home_team"], out["away_team"])
    ]
    for col in INT_COLUMNS:
        out[col] = out[col].astype("Int64")
    return out[CANONICAL_COLUMNS].reset_index(drop=True)


def build(first_season: int = 2010, refresh_current: bool = False, out_path: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch + convert everything. Returns (matches with shots, all parsed rows).

    With refresh_current and an existing output, only the current season is re-parsed; past
    seasons are reused from the existing parquet (their raw files never change). In that case
    "all parsed rows" holds only matches with shots for past seasons.
    """
    out_path = out_path or OUT_PATH
    paths = fetch(first_season=first_season, refresh_current=refresh_current)
    frames = []
    if refresh_current and out_path.exists():
        current = season_code(current_season_start())
        previous = pd.read_parquet(out_path)
        frames.append(previous[previous["season"] != f"{current_season_start()}/{current_season_start() + 1}"])
        paths = [p for p in paths if p.parent.name == current]
    for path in paths:
        code, div = path.parent.name, path.stem
        season_start = 2000 + int(code[:2])
        frames.append(to_canonical(read_raw(path), div, season_start))
    all_rows = pd.concat(frames, ignore_index=True)
    all_rows = all_rows.drop_duplicates(subset="match_id", keep="last")
    # Matches without shot data are dropped, never imputed (CLAUDE.md).
    has_shots = all_rows[["home_shots", "away_shots"]].notna().all(axis=1)
    matches = all_rows[has_shots].sort_values(["date", "competition"]).reset_index(drop=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    matches.to_parquet(out_path, index=False)
    return matches, all_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--first-season", type=int, default=2010)
    parser.add_argument("--refresh-current", action="store_true", help="re-download the in-progress season")
    parser.add_argument("--output", type=Path, default=OUT_PATH, help="matches parquet path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    matches, all_rows = build(args.first_season, args.refresh_current, args.output)
    log.info("parsed %d rows, kept %d with shots -> %s", len(all_rows), len(matches), args.output)


if __name__ == "__main__":
    main()
