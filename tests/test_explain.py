"""Explanations of the expected total (shots.models.explain): exact additivity, sign sanity, the three
routes (odds, no-odds, experimental cross-border), determinism, no effect on predictions, storage with
the prediction (migration 0005), the export contract and the independent verifier."""

from __future__ import annotations

import itertools
import json
import math
from datetime import date, timedelta

import jsonschema
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from shots.db import load, migrate
from shots.db.engine import make_engine
from shots.export import ExportError
from shots.export import verify as verify_mod
from shots.models import explain as ex
from shots.models.calibrated import NegBinPitComp
from shots.models.cross_border import CrossBorderModel
from shots.predict import board as bd
from shots.predict import scan as sc
from tests.test_export import BUILT, D5, FIX, M4, M5, M6, NAMES, NOW, SCHEMA, board_frame, meta_at, read, run_export
from tests.test_db_load import fixtures
from tests.test_models import SEASONS, synthetic

UCL = "UEFA Champions League"


def no_odds(f: pd.DataFrame) -> pd.DataFrame:
    f = f.copy()
    f[[c for c in f.columns if c.startswith("mkt_")]] = np.nan
    return f


@pytest.fixture(scope="module")
def data():
    df = synthetic(n_per_season=1200, seed=11)
    names = {"L1": "One", "L2": "Two", "L3": "Three"}
    df["home_team"] = "Home " + df["competition"].map(names)
    df["away_team"] = "Away " + df["competition"].map(names)
    train, test = df[df["season"] < SEASONS[-1]], df[df["season"] == SEASONS[-1]]
    test = test[test[["mkt_p_home", "mkt_p_over25"]].notna().all(axis=1)].head(250)
    return train, test.drop(columns=["home_shots", "away_shots", "total_shots"])


@pytest.fixture(scope="module")
def odds_model(data):
    return NegBinPitComp(min_cal_rows=800).fit(data[0])


@pytest.fixture(scope="module")
def noodds_model(data):
    return NegBinPitComp(min_cal_rows=800).fit(no_odds(data[0]))


@pytest.fixture(scope="module")
def xb_model(data, noodds_model):
    return CrossBorderModel.from_base(noodds_model, no_odds(data[0]), n_profiles=40)


def xb_rows(test: pd.DataFrame) -> pd.DataFrame:
    rows = no_odds(test.dropna(subset=["home_shots_for_ewm", "away_shots_for_ewm"]).head(40)).copy()
    rows["competition"] = UCL
    rows["home_known"] = [True, True, False, False] * 10
    rows["away_known"] = [True, False, True, False] * 10
    return rows


def tenths(x) -> int:
    return int(round(float(x) * 10))


# ----------------------------------------------------------------------------- the decomposition

def brute_shapley(delta: np.ndarray) -> np.ndarray:
    g = len(delta)
    phi = np.zeros(g)
    for order in itertools.permutations(range(g)):
        acc = 0.0
        for i in order:
            phi[i] += math.exp(acc + delta[i]) - math.exp(acc)
            acc += delta[i]
    return phi / math.factorial(g)


def test_shapley_closed_form_equals_the_average_over_all_orders():
    rng = np.random.default_rng(0)
    delta = rng.normal(0, 0.4, size=(6, 5))
    got = ex.shapley_product(delta)
    np.testing.assert_allclose(got, np.array([brute_shapley(d) for d in delta]), rtol=1e-12, atol=1e-13)
    np.testing.assert_allclose(got.sum(axis=1), np.expm1(delta.sum(axis=1)), rtol=1e-12)  # efficiency: exact
    assert (np.sign(got) == np.sign(delta)).all()  # a factor never changes sign on the way to shots
    wide = rng.normal(0, 0.3, size=(4, 14))  # as many factors as the production design has
    np.testing.assert_allclose(ex.shapley_product(wide).sum(axis=1), np.expm1(wide.sum(axis=1)), rtol=1e-11)
    assert ex.shapley_product(np.zeros((3, 0))).shape == (3, 0)


