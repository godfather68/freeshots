"""Web UI: experimental per-team predictions (row split, team-threshold filter, match page, old boards)."""

import numpy as np
import pandas as pd
import pytest
from starlette.datastructures import QueryParams

from shots.web import filters as flt
from tests.test_web import TODAY, make_board, make_client, shown_ids


def with_team(df: pd.DataFrame) -> pd.DataFrame:
    """Add team columns: home mean = 55% of expected, away 45%; logistic curves."""
    df = df.copy()
    for side, share in (("home", .55), ("away", .45)):
        mu = df["expected_shots"] * share
        df[f"{side}_mean"] = mu.round(2)
        df[f"{side}_median"] = mu.round().astype(int)
        for q, d in ((70, 2), (80, 3), (90, 4)):
            df[f"{side}_lb_{q}"] = (mu - d).round().astype(int)
        for n in range(3, 26):
            df[f"p{side[0]}_{n}"] = (1 / (1 + np.exp((n - mu) / 2))).round(4)
    return df


@pytest.fixture
def client(tmp_path, migrated_db):
    return make_client(tmp_path, with_team(make_board()))


@pytest.fixture
def old_client(tmp_path, migrated_db):
    return make_client(tmp_path, make_board())


def ids(c, qs):
    r = c.get("/?" + qs)
    assert r.status_code == 200
    return shown_ids(r.text)


def test_row_shows_team_split_and_experimental(client):
    html = client.get("/").text
    assert "16 – 14" in html  # a: 30 -> 16.5 / 13.5, shown rounded to whole shots
    assert "exp." in html and "experimental" in html.lower()


def test_split_values_match_board(client):
    assert "15 – 12" in client.get("/").text  # c: 27 -> 14.85 / 12.15


def test_team_filter_home_away(client):
    b = with_team(make_board()).set_index("match_id")
    for side, col in (("home", "ph"), ("away", "pa")):
        for pmin in (50, 70, 90):
            want = [m for m in "abcd" if b.loc[m, f"{col}_12"] >= pmin / 100]
            assert ids(client, f"side={side}&tn=12&tp={pmin}") == want


def test_team_filter_either_is_max(client):
    b = with_team(make_board()).set_index("match_id")
    for n, pmin in ((14, 50), (16, 50), (12, 80)):
        want = [m for m in "abcd" if max(b.loc[m, f"ph_{n}"], b.loc[m, f"pa_{n}"]) >= pmin / 100]
        assert ids(client, f"side=either&tn={n}&tp={pmin}") == want
    assert set(ids(client, "side=away&tn=14&tp=50")) < set(ids(client, "side=either&tn=14&tp=50"))


def test_apply_tp_column_and_side():
    b = flt.prepare(with_team(make_board()))
    out = flt.apply(b, flt.Filters(side="either", tn=14), TODAY).set_index("match_id")
    r = b.set_index("match_id")
    for mid in out.index:
        assert out.loc[mid, "tp"] == max(r.loc[mid, "ph_14"], r.loc[mid, "pa_14"])
        assert out.loc[mid, "tside"] == "home"  # home share is larger
    out = flt.apply(b, flt.Filters(side="away", tn=9), TODAY).set_index("match_id")
    assert (out["tside"] == "away").all() and out.loc["a", "tp"] == r.loc["a", "pa_9"]
    assert "tp" not in flt.apply(b, flt.Filters(), TODAY).columns


def test_team_filter_chip_reset_and_row_probability(client):
    html = client.get("/?side=home&tn=14&tp=50").text
    assert "home team 14+ shots, P ≥ 50% (experimental)" in html
    assert 'name="side"' in html and 'name="tn"' in html and 'name="tp"' in html
    assert 'class="reset" href="/"' in html
    assert "home team 14+ shots" in html and "tprob" in html
    assert flt.parse(QueryParams("side=home&tn=14&tp=50")).qs() == "side=home&tn=14&tp=50"
    assert flt.parse(QueryParams("tn=14&tp=50")).qs() == ""  # team params without a side are inert


def test_team_params_bad_values_clamped(client):
    for qs in ("side=sideways", "side=home&tn=99", "side=home&tn=-4", "side=home&tn=abc&tp=nan",
               "side=either&tp=1000", "tn=7&tp=x"):
        assert client.get("/?" + qs).status_code == 200
    f = flt.parse(QueryParams("side=home&tn=99&tp=1000"))
    assert (f.side, f.tn, f.tp) == ("home", flt.TN_HI, 95)
    assert flt.parse(QueryParams("side=bogus")).side == ""


def test_team_filter_empty_state(client):
    r = client.get("/?side=either&tn=25&tp=95")
    assert r.status_code == 200 and "No matches fit these filters" in r.text


def test_match_page_team_section(client):
    r = client.get("/match/a?tn=14")
    assert r.status_code == 200
    assert 'class="curve teamcurve"' in r.text and 'class="tline home"' in r.text and 'class="tline away"' in r.text
    assert "experimental" in r.text and "per-competition" in r.text
    assert "90% sure: at least" in r.text and "Median" in r.text
    b = with_team(make_board()).set_index("match_id")
    assert f"{round(b.loc['a', 'ph_14'] * 100)}%" in r.text
    assert client.get("/match/a?tn=999").status_code == 200


def test_about_mentions_experimental(client):
    t = client.get("/about").text
    assert "experimental" in t and "per-competition" in t


def test_old_board_without_team_columns_renders(old_client):
    assert not flt.has_team(make_board())
    html = old_client.get("/").text
    assert "17 – 13" not in html and 'name="side"' not in html
    assert ids(old_client, "side=either&tn=12&tp=80") == ["a", "b", "c", "d"]  # ignored, no 500
    m = old_client.get("/match/a?tn=12")
    assert m.status_code == 200 and "teamcurve" not in m.text
    assert old_client.get("/about").status_code == 200
