"""Web UI: filter logic, curve lookup, empty/error states, and a render smoke test per page.
The app reads the database through the repository; fixtures seed a temp DB with the board loader."""

import re
from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from shots.db import load, migrate
from shots.db.engine import make_engine
from shots.predict import board as bd
from shots.web import filters as flt
from shots.web.app import create_app

TODAY = date(2026, 10, 8)  # a Thursday


def row(mid, day, ko, comp, country, tier, home, away, exp, odds=False, cold=False, rnd="Regular Season - 8"):
    r = dict(match_id=mid, date=pd.Timestamp(day), kickoff=ko, competition=comp, country=country, tier=tier,
             round=rnd, home_team=home, away_team=away, expected_shots=float(exp), lb_70=int(exp - 3),
             lb_80=int(exp - 4), lb_90=int(exp - 6), league_avg_shots=26.0, above_league=round(exp - 26.0, 1),
             odds=odds, mkt_p_over25=0.55 if odds else np.nan, cold_start=cold,
             model="negbin_pit_comp_x" if odds else "negbin_pit_comp-noodds_x")
    for n in bd.NS:
        r[f"p_{n}"] = round(float(1 / (1 + np.exp((n - exp) / 3))), 4)
    return r


def make_board(extra=()):
    rows = [
        row("a", "2026-10-08", "20:00", "Spain La Liga", "Spain", 1, "Atlético Madrid", "Betis", 30, odds=True),
        row("b", "2026-10-10", "15:00", "Italy Serie B", "Italy", 2, "Bari", "Cesena", 24, cold=True),
        row("c", "2026-10-11", "13:00", "England Premier League", "England", 1, "Fulham", "Brentford", 27),
        row("d", "2026-10-20", "19:45", "England Championship", "England", 2, "Hull", "Leeds", 22),
        row("old", "2026-10-05", "19:00", "Spain La Liga", "Spain", 1, "Past", "Match", 28),
        *extra,
    ]
    return pd.DataFrame(rows)


META = dict(built_at="2026-10-08T09:30:00", results_up_to="2026-10-05", n_fixtures=4, n_with_odds=1,
            n_without_odds=3, models=["negbin_pit_comp_x"])


def write_board(path, df=None, meta=None):
    """Record a board as a prediction run in the DB at `path` (migrated by the caller)."""
    engine = make_engine(path)
    load.import_board(engine, df if df is not None else make_board(), meta or META)
    engine.dispose()
    return path