@pytest.mark.parametrize("route", ["odds", "noodds"])
def test_decomposition_adds_up_to_the_model_mean(route, data, odds_model, noodds_model):
    model = odds_model if route == "odds" else noodds_model
    rows = data[1] if route == "odds" else no_odds(data[1])
    raw = ex.raw_explain(model, rows)
    fx = raw[[c for c in raw.columns if c.startswith("fx_")]]
    total = raw["baseline"] + fx.sum(axis=1) + raw["residual"]
    np.testing.assert_allclose(total, raw["expected_mean"], atol=1e-6, rtol=0)
    assert raw["residual"].abs().max() < 1e-9  # Shapley is exactly additive: nothing is left over
    # the GLM part alone reproduces the model's own means, and the calibration factor is the rest
    mu = model.nb_.side_mu(rows, "home") + model.nb_.side_mu(rows, "away")
    np.testing.assert_allclose(raw["baseline"] + fx.drop(columns="fx_calibration").sum(axis=1), mu, atol=1e-9)
    np.testing.assert_allclose(raw["raw_mean"], mu, atol=1e-12)
    cdf = np.cumsum(model.nb_.total_pmf(rows), axis=1)
    few = rows.head(40)  # mean of the published curve: sum over the whole support of P(total >= n)
    surv = np.column_stack([model.prob_at_least(few, n) for n in range(1, cdf.shape[1])])
    np.testing.assert_allclose(raw["expected_mean"].head(40), surv.sum(axis=1), atol=1e-9)
    has_market = {"fx_market_strength", "fx_market_goals"} <= set(fx.columns)
    assert has_market == (route == "odds")  # the no-odds model has no market term at all
    assert (fx["fx_other"].abs() < 1e-12).all()  # every design column belongs to a named factor


def test_reference_fixture_has_no_effects(data, odds_model):
    rows = data[1].head(20)
    ref = ex.reference_rows(odds_model.nb_, rows)
    raw = ex.raw_explain(odds_model, ref)
    fx = raw[[c for c in raw.columns if c.startswith("fx_") and c not in ("fx_league_level", "fx_calibration")]]
    assert fx.abs().to_numpy().max() < 1e-12
    np.testing.assert_allclose(raw["baseline"], 2 * rows["league_prior_shots"], atol=1e-12)


def test_raising_a_teams_attack_raises_its_attack_effect_only(data, odds_model):
    rows = data[1].dropna(subset=["home_shots_for_ewm"]).head(60)
    up = rows.copy()
    for c in ("home_shots_for_shrunk", "home_shots_for_ewm", "home_shots_for_std", "home_sot_for_ewm"):
        up[c] = up[c] * 1.3
    a, b = ex.raw_explain(odds_model, rows), ex.raw_explain(odds_model, up)
    assert (b["fx_home_attack"] > a["fx_home_attack"] + 0.3).all()
    assert (b["expected_mean"] > a["expected_mean"]).all()
    # the other factors keep their log contribution; in shots they move only through the interaction
    for c in ("fx_away_attack", "fx_home_defence", "fx_league_level"):
        assert (b[c] - a[c]).abs().max() < 1e-9
    # the factor sharing the home side's shots keeps its sign and scales mildly with the larger total
    assert (np.sign(b["fx_away_defence"]) == np.sign(a["fx_away_defence"])).all()
    assert ((b["fx_away_defence"] - a["fx_away_defence"]).abs() <= 0.2 * a["fx_away_defence"].abs() + 1e-9).all()
    # a weaker defence of the away side (more shots conceded) raises the home side's shots
    weak = rows.copy()
    for c in ("away_shots_against_shrunk", "away_shots_against_ewm", "away_shots_against_std", "away_sot_against_ewm"):
        weak[c] = weak[c] * 1.3
    assert (ex.raw_explain(odds_model, weak)["fx_away_defence"] > a["fx_away_defence"] + 0.3).all()
    # a team above the league reference has a positive attack effect, a team below a negative one
    ratio = rows["home_shots_for_shrunk"] / rows["league_prior_shots"]
    assert (a.loc[ratio > 1.25, "fx_home_attack"] > 0).all() and (a.loc[ratio < 0.8, "fx_home_attack"] < 0).all()


