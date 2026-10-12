"""Pre-match integrity (predictions.pre_match, official picks), kick-off time zones, and the board
moving on once a match is played."""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select, text, update

from shots.db import load
from shots.db.engine import make_engine, session_scope
from shots.db.models import Match, Prediction
from shots.db.repo import Repository
from shots.ingest.schema import make_match_id
from tests.test_db_load import fixtures, match, played, write_picks
from tests.test_web import META, row

# The match is always in the future: a prediction counts as pre-match only if it is also INSERTED (DB
# clock) before the lock moment (migration 0003), so fixed past dates would make these tests expire.
DAY = date.today() + timedelta(days=10)
DAY_S, NEXT_S = DAY.isoformat(), (DAY + timedelta(days=1)).isoformat()
KICKOFF = datetime.combine(DAY, time(14, 0))  # UTC
MID = make_match_id("Spain La Liga", pd.Timestamp(DAY_S), "Betis", "Getafe")


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


def local(utc: datetime) -> datetime:
    """UTC instant -> this machine's naive wall clock (the pipeline stamps runs in local time)."""
    return utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def meta_at(utc: datetime) -> dict:
    return dict(META, built_at=local(utc).isoformat(timespec="seconds"))


def board(mid=MID, day=DAY_S, ko="16:00", home="Betis", away="Getafe"):
    return pd.DataFrame([row(mid, day, ko, "Spain La Liga", "Spain", 1, home, away, 27)])


def scheduled(engine, kickoff_utc=KICKOFF):
    fx = fixtures([("Spain La Liga", "Spain", 1, DAY_S, "Betis", "Getafe", "16:00")])
    fx["kickoff_utc"] = pd.Timestamp(kickoff_utc) if kickoff_utc else pd.NaT
    if kickoff_utc is None:
        fx["kickoff"] = None
    load.upsert_fixtures(engine, fx, today=date.today())


def flags(engine):
    with engine.connect() as c:
        return c.execute(select(Prediction.run_id, Prediction.pre_match).order_by(Prediction.id)).all()


# ----------------------------------------------------------------------------- pre_match flag

def test_board_built_before_kickoff_is_pre_match_after_is_not(engine):
    scheduled(engine)
    load.import_board(engine, board(), meta_at(KICKOFF - timedelta(hours=5)))
    load.import_board(engine, board().assign(expected_shots=28.0), meta_at(KICKOFF + timedelta(minutes=10)))
    assert [f for _, f in flags(engine)] == [True, False]


def test_unknown_kickoff_uses_the_rows_time_conservatively(engine):
    """No kickoff_utc in the DB: the board's "16:00" may be Madrid or UK time; the earlier reading
    (16:00 Madrid, e.g. 14:00 UTC in summer time) is the lock moment."""
    scheduled(engine, kickoff_utc=None)
    lock = datetime.combine(DAY, time(16, 0), ZoneInfo("Europe/Madrid")).astimezone(timezone.utc).replace(tzinfo=None)
    load.import_board(engine, board(), meta_at(lock - timedelta(minutes=10)))
    load.import_board(engine, board().assign(expected_shots=28.0), meta_at(lock + timedelta(minutes=5)))
    assert [f for _, f in flags(engine)] == [True, False]


def test_unknown_kickoff_and_time_locks_at_midnight_utc(engine):
    scheduled(engine, kickoff_utc=None)
    midnight = datetime.combine(DAY, time(0, 0))
    load.import_board(engine, board(ko=""), meta_at(midnight - timedelta(minutes=30)))
    load.import_board(engine, board(ko="").assign(expected_shots=28.0), meta_at(midnight + timedelta(minutes=30)))
    assert [f for _, f in flags(engine)] == [True, False]


def test_lock_moment_rules():
    d = date(2026, 10, 17)
    assert load.lock_moment(d, datetime(2026, 10, 17, 16, 0)) == datetime(2026, 10, 17, 16, 0)
    assert load.lock_moment(d, None, "19:00", "Turkey") == datetime(2026, 10, 17, 16, 0)  # Istanbul UTC+3 < UK
    assert load.lock_moment(d, None, "15:00", "England") == datetime(2026, 10, 17, 14, 0)
    assert load.lock_moment(d, None, "", "Spain") == datetime(2026, 10, 17, 0, 0)
    assert load.lock_moment(d, None, "TBD", None) == datetime(2026, 10, 17, 0, 0)


