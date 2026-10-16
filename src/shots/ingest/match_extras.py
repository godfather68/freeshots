"""Side table with the football-data columns the canonical schema ignores.

Usage:
    python -m shots.ingest.match_extras      # data/raw/football-data/**.csv -> data/processed/match_extras.parquet

One row per ``match_id`` (same id as ``matches.parquet``; rows without a result yet are kept too).
The canonical schema and ``matches.parquet`` are untouched; this table is only read by candidate
feature code (``shots.features.candidates``).

=====================  ==========================================================================
column                  definition
=====================  ==========================================================================
kickoff_time            "HH:MM" local time as published (only since ~2019/20; else NA)
referee                 referee name as published (some leagues/seasons only; else NA)
home_corners/away_*     HC / AC          home_fouls/away_*   HF / AF
home_yellow/away_*      HY / AY          home_red/away_*     HR / AR
ah_line                 pre-match Asian handicap for the HOME team (AHh, older BbAHh); negative =
                        home favourite. Never the closing line (AHCh).
ah_home_odds/away_odds  pre-match market-average AH prices (AvgAHH/A, older BbAvAHH/A, else
                        B365AHH/A). Never closing (AvgCAHH ... are blocked).
=====================  ==========================================================================

Every source column is checked with ``is_closing_odds_column`` before use; a closing column in
the candidate lists raises at import.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from shots.ingest.football_data import (
    DIVISIONS, RAW_DIR, SOURCE, ROOT, is_closing_odds_column, read_raw,
)
from shots.ingest.schema import make_match_id, normalize_team

log = logging.getLogger(__name__)

OUT_PATH = ROOT / "data" / "processed" / "match_extras.parquet"

COUNT_COLUMNS = {
    "home_corners": "HC", "away_corners": "AC", "home_fouls": "HF", "away_fouls": "AF",
    "home_yellow": "HY", "away_yellow": "AY", "home_red": "HR", "away_red": "AR",
}
# first present column wins
AH_LINE_COLUMNS = ["AHh", "BbAHh"]
AH_ODDS_COLUMNS = {
    "ah_home_odds": ["AvgAHH", "BbAvAHH", "B365AHH"],
    "ah_away_odds": ["AvgAHA", "BbAvAHA", "B365AHA"],
}
EXTRAS_COLUMNS = [
    "match_id", "kickoff_time", "referee", *COUNT_COLUMNS, "ah_line", *AH_ODDS_COLUMNS,
]


def _check_no_closing() -> None:
    used = [*COUNT_COLUMNS.values(), *AH_LINE_COLUMNS, "Time", "Referee",
            *[c for cands in AH_ODDS_COLUMNS.values() for c in cands]]
    bad = [c for c in used if is_closing_odds_column(c)]
    if bad:
        raise AssertionError(f"closing-odds columns configured as pre-match extras: {bad}")


_check_no_closing()


def _first(df: pd.DataFrame, candidates: list[str]) -> pd.Series:
    """First candidate with any value per row (older/newer header variants never mix in a file)."""
    out = pd.Series(np.nan, index=df.index, dtype="float64")
    for c in candidates:
        if c in df:
            out = out.fillna(pd.to_numeric(df[c], errors="coerce"))
    return out


def _parse_dates(col: pd.Series) -> pd.Series:
    """Same result as the canonical adapter (dayfirst, mixed formats), but fast on the common formats."""
    d = pd.to_datetime(col, format="%d/%m/%Y", errors="coerce")
    rest = d.isna()
    if rest.any():
        d2 = pd.to_datetime(col[rest], format="%d/%m/%y", errors="coerce")
        d[rest] = d2
        rest = d.isna()
        if rest.any():
            d[rest] = pd.to_datetime(col[rest], dayfirst=True, format="mixed", errors="coerce")
    return d


def to_extras(raw: pd.DataFrame, div: str) -> pd.DataFrame:
    competition, country, _ = DIVISIONS[div]
    comp = f"{country} {competition}"
    df = raw.rename(columns={"HomeTeam": "home_team", "AwayTeam": "away_team"})
    df = df.dropna(subset=["home_team", "away_team", "Date"])
    date = _parse_dates(df["Date"])
    home = df["home_team"].map(lambda n: normalize_team(SOURCE, n))
    away = df["away_team"].map(lambda n: normalize_team(SOURCE, n))
    out = pd.DataFrame(index=df.index)
    out["kickoff_time"] = df["Time"].where(df["Time"].str.fullmatch(r"\d{1,2}:\d{2}", na=False)) if "Time" in df else None
    if "Referee" in df:
        ref = df["Referee"].str.strip()
        out["referee"] = ref.where(ref.notna() & (ref != ""))
    else:
        out["referee"] = None
    for name, col in COUNT_COLUMNS.items():
        out[name] = pd.to_numeric(df[col], errors="coerce") if col in df else np.nan
    out["ah_line"] = _first(df, AH_LINE_COLUMNS)
    for name, cands in AH_ODDS_COLUMNS.items():
        o = _first(df, cands)
        out[name] = o.where(o > 1.0)
    ok = date.notna()
    out = out[ok]
    out.insert(0, "match_id", [make_match_id(comp, d, h, a) for d, h, a in zip(date[ok], home[ok], away[ok])])
    return out.reset_index(drop=True)


def build(raw_dir: Path | None = None, out_path: Path | None = None) -> pd.DataFrame:
    raw_dir = raw_dir or RAW_DIR
    frames = []
    for path in sorted(raw_dir.glob("*/*.csv")):
        div = path.stem
        if div not in DIVISIONS:
            continue
        frames.append(to_extras(read_raw(path), div))
    ex = pd.concat(frames, ignore_index=True).drop_duplicates("match_id", keep="last")
    ex["kickoff_time"] = ex["kickoff_time"].astype("string")
    ex["referee"] = ex["referee"].astype("string")
    ex = ex[EXTRAS_COLUMNS]
    if out_path is not False:
        out_path = out_path or OUT_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ex.to_parquet(out_path, index=False)
    return ex


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    ap.add_argument("--output", type=Path, default=OUT_PATH)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ex = build(args.raw_dir, args.output)
    log.info("wrote %d rows to %s", len(ex), args.output)


if __name__ == "__main__":
    main()