def test_column_groups_cover_the_design(odds_model):
    for side in ("home", "away"):
        other = "away" if side == "home" else "home"
        groups = {c: ex.column_group(c, side) for c in odds_model.nb_.sides_[side]["cols"]}
        assert groups["log_s_shots_for_shrunk"] == f"{side}_attack" and groups["log_o_shots_against_ewm"] == f"{other}_defence"
        assert groups["log_s_shots_for_venue_l5"] == f"{side}_venue" and groups["log_o_shots_against_venue_l5"] == f"{other}_venue"
        assert groups["s_log_rest"] == f"{side}_rest" and groups["o_log_rest"] == f"{other}_rest"
        assert groups["mkt_p_s"] == groups["mkt_p_o"] == "market_strength" and groups["mkt_p_over25"] == "market_goals"
        assert set(groups.values()) - {"other"} <= set(ex.FACTOR_ORDER)
        assert {c for c, g in groups.items() if g == "other"} <= {"const", "log_prior"}


# ----------------------------------------------------------------------------- the published object

def check_published(e: dict, odds: bool) -> None:
    assert list(e) == ["expected_mean", "baseline", "factors", "residual", "signals"]
    effects = [f["effect"] for f in e["factors"]]
    assert tenths(e["baseline"]["value"]) + sum(map(tenths, effects)) + tenths(e["residual"]) == tenths(e["expected_mean"])
    assert abs(tenths(e["residual"])) <= 5 and all(tenths(x) != 0 for x in effects)
    main = [f for f in e["factors"] if f["id"] != "other"]
    assert len(main) <= ex.MAX_FACTORS and all(abs(tenths(f["effect"])) >= ex.MIN_EFFECT_TENTHS for f in main)
    assert [abs(f["effect"]) for f in main] == sorted((abs(f["effect"]) for f in main), reverse=True)
    assert "other" not in [f["id"] for f in e["factors"][:-1]]
    for f in e["factors"]:
        assert list(f) == ["id", "side", "label", "detail", "effect"] and f["side"] in ("home", "away", "match")
        assert f["id"] in ex.FACTOR_ORDER and f["label"] and f["detail"]
        if f["id"].startswith("market"):
            assert odds and not any(ch.isdigit() for ch in f["label"] + f["detail"])  # words, never a price
    sig = e["signals"]
    assert list(sig) == ["home_matches", "away_matches", "odds_used", "home_volatility", "away_volatility", "model_gap"]
    assert sig["odds_used"] is odds and sig["home_volatility"] is None and sig["away_volatility"] is None
    jsonschema.Draft202012Validator({"$ref": "#/$defs/explain", "$defs": SCHEMA["$defs"]}).validate(e)


def test_published_explanations_for_the_odds_and_no_odds_routes(data, odds_model, noodds_model):
    rows = data[1]
    league = pd.Series("Liga " + rows["competition"], index=rows.index)
    with_odds = ex.explain_rows(odds_model, rows, odds_used=True, league=league, gap_model=noodds_model,
                                gap_rows=no_odds(rows))
    without = ex.explain_rows(noodds_model, no_odds(rows), odds_used=False, league=league)
    raw, raw_no = ex.raw_explain(odds_model, rows), ex.raw_explain(noodds_model, no_odds(rows))
    for i, (a, b) in enumerate(zip(with_odds, without)):
        check_published(a, True)
        check_published(b, False)
        assert a["expected_mean"] == round(raw["expected_mean"].iloc[i], 1) and a["baseline"]["value"] == round(raw["baseline"].iloc[i], 1)
        assert a["signals"]["model_gap"] == round(raw["expected_mean"].iloc[i] - raw_no["expected_mean"].iloc[i], 1) + 0.0
        assert b["signals"]["model_gap"] is None
        assert a["signals"]["home_matches"] == 50 and a["baseline"]["label"].startswith("Liga L") and a["baseline"]["label"].endswith("average this season")
        # each listed effect is the rounded exact effect of that factor
        for f in a["factors"]:
            if f["id"] != "other":
                assert tenths(f["effect"]) == int(round(10 * raw[f"fx_{f['id']}"].iloc[i]))
    assert any(f["id"].startswith("market") for e in with_odds for f in e["factors"])
    assert any(f["id"] == "home_attack" and "shots per match, last 10 (league" in f["detail"] for e in with_odds for f in e["factors"])
    # deterministic, and JSON text is stable
    again = ex.explain_rows(odds_model, rows, odds_used=True, league=league, gap_model=noodds_model, gap_rows=no_odds(rows))
    assert [ex.dumps(e) for e in again] == [ex.dumps(e) for e in with_odds]
    assert ex.dumps(None) is None and json.loads(ex.dumps(with_odds[0])) == with_odds[0]


