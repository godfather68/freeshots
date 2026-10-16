"""match_extras side table: parsing, id parity with the canonical adapter, closing-odds guard."""

import numpy as np
import pandas as pd
import pytest

from shots.ingest import football_data as fd
from shots.ingest import match_extras as me

NEW = pd.DataFrame({
    "Div": ["E0", "E0"], "Date": ["16/08/2024", "17/08/2024"], "Time": ["20:00", "12:30"],
    "HomeTeam": ["Man United", "Ipswich"], "AwayTeam": ["Fulham", "Liverpool"],
    "FTHG": ["1", "0"], "FTAG": ["0", "2"], "Referee": ["R Jones", "  "],
    "HS": ["14", "8"], "AS": ["10", "15"], "HST": ["5", "2"], "AST": ["2", "6"],
    "HF": ["12", "9"], "AF": ["10", "11"], "HC": ["7", "3"], "AC": ["4", "8"],
    "HY": ["1", "2"], "AY": ["2", "0"], "HR": ["0", "0"], "AR": ["0", "1"],
    "AvgH": ["1.7", "5.0"], "AvgD": ["3.9", "4.4"], "AvgA": ["5.0", "1.6"],
    "Avg>2.5": ["1.9", "1.8"], "Avg<2.5": ["1.9", "2.0"],
    "AHh": ["-0.75", "1.0"], "AvgAHH": ["1.95", "1.9"], "AvgAHA": ["1.97", "2.0"],
    "AHCh": ["-1.25", "1.5"], "AvgCAHH": ["1.5", "1.4"], "AvgCAHA": ["2.4", "2.5"],
    "B365CH": ["1.5", "6"],
})
OLD = pd.DataFrame({
    "Div": ["E0"], "Date": ["20/08/11"], "HomeTeam": ["Arsenal"], "AwayTeam": ["Chelsea"],
    "FTHG": ["1"], "FTAG": ["1"], "HS": ["10"], "AS": ["9"], "HC": ["5"], "AC": ["6"],
    "BbAHh": ["-0.25"], "BbAvAHH": ["1.9"], "BbAvAHA": ["2.0"],
})


def test_closing_guard_covers_asian_handicap_and_all_bookmaker_prefixes():
    closing = ["AHCh", "AvgCAHH", "AvgCAHA", "B365CAHH", "PCAHA", "MaxCAHH", "BFECAHH", "1XBCH", "PSCH",
               "WHCH", "AvgCH", "BbAvC>2.5", "B365C>2.5"]
    assert all(fd.is_closing_odds_column(c) for c in closing)
    prematch = ["AHh", "BbAHh", "AvgAHH", "AvgAHA", "BbAvAHH", "B365AHH", "B365AHA", "1XBH", "AvgH", "Avg>2.5",
                "HC", "HF", "HY", "Referee", "Time"]
    assert not any(fd.is_closing_odds_column(c) for c in prematch)


def test_every_extras_source_column_is_prematch():
    cols = [*me.COUNT_COLUMNS.values(), *me.AH_LINE_COLUMNS, *(c for v in me.AH_ODDS_COLUMNS.values() for c in v)]
    assert cols and not any(fd.is_closing_odds_column(c) for c in cols)


def test_closing_values_are_never_read():
    ex = me.to_extras(NEW, "E0")
    assert ex["ah_line"].tolist() == [-0.75, 1.0]                 # AHh, not AHCh
    assert ex["ah_home_odds"].tolist() == [1.95, 1.9]             # AvgAHH, not AvgCAHH
    assert ex["ah_away_odds"].tolist() == [1.97, 2.0]


def test_parsing_new_format():
    ex = me.to_extras(NEW, "E0")
    assert ex["kickoff_time"].tolist() == ["20:00", "12:30"]
    assert ex["referee"].iloc[0] == "R Jones" and pd.isna(ex["referee"].iloc[1])     # blank -> missing
    assert ex[["home_corners", "away_corners", "home_fouls", "away_fouls"]].iloc[0].tolist() == [7, 4, 12, 10]
    assert ex["away_red"].iloc[1] == 1


def test_parsing_old_format_and_missing_columns_are_nan():
    ex = me.to_extras(OLD, "E0")
    assert ex["ah_line"].iloc[0] == -0.25 and ex["ah_home_odds"].iloc[0] == 1.9
    assert pd.isna(ex["kickoff_time"].iloc[0]) and pd.isna(ex["referee"].iloc[0])
    assert pd.isna(ex["home_fouls"].iloc[0])          # absent column stays NaN, never 0


def test_match_ids_equal_the_canonical_adapter():
    for raw in (NEW, OLD):
        canon = fd.to_canonical(raw, "E0", 2024)
        ex = me.to_extras(raw, "E0")
        assert ex["match_id"].tolist() == canon["match_id"].tolist()


def test_build_reads_a_raw_dir_linux_safe(tmp_path):
    d = tmp_path / "2425"
    d.mkdir()
    NEW.to_csv(d / "E0.csv", index=False)
    (tmp_path / "2425" / "notes.csv").write_text("x\n1\n")
    out = me.build(raw_dir=tmp_path, out_path=False)
    assert len(out) == 2 and out["match_id"].is_unique
    assert list(out.columns) == me.EXTRAS_COLUMNS
