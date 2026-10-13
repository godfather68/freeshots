"""Cross-border competitions (UEFA Champions League), end to end: club mapping and calendar ingest
(non-ASCII names, unknown clubs), features from domestic history with domestic features untouched,
the experimental model, board / scan routing, the database flag and its exclusion from the official
record, and the additive export fields."""

from __future__ import annotations

import json
import pickle
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shots.db import load, migrate
from shots.db.engine import make_engine
from shots.db.repo import Repository
from shots.export import export
from shots.export import verify as verify_mod
from shots.features import cross_border as cbf
from shots.features import team_form as tf
from shots.ingest import calendar_ics as cal
from shots.ingest import cross_border as xb
from shots.ingest.schema import CANONICAL_COLUMNS, make_match_id, normalize_team
from shots.models.calibrated import NegBinPitComp
from shots.models.cross_border import RATIO_COLS, CrossBorderModel, make_profiles
from shots.predict import board as bd
from shots.predict import scan as sc
from shots.predict import track
from tests.test_board import StubModel
from tests.test_db_integrity import local
from tests.test_db_load import fixtures, played, write_picks
from tests.test_models import SEASONS, synthetic
from tests.test_web import META, row
from tests.test_web_team import with_team

ROOT = Path(__file__).resolve().parents[1]
XB_CAL = Path(__file__).parent / "fixtures" / "calendars_xb"
UCL = "UEFA Champions League"
SCHEMA = json.loads((ROOT / "docs" / "static-api.schema.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- mapping / ingest

def test_club_mapping_is_reviewed_data_in_the_package():
    table = xb.club_table()
    assert not table.duplicated(["competition", "club"]).any()
    ucl = table[table["competition"] == UCL]
    assert len(ucl) == 36 and int(ucl["history"].sum()) == 29
    assert xb.club_country(UCL, "Sp Lisbon") == "Portugal" and xb.club_has_history(UCL, "Sp Lisbon")
    assert xb.club_country(UCL, "Bodo/Glimt") == "Norway" and not xb.club_has_history(UCL, "Bodo/Glimt")
    assert xb.club_country(UCL, "Nobody FC") is None and not xb.club_has_history(UCL, "Nobody FC")
    # every country of the mapping has a time zone, and every zone exists on this platform
    assert set(ucl["country"]) <= set(cal.TIMEZONES)
    for zone in cal.TIMEZONES.values():
        assert cal._zone(zone) is not None
    assert xb.is_cross_border(UCL) and not xb.is_cross_border("England Premier League")


def test_data_files_are_utf8_whatever_the_platform_default():
    for name in ("team_aliases.csv", "club_countries.csv"):
        raw = (ROOT / "src" / "shots" / "ingest" / name).read_bytes()
        raw.decode("utf-8")  # raises if not UTF-8
    assert normalize_team("calendar", "Fenerbahçe") == "Fenerbahce"
    assert normalize_team("calendar", "Bayern München") == "Bayern Munich"
    assert normalize_team("calendar", "Paris Saint Germain") == "Paris SG"
    assert normalize_team("calendar", "Sporting CP") == "Sp Lisbon"
    assert normalize_team("calendar", "FC Porto") == "Porto"
    assert normalize_team("calendar", "AEK Athens FC") == "AEK"


def xb_fixtures() -> pd.DataFrame:
    return cal.to_canonical(cal.read_calendars(XB_CAL))


def test_calendar_resolves_clubs_to_domestic_ids_and_home_club_time():
    fx = xb_fixtures().set_index("home_team")
    assert (fx["competition"] == UCL).all() and (fx["country"] == "Europe").all() and (fx["tier"] == 1).all()
    villa = fx.loc["Aston Villa"]
    assert (villa.home_team_id, villa.away_team, villa.away_team_id) == ("england:aston-villa", "Fenerbahce", "turkey:fenerbahce")
    assert (villa.home_country, villa.away_country) == ("England", "Turkey")
    assert villa.date == pd.Timestamp("2026-10-14") and villa.kickoff == "20:00"  # 19:00 UTC, London (BST)
    assert villa.kickoff_utc == pd.Timestamp("2026-10-14 19:00")
    porto = fx.loc["Porto"]
    assert (porto.home_team_id, porto.away_team_id) == ("portugal:porto", "france:paris-sg")
    assert porto.kickoff == "20:00"  # Lisbon is UTC+0 in November
    assert fx.loc["Viking", "home_team_id"] == "norway:viking" and fx.loc["Viking", "kickoff"] == "21:00"
    sabah = fx.loc["Sabah FA"]
    assert (sabah.home_team_id, sabah.away_team_id) == ("azerbaijan:sabah-fa", "czechia:slavia-praha")
    assert sabah.kickoff == "20:45" and sabah.date == pd.Timestamp("2026-10-13")  # Baku, UTC+4
    # a club that is not in the mapping: own id under the competition's label, UTC is never guessed away
    unk = fx.loc["Sp Lisbon"]
    assert unk.away_team == "Ünknown Çlub" and unk.away_team_id == "europe:unknown-club" and unk.away_country == "Europe"
    assert xb_fixtures()["match_id"].is_unique
    assert league_slug(UCL) == "uefa-champions-league"


def league_slug(name):
    from shots.export import league_id

    return league_id(name)


def domestic_history() -> pd.DataFrame:
    """Two seasons of results for a few clubs in three leagues (shots only)."""
    rng = np.random.default_rng(5)
    leagues = {("England Premier League", "England"): ["Aston Villa", "Arsenal", "Fulham", "Leeds"],
               ("Turkey Super Lig", "Turkey"): ["Fenerbahce", "Galatasaray", "Besiktas", "Goztep"],
               ("Portugal Primeira Liga", "Portugal"): ["Porto", "Sp Lisbon", "Benfica", "Estoril"],
               ("France Ligue 1", "France"): ["Paris SG", "Lille", "Lens", "Lyon"],
               ("Germany Bundesliga", "Germany"): ["Bayern Munich", "Dortmund", "Stuttgart", "RB Leipzig"]}
    rows = []
    for (comp, country), teams in leagues.items():
        for season, start in (("2025/2026", "2025-08-10"), ("2026/2027", "2026-08-09")):
            for week in range(30 if season == "2025/2026" else 8):
                order = list(rng.permutation(teams))
                day = pd.Timestamp(start) + pd.Timedelta(days=7 * week)
                for i in (0, 2):
                    rows.append(dict(competition=comp, country=country, tier=1, season=season, date=day,
                                     home_team=order[i], away_team=order[i + 1],
                                     home_shots=int(rng.integers(6, 24)), away_shots=int(rng.integers(5, 20))))
    df = pd.DataFrame(rows)
    df["match_id"] = [make_match_id(c, d, h, a) for c, d, h, a in zip(df.competition, df.date, df.home_team, df.away_team)]
    df["source"] = "football-data"
    from shots.ingest.schema import make_team_id

    df["home_team_id"] = [make_team_id(c, t) for c, t in zip(df.country, df.home_team)]
    df["away_team_id"] = [make_team_id(c, t) for c, t in zip(df.country, df.away_team)]
    df["home_sot"], df["away_sot"] = df.home_shots // 3, df.away_shots // 3
    df["home_goals"], df["away_goals"] = 1, 1
    for c in ("odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"):
        df[c] = np.nan
    return df[CANONICAL_COLUMNS].sort_values("date").reset_index(drop=True)


def test_calendar_build_reports_unknown_clubs_without_failing(tmp_path, monkeypatch, caplog):
    domestic_history().to_parquet(tmp_path / "matches.parquet")
    monkeypatch.setattr(cal.fd, "OUT_PATH", tmp_path / "matches.parquet")
    with caplog.at_level("INFO"):
        fx = cal.build(XB_CAL, tmp_path / "fixtures.parquet", tmp_path / "fixtures.csv")
    assert len(fx) == 5
    report = cal.cross_border_report(fx, domestic_history()).set_index("club")["status"].to_dict()
    assert report["Aston Villa"] == "ok" and report["Fenerbahce"] == "ok"
    assert report["Viking"] == "no_data" and report["Slavia Praha"] == "no_data"
    assert report["Ünknown Çlub"] == "unknown"
    assert "Ünknown Çlub [unknown]" in caplog.text and "need review" in caplog.text
    # clubs whose league is knowingly not covered are not reported as unmapped names
    assert "Viking" not in set(cal.unmapped(fx, domestic_history())["team"])
    back = pd.read_parquet(tmp_path / "fixtures.parquet")
    assert {"kickoff_utc", "home_country", "away_country"} <= set(back.columns)
    assert "Ünknown Çlub" in (tmp_path / "fixtures.csv").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- features

SIDE_COLS = [c for c in cbf.SIDE_FEATURES]


def test_team_snapshot_equals_the_production_features():
    """The snapshot used for cross-border fixtures is the production feature definition: for every
    played match and both sides it reproduces team_form.build_features (given the match's own tier)."""
    m = domestic_history()
    feats = tf.build_features(m).set_index("match_id")
    prep = cbf._prepare(m)
    hists = cbf.team_histories(prep, pd.concat([prep.home_team_id, prep.away_team_id]).unique())
    levels = cbf.LeagueLevels(prep)
    checked = 0
    for r in prep.iloc[::3].itertuples(index=False):
        want = feats.loc[r.match_id]
        prior, n = levels.prior(r.competition, r.season, r.date)
        if np.isnan(prior):
            assert np.isnan(want.league_prior_shots)
        else:
            assert prior == pytest.approx(want.league_prior_shots, abs=1e-12) and n == want.league_prior_n
        for side in ("home", "away"):
            snap = cbf.team_snapshot(hists[getattr(r, f"{side}_team_id")], r.date, side == "home", r.season, prior,
                                     tier=r.tier)
            for c in SIDE_COLS:
                a, b = snap[c], want[f"{side}_{c}"]
                assert (np.isnan(a) and np.isnan(b)) or a == pytest.approx(b, abs=1e-9), (c, a, b)
            checked += 1
    assert checked > 100


def test_cross_border_features_use_domestic_history_and_combine_league_levels():
    m = domestic_history()
    fx = xb_fixtures()
    out = cbf.build_cross_border_features(m, fx).set_index("home_team")
    villa = out.loc["Aston Villa"]
    dom = tf.build_features(m)
    # Aston Villa's rates are its domestic ones: equal to what its next domestic home match would see
    hist = cbf.team_histories(cbf._prepare(m), ["england:aston-villa"])["england:aston-villa"]
    assert villa.home_shots_for_ewm == pytest.approx(hist["shots_for"].ewm(halflife=5, ignore_na=True).mean().iloc[-1])
    assert villa.home_n_prior_matches == len(hist) and villa.home_tier_change == 0.0
    assert villa.home_domestic == "England Premier League" and villa.away_domestic == "Turkey Super Lig"
    levels = cbf.LeagueLevels(cbf._prepare(m))
    eng = levels.prior("England Premier League", "2026/2027", "2026-10-14")[0]
    tur = levels.prior("Turkey Super Lig", "2026/2027", "2026-10-14")[0]
    assert villa.home_league_prior == pytest.approx(eng) and villa.away_league_prior == pytest.approx(tur)
    assert villa.league_prior_shots == pytest.approx((eng + tur) / 2) and villa.data_quality == "full"
    assert np.isnan(villa.mkt_p_home) and np.isnan(villa.mkt_p_over25)
    # shrunk rates shrink toward the club's own league
    assert villa.away_shots_for_shrunk == pytest.approx((10 * villa.away_shots_for_l10 + 5 * tur) / 15)
    # one unknown club: no features for it, the level is the known club's league
    viking = out.loc["Viking"]
    assert viking.data_quality == "partial" and not viking.home_known and viking.away_known
    assert np.isnan(viking.home_shots_for_ewm) and viking.home_n_prior_matches == 0
    assert viking.league_prior_shots == pytest.approx(levels.prior("Germany Bundesliga", "2026/2027", "2026-10-13")[0])
    # neither club known: the mean level of the known clubs of the competition
    sabah = out.loc["Sabah FA"]
    assert sabah.data_quality == "none"
    known = pd.concat([out["home_league_prior"], out["away_league_prior"]]).mean()
    assert sabah.league_prior_shots == pytest.approx(known)
    assert out.loc["Sp Lisbon", "data_quality"] == "partial"  # the unmapped club has no history
    # no leakage: results on or after the fixture date change nothing
    later = m.copy()
    late = later["date"] >= pd.Timestamp("2026-10-13")
    assert not late.any()
    extra = m.iloc[-4:].assign(date=pd.Timestamp("2026-10-14"), home_shots=40, away_shots=40,
                               match_id=lambda d: d["match_id"] + "x")
    again = cbf.build_cross_border_features(pd.concat([m, extra]), fx).set_index("home_team")
    cols = [c for c in out.columns if c.startswith(("home_shots", "away_shots", "league_prior"))]
    pd.testing.assert_series_equal(out.loc["Viking", cols], again.loc["Viking", cols])  # 13 Oct: unaffected
    assert len(dom) == len(m)


def scan_inputs(with_xb: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    m = domestic_history()
    base = dict(source="calendar", season="2026/2027", tier=1)
    dom = pd.DataFrame([
        dict(base, competition="England Premier League", country="England", date=pd.Timestamp("2026-10-17"),
             home_team="Aston Villa", away_team="Arsenal", kickoff="15:00"),
        dict(base, competition="Turkey Super Lig", country="Turkey", date=pd.Timestamp("2026-10-18"),
             home_team="Fenerbahce", away_team="Besiktas", kickoff="19:00"),
        dict(base, competition="England Premier League", country="England", date=pd.Timestamp("2026-10-24"),
             home_team="Fulham", away_team="Aston Villa", kickoff="15:00")])
    from shots.ingest.schema import make_team_id

    dom["home_team_id"] = [make_team_id(c, t) for c, t in zip(dom.country, dom.home_team)]
    dom["away_team_id"] = [make_team_id(c, t) for c, t in zip(dom.country, dom.away_team)]
    dom["match_id"] = [make_match_id(c, d, h, a) for c, d, h, a in zip(dom.competition, dom.date, dom.home_team, dom.away_team)]
    for c in set(CANONICAL_COLUMNS) - set(dom.columns):
        dom[c] = np.nan
    dom = dom[CANONICAL_COLUMNS + ["kickoff"]]
    x = xb_fixtures().drop(columns=["round", "status", "kickoff_utc", "home_country", "away_country"])
    dom = dom.astype({c: x[c].dtype for c in dom.columns if c != "date"})  # one calendar source: same dtypes
    return m, (pd.concat([dom, x], ignore_index=True) if with_xb else dom)


@pytest.fixture
def stub_models(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "FEATURES_PATH", tmp_path / "features.parquet")
    monkeypatch.setattr(sc, "STORE", tmp_path / "store")
    monkeypatch.setattr(sc, "latest_model", lambda name, last, age: tmp_path / "negbin_pit_comp_2026-10-01")
    monkeypatch.setattr(sc, "load", lambda p: HintModel(team=True))
    monkeypatch.setattr(sc, "latest_no_odds_model", lambda name, played, age: (HintModel(-2.0, team=True), "noodds_label"))
    monkeypatch.setattr(sc, "cross_border_model", lambda nomodel, nolabel, played, persist=True: HintModel(-5.0, team=True))
    return tmp_path


class HintModel(StubModel):
    """StubModel driven by the league prior instead of a `hint` column; optional team methods."""

    def __init__(self, shift=0.0, team=False):
        super().__init__(shift)
        self.has_team_calibration = team

    def _mu(self, rows):
        return 2 * rows["league_prior_shots"].to_numpy(float) + self.shift

    def team_predict(self, rows, side):
        mu = self._mu(rows) / 2
        return pd.DataFrame({"mean": mu, "median": np.round(mu), "lb_70": np.round(mu - 2), "lb_80": np.round(mu - 3),
                             "lb_90": np.round(mu - 4)}, index=rows.index)

    def team_prob_at_least(self, rows, side, n):
        mu = self._mu(rows) / 2
        return 1 / (1 + np.exp((np.broadcast_to(np.asarray(n), mu.shape) - mu) / 2))


def test_domestic_features_are_identical_with_cross_border_fixtures(stub_models, monkeypatch):
    """Cross-border fixtures never reach the domestic feature pass: the frame given to
    build_features and every domestic feature value are the same with and without them."""
    seen = []
    real = tf.build_features

    def spy(combined):
        seen.append(combined)
        return real(combined)

    monkeypatch.setattr(sc, "build_features", spy)
    m, dom = scan_inputs(False)
    _, both = scan_inputs(True)
    a = sc.prepare_upcoming(dom, m)
    b = sc.prepare_upcoming(both, m)
    pd.testing.assert_frame_equal(seen[0], seen[1])
    assert not seen[1]["competition"].map(xb.is_cross_border).any()
    assert a.experimental is None and b.experimental.sum() == 5 and len(b.upcoming) == len(a.upcoming) + 5
    n = len(a.upcoming)
    pd.testing.assert_frame_equal(a.upcoming, b.upcoming.iloc[:n][a.upcoming.columns], check_dtype=False)
    pd.testing.assert_frame_equal(a.played, b.played)
    # the later domestic fixture of a Champions League club is untouched by its European match in between
    late = a.upcoming.set_index("home_team").loc["Fulham"]
    late_b = b.upcoming.iloc[:n].set_index("home_team").loc["Fulham"]
    assert late.away_rest_days == late_b.away_rest_days and late.away_n_prior_matches == late_b.away_n_prior_matches
    # and the routed labels: domestic rows keep theirs, cross-border rows get the experimental label
    labels = {tuple(b.upcoming.loc[mask, "competition"].unique()): label for mask, _, _, label in b.routes()}
    assert labels[(UCL,)] == "noodds_label-ucl-exp"
    assert [label for mask, _, _, label in a.routes()] == ["noodds_label"]


# ----------------------------------------------------------------------------- model

def no_odds(f):
    f = f.copy()
    f[[c for c in f.columns if c.startswith("mkt_")]] = np.nan
    return f


@pytest.fixture(scope="module")
def fitted():
    df = no_odds(synthetic(n_per_season=1200, seed=4))
    train, test = df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]
    base = NegBinPitComp(min_cal_rows=800).fit(train)
    test = test.dropna(subset=["home_shots_for_ewm", "away_shots_for_ewm"]).head(300).copy()
    test["competition"] = UCL  # unseen by the calibration: global fallback
    return base, train, test


def test_full_data_rows_without_widening_equal_the_base_model(fitted):
    base, train, test = fitted
    m = CrossBorderModel.from_base(base, train, level_sd=0.0, split_sd=0.0)
    pd.testing.assert_frame_equal(m.predict(test), base.predict(test))
    np.testing.assert_allclose(m.prob_at_least(test, 24), base.prob_at_least(test, 24), atol=1e-12)
    for side in ("home", "away"):
        pd.testing.assert_frame_equal(m.team_predict(test, side), base.team_predict(test, side), check_exact=False, atol=1e-9)
        np.testing.assert_allclose(m.team_prob_at_least(test, side, 12), base.team_prob_at_least(test, side, 12), atol=1e-12)


def test_widening_lowers_bounds_but_not_the_centre(fitted):
    base, train, test = fitted
    plain = CrossBorderModel.from_base(base, train, level_sd=0.0, split_sd=0.0)
    wide = CrossBorderModel.from_base(base, train, level_sd=0.08, split_sd=0.15)
    a, b = plain.predict(test), wide.predict(test)
    assert (b["lb_90"] <= a["lb_90"]).all() and (b["lb_90"] < a["lb_90"]).mean() > 0.2
    assert abs(wide.mean(test).mean() / plain.mean(test).mean() - 1) < 0.005  # mean-preserving
    assert (b["median"] - a["median"]).abs().max() <= 1
    # survival curves stay valid and monotone
    curve = np.column_stack([wide.prob_at_least(test, n) for n in range(10, 46)])
    assert ((curve >= 0) & (curve <= 1)).all() and (np.diff(curve, axis=1) <= 1e-12).all()
    tcurve = np.column_stack([wide.team_prob_at_least(test, "home", n) for n in range(3, 26)])
    assert (np.diff(tcurve, axis=1) <= 1e-12).all()
    # the split uncertainty widens the per-team bounds
    assert wide.team_predict(test, "home")["lb_90"].mean() < plain.team_predict(test, "home")["lb_90"].mean()


def test_unknown_clubs_get_a_neutral_wider_distribution(fitted):
    base, train, test = fitted
    m = CrossBorderModel.from_base(base, train, level_sd=0.0, split_sd=0.0, n_profiles=120)
    rows = test.head(60)
    full = m.predict(rows.assign(home_known=True, away_known=True))
    partial_rows = rows.assign(home_known=False, away_known=True)
    # the hidden side's own features must not matter at all
    scrambled = partial_rows.copy()
    for c in [c for c in scrambled.columns if c.startswith("home_") and c not in ("home_known", "home_team", "home_shots")]:
        scrambled[c] = np.nan
    partial, blind = m.predict(partial_rows), m.predict(scrambled)
    pd.testing.assert_frame_equal(partial, blind)
    none = m.predict(rows.assign(home_known=False, away_known=False))
    spread = lambda p: (p["median"] - p["lb_90"]).mean()
    assert spread(full) < spread(partial) < spread(none)
    assert partial["lb_90"].mean() < full["lb_90"].mean()
    # neutral: on average the centre stays where the league level puts it
    assert abs(partial["median"].mean() - full["median"].mean()) < 1.5
    # with no club data only the league level can differ between rows
    same_level = m.predict(rows.assign(home_known=False, away_known=False, league_prior_shots=12.0))
    assert same_level.drop_duplicates().shape[0] == 1
    # per-row inference of "known" from missing features when the flags are absent
    auto = m.predict(scrambled.drop(columns=["home_known", "away_known"]))
    pd.testing.assert_frame_equal(auto, partial)


def test_profiles_are_deterministic_and_the_model_pickles(fitted, tmp_path):
    base, train, test = fitted
    p1, p2 = make_profiles(train, 50, seed=1), make_profiles(train.sample(frac=1.0, random_state=0), 50, seed=1)
    for side in ("home", "away"):
        pd.testing.assert_frame_equal(p1[side], p2[side])
        assert set(RATIO_COLS) <= set(p1[side].columns) and p1[side][RATIO_COLS].notna().all().all()
    m = CrossBorderModel.from_base(base, train)
    rows = test.head(20).assign(home_known=[True, False] * 10, away_known=True)
    before = m.predict(rows)
    assert "_memo_" in m.__dict__
    clone = pickle.loads(pickle.dumps(m))
    assert "_memo_" not in clone.__dict__
    pd.testing.assert_frame_equal(clone.predict(rows), before)
    from shots.models.persist import load as load_model
    from shots.models.persist import load_metadata, save

    save(m, tmp_path / "xb", train, base_model="noodds", status="experimental")
    meta = load_metadata(tmp_path / "xb")
    assert meta["status"] == "experimental" and meta["params"]["level_sd"] == m.level_sd and meta["training"]["n_rows"] == len(train)
    pd.testing.assert_frame_equal(load_model(tmp_path / "xb").predict(rows), before)
    with pytest.raises(RuntimeError):
        CrossBorderModel().fit(train)


# ----------------------------------------------------------------------------- board / scan routing

def test_board_and_scan_route_cross_border_fixtures_to_the_experimental_model(stub_models, monkeypatch, tmp_path):
    m, both = scan_inputs(True)
    _, dom = scan_inputs(False)
    m.to_parquet(tmp_path / "matches.parquet")
    monkeypatch.setattr(sc.fd, "OUT_PATH", tmp_path / "matches.parquet")
    monkeypatch.setattr(sc, "PICKS_DIR", tmp_path / "picks")
    monkeypatch.setattr(sc, "CALENDAR_PATH", tmp_path / "no_calendar.parquet")
    current = {"fx": both}
    monkeypatch.setattr(sc, "gather_fixtures", lambda start, days, use_calendar=True: current["fx"])
    board = bd.build(days=40, refresh=False, today=date(2026, 10, 12), out_path=tmp_path / "board.parquet")
    r = board.set_index("home_team")
    ucl = board[board["competition"] == UCL]
    assert len(ucl) == 5 and ucl["experimental"].all() and not board.loc[board["competition"] != UCL, "experimental"].any()
    assert (ucl["model"] == "noodds_label-ucl-exp").all() and (ucl["country"] == "Europe").all()
    assert not ucl["odds"].any()
    assert r.loc["Aston Villa", "data_quality"].tolist().count("full") == 2  # the domestic and the European fixture
    assert r.loc["Viking", "data_quality"] == "partial" and r.loc["Sabah FA", "data_quality"] == "none"
    assert r.loc["Viking", "cold_start"] and r.loc["Sabah FA", "cold_start"]
    # per-team numbers only where both clubs have data
    assert np.isnan(r.loc["Viking", "home_mean"]) and np.isnan(r.loc["Viking", "ph_10"]) and pd.isna(r.loc["Viking", "home_lb_80"])
    porto = r.loc["Porto"]
    assert porto.home_mean > 0 and 0 < porto.ph_10 < 1 and porto.data_quality == "full"
    meta = json.loads(bd.meta_path(tmp_path / "board.parquet").read_text())
    assert meta["n_experimental"] == 5 and "noodds_label-ucl-exp" in meta["models"]

    # the same build without the cross-border fixtures: domestic rows are value-for-value identical
    current["fx"] = dom
    plain = bd.build(days=40, refresh=False, today=date(2026, 10, 12), out_path=tmp_path / "plain.parquet")
    assert "experimental" not in plain.columns and "data_quality" not in plain.columns
    assert "n_experimental" not in json.loads(bd.meta_path(tmp_path / "plain.parquet").read_text())
    same = board[board["competition"] != UCL].reset_index(drop=True)[plain.columns]
    pd.testing.assert_frame_equal(same, plain.reset_index(drop=True))

    # scan: extra columns only when experimental fixtures are present; domestic rows unchanged
    out_plain = sc.scan(shots=24, min_prob=0.7, refresh=False, today=date(2026, 10, 12), days=40)
    assert "experimental" not in out_plain.columns
    current["fx"] = both
    out = sc.scan(shots=24, min_prob=0.7, refresh=False, today=date(2026, 10, 12), days=40)
    assert out["experimental"].sum() == 5 and set(out.loc[out["experimental"], "data_quality"]) == {"full", "partial", "none"}
    assert (out.loc[~out["experimental"], "data_quality"] == "full").all()
    dom_rows = out[~out["experimental"]].reset_index(drop=True)[out_plain.columns]
    pd.testing.assert_frame_equal(dom_rows, out_plain.reset_index(drop=True))
    # the file-based track record never counts experimental picks
    picks = track.load_picks(tmp_path / "picks")
    assert picks["experimental"].astype("boolean").fillna(False).any()
    kept = track.dedupe(picks.assign(scan_ts=pd.Timestamp("2026-10-01 09:00")))
    assert len(kept) and not kept["competition"].eq(UCL).any()


# ----------------------------------------------------------------------------- database

NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
D1, D2 = (date.today() + timedelta(days=k) for k in (3, 4))
DOM = ("Spain La Liga", "Spain", 1, D1, "Betis", "Getafe", "16:00")
EU1 = (UCL, "Europe", 1, D1, "Arsenal", "Lille", "20:00")
EU2 = (UCL, "Europe", 1, D2, "Viking", "Bayern Munich", "21:00")
M_DOM, M_EU1, M_EU2 = (make_match_id(c, pd.Timestamp(d), h, a) for c, _, _, d, h, a, _ in (DOM, EU1, EU2))


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


def db_fixtures() -> pd.DataFrame:
    fx = fixtures([(c, co, t, str(d), h, a, ko) for c, co, t, d, h, a, ko in (DOM, EU1, EU2)])
    fx["kickoff_utc"] = [pd.Timestamp(datetime.combine(d, datetime.min.time()).replace(hour=18)) for d in (D1, D1, D2)]
    fx["home_country"], fx["away_country"] = ["Spain", "England", "Norway"], ["Spain", "France", "Germany"]
    fx["home_team_id"] = ["spain:betis", "england:arsenal", "norway:viking"]
    fx["away_team_id"] = ["spain:getafe", "france:lille", "germany:bayern-munich"]
    return fx


def db_board() -> pd.DataFrame:
    rows = [row(M_DOM, str(D1), "16:00", *DOM[:3], "Betis", "Getafe", 27),
            row(M_EU1, str(D1), "20:00", *EU1[:3], "Arsenal", "Lille", 26, rnd="League Stage - 2"),
            row(M_EU2, str(D2), "21:00", *EU2[:3], "Viking", "Bayern Munich", 25, cold=True, rnd="League Stage - 2")]
    df = with_team(pd.DataFrame(rows))
    df["experimental"] = [False, True, True]
    df["data_quality"] = ["full", "full", "partial"]
    df.loc[df["match_id"] != M_DOM, "model"] = "negbin_pit_comp-noodds_x-ucl-exp"
    team_cols = [c for c in df.columns if c.startswith(("home_m", "home_lb", "away_m", "away_lb", "ph_", "pa_"))]
    df[team_cols] = df[team_cols].astype(float)
    df.loc[df["match_id"] == M_EU2, team_cols] = np.nan  # partial: no per-team numbers
    return df


def seed_db(engine, tmp_path) -> None:
    load.upsert_fixtures(engine, db_fixtures(), today=date.today())
    meta = dict(META, built_at=local(NOW - timedelta(hours=1)).isoformat(timespec="seconds"), results_up_to=str(date.today()))
    load.import_board(engine, db_board(), meta)
    picks = tmp_path / "picks"
    picks.mkdir(exist_ok=True)
    path = picks / f"{date.today():%Y-%m-%d}_0001_at_least_24.csv"
    write_picks(path, [(str(D1), "16:00", DOM[0], "Betis", "Getafe", 0.80, 27.0, M_DOM),
                       (str(D1), "20:00", UCL, "Arsenal", "Lille", 0.78, 26.0, M_EU1),
                       (str(D2), "21:00", UCL, "Viking", "Bayern Munich", 0.74, 25.0, M_EU2)])
    df = pd.read_csv(path, dtype={"kickoff": str})
    df["experimental"], df["data_quality"] = [False, True, True], ["full", "full", "partial"]
    df.to_csv(path, index=False)
    load.import_picks_dir(engine, picks)


def q(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


def test_predictions_are_stored_flagged_and_stay_out_of_the_official_record(engine, tmp_path):
    seed_db(engine, tmp_path)
    flags = dict(q(engine, "SELECT p.match_id || ':' || r.kind, p.experimental || ':' || COALESCE(p.data_quality, '-') "
                           "FROM predictions p JOIN prediction_runs r ON r.id = p.run_id"))
    assert flags == {f"{M_DOM}:board": "0:-", f"{M_EU1}:board": "1:full", f"{M_EU2}:board": "1:partial",
                     f"{M_DOM}:picks": "0:-", f"{M_EU1}:picks": "1:full", f"{M_EU2}:picks": "1:partial"}
    assert [r[0] for r in q(engine, "SELECT match_id FROM v_official_picks")] == [M_DOM]
    assert sorted(r[0] for r in q(engine, "SELECT match_id FROM v_experimental_picks")) == sorted([M_EU1, M_EU2])
    live = Repository(engine).track_records("live")
    assert live["match_id"].tolist() == [M_DOM] and live["status"].tolist() == ["pending"]
    # clubs keep their own country and domestic id; the competition is "Europe"
    teams = dict(q(engine, "SELECT team_id, country FROM teams"))
    assert teams["england:arsenal"] == "England" and teams["norway:viking"] == "Norway" and teams["france:lille"] == "France"
    assert not any(t.startswith("europe:") for t in teams)
    assert q(engine, "SELECT country, tier, code FROM competitions WHERE name = :n", n=UCL) == [("Europe", 1, None)]
    # kick-off instants come from the fixtures (no zone exists for "Europe"), so pre-match is exact
    assert all(r[0] == 1 for r in q(engine, "SELECT pre_match FROM predictions"))
    # append-only still holds for the new columns
    with pytest.raises(DBAPIError):
        with engine.begin() as c:
            c.execute(text("UPDATE predictions SET experimental = 0 WHERE match_id = :m"), {"m": M_EU1})

    # even if a result with shots appeared one day, an experimental prediction never becomes official
    res = played([(UCL, "Europe", 1, str(D1), "Arsenal", "Lille", 15, 11), (*DOM[:3], str(D1), "Betis", "Getafe", 14, 12)])
    res["home_team_id"], res["away_team_id"] = ["england:arsenal", "spain:betis"], ["france:lille", "spain:getafe"]
    load.upsert_matches(engine, res)
    repo = Repository(engine)
    assert [r["match_id"] for r in repo.official_results(date.today(), D2 + timedelta(days=1))] == [M_DOM]
    assert Repository(engine).track_records("live")["match_id"].tolist() == [M_DOM]
    assert dict(q(engine, "SELECT team_id, country FROM teams"))["england:arsenal"] == "England"


def test_a_picks_file_without_the_flag_is_still_experimental_by_competition(engine, tmp_path):
    """Safety net: a cross-border competition can never enter the official record, flag or no flag,
    and a rebuilt database never invents "europe:" teams for known clubs."""
    picks = tmp_path / "p"
    picks.mkdir()
    write_picks(picks / f"{date.today():%Y-%m-%d}_0002_at_least_24.csv",
                [(str(D1), "20:00", UCL, "Arsenal", "Lille", 0.78, 26.0, M_EU1)])
    load.import_picks_dir(engine, picks)  # no fixtures loaded first: the match is created from the picks row
    assert q(engine, "SELECT experimental, data_quality FROM predictions") == [(1, None)]
    assert q(engine, "SELECT count(*) FROM v_official_picks") == [(0,)]
    assert q(engine, "SELECT home_team_id, away_team_id FROM matches") == [("england:arsenal", "france:lille")]


def test_migration_0004_upgrades_a_0003_database_in_place_and_roundtrips(tmp_path):
    path = tmp_path / "old.db"
    migrate.upgrade(path, "0003_run_integrity")
    engine = make_engine(path)
    load_fx = fixtures([(c, co, t, str(d), h, a, ko) for c, co, t, d, h, a, ko in (DOM,)])
    with engine.begin() as c:
        c.execute(text("INSERT INTO competitions (id, name, country, tier) VALUES (1, 'Spain La Liga', 'Spain', 1)"))
        c.execute(text("INSERT INTO teams (team_id, name, country) VALUES ('spain:betis', 'Betis', 'Spain'), ('spain:getafe', 'Getafe', 'Spain')"))
        c.execute(text("INSERT INTO matches (match_id, source, competition_id, date, status, home_team_id, away_team_id, home_team, "
                       "away_team, search_key, created_at, updated_at) VALUES (:m, 'x', 1, :d, 'scheduled', 'spain:betis', "
                       "'spain:getafe', 'Betis', 'Getafe', 'betis | getafe', datetime('now'), datetime('now'))"),
                  {"m": M_DOM, "d": str(D1)})
        c.execute(text("INSERT INTO prediction_runs (id, kind, source_key, status, built_at, models, params, meta, n_predictions, created_at) "
                       "VALUES (1, 'picks', 'picks:x', 'complete', datetime('now'), '[]', "
                       "json_object('source', 'live', 'scan_ts', strftime('%Y-%m-%dT%H:%M:%S', 'now'), 'n', 24), '{}', 1, datetime('now'))"))
        c.execute(text("INSERT INTO predictions (run_id, match_id, points, created_at, pre_match) "
                       "VALUES (1, :m, '{\"24\": 0.8}', strftime('%Y-%m-%d %H:%M:%f', 'now'), 1)"), {"m": M_DOM})
        before = c.execute(text("SELECT id, run_id, match_id, points, created_at, pre_match FROM predictions")).all()
        triggers = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))}
    engine.dispose()
    assert len(load_fx) == 1
    assert migrate.upgrade(path) == migrate.head() == "0004_experimental"
    engine = make_engine(path)
    with engine.connect() as c:
        assert c.execute(text("SELECT id, run_id, match_id, points, created_at, pre_match FROM predictions")).all() == before
        assert c.execute(text("SELECT experimental, data_quality FROM predictions")).all() == [(0, None)]
        assert {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))} == triggers
        assert c.execute(text("SELECT match_id FROM v_official_picks")).all() == [(M_DOM,)]  # the record is unchanged
        assert c.execute(text("SELECT count(*) FROM v_experimental_picks")).scalar() == 0
    engine.dispose()
    assert migrate.downgrade(path, "0003_run_integrity") == "0003_run_integrity"
    engine = make_engine(path)
    with engine.connect() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(predictions)"))}
        assert "experimental" not in cols and "data_quality" not in cols
        assert c.execute(text("SELECT match_id FROM v_official_picks")).all() == [(M_DOM,)]
        assert c.execute(text("SELECT id, run_id, match_id, points, created_at, pre_match FROM predictions")).all() == before
    engine.dispose()
    assert migrate.upgrade(path) == "0004_experimental"