def test_market_wording_never_states_a_price():
    assert ex.strength_category(0.71, 0.10, "Inter", "Lecce") == "bookmaker odds make Inter strong favourite"
    assert ex.strength_category(0.30, 0.52, "Inter", "Lecce") == "bookmaker odds make Lecce favourite"
    assert ex.strength_category(0.42, 0.30, "Inter", "Lecce") == "bookmaker odds make Inter slight favourite"
    assert ex.strength_category(0.36, 0.35, "Inter", "Lecce") == "bookmaker odds rate the teams about level"
    assert "high-scoring" in ex.goals_category(0.63) and "low-scoring" in ex.goals_category(0.40)
    assert "average" in ex.goals_category(0.50)
    for text_ in (ex.strength_category(0.71, 0.1, "A", "B"), ex.goals_category(0.63), ex.strength_category(None, None, "A", "B")):
        assert not any(ch.isdigit() for ch in text_)


def test_cold_start_and_early_season_wording(data, noodds_model):
    rows = no_odds(data[1]).head(3).copy()
    for c in [c for c in rows.columns if c.startswith("home_") and c.endswith(("_l5", "_l10", "_std", "_ewm", "_venue_l5"))]:
        rows.loc[rows.index[0], c] = np.nan
    rows.loc[rows.index[0], ["home_n_prior_matches", "home_n_prior_season", "home_rest_days"]] = [0.0, 0.0, np.nan]
    rows.loc[rows.index[1], "away_n_prior_season"] = 3.0
    rows["league_prior_n"] = [100.0, 100.0, 4.0]
    raw = ex.raw_explain(noodds_model, rows)
    np.testing.assert_allclose(raw["baseline"] + raw[[c for c in raw.columns if c.startswith("fx_")]].sum(axis=1),
                               raw["expected_mean"], atol=1e-9)
    ctx = dict(cross=False, prior=12.0, reference=24.5, rest_med=7.0, n_profiles=0, own_calibration=True)
    cold = ex._factor_text("home_early", rows.iloc[0].to_dict(), ctx)
    assert cold == ("home", rows["home_team"].iloc[0] + " early season", "no previous matches, league average stands in")
    assert ex._factor_text("home_attack", rows.iloc[0].to_dict(), ctx)[2] == "no previous matches"
    assert ex._factor_text("home_rest", rows.iloc[0].to_dict(), ctx)[2] == "no previous match"
    assert ex._factor_text("away_early", rows.iloc[1].to_dict(), ctx)[2] == "3 matches played this season"
    out = ex.explain_rows(noodds_model, rows, odds_used=False)
    assert out[0]["signals"]["home_matches"] == 0
    assert out[2]["baseline"]["label"].endswith("long-run average") and out[1]["baseline"]["label"].endswith("average this season")


