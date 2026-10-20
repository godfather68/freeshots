"""Provisional results: hand-entered final shot counts for football-data competitions, until football-data
publishes the match.

football-data.co.uk sometimes stops publishing for days. Matches we predicted can then not settle. A person
may type their final counts from a public match page (ESPN team stats so far) into a committed CSV, one file
per competition and season:

    results_provisional/<league id>_<season>.csv      e.g. england-premier-league_2026-27.csv

<league id> is the slug of the canonical competition name ("England Premier League" -> england-premier-league,
the same id as in the static export). ONLY competitions football-data covers (shots.ingest.football_data.
DIVISIONS) are accepted; a cross-border competition belongs in results_manual/ instead.

Columns, validation and normalisation are exactly those of results_manual/ (shots.ingest.manual_results):
date, home, away, home_shots, away_shots, home_sot, away_sot, home_goals, away_goals, source, entered_at,
notes. In addition the source may not be football-data itself (its numbers arrive automatically).

What a provisional result does (shots.db.provisional loads it): it settles the match in the database (status
'played', source 'provisional'; outcomes are derived views, so the predictions settle, the official live
record included) and is flagged provisional everywhere it is shown. As soon as football-data publishes the
match, the regular results load overwrites it (source 'football-data') and the result is 'confirmed'; both
values and their difference stay queryable (v_provisional_agreement, provisional_result_edits).

Provisional results never reach the modelling pipeline: features, ratings, league levels and the models
read data/processed/matches.parquet, which is built from football-data only (shots.ingest.football_data);
this module is never imported there.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from shots.ingest import football_data as fd
from shots.ingest import manual_results as mr

PROVISIONAL_DIR = fd.ROOT / "results_provisional"
COMMITTED_DIR = PROVISIONAL_DIR  # the repository folder (tests redirect PROVISIONAL_DIR to an empty one)
COLUMNS = mr.COLUMNS
_SEASON = re.compile(r"^(\d{4})-(\d{2})$")
_LABEL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REFUSED_HOSTS = ("football-data.co.uk",)  # the automatic source itself: never typed in by hand


class ProvisionalResultsError(mr.ManualResultsError):
    """One or more problems in the provisional results (`problems`: "<file>:<line>: <what>")."""

    what = "provisional results"


@dataclass(frozen=True)
class Covered:
    """A competition football-data covers (one of its divisions)."""
    name: str  # canonical competition name, e.g. "England Premier League"
    country: str
    tier: int
    code: str  # football-data division code, e.g. "E0"

    @property
    def prefix(self) -> str:
        return slug(self.name)


def slug(text: str) -> str:
    """'England Premier League' -> 'england-premier-league' (the static export's league id)."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


COMPETITIONS: dict[str, Covered] = {}
for _code, (_comp, _country, _tier) in fd.DIVISIONS.items():
    _c = Covered(name=f"{_country} {_comp}", country=_country, tier=_tier, code=_code)
    COMPETITIONS[_c.prefix] = _c


def files(directory: Path | None = None) -> list[Path]:
    return mr.files(directory or PROVISIONAL_DIR)


def fingerprint(directory: Path | None = None) -> str:
    """Short hash of the names and contents of the provisional results files (line endings normalised)."""
    return mr.fingerprint(directory or PROVISIONAL_DIR)


def has_rows(directory: Path | None = None) -> bool:
    return mr.has_rows(directory or PROVISIONAL_DIR)


def competition_for(path: Path) -> tuple[Covered, int]:
    """'england-premier-league_2026-27.csv' -> (the competition, 2026). ValueError if not acceptable."""
    name = Path(path).name
    prefix, sep, season = Path(path).stem.partition("_")
    m = _SEASON.match(season)
    if not sep or not m:
        raise ValueError(f"{name}: file name must be <league id>_<YYYY-YY>.csv")
    start, end = int(m.group(1)), int(m.group(2))
    if (start + 1) % 100 != end:
        raise ValueError(f"{name}: season {season} is not two consecutive years")
    comp = COMPETITIONS.get(prefix)
    if comp is None:
        from shots.ingest import cross_border as xb

        hint = (" (a competition without an automatic results source: use results_manual/)"
                if prefix in xb.COMPETITIONS else "")
        raise ValueError(f"{name}: {prefix!r} is not a competition football-data covers{hint} "
                         f"(known: {', '.join(sorted(COMPETITIONS))})")
    return comp, start


def provider(url: str | None) -> str | None:
    """The provider label of a source URL: its host without 'www.' / 'm.' and without the domain suffix,
    'https://www.espn.com/soccer/...' -> 'espn', 'https://www.espn.co.uk/...' -> 'espn'. None if no host."""
    host = (urlsplit(url or "").hostname or "").lower()
    for p in ("www.", "m."):
        if host.startswith(p):
            host = host[len(p):]
    label = host.split(".")[0] if host else ""
    return label if _LABEL.match(label) else None


def _check_source(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in REFUSED_HOSTS):
        return "source is football-data itself: its results are loaded automatically, not typed in"
    label = provider(url)
    if label is None:
        return f"source {url!r} has no usable host name"
    if label in ("football-data", "manual", "provisional"):
        return f"source host {label!r} is a reserved name"
    return None


def read_file(path: Path) -> mr.ManualFile:
    """Parse and check one file (shots.ingest.manual_results.read_file's rules plus the file-name rule
    above). Raises ProvisionalResultsError listing every problem. `competition` is a Covered."""
    return mr.parse_file(path, competition_for, ProvisionalResultsError, _check_source)


__all__ = ["COLUMNS", "COMPETITIONS", "Covered", "PROVISIONAL_DIR", "ProvisionalResultsError", "competition_for",
           "files", "fingerprint", "has_rows", "provider", "read_file"]
