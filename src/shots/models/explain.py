"""Why a fixture's expected total is what it is: an exact additive decomposition of the fitted model.

Evidence, error sizes and limits: reports/2026-10-13_under_side_and_explanations.md.

The production models are two log-linear NB regressions (``negbin.py``): for side s,
``mu_s = exp(sum_j beta_j x_j)``; the total's raw mean is ``mu_home + mu_away`` and the published
distribution is its PIT-recalibrated version (``calibrated.py``). Nothing here is fitted or
estimated: every number is a function of the fitted coefficients and the fixture's feature row.

Decomposition (per fixture)

    expected_mean = baseline + sum(effects) + residual

* ``expected_mean``: mean of the published (recalibrated) total-shots distribution.
* ``baseline``: the league average the fixture already shows (``2 * league_prior_shots``: shots per
  match in that competition this season; the competition's long-run mean early in a season; for
  cross-border fixtures the blend of the two clubs' domestic levels).
* Reference fixture: "a league-average team against a league-average team": every rate feature equal
  to the league prior (shots on target: prior x the training SOT share), typical rest (training
  median), ten or more season matches, no division change, no missing-data flags, and market
  probabilities at their training means. ``mu0_s`` = the model's prediction for it.
  ``league_level`` = ``mu0_home + mu0_away - baseline``: what the model itself makes of that league
  level (intercept, league-prior coefficient, home advantage), as opposed to the raw average.
* Every design column j contributes ``beta_j (x_j - x0_j)`` to ``log(mu_s / mu0_s)``. Columns are
  summed into a few human factors (``column_group``); a factor may live in both side models (e.g.
  a home team's rest enters its own shots and the opponent's).
* Log contributions are turned into shots with the Shapley value of the multiplicative game
  ``v(S) = mu0_s * prod_{g in S} exp(delta_g)``: the average, over all orders in which the factors
  could be switched from the reference to the fixture's values, of the shots each one adds. It is
  order-independent, keeps the sign of the log contribution, and is exactly additive
  (``sum_g phi_g = mu_s - mu0_s``), so no linearisation error is introduced. It is computed in closed
  form from the multilinear extension (Owen 1972):
  ``phi_g = mu0 (a_g - 1) int_0^1 prod_{j != g} (1 + t (a_j - 1)) dt``, ``a_g = exp(delta_g)``,
  a polynomial integral evaluated exactly by Gauss-Legendre quadrature.
* ``calibration`` = recalibrated mean - raw mean (the PIT map reshapes the distribution).
* ``residual``: before rounding it is float noise (< 1e-9) for domestic fixtures, and for the
  cross-border model the tiny quadrature / truncation error of its mixture (< 0.01 shots). After
  rounding to one decimal it carries the rounding of the parts only (at most a few tenths), so the
  published numbers add up exactly.
* Published list: the six largest factors of at least 0.2 shots, sorted by absolute effect; all
  the others are summed into one last entry ``other`` ("Smaller factors"), so nothing is hidden in
  the residual.

Cross-border (experimental) fixtures: a club without data is replaced by ``n_profiles`` real team
profiles; the effects are averaged over those scenarios (the decomposition is linear in the
scenario) and everything belonging to the unknown club is reported as one ``*_standin`` factor.

These are attributions of THIS model, not causal statements: correlated inputs (form and odds)
share credit the way the fitted coefficients split it.
"""

from __future__ import annotations

import json
import logging
import math

import numpy as np
import pandas as pd

from shots.models.common import SIDES
from shots.models.negbin import RATE_COLS, SOT_COLS

log = logging.getLogger(__name__)

