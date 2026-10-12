"""Canonical match schema shared by every source adapter (see CLAUDE.md)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache
from importlib import resources

import pandas as pd

CANONICAL_COLUMNS = [
    "match_id", "source", "competition", "country", "tier", "season", "date",
    "home_team", "away_team", "home_team_id", "away_team_id",
    "home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals",
    # Pre-match market odds (never closing odds: those are set after kick-off info is known).
    "odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25",
]

INT_COLUMNS = ["home_shots", "away_shots", "home_sot", "away_sot", "home_goals", "away_goals", "tier"]


def make_match_id(competition: str, date: pd.Timestamp, home: str, away: str) -> str:
    key = f"{competition}|{date:%Y-%m-%d}|{home}|{away}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def make_team_id(country: str, team: str) -> str:
    """Stable team key "<country>:<normalized name>" (slug style), e.g. "england:man-city".

    ``team`` must already be alias-normalized (``normalize_team``). The country, not the
    division, scopes the name, so a club promoted/relegated within its country keeps one id
    while same-named clubs in different countries stay apart.
    """
    return f"{_slug(country)}:{_slug(team)}"


@lru_cache(maxsize=1)
def _aliases() -> dict[tuple[str, str], str]:
    with resources.files("shots.ingest").joinpath("team_aliases.csv").open(encoding="utf-8") as f:
        table = pd.read_csv(f, dtype=str)
    return {(r.source, r.alias): r.canonical for r in table.itertuples()}


def normalize_team(source: str, name: str) -> str:
    name = " ".join(str(name).split())
    return _aliases().get((source, name), name)
