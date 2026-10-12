import json
from pathlib import Path

import pandas as pd

from shots.ingest.schema import CANONICAL_COLUMNS
from shots.ingest.statsbomb import count_shots, fix_mojibake, match_row, pick_sample, to_canonical, unmapped_teams

FIXTURES = Path(__file__).parent / "fixtures" / "statsbomb"
MATCH = json.loads((FIXTURES / "match.json").read_text())
EVENTS = json.loads((FIXTURES / "events.json").read_text())


def test_shot_and_sot_definitions():
    counts = count_shots(EVENTS)
    # Blocked/Off T/Post count as shots but not SoT; shoot-out (period 5) is excluded.
    assert counts["Chelsea"] == {"shots": 5, "sot": 2}
    # Saved To Post is on target; Saved Off Target and Wayward are not.
    assert counts["Arsenal"] == {"shots": 3, "sot": 1}


def test_match_row_canonical():
    df = to_canonical([match_row(MATCH, EVENTS)])
    assert list(df.columns) == CANONICAL_COLUMNS
    row = df.iloc[0]
    assert row.source == "statsbomb" and row.competition == "England Premier League" and row.tier == 1
    assert row.season == "2015/2016" and row.date == pd.Timestamp("2015-09-19")
    assert (row.home_shots, row.away_shots, row.home_sot, row.away_sot) == (5, 3, 2, 1)
    assert (row.home_goals, row.away_goals) == (2, 0)
    assert row[["odds_home", "odds_over25"]].isna().all()


def test_unmapped_competition_skipped():
    other = {**MATCH, "competition": {"competition_id": 43, "country_name": "International",
                                      "competition_name": "FIFA World Cup"}}
    assert match_row(other, EVENTS) is None


def test_pick_sample_is_capped_and_deterministic():
    ms = [{"match_id": i, "match_date": f"2016-01-{i % 28 + 1:02d}"} for i in range(100)]
    a, b = pick_sample(ms, 10), pick_sample(ms, 10)
    assert len(a) == 10 and a == b


def test_mojibake_and_unmapped_report():
    assert fix_mojibake("AtlÃ©tico") == "Atlético"
    df = to_canonical([match_row(MATCH, EVENTS)])
    out = unmapped_teams(df, {"England Premier League": {"Chelsea"}})
    assert out.team.tolist() == ["Arsenal"]