# ----------------------------------------------------------------------------- export

def validate(name, doc):
    jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]}).validate(doc)


def test_export_marks_experimental_fixtures_and_features_the_competition(engine, tmp_path, migrated_db):
    seed_db(engine, tmp_path)
    out = tmp_path / "site"
    built = datetime.combine(date.today(), datetime.min.time()).replace(hour=9, tzinfo=timezone.utc)
    export(out, Repository(engine), built_at=built)
    docs = {n: json.loads((out / f"{n}.json").read_text(encoding="utf-8")) for n in ("meta", "board", "results", "track")}
    for name, doc in docs.items():
        validate(name, doc)
    assert docs["meta"]["schema"] == 1 and docs["board"]["schema"] == 1
    fx = {f["id"]: f for f in docs["board"]["fixtures"]}
    assert "experimental" not in fx[M_DOM] and "data_quality" not in fx[M_DOM]  # ordinary fixtures are unchanged
    assert fx[M_EU1]["experimental"] is True and fx[M_EU1]["data_quality"] == "full"
    assert fx[M_EU1]["league"] == "uefa-champions-league" and fx[M_EU1]["team"] is not None
    assert fx[M_EU1]["kickoff_utc"].endswith("T18:00:00Z") and fx[M_EU1]["odds"] is False
    assert fx[M_EU2]["experimental"] is True and fx[M_EU2]["data_quality"] == "partial" and fx[M_EU2]["team"] is None
    assert fx[M_EU2]["model"].endswith("-ucl-exp")
    leagues = {x["id"]: x for x in docs["meta"]["leagues"]}
    assert leagues["uefa-champions-league"] == {"id": "uefa-champions-league", "name": "UEFA Champions League",
                                                "country": "Europe", "tier": 1, "fixtures": 2, "featured": True}
    assert "featured" not in leagues["spain-la-liga"]
    # the official record ignores them: one pending pick, nothing else
    assert docs["track"]["records"]["live"]["pending"] == 1 and docs["results"]["results"] == []
    # the independent verifier agrees, and notices a dropped or invented flag
    res = verify_mod.verify(out, migrated_db, today=date.today())
    assert res["ok"], res["problems"]

    def tampered(name, change):
        d = tmp_path / f"t_{change.__name__}"
        d.mkdir()
        for n, doc in docs.items():
            doc = json.loads(json.dumps(doc))
            if n == name:
                change(doc)
            (d / f"{n}.json").write_text(json.dumps(doc), encoding="utf-8")
        return verify_mod.verify(d, migrated_db, today=date.today())

    def drop_flag(doc):
        next(f for f in doc["fixtures"] if f["id"] == M_EU1).pop("experimental")

    def invent_flag(doc):
        next(f for f in doc["fixtures"] if f["id"] == M_DOM)["experimental"] = True

    def upgrade_quality(doc):
        next(f for f in doc["fixtures"] if f["id"] == M_EU2)["data_quality"] = "full"

    def unfeature(doc):
        next(x for x in doc["leagues"] if x["id"] == "uefa-champions-league").pop("featured")

    for name, change, word in (("board", drop_flag, "experimental"), ("board", invent_flag, "experimental"),
                               ("board", upgrade_quality, "data_quality"), ("meta", unfeature, "featured")):
        res = tampered(name, change)
        assert not res["ok"] and any(word in p for p in res["problems"]), (change.__name__, res["problems"])