FULL_SEASON_MATCHES = 10       # ``{s}_early`` weight is 1 from the tenth season match on
MIN_EFFECT_TENTHS = 2          # factors below 0.2 shots are not listed one by one ...
MAX_FACTORS = 6                # ... nor are those beyond the six largest: they are summed into "other"
MKT_COLS = ("mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25")
SIDE_PARTS = ("attack", "defence", "venue", "rest", "early")
# Display / tie-break order of the factor ids.
FACTOR_ORDER = ["home_attack", "away_attack", "home_defence", "away_defence", "home_venue", "away_venue",
                "market_strength", "market_goals", "home_rest", "away_rest", "home_early", "away_early",
                "home_standin", "away_standin", "league_level", "calibration",
                # fin_c (no-odds candidate, shots.models.candidates.FinC): opponent-adjusted ratings, head-to-head,
                # season context and the pull of the total toward the league level
                "home_rating_attack", "away_rating_attack", "home_rating_defence", "away_rating_defence",
                "head_to_head", "season_context", "shrinkage", "other"]
# Team shot-total volatility is deliberately not reported (always null): a team's recent standard
# deviation of match totals does not predict its future spread (rank correlation of last-10 vs next-10
# std 0.04, 0.00 once the mean level is controlled for; 92,865 team-matches since 2019). See the report.
VOLATILITY = None


def _order(fid: str) -> int:
    return FACTOR_ORDER.index(fid) if fid in FACTOR_ORDER else len(FACTOR_ORDER)


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


def column_group(col: str, side: str) -> str:
    """Human factor of one design column of the side-``side`` model (s = that side, o = its opponent)."""
    team = {"s": side, "o": _other(side)}
    if col in ("mkt_p_s", "mkt_p_o", "mkt_missing"):
        return "market_strength"
    if col in ("mkt_p_over25", "mkt_o25_missing"):
        return "market_goals"
    for who in ("s", "o"):
        role = "attack" if who == "s" else "defence"  # s shoots, o concedes, in the side-s model
        if col.startswith(f"log_{who}_"):
            return f"{team[who]}_{'venue' if col.endswith('_venue_l5') else role}"
        if col == f"miss_{who}_venue":
            return f"{team[who]}_venue"
        if col == f"{who}_log_rest":
            return f"{team[who]}_rest"
        if col.startswith(f"miss_{who}_") or col in (f"{who}_no_history", f"{who}_tier_change", f"{who}_early"):
            return f"{team[who]}_early"
    return "other"  # const / log_prior (zero by construction of the reference) and any future column


def _design(nb):
    """The production feature design (``negbin._Design``) of an NB GLM or of a ``FlexNB`` candidate base."""
    return nb.sides_["home"]["design"] if hasattr(nb, "sides_") else nb.side_fits_["home"]["design"]


def reference_rows(nb, rows: pd.DataFrame) -> pd.DataFrame:
    """The same fixtures played by league-average teams (see module docstring)."""
    design = _design(nb)
    ref = rows.copy()
    prior = rows["league_prior_shots"].astype(float).fillna(design.prior_mean_).clip(lower=1.0)
    rates = sorted({name for _, name in RATE_COLS} | {name.replace("_for_", "_against_") for _, name in RATE_COLS}
                   | {name.replace("_against_", "_for_") for _, name in RATE_COLS})
    sots = sorted({name for _, name in SOT_COLS} | {name.replace("_for_", "_against_") for _, name in SOT_COLS}
                  | {name.replace("_against_", "_for_") for _, name in SOT_COLS})
    for side in SIDES:
        for name in rates:
            ref[f"{side}_{name}"] = prior
        for name in sots:
            ref[f"{side}_{name}"] = prior * design.sot_ratio_
        ref[f"{side}_rest_days"] = design.rest_med_
        ref[f"{side}_tier_change"] = 0.0
        ref[f"{side}_n_prior_season"] = float(FULL_SEASON_MATCHES)
    for c in MKT_COLS:  # market at its training mean where the fixture has odds; untouched (missing) otherwise
        if c in rows and c in design.mkt_mean_:
            ref[c] = np.where(rows[c].notna(), design.mkt_mean_[c], np.nan)
    return ref