def test_cross_border_route_with_and_without_club_data(data, xb_model):
    rows = xb_rows(data[1])
    raw = ex.raw_explain(xb_model, rows)
    fx = raw[[c for c in raw.columns if c.startswith("fx_")]]
    np.testing.assert_allclose(raw["baseline"] + fx.sum(axis=1) + raw["residual"], raw["expected_mean"], atol=1e-6, rtol=0)
    assert raw["residual"].abs().max() < 0.01  # quadrature / truncation of the mixture only
    np.testing.assert_allclose(raw["raw_mean"], xb_model.mean(rows), atol=1e-12)
    hk, ak = rows["home_known"].to_numpy(), rows["away_known"].to_numpy()
    # a club without data has one stand-in factor and no factor of its own
    own_home = [f"fx_home_{p}" for p in ex.SIDE_PARTS]
    assert (fx.loc[~hk, own_home].to_numpy() == 0).all() and (fx.loc[hk, "fx_home_standin"] == 0).all()
    assert (fx.loc[~ak, [f"fx_away_{p}" for p in ex.SIDE_PARTS]].to_numpy() == 0).all()
    assert not {"fx_market_strength", "fx_market_goals"} & set(fx.columns)
    # hiding a club's features changes nothing: they are not used when the club is unknown
    blind = rows.copy()
    for c in [c for c in blind.columns if c.startswith("home_") and c not in ("home_known", "home_team")]:
        blind.loc[~hk, c] = np.nan
    pd.testing.assert_frame_equal(ex.raw_explain(xb_model, blind), raw)
    out = ex.explain_rows(xb_model, rows, odds_used=False)
    for e, h, a in zip(out, hk, ak):
        check_published(e, False)
        ids = {f["id"] for f in e["factors"]}
        assert e["signals"]["model_gap"] is None
        if not h:
            assert not ids & {"home_attack", "home_defence", "home_venue", "home_rest", "home_early"}
        want = ("Average of the two clubs' domestic leagues" if h and a else
                "Average of the known clubs' domestic leagues" if not (h or a) else None)
        assert want is None or e["baseline"]["label"] == want
    partial = out[1]  # home known, away unknown
    assert partial["baseline"]["label"] == rows["home_team"].iloc[1] + "'s domestic league average"
    assert any("domestic (reference" in f["detail"] for e in out for f in e["factors"] if f["id"].endswith("_attack"))


def test_explaining_does_not_change_any_prediction(data, odds_model, xb_model):
    rows = data[1]
    before = odds_model.predict(rows), odds_model.prob_at_least(rows, 24), odds_model.team_predict(rows, "home")
    ex.explain_rows(odds_model, rows, odds_used=True)
    pd.testing.assert_frame_equal(odds_model.predict(rows), before[0])
    np.testing.assert_array_equal(odds_model.prob_at_least(rows, 24), before[1])
    pd.testing.assert_frame_equal(odds_model.team_predict(rows, "home"), before[2])
    xr = xb_rows(rows)
    b = xb_model.predict(xr), xb_model.prob_at_least(xr, 24)
    ex.explain_rows(xb_model, xr, odds_used=False)
    pd.testing.assert_frame_equal(xb_model.predict(xr), b[0])
    np.testing.assert_array_equal(xb_model.prob_at_least(xr, 24), b[1])


def test_models_without_a_glm_get_no_explanation():
    class Stub:
        pass

    rows = pd.DataFrame({"league_prior_shots": [12.0, 13.0]})
    assert not ex.supports(Stub()) and ex.raw_explain(Stub(), rows) is None
    assert ex.explain_rows(Stub(), rows, odds_used=False) == [None, None]


# ----------------------------------------------------------------------------- board

def test_board_carries_explanations_for_every_route(data, odds_model, noodds_model, xb_model):
    rows = data[1].head(16).copy().reset_index(drop=True)
    rows["match_id"] = [f"x{i}" for i in range(len(rows))]
    rows.loc[10:, "competition"] = UCL
    rows["home_known"], rows["away_known"] = True, True
    rows.loc[13:, "away_known"] = False
    rows["data_quality"] = np.where(rows["away_known"], "full", "partial")
    exp = pd.Series(rows["competition"] == UCL)
    rows.loc[5:, [c for c in rows.columns if c.startswith("mkt_")]] = np.nan
    has_odds = rows[["mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25"]].notna().all(axis=1)
    prep = sc.Prepared(rows, has_odds, pd.Series("15:00", index=rows["match_id"]), data[0], odds_model, "odds_model",
                       noodds_model, "noodds_model", exp, xb_model)
    info = pd.DataFrame({"country": np.where(exp, "Europe", "Spain"), "tier": 1}, index=rows["match_id"])
    board = bd.assemble(prep, info)
    assert list(board.columns)[-1] == "explain" and board["explain"].notna().all()
    plain = board.drop(columns="explain")
    by_id = board.set_index("match_id")
    for mid, cell in by_id["explain"].items():
        e = json.loads(cell)
        route_odds = bool(by_id.loc[mid, "odds"]) and not bool(by_id.loc[mid, "experimental"])
        check_published(e, route_odds)
        assert (e["signals"]["model_gap"] is not None) == route_odds
    # the board's predictions are exactly those of a board built without explanations
    class NoExplain:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name in ("nb_", "base_", "pit_map_"):
                raise AttributeError(name)
            return getattr(self._inner, name)

    bare = sc.Prepared(rows, has_odds, prep.kickoff, data[0], NoExplain(odds_model), "odds_model", NoExplain(noodds_model),
                       "noodds_model", exp, NoExplain(xb_model))
    without = bd.assemble(bare, info)
    assert "explain" not in without.columns
    pd.testing.assert_frame_equal(without, plain)
    # deterministic text
    assert bd.assemble(prep, info)["explain"].tolist() == board["explain"].tolist()