def test_view_rechecks_against_a_later_kickoff_change(engine):
    scheduled(engine)
    load.import_board(engine, board(), meta_at(KICKOFF - timedelta(hours=2)))
    with engine.connect() as c:
        assert c.execute(text("SELECT pre_match FROM v_prediction_outcomes")).scalar() == 1
    with session_scope(engine) as s:  # kick-off brought forward, before the run was built
        s.execute(update(Match).where(Match.match_id == MID).values(kickoff_utc=KICKOFF - timedelta(hours=3)))
    with engine.connect() as c:
        assert c.execute(text("SELECT pre_match FROM v_prediction_outcomes")).scalar() == 0
        assert c.execute(select(Prediction.pre_match)).scalar() is True  # stored flag never rewritten


# ----------------------------------------------------------------------------- official picks

def test_official_picks_count_only_pre_match_scans(engine, tmp_path):
    scheduled(engine)
    other = make_match_id("Spain La Liga", pd.Timestamp(DAY_S), "Sevilla", "Celta")
    d = tmp_path / "picks"
    d.mkdir()
    before, after = local(KICKOFF - timedelta(hours=5)), local(KICKOFF + timedelta(hours=4))
    write_picks(d / f"{before:%Y-%m-%d_%H%M}_at_least_24.csv",
                [(DAY_S, "16:00", "Spain La Liga", "Betis", "Getafe", 0.66, 25.0, MID)])
    write_picks(d / f"{after:%Y-%m-%d_%H%M}_at_least_24.csv",  # same day, after kick-off
                [(DAY_S, "16:00", "Spain La Liga", "Betis", "Getafe", 0.99, 25.0, MID),
                 (DAY_S, "16:00", "Spain La Liga", "Sevilla", "Celta", 0.90, 25.0, other)])
    load.import_picks_dir(engine, d)
    load.upsert_matches(engine, played([("Spain La Liga", "Spain", 1, DAY_S, "Betis", "Getafe", 14, 12),
                                        ("Spain La Liga", "Spain", 1, DAY_S, "Sevilla", "Celta", 9, 8)]))
    rec = Repository(engine).track_records("live").set_index(["match_id", "n"])
    assert rec.loc[(MID, 24), "p"] == 0.66 and bool(rec.loc[(MID, 24), "hit"]) is True
    assert (other, 24) not in rec.index  # only ever predicted after its kick-off: not in the record
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM v_official_picks")).scalar() == 1
        assert sorted(c.execute(text("SELECT pre_match FROM v_prediction_outcomes")).scalars()) == [0, 0, 1]


# ----------------------------------------------------------------------------- kick-off time zones

def fd_csv(path, rows):
    path.write_text("Div,Date,Time,HomeTeam,AwayTeam,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5\n"
                    + "".join(f"{r},1.5,4.0,6.0,1.8,2.0\n" for r in rows), encoding="utf-8")


def test_football_data_uk_times_become_league_local_and_utc(engine, tmp_path):
    fd_dir = tmp_path / "fx"
    fd_dir.mkdir()
    fd_csv(fd_dir / "fixtures_2026-10-08.csv", [
        "T1,17/10/2026,17:00,Galatasaray,Fenerbahce",    # BST 17:00 = 16:00 UTC = 19:00 Istanbul
        "SP1,17/10/2026,20:00,Betis,Getafe",             # BST 20:00 = 19:00 UTC = 21:00 Madrid
        "SP1,07/11/2026,20:00,Sevilla,Celta",            # after the clocks change: GMT 20:00 = 21:00 CET
        "T1,07/11/2026,17:00,Besiktas,Trabzonspor",      # Turkey has no DST: 17:00 UTC = 20:00
        "E0,17/10/2026,15:00,Fulham,Brentford",          # UK league: local = UK time
    ])
    fx = load.read_fixtures(date(2026, 10, 8), tmp_path / "no_calendar.parquet", fd_dir).set_index("home_team")
    want = {"Galatasaray": ("19:00", "2026-10-17 16:00"), "Betis": ("21:00", "2026-10-17 19:00"),
            "Sevilla": ("21:00", "2026-11-07 20:00"), "Besiktas": ("20:00", "2026-11-07 17:00"),
            "Fulham": ("15:00", "2026-10-17 14:00")}
    for team, (ko, utc) in want.items():
        assert (fx.loc[team, "kickoff"], fx.loc[team, "kickoff_utc"]) == (ko, pd.Timestamp(utc)), team
    load.upsert_fixtures(engine, fx.reset_index(), today=date(2026, 10, 8))
    m = match(engine, fx.loc["Galatasaray", "match_id"])
    assert (m["kickoff_local"], m["kickoff_utc"]) == ("19:00", datetime(2026, 10, 17, 16, 0))


