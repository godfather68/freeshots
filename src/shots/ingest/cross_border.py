"""Cross-border competitions (clubs from several countries): the UEFA Champions, Europa and Conference Leagues.

Domestic competitions scope a team by the competition's country (``<country>:<slug>``). In a
cross-border competition each club keeps its DOMESTIC team id, so its rolling form comes from its
domestic matches. The club -> country mapping is a reviewed file, ``club_countries.csv`` (columns
``competition, club, country, history``; ``club`` is the alias-normalized name):

  history = yes   the club plays in a domestic league we have shot data for
  history = no    its domestic league is not covered: the club gets its own id and no history

A club missing from the file is reported and treated as ``history = no`` under the competition's
country label ("Europe"); it never fails the build. We have no shot counts for these competitions
(no source yet), so their fixtures are scored by an explicitly experimental model
(``shots.models.cross_border``) and can never be settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import pandas as pd


@dataclass(frozen=True)
class CrossBorder:
    name: str          # canonical competition name (no country prefix)
    country: str       # label used where a domestic competition has its country
    tier: int
    label: str         # suffix of the experimental model label, e.g. "<base model>-ucl-exp"


# calendar file prefix -> competition
COMPETITIONS: dict[str, CrossBorder] = {
    "uefa-champions-league": CrossBorder("UEFA Champions League", "Europe", 1, "ucl-exp"),
    "uefa-europa-league": CrossBorder("UEFA Europa League", "Europe", 1, "uel-exp"),
    "uefa-conference-league": CrossBorder("UEFA Conference League", "Europe", 1, "uecl-exp"),
}
BY_NAME: dict[str, CrossBorder] = {c.name: c for c in COMPETITIONS.values()}
COUNTRY_LABELS: frozenset[str] = frozenset(c.country for c in COMPETITIONS.values())


def is_cross_border(competition) -> bool:
    return competition in BY_NAME


@lru_cache(maxsize=1)
def club_table() -> pd.DataFrame:
    with resources.files("shots.ingest").joinpath("club_countries.csv").open(encoding="utf-8") as f:
        table = pd.read_csv(f, dtype=str).fillna("")
    table["history"] = table["history"].str.strip().str.lower() == "yes"
    return table


@lru_cache(maxsize=1)
def _clubs() -> dict[tuple[str, str], tuple[str, bool]]:
    return {(r.competition, r.club): (r.country, bool(r.history)) for r in club_table().itertuples()}


def club_country(competition: str, club: str) -> str | None:
    """Reviewed domestic country of ``club`` (alias-normalized name) in ``competition``, or None."""
    hit = _clubs().get((competition, club))
    return hit[0] if hit else None


def club_has_history(competition: str, club: str) -> bool:
    """True when the mapping says the club plays in a domestic league with shot data."""
    hit = _clubs().get((competition, club))
    return bool(hit and hit[1])