def test_schema_rejects_false_flags_and_unknown_quality():
    fixture = {"id": "x", "league": "uefa-champions-league", "round": None, "date": "2026-10-13", "kickoff_local": None,
               "kickoff_utc": None, "home": "A", "away": "B", "expected": 25, "lb": {"70": 22, "80": 21, "90": 19},
               "league_avg": 27.0, "above_league": -2.0, "odds": False, "cold_start": False, "model": "m", "curve": [0.5],
               "team": None}
    validate("board", {"schema": 1, "fixtures": [fixture]})
    validate("board", {"schema": 1, "fixtures": [dict(fixture, experimental=True, data_quality="none")]})
    for bad in (dict(fixture, experimental=False), dict(fixture, experimental=True, data_quality="good"),
                dict(fixture, experimantal=True)):
        with pytest.raises(jsonschema.ValidationError):
            validate("board", {"schema": 1, "fixtures": [bad]})


def test_fast_pmf_equals_the_production_pmf(fitted):
    from shots.models.cross_border import fast_nb_pmf, total_pmf
    from shots.models.negbin import nb_pmf

    mu = np.array([0.3, 4.0, 11.5, 27.0, 60.0])
    for alpha in (0.0, 0.04, np.array([0.0, 0.01, 0.05, 0.2, 0.5])):
        np.testing.assert_allclose(fast_nb_pmf(mu, alpha, 80), nb_pmf(mu, alpha, 80), rtol=1e-9, atol=1e-13)
    base, _, test = fitted
    mh, ma = base.nb_.side_mu(test, "home"), base.nb_.side_mu(test, "away")
    np.testing.assert_allclose(total_pmf(base.nb_, mh, ma), base.nb_.pmf_from_mu(mh, ma), rtol=1e-9, atol=1e-13)
    np.testing.assert_allclose(base.nb_.pmf_from_mu(mh, ma), base.nb_.total_pmf(test), rtol=0, atol=0)