def seed_backfill(path, back):
    """Played matches for the backfill rows (actual total split home/away) + the backfill run."""
    m = back.drop_duplicates("match_id")
    matches = pd.DataFrame({
        "match_id": m["match_id"], "source": "test", "competition": m["competition"], "country": "Spain", "tier": 1,
        "season": "2025/2026", "date": m["date"], "home_team": "H " + m["match_id"], "away_team": "A " + m["match_id"],
        "home_shots": (m["actual"] // 2).astype("Int64"), "away_shots": (m["actual"] - m["actual"] // 2).astype("Int64")})
    engine = make_engine(path)
    load.upsert_matches(engine, matches)
    load.import_backfill(engine, back)
    engine.dispose()


def make_client(tmp_path, df=None, back=None, today=lambda: TODAY):
    """App on tmp_path/shots.db (the migrated_db fixture's copy when requested, else migrated here)."""
    path = tmp_path / "shots.db"
    if not path.exists():
        migrate.upgrade(path)
    write_board(path, df)
    if back is not None:
        seed_backfill(path, back)
    return TestClient(create_app(path, today=today))


@pytest.fixture
def client(tmp_path, migrated_db):
    return make_client(tmp_path)


def shown_ids(html):
    return re.findall(r'href="/match/(\w+)', html)


# ---------------------------------------------------------------- rendering

def test_home_renders_grouped_by_day(client):
    r = client.get("/")
    assert r.status_code == 200
    assert shown_ids(r.text) == ["a", "b", "c", "d"]  # past match excluded, kickoff order
    assert "Today" in r.text and "Tomorrow" not in r.text
    assert "Atlético Madrid" in r.text
    assert "4 matches" in r.text or "of 4 matches shown" in r.text
    assert "Data as of 2026-10-08 09:30" in r.text and "python -m shots.predict.board" in r.text
    assert "no odds" in r.text and "cold start" in r.text


def test_match_page_renders_curve_and_bounds(client):
    r = client.get("/match/a?n=28")
    assert r.status_code == 200
    assert "<svg" in r.text and "28+ shots" in r.text
    assert r.text.count('class="bdot"') == 3
    assert "70% sure" in r.text and "90% sure" in r.text and "negbin_pit_comp_x" in r.text
    assert "odds model was used" in r.text
    assert client.get("/match/b").text.count("no-odds model") >= 1
    assert "cold start" in client.get("/match/b").text


def test_match_unknown_is_404(client):
    assert client.get("/match/zzz").status_code == 404


def test_about_and_static(client):
    assert client.get("/about").status_code == 200
    css = client.get("/static/app.css")
    assert css.status_code == 200 and "prefers-color-scheme" in css.text
    assert client.get("/static/app.js").status_code == 200


def test_names_are_escaped(tmp_path, migrated_db):
    evil = row("x", "2026-10-09", "12:00", "Spain La Liga", "Spain", 1, "<script>alert(1)</script>", "B", 26)
    c = make_client(tmp_path, make_board([evil]))
    assert "<script>alert(1)</script>" not in c.get("/").text
    assert "&lt;script&gt;" in c.get("/match/x").text


# ---------------------------------------------------------------- curve lookup

def test_threshold_reads_stored_curve(client):
    row_a = make_board().set_index("match_id").loc["a"]
    for n in (15, 24, 40):
        html = client.get(f"/?n={n}&league=Spain+La+Liga").text
        assert f"{round(row_a[f'p_{n}'] * 100):.0f}%" in html
        assert f"chance of {n}+ shots" in html


def test_apply_p_is_stored_value():
    b = flt.prepare(make_board())
    for n in (15, 22, 33, 40):
        out = flt.apply(b, flt.Filters(n=n), TODAY)
        for rr in out.itertuples():
            assert rr.p == getattr(b.set_index("match_id").loc[rr.match_id], f"p_{n}")


# ---------------------------------------------------------------- filters

def ids(client, qs):
    r = client.get("/?" + qs)
    assert r.status_code == 200
    return shown_ids(r.text)


def test_league_filter_multi(client):
    assert ids(client, "league=Spain+La+Liga") == ["a"]
    assert ids(client, "league=Spain+La+Liga&league=Italy+Serie+B") == ["a", "b"]
    assert ids(client, "league=Nowhere+League") == ["a", "b", "c", "d"]  # unknown ignored, not "match nothing"


def test_quick_groups(client):
    assert ids(client, "grp=top5") == ["a", "c"]
    assert ids(client, "grp=second") == ["b", "d"]
    assert ids(client, "grp=top5&grp=second") == ["a", "b", "c", "d"]
    assert ids(client, "grp=second&league=Spain+La+Liga") == ["a", "b", "d"]


def test_shot_range(client):
    assert ids(client, "smin=26") == ["a", "c"]
    assert ids(client, "smax=24") == ["b", "d"]
    assert ids(client, "smin=24&smax=27") == ["b", "c"]
    assert ids(client, "smin=27&smax=24") == ["b", "c"]  # swapped, never an error
    assert ids(client, "smin=24&smax=24") == ["b"]


def test_min_probability(client):
    # N=24: p(a)=.88 p(b)=.5 p(c)=.61 p(d)=.34 (stored curve)
    assert ids(client, "p=60") == ["a", "c"]
    assert ids(client, "p=90") == []
    assert ids(client, "n=30&p=50") == ["a"]


def test_date_windows(client):
    assert ids(client, "when=today") == ["a"]
    assert ids(client, "when=3d") == ["a", "b", "c"]  # today..Sun 11 Oct is 3 days ahead
    assert ids(client, "when=weekend") == ["b", "c"]
    assert ids(client, "when=7d") == ["a", "b", "c"]
    assert ids(client, "when=30d") == ["a", "b", "c", "d"]


def test_weekend_helper():
    assert flt.weekend(date(2026, 10, 8)) == (date(2026, 10, 10), date(2026, 10, 11))
    assert flt.weekend(date(2026, 10, 10)) == (date(2026, 10, 10), date(2026, 10, 11))
    assert flt.weekend(date(2026, 10, 11)) == (date(2026, 10, 10), date(2026, 10, 11))


def test_odds_and_cold_toggles(client):
    assert ids(client, "odds=1") == ["a"]
    assert ids(client, "nocold=1") == ["a", "c", "d"]
    assert ids(client, "odds=1&nocold=1") == ["a"]


def test_team_search_accent_insensitive(client):
    assert ids(client, "q=atletico") == ["a"]
    assert ids(client, "q=ATLÉTICO") == ["a"]
    assert ids(client, "q=bet") == ["a"]
    assert ids(client, "q=bari+cesena") == ["b"]
    assert ids(client, "q=nobody") == []


def test_sorting(client):
    assert ids(client, "sort=prob") == ["a", "c", "b", "d"]
    assert ids(client, "sort=shots") == ["a", "c", "b", "d"]
    assert ids(client, "sort=above") == ["a", "c", "b", "d"]
    assert ids(client, "sort=kickoff") == ["a", "b", "c", "d"]
    r = client.get("/?sort=prob").text
    assert "Ranked by probability" in r and "Today ·" not in r  # flat ranked list, no day groups


def test_combined_filters(client):
    assert ids(client, "grp=second&when=7d&nocold=1") == []
    assert ids(client, "grp=second&when=7d&p=50") == ["b"]


def test_chips_and_reset(client):
    r = client.get("/?league=Spain+La+Liga&n=30&p=20&odds=1")
    chips = re.search(r'<ul class="chips".*?</ul>', r.text, re.S).group(0)
    assert "Spain La Liga" in chips and "P ≥ 20%" in chips and "odds published only" in chips
    hrefs = re.findall(r'href="([^"]*)"', chips)
    assert "/?n=30&amp;p=20&amp;odds=1" in hrefs  # league chip removed, rest kept
    assert "/?league=Spain+La+Liga&amp;n=30&amp;odds=1" in hrefs  # p chip removed
    assert hrefs[-1] == "/"  # reset all
    assert "chips" not in client.get("/").text.replace("chips(", "") or 'class="chips"' not in client.get("/").text
    assert 'class="reset" href="/"' in r.text


def test_detail_links_keep_filters(client):
    r = client.get("/?n=28&sort=prob")
    assert "/match/a?n=28&amp;sort=prob" in r.text
    d = client.get("/match/a?n=28&sort=prob")
    assert 'href="/?n=28&amp;sort=prob"' in d.text


# ---------------------------------------------------------------- bad input and empty states

@pytest.mark.parametrize("qs", [
    "n=abc", "n=9999", "n=-3", "n=nan", "n=", "p=-5", "p=1000", "p=x", "smin=nan", "smax=inf", "smin=abc&smax=9e99",
    "when=zzz", "sort=zzz", "league=", "grp=nope", "odds=maybe", "q=" + "x" * 500, "q=%00%ff", "n=24&n=30",
    "unknown=1", "league=%3Cscript%3E",
])
def test_bad_params_never_500(client, qs):
    assert client.get("/?" + qs).status_code == 200
    assert client.get("/match/a?" + qs).status_code == 200


def test_parse_clamps():
    class P(dict):
        def getlist(self, k):
            v = self.get(k)
            return [] if v is None else v if isinstance(v, list) else [v]

    f = flt.parse(P(n="9999", p="1000", smin="1", smax="99", when="zz", sort="zz"), ["X"])
    assert (f.n, f.pmin, f.smin, f.smax, f.when, f.sort) == (40, 95, 10.0, 45.0, "", "kickoff")
    f = flt.parse(P(n="2", p="37", league=["X", "Y"]), ["X"])
    assert (f.n, f.pmin, f.leagues) == (15, 35, ("X",))
    assert flt.parse(P(n="abc"), []).n == 24


def test_empty_filter_result_state(client):
    r = client.get("/?p=90")
    assert r.status_code == 200
    assert "No matches fit these filters" in r.text and "Reset filters" in r.text
    assert "0</b><span>of 4 matches shown" in r.text


def test_board_all_past_state(tmp_path, migrated_db):
    old = make_board().iloc[[4]]
    c = make_client(tmp_path, old)
    r = c.get("/")
    assert r.status_code == 200 and "no upcoming matches" in r.text


def test_missing_board(tmp_path):
    path = tmp_path / "nope.db"
    c = TestClient(create_app(path, today=lambda: TODAY))
    for url in ("/", "/match/a", "/about", "/track"):
        r = c.get(url)
        assert r.status_code == 200 and "No board yet" in r.text and "python -m shots.predict.board" in r.text
    assert not path.exists()  # the read-only web app never creates the database


def test_migrated_db_without_board_is_missing_state(migrated_db):
    r = TestClient(create_app(migrated_db, today=lambda: TODAY)).get("/")
    assert r.status_code == 200 and "No board yet" in r.text


def test_corrupt_board_does_not_500(tmp_path):
    p = tmp_path / "shots.db"
    p.write_bytes(b"not a database" * 100)
    c = TestClient(create_app(p, today=lambda: TODAY))
    r = c.get("/")
    assert r.status_code == 200 and "could not be read" in r.text


def test_shows_latest_run_after_new_import(tmp_path, migrated_db):
    c = make_client(tmp_path)
    assert len(shown_ids(c.get("/").text)) == 4
    write_board(migrated_db, make_board().iloc[:2], dict(META, built_at="2026-10-08T12:00:00"))
    assert shown_ids(c.get("/").text) == ["a", "b"]


def test_stale_board_notice(tmp_path, migrated_db):
    c = make_client(tmp_path, today=lambda: date(2026, 10, 12))
    assert "built 4 days ago" in c.get("/").text


# ---------------------------------------------------------------- track page

def back_frame():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.05, 0.95, 600)
    actual = np.where(rng.uniform(size=600) < p, 30, 18)
    d = pd.DataFrame({"match_id": [f"m{i}" for i in range(600)], "date": pd.Timestamp("2026-01-01"),
                      "competition": "Spain La Liga", "N": 24, "p": p, "actual": actual})
    return d


def test_track_page_renders_with_backfill(tmp_path, migrated_db):
    c = make_client(tmp_path, back=back_frame())
    r = c.get("/track")
    assert r.status_code == 200
    assert "Reliability table" in r.text and "<svg" in r.text and "called at 70% or more" in r.text
    assert c.get("/track?n=24&src=backfill").status_code == 200
    assert c.get("/track?n=99").status_code == 200  # unknown N falls back to an available one
    assert c.get("/track?src=live").status_code == 200  # no live picks: empty state
    assert "Nothing to show" in c.get("/track?src=live").text


def test_track_page_empty_and_invalid(client):
    r = client.get("/track")
    assert r.status_code == 200 and "Nothing to show" in r.text
    assert client.get("/track?n=abc").status_code == 422  # validated, not a 500