def shapley_product(delta: np.ndarray) -> np.ndarray:
    """Shapley values of the game v(S) = prod_{g in S} exp(delta_g) - 1 (rows = fixtures, columns =
    factors). Exact: phi_g = (a_g - 1) * int_0^1 prod_{j != g} (1 + t (a_j - 1)) dt, a polynomial of
    degree G - 1 integrated by Gauss-Legendre with ceil(G / 2) nodes. Rows sum to exp(sum delta) - 1."""
    delta = np.asarray(delta, float)
    n, g = delta.shape
    if g == 0:
        return np.zeros((n, 0))
    a1 = np.expm1(delta)                                        # a_g - 1
    x, w = np.polynomial.legendre.leggauss(max(1, math.ceil(g / 2)))
    t, w = (x + 1.0) / 2.0, w / 2.0                             # nodes / weights on [0, 1]
    terms = 1.0 + t[:, None, None] * a1[None, :, :]             # (nodes, n, g); > 0 because a_g > 0
    others = terms.prod(axis=2, keepdims=True) / terms          # product over j != g
    return a1 * (w[:, None, None] * others).sum(axis=0)


def glm_effects(nb, rows: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """-> (effects in shots: one column per factor, home + away models added; reference total
    mu0_home + mu0_away; raw total mu_home + mu_away). effects.sum(axis=1) == raw - reference."""
    ref = reference_rows(nb, rows)
    effects: dict[str, np.ndarray] = {}
    mu0_total, mu_total = np.zeros(len(rows)), np.zeros(len(rows))
    for side in SIDES:
        s = nb.sides_[side]
        cols, beta = list(s["cols"]), np.asarray(s["beta"], float)
        x = s["design"].transform(rows)[cols].to_numpy(float)
        x0 = s["design"].transform(ref)[cols].to_numpy(float)
        contrib = (x - x0) * beta[None, :]
        groups = [column_group(c, side) for c in cols]
        names = sorted(set(groups), key=lambda k: (FACTOR_ORDER.index(k) if k in FACTOR_ORDER else 99, k))
        delta = np.column_stack([contrib[:, [i for i, k in enumerate(groups) if k == name]].sum(axis=1)
                                 for name in names])
        mu0 = np.exp(x0 @ beta)
        phi = mu0[:, None] * shapley_product(delta)
        for j, name in enumerate(names):
            effects[name] = effects.get(name, 0.0) + phi[:, j]
        mu0_total += mu0
        mu_total += np.exp(x @ beta)
    return pd.DataFrame(effects, index=rows.index), mu0_total, mu_total


def _is_flex(nb) -> bool:
    """A ``FlexNB`` candidate base (fin_c): production design + candidate families + total shrinkage."""
    return (hasattr(nb, "side_fits_") and not getattr(nb, "comp_intercept", False)
            and getattr(nb, "total", "") == "moment")


def _family_reference(block, rows: pd.DataFrame) -> dict[str, np.ndarray]:
    """Reference value of each family column for "an average matchup": ratings 0 (league average; the rating
    expectation is then the league level the fixture shows), no head-to-head deviation, no season-end or
    dead-rubber flag, the training median for the rest (rating uncertainty, season progress, gap to safety),
    missing flags 0."""
    out = {}
    n = len(rows)
    for k in block.cols_:
        if k == "A_log_exp":
            lvl = rows[f"adj_lvl_{block.side}"].astype(float).clip(lower=1.0)
            out[k] = np.log(lvl).fillna(block.fill_[k]).to_numpy(float)
        elif k in ("A_sd", "G_progress", "G_gap_safety_s"):
            out[k] = np.full(n, block.fill_[k])
        else:  # *_missing, ratings, E_*, G_dead_n, G_last3_*
            out[k] = np.zeros(n)
    return out


def _family_groups(block, rows: pd.DataFrame, side: str, x: np.ndarray, x0: np.ndarray, beta: np.ndarray,
                   eff: dict) -> None:
    """Add beta * (x - x0) of each family column to its factor (log scale) in ``eff``. Exact: the columns that
    mix both teams are split along their own definition (A_log_exp = log level + att_s + def_o; A_sd =
    att_sd_s + def_sd_o); whatever a split cannot place (clipping, a filled value) goes to ``other``."""
    o = _other(side)
    for j, k in enumerate(block.cols_):
        c = beta[j] * (x[:, j] - x0[:, j])
        if k == "A_log_exp":
            att = rows[f"{side}_adj_att"].astype(float).fillna(0.0).to_numpy()
            de = rows[f"{o}_adj_def"].astype(float).fillna(0.0).to_numpy()
            parts = {f"{side}_rating_attack": beta[j] * att, f"{o}_rating_defence": beta[j] * de}
        elif k == "A_sd":
            half = block.fill_[k] / 2.0
            a = rows[f"{side}_adj_att_sd"].astype(float).fillna(half).to_numpy()
            d = rows[f"{o}_adj_def_sd"].astype(float).fillna(half).to_numpy()
            parts = {f"{side}_early": beta[j] * (a - half), f"{o}_early": beta[j] * (d - half)}
        elif k in ("A_att_s", "A_sot_att_s"):
            parts = {f"{side}_rating_attack": c}
        elif k in ("A_def_o", "A_sot_def_o"):
            parts = {f"{o}_rating_defence": c}
        elif k.startswith("E_"):
            parts = {"head_to_head": c}
        elif k.startswith("G_"):
            parts = {"season_context": c}
        else:
            parts = {}
        rest = c - sum(parts.values()) if parts else c
        for name, v in parts.items():
            eff[name] = eff.get(name, 0.0) + v
        if np.any(np.abs(rest) > 0.0):
            eff["other"] = eff.get("other", 0.0) + rest


def flex_effects(nb, rows: pd.DataFrame):
    """``glm_effects`` for a ``FlexNB`` base -> (effects in shots, reference total, raw total before the
    shrinkage, raw total after it). Each side model is log-linear in [production design, family block], so the
    same Shapley decomposition applies; the shrinkage toward the league level is a total-level factor
    (``raw_explain`` reports it as ``shrinkage``)."""
    ref = reference_rows(nb, rows)
    effects: dict = {}
    n = len(rows)
    mu0_total, mu_total = np.zeros(n), np.zeros(n)
    raw = {}
    for side in SIDES:
        s = nb.side_fits_[side]
        cols, beta = list(s["cols"]), np.asarray(s["beta"], float)
        x = s["design"].transform(rows)[cols].to_numpy(float)
        x0 = s["design"].transform(ref)[cols].to_numpy(float)
        log_eff: dict = {}
        for j, c in enumerate(cols):
            g = column_group(c, side)
            log_eff[g] = log_eff.get(g, 0.0) + beta[j] * (x[:, j] - x0[:, j])
        X, X0 = x, x0
        block = s.get("block")
        if block is not None and block.cols_:
            F = block(rows)
            ref0 = _family_reference(block, rows)
            F0 = np.column_stack([ref0[k] for k in block.cols_])
            _family_groups(block, rows, side, F, F0, beta[len(cols):], log_eff)
            X, X0 = np.column_stack([x, F]), np.column_stack([x0, F0])
        names = sorted(log_eff, key=lambda k: (FACTOR_ORDER.index(k) if k in FACTOR_ORDER else 99, k))
        delta = np.column_stack([np.broadcast_to(np.asarray(log_eff[k], float), (n,)) for k in names])
        mu0 = np.exp(np.clip(X0 @ beta, -20, 20))
        phi = mu0[:, None] * shapley_product(delta)
        for j, name in enumerate(names):
            effects[name] = effects.get(name, 0.0) + phi[:, j]
        mu0_total += mu0
        raw[side] = np.exp(np.clip(X @ beta, -20, 20))
        mu_total += raw[side]
    ratio = nb._ratio(rows, raw["home"], raw["away"])
    return pd.DataFrame(effects, index=rows.index), mu0_total, mu_total, mu_total * ratio


def _calibrated_mean(pit_map, cdf: np.ndarray, groups) -> np.ndarray:
    """E[T] = sum_{k >= 1} P(T >= k) under the recalibrated CDF G(F(k))."""
    k = cdf.shape[1] - 1
    g = np.repeat(np.asarray(groups), k) if groups is not None else None
    return (1.0 - pit_map(cdf[:, :k].ravel(), g).reshape(len(cdf), k)).sum(axis=1)


def supports(model) -> bool:
    """NB GLMs (production, and the cross-border wrapper around one) and ``FlexNB`` candidate bases (fin_c)."""
    base = getattr(model, "base_", model)
    nb = getattr(base, "nb_", None)
    if nb is None or not hasattr(base, "pit_map_"):
        return False
    if hasattr(model, "base_"):  # cross-border: needs an NB GLM base
        return hasattr(nb, "sides_")
    return hasattr(nb, "sides_") or _is_flex(nb)


def raw_explain(model, rows: pd.DataFrame) -> pd.DataFrame | None:
    """Unrounded decomposition, one row per fixture: ``expected_mean``, ``raw_mean`` (before
    recalibration), ``baseline``, ``residual`` and one ``fx_<factor>`` column per factor, with
    expected_mean == baseline + sum(fx_*) + residual. None when the model is not an NB GLM
    (nothing truthful could be said)."""
    if not supports(model) or not len(rows):
        return None
    cross = hasattr(model, "base_")
    base = model.base_ if cross else model
    nb = base.nb_
    groups = rows[base.group] if getattr(base, "group", None) else None
    shrink = None
    if not cross and _is_flex(nb):
        effects, mu0, unshrunk, raw = flex_effects(nb, rows)
        cdf = nb.total_cdf(rows)
        mixture = raw
        shrink = raw - unshrunk
    elif not cross:
        effects, mu0, raw = glm_effects(nb, rows)
        cdf = np.cumsum(nb.total_pmf(rows), axis=1)
        mixture = raw
    else:
        effects, mu0, raw = _cross_border_effects(model, nb, rows)
        cdf = model._cdfs(rows)["total"]
        mixture = (1.0 - cdf).sum(axis=1)  # mean of the mixture actually published (before recalibration)
    expected = _calibrated_mean(base.pit_map_, cdf, groups)
    prior = rows["league_prior_shots"].astype(float).fillna(_design(nb).prior_mean_).clip(lower=1.0)
    baseline = 2.0 * prior.to_numpy()
    out = pd.DataFrame({"expected_mean": expected, "raw_mean": mixture, "baseline": baseline}, index=rows.index)
    fx = effects.copy()
    fx["league_level"] = mu0 - baseline
    if shrink is not None:
        fx["shrinkage"] = shrink
    fx["calibration"] = expected - mixture
    for name in fx.columns:
        out[f"fx_{name}"] = fx[name].to_numpy(float)
    out["residual"] = out["expected_mean"] - out["baseline"] - fx.sum(axis=1).to_numpy()
    return out


def _cross_border_effects(model, nb, rows: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """``glm_effects`` averaged over the model's team-profile scenarios; the factors of a club without
    data collapse into ``<side>_standin``."""
    known = {side: model._known(rows, side) for side in SIDES}
    pos = np.arange(len(rows))
    parts = []
    mu0, raw = np.zeros(len(rows)), np.zeros(len(rows))
    for unknown in ((False, False), (True, False), (False, True), (True, True)):
        mask = (~known["home"] == unknown[0]) & (~known["away"] == unknown[1])
        if not mask.any():
            continue
        idx = pos[mask]
        scen, s = model._expand(rows.iloc[idx], unknown)      # the model's own scenario rows
        eff, m0, m = glm_effects(nb, scen)
        mean = pd.DataFrame(eff.to_numpy().reshape(len(idx), s, -1).mean(axis=1), columns=eff.columns,
                            index=rows.index[idx])
        for side, miss in zip(SIDES, unknown):
            if miss:
                own = [c for c in mean.columns if c in {f"{side}_{p}" for p in SIDE_PARTS}]
                mean[f"{side}_standin"] = mean[own].sum(axis=1)
                mean = mean.drop(columns=own)
        parts.append(mean)
        mu0[idx] = m0.reshape(len(idx), s).mean(axis=1)
        raw[idx] = m.reshape(len(idx), s).mean(axis=1)
    effects = pd.concat(parts).reindex(rows.index).fillna(0.0)
    return effects, mu0, raw


# ----------------------------------------------------------------------------- wording

def _f1(x) -> str:
    return f"{float(x):.1f}"


def _val(row, key):
    v = row.get(key)
    return None if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v) else float(v)


def league_label(competition: str, country: str | None) -> str:
    """'England Premier League' -> 'Premier League' (same rule as the export's league names)."""
    if country and competition.startswith(country + " ") and len(competition) > len(country) + 1:
        return competition[len(country) + 1:]
    return competition


def strength_category(p_home: float | None, p_away: float | None, home: str, away: str) -> str:
    """Words for the 1X2 market; never a price or a probability."""
    if p_home is None or p_away is None:
        return "bookmaker odds on the match result"
    fav, p, gap = (home, p_home, p_home - p_away) if p_home >= p_away else (away, p_away, p_away - p_home)
    if p >= 0.65:
        return f"bookmaker odds make {fav} strong favourite"
    if p >= 0.50:
        return f"bookmaker odds make {fav} favourite"
    if gap >= 0.08:
        return f"bookmaker odds make {fav} slight favourite"
    return "bookmaker odds rate the teams about level"


def goals_category(p_over25: float | None) -> str:
    """Words for the over/under 2.5 goals market; never a price or a probability."""
    if p_over25 is None:
        return "bookmaker odds on goals"
    if p_over25 >= 0.58:
        return "bookmaker odds point to a high-scoring match"
    if p_over25 <= 0.44:
        return "bookmaker odds point to a low-scoring match"
    return "bookmaker odds point to an average number of goals"


def _factor_text(fid: str, row: dict, ctx: dict) -> tuple[str, str, str]:
    """-> (side, label, detail) of one factor for one fixture. ``row``: the fixture's feature row."""
    home, away, cross = row["home_team"], row["away_team"], ctx["cross"]
    side = fid.split("_")[0] if fid.split("_")[0] in SIDES else "match"
    team = {"home": home, "away": away}.get(side)
    level = ctx["prior"]
    ref = f"reference {_f1(level)}" if cross else f"league {_f1(level)}"
    n_all = int(_val(row, f"{side}_n_prior_matches") or 0) if team else 0
    last = f"last {min(n_all, 10)}" + (" domestic" if cross else "")
    part = fid.split("_", 1)[1] if team else fid
    if part == "attack":
        v = _val(row, f"{side}_shots_for_l10")
        return side, f"{team} attack", (f"{_f1(v)} shots per match, {last} ({ref})" if v is not None
                                       else "no previous matches")
    if part == "defence":
        v = _val(row, f"{side}_shots_against_l10")
        return side, f"{team} defence", (f"concedes {_f1(v)} shots per match, {last} ({ref})" if v is not None
                                        else "no previous matches")
    if part == "venue":
        where = "home" if side == "home" else "away"
        f, a = _val(row, f"{side}_shots_for_venue_l5"), _val(row, f"{side}_shots_against_venue_l5")
        label = f"{team} at home" if side == "home" else f"{team} away"
        if f is None or a is None:
            return side, label, f"no {where} matches yet"
        return side, label, f"{_f1(f)} shots for, {_f1(a)} against in its latest {where} matches (up to 5)"
    if part == "rest":
        d = _val(row, f"{side}_rest_days")
        typical = f"typical {ctx['rest_med']:.0f}"
        if d is None:
            return side, f"{team} rest", "no previous match"
        if d > 30:
            return side, f"{team} rest", f"more than 30 days since its last match ({typical})"
        return side, f"{team} rest", f"{d:.0f} day{'s' if d != 1 else ''} since its last match ({typical})"
    if part == "early":
        bits = []
        k = _val(row, f"{side}_n_prior_season")
        tc = _val(row, f"{side}_tier_change")
        if n_all == 0:
            bits.append("no previous matches, league average stands in")
        elif k is not None and k < FULL_SEASON_MATCHES:
            bits.append(f"{k:.0f} match{'es' if k != 1 else ''} played this season")
        if tc:
            bits.append("first match after moving " + ("down" if tc > 0 else "up") + " a division")
        return side, f"{team} early season", "; ".join(bits) or "early-season adjustment"
    if part == "rating_attack":
        return side, f"{team} attack rating", _rating_text(_val(row, f"{side}_adj_att"), "creates")
    if part == "rating_defence":
        return side, f"{team} defence rating", _rating_text(_val(row, f"{side}_adj_def"), "concedes")
    if part == "standin":
        return side, f"{team} (no data)", f"no shot data: average of {ctx['n_profiles']} typical top-division teams"
    if fid == "market_strength":
        return "match", "Match odds", strength_category(_val(row, "mkt_p_home"), _val(row, "mkt_p_away"), home, away)
    if fid == "market_goals":
        return "match", "Goals odds", goals_category(_val(row, "mkt_p_over25"))
    if fid == "league_level":
        avg = "reference level" if cross else "league average"
        return "match", "League level", (f"the model rates an average matchup at this level at {_f1(ctx['reference'])} "
                                         f"shots ({avg} {_f1(2 * level)})")
    if fid == "head_to_head":
        n = int(_val(row, "h2h_n") or 0)
        mean = _val(row, "h2h_total_wmean")
        if n == 0 or mean is None:
            return "match", "Head to head", "no previous meetings in the data"
        return "match", "Head to head", (f"{n} previous meeting{'s' if n != 1 else ''}, {_f1(mean)} total shots "
                                         "on average (recent ones count more)")
    if fid == "season_context":
        return "match", "Season context", _context_text(row, home, away)
    if fid == "shrinkage":
        return "match", "Pull toward league level", ("forecasts far from the league average are pulled back "
                                                     "toward it, as past seasons showed they should be")
    if fid == "calibration":
        scope = "this league" if ctx["own_calibration"] else "all leagues"
        return "match", "Calibration", f"adjustment from past forecast errors in {scope}"
    return "match", "Smaller factors", "remaining terms of the model"


def _rating_text(v, verb: str) -> str:
    if v is None:
        return "no rating yet"
    pct = (math.exp(v) - 1.0) * 100.0
    if abs(pct) < 0.5:
        return f"{verb} about as many shots as an average team (adjusted for opponents and venue)"
    more = "more" if pct > 0 else "fewer"
    return f"{verb} {abs(pct):.0f}% {more} shots than an average team (adjusted for opponents and venue)"


def _context_text(row: dict, home: str, away: str) -> str:
    bits = []
    p = _val(row, "ctx_progress")
    if p is not None:
        bits.append(f"season {p * 100:.0f}% played")
    for side, team in (("home", home), ("away", away)):
        left = _val(row, f"{side}_ctx_games_left")
        if _val(row, f"{side}_ctx_dead"):
            bits.append(f"{team}: nothing left to play for")
        elif left is not None and left <= 3:
            bits.append(f"{team}: one of its last three league matches")
    return "; ".join(bits)[:160] or "stage of the season"


def _baseline_label(row: dict, cross: bool, league: str) -> str:
    if cross:
        hk, ak = bool(row.get("home_known", True)), bool(row.get("away_known", True))
        if hk and ak:
            return "Average of the two clubs' domestic leagues"
        if hk or ak:
            return f"{row['home_team'] if hk else row['away_team']}'s domestic league average"
        return "Average of the known clubs' domestic leagues"
    n = _val(row, "league_prior_n")
    return f"{league} average this season" if n is not None and n >= 20 else f"{league} long-run average"


def explain_rows(model, rows: pd.DataFrame, *, odds_used: bool, league: pd.Series | None = None,
                 gap_model=None, gap_rows: pd.DataFrame | None = None) -> list[dict | None]:
    """Published explanation (docs/static-api.md, ``explain``) per fixture of ``rows``, in row order.

    odds_used: the odds-aware model scored these rows. league: display name per row (default: the
    competition). gap_model / gap_rows: the other variant of the two-model set-up (the no-odds model
    and the same fixtures with market features blanked); when given, ``signals.model_gap`` =
    expected mean of ``model`` minus expected mean of ``gap_model``.
    Deterministic: a pure function of the fitted model and the rows. [None, ...] when unsupported."""
    raw = raw_explain(model, rows)
    if raw is None:
        return [None] * len(rows)
    cross = hasattr(model, "base_")
    base = model.base_ if cross else model
    design = _design(base.nb_)
    gap = None
    if gap_model is not None and gap_rows is not None:
        other = raw_explain(gap_model, gap_rows)
        if other is not None:
            gap = raw["expected_mean"].to_numpy() - other["expected_mean"].to_numpy()
    own_curves = getattr(base.pit_map_, "curves_", {})
    fx_cols = [c for c in raw.columns if c.startswith("fx_")]
    out = []
    for i, (idx, row) in enumerate(rows.iterrows()):
        row = row.to_dict()
        r = raw.loc[idx]
        comp = str(row.get("competition"))
        ctx = {"cross": cross, "prior": r["baseline"] / 2.0, "reference": r["baseline"] + r["fx_league_level"],
               "rest_med": design.rest_med_, "n_profiles": getattr(model, "n_profiles", 0),
               "own_calibration": (not cross) and comp in own_curves}
        e10, b10 = int(round(10 * r["expected_mean"])), int(round(10 * r["baseline"]))
        ranked = sorted(((c[3:], float(r[c])) for c in fx_cols),
                        key=lambda f: (-abs(round(10 * f[1])), _order(f[0]), f[0]))
        factors, rest = [], 0.0
        for fid, value in ranked:
            t = int(round(10 * value))
            if fid == "other" or abs(t) < MIN_EFFECT_TENTHS or len(factors) >= MAX_FACTORS:
                rest += value
                continue
            side, label, detail = _factor_text(fid, row, ctx)
            factors.append((t, {"id": fid, "side": side, "label": label, "detail": detail, "effect": t / 10}))
        n_rest = len(ranked) - len(factors) - sum(1 for fid, v in ranked if fid == "other" or v == 0.0)
        t = int(round(10 * rest))
        if t:  # the factors not listed one by one, so that the list still adds up
            factors.append((t, {"id": "other", "side": "match", "label": "Smaller factors",
                                "detail": f"{n_rest} smaller factor{'s' if n_rest != 1 else ''} combined",
                                "effect": t / 10}))
        residual = e10 - b10 - sum(f[0] for f in factors)
        name = str(league.loc[idx]) if league is not None else comp
        out.append({
            "expected_mean": e10 / 10,
            "baseline": {"value": b10 / 10, "label": _baseline_label(row, cross, name)},
            "factors": [f[1] for f in factors],
            "residual": residual / 10,
            "signals": {
                "home_matches": int(_val(row, "home_n_prior_matches") or 0),
                "away_matches": int(_val(row, "away_n_prior_matches") or 0),
                "odds_used": bool(odds_used),
                "home_volatility": VOLATILITY,
                "away_volatility": VOLATILITY,
                "model_gap": None if gap is None or math.isnan(gap[i]) else round(float(gap[i]), 1) + 0.0,
            },
        })
    return out


def dumps(explanation: dict | None) -> str | None:
    """Compact, stable JSON text of one explanation (the board parquet stores it as a string)."""
    return None if explanation is None else json.dumps(explanation, ensure_ascii=False, separators=(",", ":"))


__all__ = ["explain_rows", "raw_explain", "glm_effects", "flex_effects", "shapley_product", "reference_rows", "column_group",
           "strength_category", "goals_category", "league_label", "dumps", "supports", "FACTOR_ORDER"]