def test_a_failing_explanation_never_costs_the_board(data, odds_model, noodds_model, monkeypatch, caplog):
    rows = data[1].head(6).copy().reset_index(drop=True)
    rows["match_id"] = [f"y{i}" for i in range(len(rows))]
    has_odds = pd.Series(True, index=rows.index)
    prep = sc.Prepared(rows, has_odds, pd.Series("15:00", index=rows["match_id"]), data[0], odds_model, "odds_model")
    info = pd.DataFrame({"country": "Spain", "tier": 1}, index=rows["match_id"])
    good = bd.assemble(prep, info)
    monkeypatch.setattr(bd.ex, "explain_rows", lambda *a, **k: 1 / 0)
    with caplog.at_level("ERROR"):
        bad = bd.assemble(prep, info)
    assert "explain" not in bad.columns and "explanations failed" in caplog.text
    pd.testing.assert_frame_equal(bad, good.drop(columns="explain"))
    assert json.loads(good["explain"].iloc[0])["signals"]["model_gap"] is None  # no no-odds model loaded: no gap


# ----------------------------------------------------------------------------- database + export

def explanation(expected=27.4, odds=False) -> dict:
    factors = [{"id": "home_attack", "side": "home", "label": "Hull attack", "detail": "15.1 shots per match, last 10 (league 13.0)", "effect": 1.2}]
    if odds:
        factors.append({"id": "market_goals", "side": "match", "label": "Goals odds",
                        "detail": "bookmaker odds point to a high-scoring match", "effect": 0.3})
    factors.append({"id": "other", "side": "match", "label": "Smaller factors", "detail": "4 smaller factors combined", "effect": -0.1})
    total = 26.0 + sum(f["effect"] for f in factors)
    return {"expected_mean": expected, "baseline": {"value": 26.0, "label": "Premier League average this season"},
            "factors": factors, "residual": round(expected - total, 1),
            "signals": {"home_matches": 38, "away_matches": 12, "odds_used": odds, "home_volatility": None,
                        "away_volatility": None, "model_gap": 0.4 if odds else None}}


def explained_board() -> pd.DataFrame:
    df = board_frame(0)
    cells = {M4: ex.dumps(explanation(27.4, odds=True)), M5: ex.dumps(explanation(27.6)), M6: None}
    df["explain"] = df["match_id"].map(cells).astype(object)
    return df


@pytest.fixture
def engine(migrated_db):
    e = make_engine(migrated_db)
    yield e
    e.dispose()


@pytest.fixture
def stored(engine):
    fx = fixtures([(c, co, t, str(d), h, a, ko) for c, co, t, d, h, a, ko in FIX])
    load.upsert_fixtures(engine, fx, today=date.today())
    res = load.import_board(engine, explained_board(), meta_at(NOW - timedelta(hours=1)))
    assert res["status"] == "inserted"
    return engine


