"""StatsBomb open-data adapter (validation only): event files -> canonical match schema.

Shot definitions used here (StatsBomb event type "Shot", penalty shoot-out period 5 excluded):
  shots : every Shot event, all outcomes (Blocked, Goal, Off T, Post, Saved, Saved Off Target,
          Saved to Post, Wayward). Own goals are not Shot events and are not counted.
  SoT   : outcome in {Goal, Saved, Saved To Post}. "Post" (hit the woodwork, no save) and
          "Saved Off Target" (keeper touched a shot that was going wide) are NOT on target.
Goals come from the match score (includes own goals), not from shot outcomes.

Usage:
    python -m shots.ingest.statsbomb                  # sample fetch (<= 40 matches/comp-season) + build
    python -m shots.ingest.statsbomb --max-matches 10
    python -m shots.ingest.statsbomb --compare data/processed/matches.parquet
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import pandas as pd
import requests

from shots.ingest import football_data
from shots.ingest.schema import CANONICAL_COLUMNS, INT_COLUMNS, make_match_id, make_team_id, normalize_team

log = logging.getLogger(__name__)

SOURCE = "statsbomb"
BASE_URL = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
USER_AGENT = "shots-research/0.1 (personal research project)"
REQUEST_DELAY_S = 1.0
MAX_MATCHES_PER_SEASON = 40

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / SOURCE
OUT_PATH = ROOT / "data" / "processed" / "statsbomb_matches.parquet"

# (competition_id, season_id) pairs overlapping football-data coverage (2010/11+).
# StatsBomb's La Liga / Ligue 1 seasons are partial (mostly Barcelona / PSG matches).
DEFAULT_SAMPLE: list[tuple[int, int]] = [
    (2, 27),    # England Premier League 2015/16
    (11, 27),   # Spain La Liga 2015/16
    (9, 27),    # Germany Bundesliga 2015/16
    (12, 27),   # Italy Serie A 2015/16
    (7, 27),    # France Ligue 1 2015/16
    (9, 281),   # Germany Bundesliga 2023/24
    (11, 4),    # Spain La Liga 2018/19
    (7, 235),   # France Ligue 1 2022/23
]

# StatsBomb (country, competition_name) -> (football-data competition string, country, tier)
COMPETITION_MAP: dict[tuple[str, str], tuple[str, str, int]] = {
    ("England", "Premier League"): ("England Premier League", "England", 1),
    ("Spain", "La Liga"): ("Spain La Liga", "Spain", 1),
    ("Germany", "1. Bundesliga"): ("Germany Bundesliga", "Germany", 1),
    ("Italy", "Serie A"): ("Italy Serie A", "Italy", 1),
    ("France", "Ligue 1"): ("France Ligue 1", "France", 1),
}

SHOT_TYPE = "Shot"
SHOT_PERIOD_MAX = 4  # period 5 = penalty shoot-out
ON_TARGET_OUTCOMES = {"Goal", "Saved", "Saved To Post", "Saved to Post"}


def _get(session: requests.Session, rel: str, path: Path) -> Path:
    """Download one repo file into the raw cache unless already present."""
    if path.exists():
        return path
    resp = session.get(f"{BASE_URL}/{rel}", timeout=60)
    time.sleep(REQUEST_DELAY_S)
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    log.info("downloaded %s (%d bytes)", rel, len(resp.content))
    return path


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def pick_sample(matches: list[dict], max_matches: int) -> list[dict]:
    """Deterministic, date-spread subset of at most max_matches available matches."""
    ms = sorted((m for m in matches if m.get("match_status", "available") == "available"),
                key=lambda m: (m["match_date"], m["match_id"]))
    if len(ms) <= max_matches:
        return ms
    step = len(ms) / max_matches
    return [ms[int(i * step)] for i in range(max_matches)]


def fetch(sample=None, max_matches: int = MAX_MATCHES_PER_SEASON) -> None:
    """Download competitions.json, match lists and a sample of event files. Idempotent."""
    sample = sample or DEFAULT_SAMPLE
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    _get(session, "competitions.json", RAW_DIR / "competitions.json")
    for comp_id, season_id in sample:
        mpath = _get(session, f"matches/{comp_id}/{season_id}.json",
                     RAW_DIR / "matches" / f"{comp_id}_{season_id}.json")
        for m in pick_sample(_load(mpath), max_matches):
            _get(session, f"events/{m['match_id']}.json", RAW_DIR / "events" / f"{m['match_id']}.json")


def fix_mojibake(s: str) -> str:
    """Some StatsBomb files carry UTF-8 text decoded as latin-1; repair when possible."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def count_shots(events: list[dict]) -> dict[str, dict[str, int]]:
    """{team_name: {"shots": n, "sot": n}} from an event list (shoot-out excluded)."""
    counts: dict[str, dict[str, int]] = {}
    for ev in events:
        if ev.get("type", {}).get("name") != SHOT_TYPE or ev.get("period", 1) > SHOT_PERIOD_MAX:
            continue
        c = counts.setdefault(ev["team"]["name"], {"shots": 0, "sot": 0})
        c["shots"] += 1
        if ev.get("shot", {}).get("outcome", {}).get("name") in ON_TARGET_OUTCOMES:
            c["sot"] += 1
    return counts


