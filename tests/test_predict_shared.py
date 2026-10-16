"""Efficiency refactor of the update path (2026-10-15): one fixtures download, one domestic feature pass and
one model load per run, one CDF per route for the board curves. Every shortcut must give exactly what the
unshared code gives."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from shots.models.calibrated import NegBinPitComp
from shots.models.cross_border import CrossBorderModel
from shots.models.negbin import _Design
from shots.predict import scan as sc
from tests.test_cross_border import scan_inputs, stub_models  # noqa: F401 (fixture)
from tests.test_models import SEASONS, synthetic


@pytest.fixture(scope="module")
def fitted():
    df = synthetic()
    train, test = df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]
    return NegBinPitComp().fit(train), train, test.head(300)


def test_member_equals_isin():
    a = pd.Series(["x", "y", "z", "x"], index=[5, 3, 9, 1], dtype="string[pyarrow]")
    b = pd.Series(["x", "q"], dtype="string[pyarrow]")
    pd.testing.assert_series_equal(sc.member(a, b), a.isin(b))
    pd.testing.assert_series_equal(sc.member(a.astype(object), b), a.astype(object).isin(b))


def test_curve_methods_equal_the_scalar_methods_bit_for_bit(fitted):
    model, _, rows = fitted
    ns = list(range(10, 46))
    curve = model.prob_at_least_curve(rows, ns)
    for j, n in enumerate(ns):
        assert np.array_equal(curve[:, j], model.prob_at_least(rows, n))
    tns = list(range(3, 26))
    for side in ("home", "away"):
        tc = model.team_prob_at_least_curve(rows, side, tns)
        for j, n in enumerate(tns):
            assert np.array_equal(tc[:, j], model.team_prob_at_least(rows, side, n))


def test_cross_border_curve_methods_equal_the_scalar_methods(fitted):
    model, train, rows = fitted
    xbm = CrossBorderModel.from_base(model, train.assign(tier=1), n_profiles=5)
    rows = rows.head(40).copy()
    rows.loc[rows.index[:10], "home_shots_for_ewm"] = np.nan  # some unknown clubs -> profile mixtures
    ns = [12, 20, 31]
    c = xbm.prob_at_least_curve(rows, ns)
    for j, n in enumerate(ns):
        assert np.array_equal(c[:, j], xbm.prob_at_least(rows, n))
    tc = xbm.team_prob_at_least_curve(rows, "away", ns)
    for j, n in enumerate(ns):
        assert np.array_equal(tc[:, j], xbm.team_prob_at_least(rows, "away", n))


def test_design_transform_is_unchanged_by_the_dict_build(fitted):
    """The frame built in one go equals the old column-by-column build (values, order, dtypes, index)."""
    _, train, rows = fitted
    rows = pd.concat([rows, rows.head(3)])  # duplicate labels must behave as before
    d = _Design("home").fit(train)
    new = d.transform(rows)
    old = pd.DataFrame(index=rows.index)
    for c in new.columns:
        old[c] = new[c].to_numpy()
    pd.testing.assert_frame_equal(new, old, check_exact=True)
    assert list(new.columns[:2]) == ["const", "log_prior"] and (new.dtypes == float).all()


def _prep_fields(p):
    return p.upcoming, p.has_odds, p.played


def test_shared_run_slices_give_the_unshared_features(stub_models):  # noqa: F811
    m, dom = scan_inputs(False)
    dom = dom.sort_values("date").reset_index(drop=True)
    narrow = dom[dom["date"] <= pd.Timestamp("2026-10-18")]
    fresh_narrow = sc.prepare_upcoming(narrow, m)
    fresh_wide = sc.prepare_upcoming(dom, m)
    with sc.shared_run(30) as run:
        run.domestic = None
        wide = sc.prepare_upcoming(dom, m)           # builds and stores the wide pass
        cut = sc.prepare_upcoming(narrow, m)          # slices it
        assert run.stats == {"feature_builds": 1, "feature_slices": 1, "fetches": 0}
    for a, b in zip(_prep_fields(fresh_narrow), _prep_fields(cut)):
        if isinstance(a, pd.DataFrame):
            pd.testing.assert_frame_equal(a, b, check_exact=True)
        else:
            pd.testing.assert_series_equal(a, b, check_exact=True)
    pd.testing.assert_frame_equal(fresh_wide.upcoming, wide.upcoming, check_exact=True)


def test_shared_run_refuses_a_window_that_is_not_a_date_prefix(stub_models):  # noqa: F811
    m, dom = scan_inputs(False)
    dom = dom.sort_values("date").reset_index(drop=True)
    gap = dom.iloc[[0, 2]]  # drops the middle fixture: the later one's features would depend on it
    with sc.shared_run(30) as run:
        sc.prepare_upcoming(dom, m)
        got = sc.prepare_upcoming(gap, m)
        assert run.stats["feature_builds"] == 2 and run.stats["feature_slices"] == 0
    pd.testing.assert_frame_equal(got.upcoming, sc.prepare_upcoming(gap, m).upcoming, check_exact=True)


def test_shared_run_refuses_other_matches(stub_models):  # noqa: F811
    m, dom = scan_inputs(False)
    with sc.shared_run(30) as run:
        sc.prepare_upcoming(dom, m)
        sc.prepare_upcoming(dom, m.iloc[:-1])
        assert run.stats["feature_builds"] == 2


def test_fixtures_are_fetched_once_per_run(monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "_fetch_fixtures", lambda d: calls.append(d) or pd.DataFrame({"match_id": ["a"]}))
    with sc.shared_run(30):
        a = sc.fetch_fixtures(pd.Timestamp("2026-10-15").date())
        a["match_id"] = "mutated"
        b = sc.fetch_fixtures(pd.Timestamp("2026-10-15").date())
    sc.fetch_fixtures(pd.Timestamp("2026-10-15").date())
    assert len(calls) == 2 and b["match_id"].tolist() == ["a"]


# ----------------------------------------------------------------------------- unpublished competitions

def test_national_league_is_scored_but_never_published(tmp_path, monkeypatch):
    """England National League fixtures stay out of picks and the board; every other row is exactly what
    it is without the exclusion (it is applied after scoring)."""
    from datetime import date

    from shots.predict import board as bd
    from tests import test_board as tb

    fx = tb.fixtures_frame()
    nl = fx.iloc[[0]].assign(match_id="u4", competition="England National League", country="England", tier=5,
                             home_team="Yeovil", away_team="Woking")
    fx = pd.concat([fx, nl], ignore_index=True)
    matches = tb.matches_frame()
    matches.to_parquet(tmp_path / "matches.parquet")
    monkeypatch.setattr(sc.fd, "OUT_PATH", tmp_path / "matches.parquet")
    monkeypatch.setattr(sc, "gather_fixtures", lambda start, days, use_calendar=True: fx)
    monkeypatch.setattr(sc, "build_features", tb.fake_features)
    monkeypatch.setattr(sc, "FEATURES_PATH", tmp_path / "features.parquet")
    monkeypatch.setattr(sc, "PICKS_DIR", tmp_path / "picks")
    monkeypatch.setattr(sc, "CALENDAR_PATH", tmp_path / "no_calendar.parquet")
    monkeypatch.setattr(sc, "latest_model", lambda name, last, age: tmp_path / "m_2026-10-01")
    monkeypatch.setattr(sc, "load", lambda p: tb.StubModel())
    monkeypatch.setattr(sc, "latest_no_odds_model", lambda name, played, age: (tb.StubModel(-2.0), "noodds"))
    out = sc.scan(shots=24, min_prob=0.7, refresh=False, today=date(2026, 10, 8), days=30)
    assert out["match_id"].tolist() == ["u1", "u2", "u3"]           # same rows and order as without u4
    saved = pd.read_csv(next((tmp_path / "picks").glob("*.csv")))
    assert "England National League" not in set(saved["competition"])
    board = bd.build(days=30, refresh=False, today=date(2026, 10, 8), out_path=tmp_path / "board.parquet")
    assert "u4" not in set(board["match_id"]) and len(board) == 3
    monkeypatch.setattr(sc, "UNPUBLISHED_COMPETITIONS", frozenset())
    full = bd.assemble(sc.prepare_upcoming(fx, matches), fx.set_index("match_id")[["country", "tier"]])
    pd.testing.assert_frame_equal(board, full[full["match_id"] != "u4"].reset_index(drop=True), check_exact=True)