def test_calendar_kickoff_is_never_replaced_by_a_uk_time(engine, tmp_path):
    cal = fixtures([("Turkey Super Lig", "Turkey", 1, "2026-10-17", "Galatasaray", "Fenerbahce", "20:00")])
    cal["status"] = "To be played"
    cal.to_parquet(tmp_path / "fixtures.parquet", index=False)
    fd_dir = tmp_path / "fx"
    fd_dir.mkdir()
    fd_csv(fd_dir / "fixtures_2026-10-08.csv", ["T1,17/10/2026,18:00,Galatasaray,Fenerbahce"])
    fx = load.read_fixtures(date(2026, 10, 8), tmp_path / "fixtures.parquet", fd_dir)
    assert (fx.loc[0, "kickoff"], fx.loc[0, "kickoff_utc"]) == ("20:00", pd.Timestamp("2026-10-17 17:00"))
    assert fx.loc[0, "odds_over25"] == 1.8  # odds still come from football-data
    load.upsert_fixtures(engine, fx, today=date(2026, 10, 8))
    mid = fx.loc[0, "match_id"]
    # the board carries football-data's UK "18:00" for this match: it must not overwrite the calendar time
    b = pd.DataFrame([row(mid, "2026-10-17", "18:00", "Turkey Super Lig", "Turkey", 1, "Galatasaray",
                          "Fenerbahce", 25, rnd="Week 9")])
    load.import_board(engine, b, META)
    m = match(engine, mid)
    assert (m["kickoff_local"], m["kickoff_utc"], m["round"]) == ("20:00", datetime(2026, 10, 17, 17, 0), "Week 9")


def test_board_kickoff_only_fills_a_match_without_one(engine):
    scheduled(engine, kickoff_utc=None)
    load.import_board(engine, board(ko="16:00"), META)
    assert match(engine, MID)["kickoff_local"] == "16:00"  # best effort for display
    assert match(engine, MID)["kickoff_utc"] is None  # zone unknown: no UTC instant invented


# ----------------------------------------------------------------------------- the board moves on

FILTERS = SimpleNamespace(leagues=(), groups=(), n=24, smin=None, smax=None, pmin=0, when="", odds=False,
                          nocold=False, q="", sort="", side="", tn=12, tp=0)


def test_played_fixture_leaves_the_upcoming_board(engine):
    today = DAY
    nxt = make_match_id("Spain La Liga", pd.Timestamp(NEXT_S), "Sevilla", "Celta")
    b = pd.concat([board(), board(nxt, NEXT_S, "18:00", "Sevilla", "Celta")], ignore_index=True)
    load.import_board(engine, b, META)
    repo = Repository(engine)
    assert [r["match_id"] for r in repo.latest_board(FILTERS, today).rows] == [MID, nxt]
    # result loaded (the board step may not have rebuilt yet): the match leaves the board at once
    load.upsert_matches(engine, played([("Spain La Liga", "Spain", 1, DAY_S, "Betis", "Getafe", 14, 12)]))
    view = repo.latest_board(FILTERS, today)
    assert [r["match_id"] for r in view.rows] == [nxt] and view.total == 1
    # next day: the window rolls forward, and the rebuilt board (without the played match) is shown
    later = dict(META, built_at=f"{NEXT_S}T09:00:00")
    load.import_board(engine, board(nxt, NEXT_S, "18:00", "Sevilla", "Celta"), later)
    assert [r["match_id"] for r in repo.latest_board(FILTERS, DAY + timedelta(days=1)).rows] == [nxt]
    assert repo.latest_board(FILTERS, DAY + timedelta(days=2)).rows == []
