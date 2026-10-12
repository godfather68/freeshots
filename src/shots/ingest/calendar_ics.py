"""Season calendars (.ics, matches.app export) -> canonical upcoming fixtures.

Usage:
    python -m shots.ingest.calendar_ics                 # parse calendars/ -> data/processed/fixtures.parquet + CSV
    python -m shots.ingest.calendar_ics --suggest       # propose aliases for unmapped team names

Calendar team names differ from football-data's ("1. FC Heidenheim" vs "Heidenheim"). Names are
mapped through team_aliases.csv (source "calendar"); unmapped names are reported, never guessed
silently. Fixtures carry no shots or odds; odds are attached later when football-data publishes them.
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from shots.ingest import football_data as fd
from shots.ingest.schema import CANONICAL_COLUMNS, INT_COLUMNS, make_match_id, make_team_id, normalize_team

log = logging.getLogger(__name__)

SOURCE = "calendar"
CAL_DIR = fd.ROOT / "calendars"
OUT_PATH = fd.ROOT / "data" / "processed" / "fixtures.parquet"
CSV_PATH = fd.ROOT / "data" / "processed" / "fixtures.csv"

# calendar file prefix -> football-data division
FILE_DIVISIONS = {
    "premier-league-gb-eng": "E0",
    "championship-gb-eng": "E1",
    "premiership-gb-sct": "SC0",
    "bundesliga-de": "D1",
    "2-bundesliga-de": "D2",
    "serie-a-it": "I1",
    "serie-b-it": "I2",
    "la-liga-es": "SP1",
    "segunda-division-es": "SP2",
    "jupiler-pro-league-be": "B1",
    "super-lig-tr": "T1",
}

# Match dates are local dates in football-data, so kick-off times are converted before taking the date.
TIMEZONES = {
    "England": "Europe/London", "Scotland": "Europe/London", "Germany": "Europe/Berlin",
    "Italy": "Europe/Rome", "Spain": "Europe/Madrid", "France": "Europe/Paris",
    "Netherlands": "Europe/Amsterdam", "Belgium": "Europe/Brussels", "Portugal": "Europe/Lisbon",
    "Turkey": "Europe/Istanbul", "Greece": "Europe/Athens",
}


def parse_ics(path: Path) -> list[dict]:
    """Minimal RFC 5545 reader: unfolds continuation lines, returns one dict per VEVENT."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold
    events, current = [], None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key.split(";", 1)[0]] = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ")
    return events


def division_for(path: Path) -> str | None:
    prefix = path.stem.rsplit("_", 1)[0]
    return FILE_DIVISIONS.get(prefix)


def read_calendars(cal_dir: Path = CAL_DIR) -> pd.DataFrame:
    """Raw rows (calendar names, kick-off UTC) for every supported calendar file."""
    rows = []
    for path in sorted(cal_dir.glob("*.ics")):
        div = division_for(path)
        if div is None:
            log.warning("no division mapping for %s; skipped", path.name)
            continue
        for ev in parse_ics(path):
            home, sep, away = ev.get("SUMMARY", "").partition(" - ")
            if not sep:
                log.warning("unparseable SUMMARY in %s: %r", path.name, ev.get("SUMMARY"))
                continue
            desc = ev.get("DESCRIPTION", "")
            rows.append({
                "div": div, "uid": ev.get("UID"), "home_raw": home.strip(), "away_raw": away.strip(),
                "kickoff_utc": pd.to_datetime(ev["DTSTART"], format="%Y%m%dT%H%M%SZ", utc=True),
                "round": desc.split(" · ")[2] if desc.count(" · ") >= 2 else "",
                "status": desc.rsplit(" · ", 1)[-1],
            })
    return pd.DataFrame(rows)