def match_row(match: dict, events: list[dict]) -> dict | None:
    """One canonical row, or None for competitions we do not map."""
    comp = match["competition"]
    key = (comp["country_name"], comp["competition_name"])
    if key not in COMPETITION_MAP:
        return None
    competition, country, tier = COMPETITION_MAP[key]
    home = match["home_team"]["home_team_name"]
    away = match["away_team"]["away_team_name"]
    counts = count_shots(events)
    h = counts.get(home, {"shots": 0, "sot": 0})
    a = counts.get(away, {"shots": 0, "sot": 0})
    start = int(match["season"]["season_name"][:4])
    date = pd.Timestamp(match["match_date"])
    home_c = normalize_team(SOURCE, fix_mojibake(home))
    away_c = normalize_team(SOURCE, fix_mojibake(away))
    return {
        "match_id": make_match_id(competition, date, home_c, away_c),
        "source": SOURCE, "competition": competition, "country": country, "tier": tier,
        "season": f"{start}/{start + 1}", "date": date,
        "home_team": home_c, "away_team": away_c,
        "home_team_id": make_team_id(country, home_c), "away_team_id": make_team_id(country, away_c),
        "home_shots": h["shots"], "away_shots": a["shots"], "home_sot": h["sot"], "away_sot": a["sot"],
        "home_goals": match.get("home_score"), "away_goals": match.get("away_score"),
        "odds_home": float("nan"), "odds_draw": float("nan"), "odds_away": float("nan"),
        "odds_over25": float("nan"), "odds_under25": float("nan"),
    }


def to_canonical(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    for col in INT_COLUMNS:
        df[col] = df[col].astype("Int64")
    return df.reset_index(drop=True)


def build(sample=None, max_matches: int = MAX_MATCHES_PER_SEASON) -> pd.DataFrame:
    sample = sample or DEFAULT_SAMPLE
    fetch(sample, max_matches)
    rows = []
    for comp_id, season_id in sample:
        matches = _load(RAW_DIR / "matches" / f"{comp_id}_{season_id}.json")
        for m in pick_sample(matches, max_matches):
            row = match_row(m, _load(RAW_DIR / "events" / f"{m['match_id']}.json"))
            if row:
                rows.append(row)
    df = to_canonical(rows).drop_duplicates(subset="match_id", keep="last")
    df = df.sort_values(["date", "competition"]).reset_index(drop=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    return df


def football_data_teams(raw_dir: Path | None = None) -> dict[str, set[str]]:
    """{competition: set of football-data team names} from the cached raw season CSVs (read-only)."""
    raw_dir = raw_dir or football_data.RAW_DIR
    names: dict[str, set[str]] = {}
    for path in sorted(raw_dir.glob("*/*.csv")):
        if path.stem not in football_data.DIVISIONS:
            continue
        competition, country, _ = football_data.DIVISIONS[path.stem]
        df = football_data.read_raw(path)
        if not {"HomeTeam", "AwayTeam"} <= set(df.columns):
            continue
        teams = pd.concat([df["HomeTeam"], df["AwayTeam"]]).dropna()
        names.setdefault(f"{country} {competition}", set()).update(
            football_data.normalize_team(football_data.SOURCE, t) for t in teams)
    return names


def unmapped_teams(df: pd.DataFrame, reference: dict[str, set[str]] | None = None) -> pd.DataFrame:
    """StatsBomb team names (after alias normalization) that are not football-data names
    for the same competition. These need a row in team_aliases.csv (or are genuinely absent
    from football-data, e.g. a season not downloaded)."""
    reference = reference if reference is not None else football_data_teams()
    names = pd.concat([
        df[["competition", "home_team"]].rename(columns={"home_team": "team"}),
        df[["competition", "away_team"]].rename(columns={"away_team": "team"}),
    ]).drop_duplicates()
    mask = [t not in reference.get(c, set()) for c, t in zip(names["competition"], names["team"])]
    return names[mask].sort_values(["competition", "team"]).reset_index(drop=True)


def compare_with(matches_path: Path | str, statsbomb_path: Path | str = OUT_PATH) -> dict:
    """Join StatsBomb sample to football-data on (competition, date, home, away); report agreement.

    Differences are statsbomb minus football-data, per team-match, then per match total."""
    sb = pd.read_parquet(statsbomb_path)
    fd = pd.read_parquet(matches_path)
    if "source" in fd:
        fd = fd[fd["source"] == football_data.SOURCE]
    keys = ["competition", "date", "home_team", "away_team"]
    cols = keys + ["home_shots", "away_shots", "home_sot", "away_sot"]
    j = sb[cols].merge(fd[cols], on=keys, suffixes=("_sb", "_fd"))
    res: dict = {"statsbomb_rows": len(sb), "joined": len(j)}
    if j.empty:
        return res
    for stat in ["shots", "sot"]:
        sbv = pd.concat([j[f"home_{stat}_sb"], j[f"away_{stat}_sb"]]).astype(float)
        fdv = pd.concat([j[f"home_{stat}_fd"], j[f"away_{stat}_fd"]]).astype(float)
        d = (sbv - fdv).dropna()
        tot = ((j[f"home_{stat}_sb"] + j[f"away_{stat}_sb"])
               - (j[f"home_{stat}_fd"] + j[f"away_{stat}_fd"])).astype(float).dropna()
        res[stat] = {
            "team_rows": len(d), "exact_pct": float((d == 0).mean() * 100),
            "mae": float(d.abs().mean()), "bias_sb_minus_fd": float(d.mean()),
            "match_total_exact_pct": float((tot == 0).mean() * 100),
            "match_total_mae": float(tot.abs().mean()),
        }
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-matches", type=int, default=MAX_MATCHES_PER_SEASON)
    parser.add_argument("--compare", metavar="MATCHES_PARQUET", help="compare built output with football-data parquet")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.compare:
        print(json.dumps(compare_with(args.compare), indent=2))
        return
    df = build(max_matches=args.max_matches)
    log.info("built %d matches -> %s", len(df), OUT_PATH)
    log.info("unmapped team names:\n%s", unmapped_teams(df).to_string())


if __name__ == "__main__":
    main()
