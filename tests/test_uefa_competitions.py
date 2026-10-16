"""UEFA Europa League and Conference League: the same cross-border code path as the Champions League.
Reviewed club mapping, calendar parsing (non-ASCII names, home-club time zones), manual results files."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from shots.export import league_id
from shots.ingest import calendar_ics as cal
from shots.ingest import cross_border as xb
from shots.ingest import manual_results as mr
from shots.ingest.schema import normalize_team

ROOT = Path(__file__).resolve().parents[1]
UEFA_CAL = Path(__file__).parent / "fixtures" / "calendars_uefa"
UEL, UECL = "UEFA Europa League", "UEFA Conference League"
HEADER = ",".join(mr.COLUMNS)


def test_competitions_are_registered_generically():
    assert xb.COMPETITIONS["uefa-europa-league"].name == UEL
    assert xb.COMPETITIONS["uefa-conference-league"].name == UECL
    labels = [c.label for c in xb.COMPETITIONS.values()]
    assert len(set(labels)) == len(labels)
    assert all(c.country == "Europe" for c in xb.COMPETITIONS.values())
    assert {league_id(UEL), league_id(UECL)} == {"uefa-europa-league", "uefa-conference-league"}
    assert xb.is_cross_border(UEL) and xb.is_cross_border(UECL)


def test_every_club_of_the_season_calendars_is_reviewed():
    raw = cal.read_calendars(ROOT / "calendars")
    for div, name, n_matches in (("uefa-europa-league", UEL, 126), ("uefa-conference-league", UECL, 108)):
        g = raw[raw["div"] == div]
        assert len(g) == n_matches
        clubs = {normalize_team(cal.SOURCE, n) for n in set(g.home_raw) | set(g.away_raw)}
        assert len(clubs) == 36
        assert sorted(c for c in clubs if xb.club_country(name, c) is None) == []
    table = xb.club_table()
    assert set(table["country"]) <= set(cal.TIMEZONES)
    assert not table.duplicated(["competition", "club"]).any()


@pytest.mark.parametrize("comp, club, country, history", [
    (UEL, "Celtic", "Scotland", True), (UEL, "Sociedad", "Spain", True), (UEL, "Celta", "Spain", True),
    (UEL, "Hoffenheim", "Germany", True), (UEL, "Leverkusen", "Germany", True), (UEL, "Milan", "Italy", True),
    (UEL, "Olympiakos", "Greece", True), (UEL, "Besiktas", "Turkey", True), (UEL, "St. Gilloise", "Belgium", True),
    (UEL, "Nijmegen", "Netherlands", True), (UEL, "AZ Alkmaar", "Netherlands", True),
    (UEL, "OFI Crete", "Greece", True), (UEL, "Sunderland", "England", True),
    (UEL, "Torreense", "Portugal", False),  # second tier: no shot data
    (UEL, "Lech Poznan", "Poland", False), (UEL, "Hapoel Beer Sheva", "Israel", False),
    (UECL, "Hearts", "Scotland", True), (UECL, "Freiburg", "Germany", True), (UECL, "St Truiden", "Belgium", True),
    (UECL, "Sp Braga", "Portugal", True), (UECL, "Panathinaikos", "Greece", True), (UECL, "Brighton", "England", True),
    (UECL, "Inter Club d'Escaldes", "Andorra", False),  # not FC Andorra (Spain, Segunda)
    (UECL, "Kauno Žalgiris", "Lithuania", False), (UECL, "Egnatia Rrogozhinë", "Albania", False),
])
def test_reviewed_mapping_of_tricky_clubs(comp, club, country, history):
    assert xb.club_country(comp, club) == country
    assert xb.club_has_history(comp, club) is history


def test_aliases_resolve_to_canonical_names():
    for raw, canon in {"Olympiakos Piraeus": "Olympiakos", "NEC Nijmegen": "Nijmegen", "SC Braga": "Sp Braga",
                       "OFI": "OFI Crete", "Heart Of Midlothian": "Hearts", "Real Sociedad": "Sociedad",
                       "Celta Vigo": "Celta", "1899 Hoffenheim": "Hoffenheim", "SC Freiburg": "Freiburg",
                       "Bayer Leverkusen": "Leverkusen", "AC Milan": "Milan", "Beşiktaş": "Besiktas",
                       "St. Truiden": "St Truiden", "Union St. Gilloise": "St. Gilloise"}.items():
        assert normalize_team(cal.SOURCE, raw) == canon


def fixtures() -> pd.DataFrame:
    return cal.to_canonical(cal.read_calendars(UEFA_CAL))


def test_calendars_parse_with_non_ascii_names_ids_and_local_times():
    fx = fixtures()
    assert fx.groupby("competition").size().to_dict() == {UEL: 5, UECL: 4}
    assert (fx["country"] == "Europe").all() and fx["match_id"].is_unique
    by = fx.set_index(["competition", "home_team"])
    lyon = by.loc[(UEL, "Lyon")]
    assert (lyon.home_team_id, lyon.away_team_id) == ("france:lyon", "england:crystal-palace")
    assert lyon.kickoff == "18:45" and lyon["round"] == "League Stage - 2"  # Paris is UTC+2 in October
    bes = by.loc[(UEL, "Besiktas")]
    assert (bes.home_team_id, bes.away_team_id) == ("turkey:besiktas", "cyprus:omonia-nicosia")
    assert bes.kickoff == "22:00" and bes.date == pd.Timestamp("2026-10-22")  # Istanbul is UTC+3
    assert by.loc[(UEL, "Sociedad")].away_team_id == "scotland:celtic"
    assert by.loc[(UEL, "Torreense")].home_team_id == "portugal:torreense"
    kg = by.loc[(UECL, "Kauno Žalgiris")]
    assert (kg.home_team_id, kg.away_team_id) == ("lithuania:kauno-zalgiris", "germany:freiburg")
    assert kg.kickoff == "20:00"  # Vilnius is UTC+3
    hearts = by.loc[(UECL, "Hearts")]
    assert hearts.away_team == "Egnatia Rrogozhinë" and hearts.away_country == "Albania"
    assert hearts.kickoff == "20:00"  # London is UTC+1 on 15 Oct
    braga = by.loc[(UECL, "Sp Braga")]
    assert braga.away_team_id == "kazakhstan:kairat-almaty" and braga.kickoff == "18:00"  # Lisbon UTC+1


def test_build_reports_unknown_clubs_without_failing(tmp_path, monkeypatch, caplog):
    text = (UEFA_CAL / "uefa-europa-league_2026-27.ics").read_text(encoding="utf-8").replace("Lillestrom", "Nowhere FC")
    (tmp_path / "uefa-europa-league_2026-27.ics").write_text(text, encoding="utf-8")
    pd.DataFrame({"season": ["2026/2027"], "country": ["England"], "home_team": ["Crystal Palace"],
                  "away_team": ["Sunderland"]}).to_parquet(tmp_path / "m.parquet")
    monkeypatch.setattr(cal.fd, "OUT_PATH", tmp_path / "m.parquet")
    with caplog.at_level("INFO"):
        fx = cal.build(tmp_path, tmp_path / "f.parquet", tmp_path / "f.csv")
    assert len(fx) == 5 and "Nowhere FC [unknown]" in caplog.text
    row = fx[fx.home_team == "Nowhere FC"].iloc[0]
    assert row.home_team_id == "europe:nowhere-fc" and row.kickoff == "17:45"  # UTC fallback, never guessed


@pytest.mark.parametrize("prefix, comp", [("uefa-europa-league", UEL), ("uefa-conference-league", UECL)])
def test_manual_results_files_for_the_new_competitions(tmp_path, prefix, comp):
    committed = mr.COMMITTED_DIR / f"{prefix}_2026-27.csv"
    lines = committed.read_text(encoding="utf-8").splitlines()
    assert lines[0] == HEADER
    f = mr.read_file(committed)
    assert f.competition.name == comp and f.season_start == 2026
    assert f.rows.empty == (len([x for x in lines if x.strip()]) <= 1)
    p = tmp_path / f"{prefix}_2026-27.csv"
    p.write_text(HEADER + "\n2026-10-15,Lyon,Crystal Palace,15,9,5,3,2,1,https://www.espn.com/soccer/x,"
                 "2026-10-16T09:00:00Z,\n", encoding="utf-8")
    r = mr.read_file(p).rows.iloc[0]
    assert (r.home_team, r.away_team, r.home_shots) == ("Lyon", "Crystal Palace", 15)
    bad = tmp_path / "uefa-super-cup_2026-27.csv"
    bad.write_text(HEADER + "\n", encoding="utf-8")
    with pytest.raises(mr.ManualResultsError):
        mr.read_file(bad)