def test_explanation_is_stored_with_the_prediction_and_exported(stored, tmp_path, migrated_db):
    with stored.connect() as c:
        got = dict(c.execute(text("SELECT match_id, explain FROM predictions")).all())
    assert json.loads(got[M4]) == explanation(27.4, odds=True) and json.loads(got[M6] or "null") is None
    # append-only: the stored explanation cannot be rewritten
    with pytest.raises(Exception, match="append-only"):
        with stored.begin() as c:
            c.execute(text("UPDATE predictions SET explain = NULL WHERE match_id = :m"), {"m": M4})
    # an identical rebuild is recognised as unchanged (the explanation is part of the content hash) ...
    assert load.import_board(stored, explained_board(), meta_at(NOW - timedelta(minutes=30)))["status"] == "unchanged"
    # ... and a board without the column still hashes like before the column existed
    plain_rows, _ = load.board_prediction_rows(board_frame(0))
    assert all("explain" not in r for r in plain_rows)

    out = tmp_path / "out"
    run_export(stored, out, today=D5 - timedelta(days=1), built_at=BUILT)
    docs = read(out)
    for name in NAMES:
        jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]}).validate(docs[name])
    by_id = {f["id"]: f for f in docs["board"]["fixtures"]}
    assert by_id[M4]["explain"] == explanation(27.4, odds=True) and list(by_id[M4])[-1] == "explain"
    assert by_id[M5]["explain"]["signals"]["odds_used"] is False and "explain" not in by_id[M6]
    text_ = (out / "board.json").read_text(encoding="utf-8")
    assert "odds_home" not in text_ and "mkt" not in text_
    res = verify_mod.verify(out, migrated_db, today=D5 - timedelta(days=1))
    assert res["ok"], res["problems"]
    assert res["checked"]["explanations"] == 2
    # same bytes from the same database state
    run_export(stored, tmp_path / "again", today=D5 - timedelta(days=1), built_at=BUILT)
    assert (tmp_path / "again" / "board.json").read_bytes() == (out / "board.json").read_bytes()


@pytest.mark.parametrize("tamper, expect", [
    (lambda e: e["factors"][0].__setitem__("effect", 1.3), "baseline + effects + residual != expected_mean"),
    (lambda e: e.__setitem__("residual", 0.1) or e.__setitem__("expected_mean", 27.5), "explain differs from the database"),
    (lambda e: e["factors"][1].__setitem__("detail", "home win priced at 1.85"), "states a number"),
    (lambda e: e["signals"].__setitem__("odds_used", False), "signals.odds_used"),
    (lambda e: e["factors"].reverse(), "`other` must be the last factor"),
    (lambda e: e["signals"].__setitem__("odds_home", 1.85), "privacy"),
])
def test_verifier_checks_the_explanation(stored, tmp_path, migrated_db, tamper, expect):
    out = tmp_path / "out"
    today = D5 - timedelta(days=1)
    run_export(stored, out, today=today, built_at=BUILT)
    doc = json.loads((out / "board.json").read_text(encoding="utf-8"))
    tamper(next(f for f in doc["fixtures"] if f["id"] == M4)["explain"])
    (out / "board.json").write_text(json.dumps(doc), encoding="utf-8")
    res = verify_mod.verify(out, migrated_db, today=today)
    assert not res["ok"] and any(expect in p for p in res["problems"]), res["problems"]


def test_verifier_allows_digits_of_team_names_in_market_wording_only():
    e = explanation(27.4, odds=True)
    e["factors"][1] = {"id": "market_strength", "side": "match", "label": "Match odds",
                       "detail": ex.strength_category(0.7, 0.1, "Schalke 04", "Hannover 96"), "effect": 0.3}
    fixture = {"id": "f1", "home": "Schalke 04", "away": "Hannover 96", "odds": True, "explain": e}
    ck = verify_mod.Checker({"board": {"fixtures": [fixture]}}, None, date.today())
    ck.explain()
    assert ck.problems == [] and ck.checked == {"explanations": 1}
    e["factors"][1]["detail"] += " (1.85)"
    ck = verify_mod.Checker({"board": {"fixtures": [fixture]}}, None, date.today())
    ck.explain()
    assert any("states a number" in p for p in ck.problems)


def test_export_refuses_an_explanation_that_does_not_add_up(engine, tmp_path):
    fx = fixtures([(c, co, t, str(d), h, a, ko) for c, co, t, d, h, a, ko in FIX])
    load.upsert_fixtures(engine, fx, today=date.today())
    bad = explanation(27.4, odds=True)
    bad["residual"] = 0.4
    df = board_frame(0)
    df["explain"] = df["match_id"].map({M4: ex.dumps(bad)}).astype(object)
    load.import_board(engine, df, meta_at(NOW - timedelta(hours=1)))
    with pytest.raises(ExportError, match="does not add up"):
        run_export(engine, tmp_path / "out", today=D5 - timedelta(days=1), built_at=BUILT)
    assert not (tmp_path / "out" / "board.json").exists()


