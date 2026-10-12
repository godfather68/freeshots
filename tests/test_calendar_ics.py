from pathlib import Path

import pandas as pd

from shots.ingest import calendar_ics as cal

CAL_DIR = Path(__file__).parent / "fixtures" / "calendars"


def test_parse_unfolds_lines_and_reads_events():
    events = cal.parse_ics(CAL_DIR / "bundesliga-de_2026-27.ics")
    assert len(events) == 2
    assert events[0]["DESCRIPTION"].endswith("To be played")  # folded line rejoined


def test_canonical_maps_names_division_and_local_date():
    fx = cal.to_canonical(cal.read_calendars(CAL_DIR))
    first = fx.iloc[0]
    assert first.competition == "Germany Bundesliga" and first.tier == 1
    assert (first.home_team, first.away_team) == ("Heidenheim", "Bayern Munich")  # via team_aliases.csv
    # 22:30 UTC is 00:30 the next day in Berlin: the local date is what football-data uses.
    assert first.date == pd.Timestamp("2026-10-11") and first.kickoff == "00:30"
    assert first.home_team_id == "germany:heidenheim"
    assert fx[["home_shots", "odds_home"]].isna().all().all()
    assert fx.match_id.is_unique


def test_unknown_file_prefix_is_skipped(tmp_path):
    (tmp_path / "unknown-league_2026-27.ics").write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
    assert cal.read_calendars(tmp_path).empty
