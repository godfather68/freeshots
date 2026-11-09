"""Data-quality report for the canonical match table.

Usage: python -m shots.ingest.quality
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from shots.ingest.football_data import OUT_PATH, RAW_DIR, ROOT, read_raw, to_canonical

REPORT_DIR = ROOT / "reports"


def _all_parsed_rows() -> pd.DataFrame:
    frames = []
    for path in sorted(RAW_DIR.glob("[0-9][0-9][0-9][0-9]/*.csv")):
        code, div = path.parent.name, path.stem
        frames.append(to_canonical(read_raw(path), div, 2000 + int(code[:2])))
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset="match_id", keep="last")


EXTRA_PAIRS = ["ht_goals", "corners", "reds", "fouls"]


def extra_coverage(matches: pd.DataFrame, last_n_seasons: int = 7) -> pd.DataFrame:
    """Share of matches (with shots) where both home and away value of each extra column is present,
    per competition, for the last N seasons."""
    seasons = sorted(matches["season"].unique())[-last_n_seasons:]
    recent = matches[matches["season"].isin(seasons)]
    cols = {}
    for name in EXTRA_PAIRS:
        h, a = f"home_{name}", f"away_{name}"
        if h in recent and a in recent:
            cols[name] = recent[h].notna() & recent[a].notna()
    flags = pd.DataFrame(cols).assign(competition=recent["competition"].values)
    return (100 * flags.groupby("competition").mean()).round(1).reset_index()


def extra_coverage_by_season(matches: pd.DataFrame) -> pd.DataFrame:
    cols = {n: matches[f"home_{n}"].notna() & matches[f"away_{n}"].notna()
            for n in EXTRA_PAIRS if f"home_{n}" in matches}
    flags = pd.DataFrame(cols).assign(season=matches["season"].values)
    return (100 * flags.groupby("season").mean()).round(1).reset_index()


def build_report() -> str:
    matches = pd.read_parquet(OUT_PATH)
    parsed = _all_parsed_rows()
    parsed["has_shots"] = parsed[["home_shots", "away_shots"]].notna().all(axis=1)
    matches["total_shots"] = matches["home_shots"] + matches["away_shots"]

    coverage = (
        parsed.groupby("competition")
        .agg(first_season=("season", "min"), last_season=("season", "max"),
             rows=("match_id", "size"), with_shots=("has_shots", "sum"))
    )
    first_shot_season = parsed[parsed.has_shots].groupby("competition")["season"].min()
    coverage["first_season_with_shots"] = first_shot_season
    coverage["pct_with_shots"] = (100 * coverage.with_shots / coverage.rows).round(1)
    shots = matches.groupby("competition")["total_shots"].agg(["mean", "std", "min", "max"]).round(1)
    shots.columns = [f"total_shots_{c}" for c in shots.columns]
    coverage = coverage.join(shots).sort_values("with_shots", ascending=False)

    sanity = {
        "home_sot > home_shots": int((matches.home_sot > matches.home_shots).sum()),
        "away_sot > away_shots": int((matches.away_sot > matches.away_shots).sum()),
        "goals > shots on target (home)": int((matches.home_goals > matches.home_sot).sum()),
        "total_shots == 0": int((matches.total_shots == 0).sum()),
        "total_shots > 60": int((matches.total_shots > 60).sum()),
        "duplicate (competition, date, home, away)": int(
            matches.duplicated(["competition", "date", "home_team", "away_team"]).sum()),
        "missing pre-match 1X2 odds": int(matches.odds_home.isna().sum()),
    }

    lines = [
        f"# Data quality: football-data.co.uk ({date.today():%Y-%m-%d})",
        "",
        f"- Parsed rows: **{len(parsed):,}**. Kept with shot data: **{len(matches):,}** "
        f"({len(parsed) - len(matches):,} dropped for missing shots).",
        f"- Date range: {matches.date.min():%Y-%m-%d} → {matches.date.max():%Y-%m-%d}",
        f"- Competitions: {matches.competition.nunique()}, teams: "
        f"{pd.concat([matches.home_team, matches.away_team]).nunique():,}",
        f"- Total shots per match overall: mean {matches.total_shots.mean():.1f}, "
        f"std {matches.total_shots.std():.1f}, variance/mean {matches.total_shots.var() / matches.total_shots.mean():.2f} "
        "(>1 means overdispersed: plain Poisson is too narrow)",
        "",
        "## Sanity checks (counts of suspicious rows)",
        "",
        "| check | rows |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in sanity.items()],
        "",
        "## Coverage by competition",
        "",
        coverage.reset_index().to_markdown(index=False),
        "",
        "## Extra columns: % of matches with shots where home and away are both present",
        "",
        "Columns: home/away_ht_goals (HTHG/HTAG), corners (HC/AC), reds (HR/AR), fouls (HF/AF). "
        "Missing stays null, never 0.",
        "",
        "### By competition, last 7 seasons",
        "",
        extra_coverage(matches).to_markdown(index=False),
        "",
        "### By season, all competitions",
        "",
        extra_coverage_by_season(matches).to_markdown(index=False),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{date.today():%Y-%m-%d}_data_quality_football_data.md"
    path.write_text(build_report(), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
