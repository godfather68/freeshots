"""Daily to-do list of results to enter by hand (.github/workflows/results-todo.yml, noon Europe/Paris).

Lists up to ~20 already-played matches that have a published prediction but no result yet, with the
links a person needs to read the final shot counts from ESPN's public team-stats pages, the CSV file the
row goes into and a pre-filled CSV block. A human (or Claude in an interactive session) opens the pages,
types the numbers and pushes the CSV; the push publishes them (update.yml).

    python -m shots.results_todo [--date YYYY-MM-DD] [--limit 20] [--data-dir DIR] [--out FILE.md]
    python3 src/shots/results_todo.py ...            (same, without installing the package)

**This job never fetches ESPN or any other site** (ESPN's terms forbid automated collection). It only
reads the public JSON we already publish (docs/static-api.md) and builds links from known URL patterns.
Standard library only, no network module imported.

Input (`--data-dir`): a checkout of the `data` branch (files under `data/`) or a `shots.export --out` dir.
`board.json` only lists fixtures from the build date on, so matches already played are found in the
**history** of `board.json` when the directory is a git checkout (the public audit trail): for each fixture
the last version committed before its kick-off is the published prediction. Without git history only the
current board is read (today's fixtures kick off after the run date, so the list is then usually empty).

Selection
  - match date (venue-local, as published) in [run date - 7 days, run date - 1 day];
  - a prediction published before kick-off (see above);
  - no result in `results.json` (by fixture id, or by league + date + teams), and no row for it yet in
    `results_provisional/*.csv` / `results_manual/*.csv` of `--repo-dir` (entered, not yet published);
  - priority: official picks (not experimental, P(total >= 24) >= 0.70) first, then experimental picks
    (European cups, same threshold), then the rest; within each by P(24+) descending, newest first on ties; leagues are interleaved round-robin (each round takes the best remaining match of
    every league, best league first) so no league fills the list; at most `--limit` rows.
  - European cups (experimental) are included; they go to `results_manual/`, the domestic leagues to
    `results_provisional/` (docs/platform.md, "Provisional results").

Prints `count=<n>` and `title=<issue title>` (GITHUB_OUTPUT format) on stdout.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SLUGS_CSV = HERE / "ingest" / "espn_leagues.csv"
MANUAL_DIR = "results_manual"
PROVISIONAL_DIR = "results_provisional"
CSV_COLUMNS = ("date", "home", "away", "home_shots", "away_shots", "home_sot", "away_sot", "home_goals",
               "away_goals", "source", "entered_at", "notes")
MANUAL_LEAGUES = frozenset({"uefa-champions-league", "uefa-europa-league", "uefa-conference-league"})
SCHEDULE_URL = "https://www.espn.com/soccer/schedule/_/date/{ymd}/league/{slug}"
TEAM_STATS_URL = "https://www.espn.com/soccer/team-stats/_/gameId/<id>"
DEFAULT_LIMIT = 20
LOOKBACK_DAYS = 7
THRESHOLD = 24
PICK_MIN_PROB = 0.70
DASH = "—"


def title_for(run_date: date) -> str:
    return f"Results to enter {DASH} {run_date.isoformat()}"


@dataclass(frozen=True)
class Item:
    id: str
    league: str
    date: str
    home: str
    away: str
    kickoff_utc: str | None
    expected: int | None
    lb80: int | None
    p24: float | None
    experimental: bool

    @property
    def official_pick(self) -> bool:
        return not self.experimental and self.p24 is not None and self.p24 >= PICK_MIN_PROB

    @property
    def experimental_pick(self) -> bool:
        return self.experimental and self.p24 is not None and self.p24 >= PICK_MIN_PROB

    def priority(self) -> tuple:
        """Smaller sorts first: official picks, then experimental (European cup) picks, then the rest; within
        each, P(24+) descending, then newest."""
        tier = 0 if self.official_pick else 1 if self.experimental_pick else 2
        return (tier, -(self.p24 or 0.0), _neg_date(self.date), self.home)


def _neg_date(d: str) -> int:
    return -date.fromisoformat(d).toordinal()


# ---- reading the public JSON -------------------------------------------------------------------------

def json_dir(data_dir: Path) -> Path:
    """The folder holding board.json: `<dir>/data` on the data branch, `<dir>` for an export dir."""
    return data_dir / "data" if (data_dir / "data" / "board.json").exists() else data_dir


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8")


def board_versions(data_dir: Path, since: date) -> list[tuple[datetime, dict]]:
    """(published at, board) pairs, oldest first: git history of board.json since `since` (commit time is
    the publication time; it is after the build, so "before kick-off" stays conservative) plus the working
    copy (time = meta.built_at)."""
    jdir = json_dir(data_dir)
    versions: list[tuple[datetime, dict]] = []
    rel = (jdir / "board.json").relative_to(data_dir).as_posix()
    log = _git(data_dir, "log", f"--since={since.isoformat()}T00:00:00Z", "--format=%H %cI", "--", rel)
    for line in (log or "").splitlines()[::-1]:
        sha, _, when = line.partition(" ")
        text = _git(data_dir, "show", f"{sha}:./{rel}")
        if text:
            try:
                versions.append((_parse_ts(when), json.loads(text)))
            except ValueError:
                continue
    built = _parse_ts(_load(jdir / "meta.json").get("built_at"))
    current = _load(jdir / "board.json")
    if current and built is not None:
        versions.append((built, current))
    versions.sort(key=lambda v: v[0])
    return versions


def _p_at(curve: list | None, lo: int, n: int) -> float | None:
    if not curve:
        return None
    i = n - lo
    return float(curve[i]) if 0 <= i < len(curve) else None


def _before_kickoff(published: datetime, fx: dict) -> bool:
    ko = _parse_ts(fx.get("kickoff_utc"))
    if ko is not None:
        return published < ko
    return published.date() <= date.fromisoformat(fx["date"])  # unknown time: published by match day


def published_predictions(versions: list[tuple[datetime, dict]], lo: int) -> dict[str, Item]:
    """Per fixture id, the last version published before kick-off."""
    out: dict[str, Item] = {}
    for published, board in versions:  # oldest first: later versions overwrite
        for fx in board.get("fixtures", []):
            if not fx.get("id") or not fx.get("date") or not _before_kickoff(published, fx):
                continue
            lb = fx.get("lb") or {}
            out[fx["id"]] = Item(
                id=fx["id"], league=fx.get("league", ""), date=fx["date"], home=fx.get("home", ""),
                away=fx.get("away", ""), kickoff_utc=fx.get("kickoff_utc"), expected=fx.get("expected"),
                lb80=lb.get("80"), p24=_p_at(fx.get("curve"), lo, THRESHOLD),
                experimental=fx.get("experimental") is True)
    return out


def _key(league: str, d: str, home: str, away: str) -> tuple:
    return (league, d, home.strip().casefold(), away.strip().casefold())


def settled_keys(jdir: Path) -> tuple[set[str], set[tuple]]:
    ids, keys = set(), set()
    for r in _load(jdir / "results.json").get("results", []):
        ids.add(r.get("id"))
        keys.add(_key(r.get("league", ""), r.get("date", ""), r.get("home", ""), r.get("away", "")))
    return ids, keys


def season_of(d: str) -> str:
    """European season label of a match date: 2026-10-18 -> 2026-27 (season starts 1 July)."""
    day = date.fromisoformat(d)
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def target_file(item: Item) -> str:
    folder = MANUAL_DIR if (item.experimental or item.league in MANUAL_LEAGUES) else PROVISIONAL_DIR
    return f"{folder}/{item.league}_{season_of(item.date)}.csv"


def entered_keys(repo_dir: Path | None) -> set[tuple]:
    """Rows already typed into the committed CSVs (entered, maybe not yet published)."""
    out: set[tuple] = set()
    if repo_dir is None:
        return out
    for folder in (MANUAL_DIR, PROVISIONAL_DIR):
        for path in sorted((repo_dir / folder).glob("*_*.csv")):
            league = path.stem.rsplit("_", 1)[0]
            try:
                rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
            except (OSError, csv.Error):
                continue
            for row in rows:
                if row.get("date") and row.get("home") and row.get("away"):
                    out.add(_key(league, row["date"], row["home"], row["away"]))
    return out


# ---- selection -------------------------------------------------------------------------------------

def select(items: list[Item], run_date: date, limit: int = DEFAULT_LIMIT, lookback: int = LOOKBACK_DAYS,
           settled_ids: set[str] = frozenset(), settled: set[tuple] = frozenset(),
           entered: set[tuple] = frozenset()) -> list[Item]:
    first = run_date - timedelta(days=lookback)
    pool = [i for i in items
            if first <= date.fromisoformat(i.date) < run_date
            and i.id not in settled_ids
            and _key(i.league, i.date, i.home, i.away) not in settled
            and _key(i.league, i.date, i.home, i.away) not in entered]
    queues: dict[str, list[Item]] = {}
    for item in sorted(pool, key=Item.priority):
        queues.setdefault(item.league, []).append(item)
    chosen: list[Item] = []
    while queues and len(chosen) < limit:
        heads = sorted(queues, key=lambda lg: queues[lg][0].priority())  # one per league per round
        for league in heads:
            if len(chosen) >= limit:
                break
            chosen.append(queues[league].pop(0))
            if not queues[league]:
                del queues[league]
    return chosen


# ---- output ----------------------------------------------------------------------------------------

def load_slugs(path: Path = SLUGS_CSV) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["league"].strip(): r["espn_slug"].strip() for r in csv.DictReader(fh) if r.get("espn_slug")}


def schedule_link(item: Item, slugs: dict[str, str]) -> str | None:
    slug = slugs.get(item.league)
    if not slug:
        return None
    return SCHEDULE_URL.format(ymd=item.date.replace("-", ""), slug=slug)


def league_names(jdir: Path) -> dict[str, str]:
    out = {}
    for lg in _load(jdir / "meta.json").get("leagues", []):
        name, country = lg.get("name", lg["id"]), lg.get("country")
        out[lg["id"]] = name if country in (None, "Europe") else f"{name} ({country})"
    return out


def league_label(league: str, names: dict[str, str]) -> str:
    """Display name; a league no longer in meta.json (no upcoming fixture) gets its id title-cased."""
    return names.get(league) or league.replace("-", " ").title()


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def csv_block(rows: list[Item]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for i in rows:
        w.writerow([i.date, i.home, i.away] + [""] * (len(CSV_COLUMNS) - 3))
    return buf.getvalue()


def render(chosen: list[Item], run_date: date, slugs: dict[str, str], names: dict[str, str],
           provisional_named: bool = True, lookback: int = LOOKBACK_DAYS) -> str:
    first, last = run_date - timedelta(days=lookback), run_date - timedelta(days=1)
    rows = sorted(chosen, key=lambda i: (_neg_date(i.date), league_label(i.league, names), i.kickoff_utc or "",
                                         i.home))
    out = [f"# {title_for(run_date)}", "",
           f"{len(rows)} match(es) played {first.isoformat()} .. {last.isoformat()} with a published prediction "
           "and no result yet. Official picks (P(24+) >= 0.70) first, leagues interleaved.", "",
           "How to enter a result (by hand; no script may fetch ESPN):",
           "1. Open the ESPN schedule link, find the match, open its **Stats** tab "
           f"(`{TEAM_STATS_URL}`).",
           "2. Read the total shots (blocked included), shots on target and goals of each team.",
           "3. Fill the row in the CSV block of its file (below), put the team-stats URL in `source` and the "
           "current UTC time in `entered_at`, e.g. `2026-10-20T12:30:00Z`.",
           "4. Check (`python -m shots.db provisional --check` / `python -m shots.db manual --check`) and push: "
           "the push publishes the result.", "",
           "| # | Date | League | Match | Kick-off (UTC) | Expected / lb80 / P(24+) | Pick | ESPN schedule | File |",
           "|---|---|---|---|---|---|---|---|---|"]
    for n, i in enumerate(rows, 1):
        ko = i.kickoff_utc[11:16] if i.kickoff_utc and len(i.kickoff_utc) >= 16 else DASH
        p = f"{i.p24:.2f}" if i.p24 is not None else DASH
        nums = f"{i.expected if i.expected is not None else DASH} / {i.lb80 if i.lb80 is not None else DASH} / {p}"
        pick = "experimental" if i.experimental else ("yes" if i.official_pick else "")
        link = schedule_link(i, slugs)
        out.append(f"| {n} | {i.date} | {_cell(league_label(i.league, names))} | {_cell(i.home)} v {_cell(i.away)} "
                   f"| {ko} | {nums} | {pick} | {f'[schedule]({link})' if link else DASH} | `{target_file(i)}` |")
    out.append("")
    if not provisional_named:
        out += [f"Note: `{PROVISIONAL_DIR}/` was not found; file names follow `<league>_<season>.csv`.", ""]
    by_file: dict[str, list[Item]] = {}
    for i in rows:
        by_file.setdefault(target_file(i), []).append(i)
    out += ["## CSV rows", "",
            "One block per file, our team names, columns in the file's order. Leave out the header line when the "
            "file already has one.", ""]
    for path in sorted(by_file):
        out += [f"### `{path}`", "", "```csv", csv_block(by_file[path]).rstrip("\n"), "```", ""]
    return "\n".join(out)


def build(data_dir: Path, run_date: date, limit: int = DEFAULT_LIMIT, repo_dir: Path | None = ROOT,
          slugs_path: Path = SLUGS_CSV, lookback: int = LOOKBACK_DAYS) -> tuple[list[Item], str]:
    jdir = json_dir(data_dir)
    meta = _load(jdir / "meta.json")
    lo = int(((meta.get("thresholds") or {}).get("total") or [10])[0])
    versions = board_versions(data_dir, run_date - timedelta(days=lookback + 7))
    items = list(published_predictions(versions, lo).values())
    ids, keys = settled_keys(jdir)
    chosen = select(items, run_date, limit, lookback, ids, keys, entered_keys(repo_dir))
    named = repo_dir is None or (repo_dir / PROVISIONAL_DIR).is_dir()
    return chosen, render(chosen, run_date, load_slugs(slugs_path), league_names(jdir), named, lookback)


def _today_paris() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:  # no tz database (bare Windows Python): UTC is within a day
        return datetime.now(timezone.utc).date()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m shots.results_todo", description=__doc__.split("\n")[0])
    ap.add_argument("--date", type=date.fromisoformat, default=None, help="run date (default: today, Paris)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "site-data",
                    help="checkout of the data branch or a shots.export output dir")
    ap.add_argument("--repo-dir", type=Path, default=ROOT, help="repository with results_manual/ (default: this one)")
    ap.add_argument("--out", type=Path, default=None, help="markdown file (default: stdout)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_date = args.date or _today_paris()
    if args.limit < 1:
        ap.error("--limit must be >= 1")
    chosen, md = build(args.data_dir, run_date, args.limit, args.repo_dir)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md + "\n", encoding="utf-8")
        print(f"count={len(chosen)}")
        print(f"title={title_for(run_date)}")
    else:
        sys.stdout.write(md + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
