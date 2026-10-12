import pytest
from pathlib import Path

import pandas as pd

from shots.ingest.football_data import current_season_start, read_raw, season_code, to_canonical
from shots.ingest.schema import CANONICAL_COLUMNS

FIXTURES = Path(__file__).parent / "fixtures"


def test_new_format_parses_bom_shots_and_prematch_odds():
    df = to_canonical(read_raw(FIXTURES / "fd_new_format.csv"), "E0", 2025)
    assert list(df.columns) == CANONICAL_COLUMNS
    first = df.iloc[0]
    assert first.home_team == "Liverpool"
    assert first.date == pd.Timestamp("2025-08-15")
    assert (first.home_shots, first.away_shots, first.home_sot, first.away_sot) == (19, 10, 10, 3)
    assert first.odds_home == 1.31  # pre-match AvgH, not closing AvgCH
    assert first.competition == "England Premier League" and first.season == "2025/2026"


def test_missing_shots_stay_missing_not_zero():
    df = to_canonical(read_raw(FIXTURES / "fd_new_format.csv"), "E0", 2025)
    assert df.iloc[2][["home_shots", "away_shots"]].isna().all()


def test_old_format_two_digit_year_and_ragged_columns():
    df = to_canonical(read_raw(FIXTURES / "fd_old_format.csv"), "D1", 2010)
    assert len(df) == 1
    row = df.iloc[0]
    assert row.date == pd.Timestamp("2010-08-20")
    assert row.home_shots == 23 and row.odds_over25 == 1.6


def test_match_id_is_stable_and_unique():
    a = to_canonical(read_raw(FIXTURES / "fd_new_format.csv"), "E0", 2025)
    b = to_canonical(read_raw(FIXTURES / "fd_new_format.csv"), "E0", 2025)
    assert a.match_id.tolist() == b.match_id.tolist()
    assert a.match_id.is_unique


def test_season_helpers():
    from datetime import date
    assert season_code(2023) == "2324"
    assert current_season_start(date(2026, 10, 8)) == 2026
    assert current_season_start(date(2027, 3, 1)) == 2026


def test_team_ids_scoped_by_country_and_stable_across_divisions():
    from shots.ingest.schema import make_team_id
    assert make_team_id("England", "Man City") == "england:man-city"
    assert make_team_id("Spain", "Atlético Madrid") == "spain:atletico-madrid"
    assert make_team_id("England", "Rovers") != make_team_id("Scotland", "Rovers")


def test_closing_odds_columns_rejected():
    from shots.ingest import football_data as fd
    closing = ["B365CH", "PSCH", "PSCD", "MaxCA", "AvgCH", "BFECH", "B365C>2.5", "PC<2.5",
               "MaxC>2.5", "AvgC<2.5", "BbAvC>2.5", "AvgCAHH", "WHCA"]
    for c in closing:
        assert fd.is_closing_odds_column(c), c
    for c in ["AvgH", "AvgD", "Avg>2.5", "Avg<2.5", "B365H", "BbAvH", "BbAv>2.5", "PSH", "MaxH", "CLH", "HC", "AC"]:
        assert not fd.is_closing_odds_column(c), c
    assert fd._check_no_closing_odds() is None
    fd.ODDS_COLUMNS["odds_home"].append("AvgCH")
    try:
        with pytest.raises(AssertionError):
            fd._check_no_closing_odds()
    finally:
        fd.ODDS_COLUMNS["odds_home"].pop()


def test_real_header_odds_sources_are_prematch():
    from shots.ingest import football_data as fd
    header = (fd.RAW_DIR / "2526" / "E0.csv")
    if not header.exists():
        pytest.skip("raw data not cached")
    cols = fd.read_raw(header).columns
    used = [next((c for c in cands if c in cols), None) for cands in fd.ODDS_COLUMNS.values()]
    assert all(used) and not any(fd.is_closing_odds_column(c) for c in used)