def to_canonical(raw: pd.DataFrame) -> pd.DataFrame:
    out = []
    for div, g in raw.groupby("div"):
        competition, country, tier = fd.DIVISIONS[div]
        local = g["kickoff_utc"].dt.tz_convert(ZoneInfo(TIMEZONES[country]))
        df = pd.DataFrame({
            "source": SOURCE, "competition": f"{country} {competition}", "country": country, "tier": tier,
            "season": f"{fd.current_season_start()}/{fd.current_season_start() + 1}",
            "date": local.dt.tz_localize(None).dt.normalize(),
            "kickoff": local.dt.strftime("%H:%M"),
            "home_team": g["home_raw"].map(lambda n: normalize_team(SOURCE, n)),
            "away_team": g["away_raw"].map(lambda n: normalize_team(SOURCE, n)),
            "round": g["round"], "status": g["status"],
        })
        df["home_team_id"] = [make_team_id(country, t) for t in df["home_team"]]
        df["away_team_id"] = [make_team_id(country, t) for t in df["away_team"]]
        out.append(df)
    df = pd.concat(out, ignore_index=True)
    df["match_id"] = [make_match_id(c, d, h, a) for c, d, h, a in
                      zip(df["competition"], df["date"], df["home_team"], df["away_team"])]
    for col in set(CANONICAL_COLUMNS) - set(df.columns):
        df[col] = pd.NA if col in INT_COLUMNS else float("nan")
    for col in INT_COLUMNS:
        df[col] = df[col].astype("Int64")
    return df[CANONICAL_COLUMNS + ["kickoff", "round", "status"]].sort_values(["date", "kickoff"]).reset_index(drop=True)


def known_teams(matches: pd.DataFrame) -> dict[str, set[str]]:
    """football-data names seen per country in the last two seasons (the canonical spellings)."""
    recent = matches[matches["season"] >= f"{fd.current_season_start() - 1}/{fd.current_season_start()}"]
    names: dict[str, set[str]] = {}
    for country, g in recent.groupby("country"):
        names[country] = set(g["home_team"]) | set(g["away_team"])
    return names


def unmapped(fixtures: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """Fixture team names that don't exist in football-data history for that country."""
    known = known_teams(matches)
    long = pd.concat([
        fixtures[["country", "competition", "home_team"]].rename(columns={"home_team": "team"}),
        fixtures[["country", "competition", "away_team"]].rename(columns={"away_team": "team"}),
    ]).drop_duplicates()
    return long[[t not in known.get(c, set()) for c, t in zip(long["country"], long["team"])]]


_NOISE = re.compile(r"\b(fc|cf|sc|sv|vfl|vfb|ac|as|ss|us|ud|cd|rcd|sd|kv|krc|rsc|afc|bsc|tsg|fsv|1|04|05|96|98|club|de|calcio|sk|spor|kulubu)\b")


def _key(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return " ".join(_NOISE.sub(" ", name).split())


def suggest_aliases(fixtures: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """Best football-data candidate per unmapped name (fuzzy, for human review)."""
    known = known_teams(matches)
    rows = []
    for country, team in unmapped(fixtures, matches)[["country", "team"]].drop_duplicates().itertuples(index=False):
        cands = sorted(known.get(country, set()))
        keys = {_key(c): c for c in cands}
        k = _key(team)
        best = difflib.get_close_matches(k, keys, n=2, cutoff=0.0)
        contains = [c for kk, c in keys.items() if kk and (kk in k or k in kk)]
        pick = contains[0] if len(contains) == 1 else (keys[best[0]] if best else "")
        score = difflib.SequenceMatcher(None, k, _key(pick)).ratio() if pick else 0.0
        rows.append({"country": country, "alias": team, "suggested": pick, "score": round(score, 2),
                     "runner_up": keys[best[1]] if len(best) > 1 else ""})
    return pd.DataFrame(rows).sort_values(["country", "score"])


def build(cal_dir: Path = CAL_DIR, out_path: Path = OUT_PATH, csv_path: Path = CSV_PATH) -> pd.DataFrame:
    fixtures = to_canonical(read_calendars(cal_dir))
    fixtures = fixtures[fixtures["status"] == "To be played"]
    missing = unmapped(fixtures, pd.read_parquet(fd.OUT_PATH))
    if len(missing):
        log.warning("%d team names have no football-data history (new/promoted or unmapped): %s",
                    len(missing), ", ".join(f"{t} ({c})" for c, t in zip(missing["competition"], missing["team"])))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fixtures.to_parquet(out_path, index=False)
    fixtures[["date", "kickoff", "competition", "round", "home_team", "away_team", "match_id"]].to_csv(csv_path, index=False)
    log.info("%d fixtures from %d competitions -> %s", len(fixtures), fixtures["competition"].nunique(), out_path)
    return fixtures


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--suggest", action="store_true", help="print alias suggestions for unmapped names")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.suggest:
        fixtures = to_canonical(read_calendars())
        pd.set_option("display.width", 200)
        print(suggest_aliases(fixtures, pd.read_parquet(fd.OUT_PATH)).to_string(index=False))
        return
    build()


if __name__ == "__main__":
    main()