def test_schema_rejects_odds_values_and_unknown_factors():
    v = jsonschema.Draft202012Validator({"$ref": "#/$defs/explain", "$defs": SCHEMA["$defs"]})
    assert v.is_valid(explanation(27.4, odds=True))
    for tamper in (lambda e: e["signals"].__setitem__("odds_home", 1.85),
                   lambda e: e["factors"][0].__setitem__("id", "bookmaker_price"),
                   lambda e: e["factors"][0].__setitem__("price", 1.85),
                   lambda e: e["signals"].__setitem__("home_volatility", "wild"),
                   lambda e: e.__setitem__("residual", 2.0),
                   lambda e: e.pop("signals")):
        e = explanation(27.4, odds=True)
        tamper(e)
        assert not v.is_valid(e)


def test_migration_0005_upgrades_a_0004_database_in_place_and_roundtrips(tmp_path):
    path = tmp_path / "old.db"
    assert migrate.upgrade(path, "0004_experimental") == "0004_experimental"
    engine = make_engine(path)
    fx = fixtures([(c, co, t, str(d), h, a, ko) for c, co, t, d, h, a, ko in FIX])
    with engine.begin() as c:
        c.execute(text("INSERT INTO competitions (id, name, country, tier) VALUES (1, 'Spain La Liga', 'Spain', 1)"))
        c.execute(text("INSERT INTO teams (team_id, name, country) VALUES ('spain:betis', 'Betis', 'Spain'), ('spain:getafe', 'Getafe', 'Spain')"))
        c.execute(text("INSERT INTO matches (match_id, source, competition_id, date, status, home_team_id, away_team_id, home_team, "
                       "away_team, search_key, created_at, updated_at) VALUES ('m1', 'x', 1, :d, 'scheduled', 'spain:betis', "
                       "'spain:getafe', 'Betis', 'Getafe', 'betis | getafe', datetime('now'), datetime('now'))"),
                  {"d": str(date.today() + timedelta(days=5))})
        c.execute(text("INSERT INTO prediction_runs (id, kind, source_key, status, built_at, models, params, meta, n_predictions, created_at) "
                       "VALUES (1, 'board', 'board:x', 'complete', datetime('now'), '[]', '{}', '{}', 1, datetime('now'))"))
        c.execute(text("INSERT INTO predictions (run_id, match_id, median, total_curve, created_at, pre_match, experimental) "
                       "VALUES (1, 'm1', 25, '[0.9, 0.8]', strftime('%Y-%m-%d %H:%M:%f', 'now'), 1, 0)"))
        cols = "id, run_id, match_id, median, total_curve, created_at, pre_match, experimental, data_quality"
        before = c.execute(text(f"SELECT {cols} FROM predictions")).all()
        objects = set(c.execute(text("SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')")).all())
    engine.dispose()
    assert len(fx) == len(FIX)
    assert migrate.upgrade(path, "0005_explain") == "0005_explain"  # 0006 has its own test (test_db_manual.py)
    engine = make_engine(path)
    with engine.connect() as c:
        assert c.execute(text(f"SELECT {cols} FROM predictions")).all() == before  # no row rewritten
        assert c.execute(text("SELECT explain FROM predictions")).all() == [(None,)]
        assert set(c.execute(text("SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')")).all()) == objects
        assert c.execute(text("SELECT count(*) FROM v_prediction_outcomes")).scalar() == 1
    engine.dispose()
    assert migrate.downgrade(path, "0004_experimental") == "0004_experimental"
    engine = make_engine(path)
    with engine.connect() as c:
        assert "explain" not in {r[1] for r in c.execute(text("PRAGMA table_info(predictions)"))}
        assert c.execute(text(f"SELECT {cols} FROM predictions")).all() == before
    engine.dispose()
    assert migrate.upgrade(path, "0005_explain") == "0005_explain"
