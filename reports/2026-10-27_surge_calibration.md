# Surge: calibrating the over-shoot correction of the odds route (2026-10-27)

Author: modeler. This follows `reports/2026-10-26_overshoot_patterns.md`. "Surge" is the public name of the
out-of-sample over-shoot correction. Its internal id is `cand_odds_surge`; the undamped first version stays as
`cand_odds_oos`.

**Nothing in production changed.** `ODDS_MODEL` is still `"negbin_pit_comp"`, and fin_c and the median switch are
untouched. Nothing is committed or pushed.

## Summary

- **Problem.** The undamped correction made P(24+) over-confident at the top. In the 0.8-0.9 band it stated 83.5%
  and realised 81.5% (production: 83.4% stated, 82.6% realised). Its 70%+ calls hit 74.9% (production 75.4%).
- **Cause.** The matches in the top band are over-predicted by 0.4 to 0.8 shots in both models. That is a
  location problem, not a dispersion one (predictive variance is not too small there). Large positive corrections
  were slightly too strong out of sample: matches with a correction above +3% came out 0.2 shots below the
  corrected mean. Selecting confident calls amplifies that.
- **Fix.** Damp the whole fitted correction to 40% (`oos_scale = 0.4`, chosen on 2019/20 to 2022/23 by a rule
  fixed in advance).
- **Result, monthly refit, 2019/20 to 2025/26.**
  - RPS -0.0082 [-0.0094, -0.0068], pinball -0.0030 [-0.0039, -0.0021], log score -0.0035 [-0.0048, -0.0020] vs
    production.
  - 80% of the undamped RPS gain is kept, with CIs about half as wide. On the confirmation seasons and on 2025/26
    alone the gain is now significant.
  - 70%+ calls hit 75.5% (n = 6,829) vs 75.4% (n = 6,591) for production.
  - The 0.8-0.9 band realises 82.1% (stated 83.4%), up from 81.5% and 0.5pp below production.
  - Calibration error of the whole curve (ECE) is back to production's level: 0.10pp vs 0.11pp (undamped 0.21pp).
- **Cost.** Damping gives back part of the season-end fix. A team in its last 3 matches: excess +3.1pp (production
  +4.9, undamped +0.4).
- **Explanation factor.** `surge`, label "Surge signals". It lists the signals behind the correction (season end,
  head-to-head, low- or high-goal run, momentum, plus ratings and league trend). It is emitted only by the Surge
  model, so only with the switch on.
- **Recommendation.** Surge now meets the decision rule on the odds route (CIs below 0 pooled, in confirmation and on
  2025/26; no competition worse pooled), with one exception: the Scotland Championship 90% cell is at 89.5%, which
  production also misses (89.6%). Adoption needs:
  - your decision on that cell, and on the one-route exception to protocol rule 4 (as for fin_c);
  - an independent verification;
  - a dry run.

## 1. What was tried

All options are causal and walk-forward:
- every correction is fitted on training rows only (the latest about 2 seasons, as predicted by a model fitted
  before them);
- the per-competition PIT recalibration of production follows unchanged;
- discovery = 2019/20 to 2022/23, monthly refit.

**Selection rule, written before the runs.** Among the variants, take the largest RPS gain on discovery subject to:
(a) 70%+ P(24+) calls hit at least as often as production's on discovery; (b) the 0.8-0.9 band realises at least as
much as production's.

| variant (discovery, monthly) | ΔRPS vs production | Δpinball | calls n / stated / hit | band 0.8-0.9 stated / realised | rule |
|---|---|---|---|---|---|
| production | | | 2,875 / 76.3 / **74.7** | 83.4 / **81.2** | |
| undamped (`cand_odds_oos`) | -0.0122 [-0.0163, -0.0083] | -0.0060 | 3,435 / 76.4 / 73.9 | 83.5 / 81.5 | (a) fails |
| damped 0.8 | -0.0129 [-0.0161, -0.0097] | -0.0062 | 3,308 / 76.3 / 74.3 | 83.5 / 81.6 | (a) fails |
| damped 0.6 | -0.0120 [-0.0145, -0.0096] | -0.0051 | 3,177 / 76.3 / 74.5 | 83.5 / 81.2 | (a) fails |
| **damped 0.4 (Surge)** | **-0.0095 [-0.0112, -0.0079]** | **-0.0041** | 3,036 / 76.3 / **75.0** | 83.5 / **81.2** | **passes** |
| cap ±2% (log), no damping | +0.0012 [-0.0021, +0.0047] | +0.0000 | 2,968 / 75.9 / 74.5 | 83.4 / 82.1 | gain lost |
| damped 0.6 + cap ±2% | -0.0031 [-0.0050, -0.0010] | -0.0012 | 2,903 / 76.0 / 75.0 | 83.4 / 82.6 | passes, small gain |
| damped 0.4, season-end terms undamped | -0.0095 [-0.0119, -0.0070] | -0.0038 | 3,145 / 76.3 / 74.7 | 83.4 / 81.7 | (a) borderline; 3 cells below q |

Looked at and dropped before any refit, by emulation on the stored curves:

- **Level-aware PIT recalibration on the previous two out-of-sample seasons** (a second PIT map per predicted-total
  band).
  - Calls hit rises (75.5% to 76.5%), but RPS gets worse by +0.002.
  - The 0.8-0.9 band does not improve (82.2%).
- **A hinge correction for high predicted totals** (fitted on the previous two seasons): no change in the band
  (81.9%).
- **Refitting the dispersion:** the top band's predictive variance is not too small (realised / stated variance
  0.93 to 0.97). The problem is location, not spread.

Why caps fail: the value of the correction sits in its large values (season end, strong head-to-head signals).
Cutting them removes the gain. Damping keeps the ranking of matches and only shrinks the step.

## 2. Decision run: monthly refit, Surge vs production, odds route

Paired on identical rows. 95% CIs from a day-block bootstrap, 1,000 resamples. Negative = Surge better.

| slice | n | RPS Surge / prod | ΔRPS | Δpinball | Δlog score |
|---|---|---|---|---|---|
| **all 2019/20-2025/26** | 49,643 | 3.0743 / 3.0824 | **-0.0082 [-0.0094, -0.0068]** | **-0.0030 [-0.0039, -0.0021]** | -0.0035 [-0.0048, -0.0020] |
| discovery 2019/20-2022/23 | 28,179 | 3.0230 / 3.0325 | -0.0095 [-0.0112, -0.0079] | -0.0041 [-0.0052, -0.0029] | -0.0044 [-0.0063, -0.0025] |
| **confirmation 2023/24-2025/26** | 21,464 | 3.1416 / 3.1479 | **-0.0063 [-0.0082, -0.0046]** | -0.0015 [-0.0027, -0.0003] | -0.0023 [-0.0043, -0.0005] |
| 2025/26 | 7,094 | 3.1190 / 3.1237 | -0.0046 [-0.0080, -0.0017] | +0.0006 [-0.0015, +0.0026] | -0.0005 [-0.0043, +0.0031] |
| 2026/27 to 21 Oct (informational) | 1,792 | 3.3733 / 3.3808 | -0.0075 [-0.0149, -0.0006] | -0.0060 [-0.0118, -0.0010] | +0.0019 [-0.0045, +0.0110] |
| *undamped, all seasons, for reference* | 49,643 | 3.0721 / 3.0824 | -0.0103 [-0.0135, -0.0071] | -0.0037 [-0.0053, -0.0020] | -0.0039 [-0.0063, -0.0016] |
| *undamped, confirmation* | 21,464 | 3.1402 / 3.1479 | -0.0078 [-0.0123, -0.0036] | -0.0006 [-0.0030, +0.0019] (n.s.) | -0.0029 [-0.0063, +0.0010] (n.s.) |
| *undamped, 2025/26* | 7,094 | | -0.0047 [-0.0131, +0.0025] (n.s.) | +0.0002 | -0.0019 |

**Ship rule (CLAUDE.md rule 3), pooled 2019/20 to 2025/26:**
- Coverage 73.2 / 82.5 / 91.5 (production 73.4 / 82.7 / 91.5).
- Mid-coverage 69.8 / 79.7 / 89.6, within ±2pp.
- Pinball 1.4035 vs `team_avg` 1.4279, and below `team_avg` in every competition (table below).
- **One cell below q:** Scotland Championship at 90%, 89.5% (n = 1,172). Production misses the same cell (89.6%),
  and also sits at 89.97% in Scotland League Two.
- Confirmation: same single cell at 89.6%; production has none in that slice.

**Competition guard, pooled:**
- RPS: no competition worse; 18 of 21 significantly better.
- Pinball: no competition worse.
- 2025/26 alone: Scotland Championship flagged (RPS and pinball; n = 180), as for the undamped version.
- 2026/27: none on RPS; Greece and Serie B on pinball (small n).

Per competition, pooled monthly:

| competition | n | cov 70/80/90 Surge | prod | mid Surge | pinball Surge / prod / team_avg | ΔRPS [CI] |
|---|---|---|---|---|---|---|
| Belgium Pro League | 2,079 | 74.3/83.5/91.2 | 74.5/83.3/91.2 | 70.7/80.6/89.6 | 1.441 / 1.445 / 1.471 | -0.0113 [-0.0175, -0.0045] |
| England Championship | 3,864 | 72.9/82.2/92.0 | 73.1/82.5/92.0 | 69.8/79.7/89.8 | 1.388 / 1.388 / 1.409 | -0.0057 [-0.0095, -0.0016] |
| England League One | 3,712 | 73.7/82.9/91.8 | 73.8/82.9/91.8 | 70.2/79.8/90.0 | 1.421 / 1.426 / 1.450 | -0.0101 [-0.0150, -0.0056] |
| England League Two | 3,752 | 73.6/82.7/91.7 | 73.7/83.0/91.8 | 70.1/79.8/90.0 | 1.406 / 1.407 / 1.427 | -0.0099 [-0.0141, -0.0057] |
| England Premier League | 2,660 | 73.2/82.3/91.2 | 73.5/82.4/91.4 | 70.3/79.7/89.5 | 1.457 / 1.461 / 1.478 | -0.0089 [-0.0145, -0.0035] |
| France Ligue 1 | 2,337 | 74.0/83.1/91.8 | 73.9/83.1/91.8 | 69.9/80.4/89.5 | 1.398 / 1.403 / 1.423 | -0.0058 [-0.0104, -0.0007] |
| France Ligue 2 | 2,407 | 72.7/82.5/91.3 | 73.2/82.7/91.5 | 68.9/79.7/89.7 | 1.372 / 1.376 / 1.394 | -0.0117 [-0.0166, -0.0069] |
| Germany 2. Bundesliga | 2,142 | 72.7/81.0/91.5 | 72.6/81.3/91.5 | 69.3/78.5/89.6 | 1.464 / 1.464 / 1.494 | +0.0004 [-0.0049, +0.0062] |
| Germany Bundesliga | 2,141 | 73.3/82.4/90.9 | 73.5/82.5/91.4 | 69.4/79.4/89.5 | 1.448 / 1.447 / 1.474 | -0.0082 [-0.0146, -0.0019] |
| Greece Super League | 1,668 | 75.0/83.8/92.3 | 74.8/84.1/92.3 | 71.2/80.7/90.3 | 1.253 / 1.259 / 1.267 | -0.0082 [-0.0138, -0.0023] |
| Italy Serie A | 2,660 | 74.3/83.5/92.0 | 74.5/83.3/92.0 | 70.9/80.8/90.2 | 1.400 / 1.402 / 1.425 | -0.0108 [-0.0166, -0.0053] |
| Italy Serie B | 2,659 | 72.8/83.2/92.0 | 73.6/83.3/92.0 | 70.0/80.3/90.2 | 1.479 / 1.482 / 1.497 | -0.0077 [-0.0129, -0.0021] |
| Netherlands Eredivisie | 2,068 | 72.9/82.3/91.7 | 73.2/82.5/91.7 | 70.0/79.6/90.2 | 1.497 / 1.498 / 1.534 | -0.0086 [-0.0144, -0.0025] |
| Portugal Primeira Liga | 2,142 | 72.3/81.9/90.6 | 72.8/82.5/90.7 | 69.5/78.9/88.8 | 1.381 / 1.379 / 1.402 | -0.0034 [-0.0101, +0.0031] |
| Scotland Championship | 1,172 | 72.0/80.9/**89.5** | 71.3/80.8/**89.6** | 67.7/77.9/87.8 | 1.343 / 1.345 / 1.351 | -0.0093 [-0.0167, -0.0018] |
| Scotland League One | 1,148 | 72.6/81.8/91.0 | 73.3/82.0/90.9 | 69.0/78.8/89.2 | 1.280 / 1.285 / 1.289 | -0.0039 [-0.0116, +0.0033] |
| Scotland League Two | 1,146 | 72.2/81.8/90.1 | 71.8/81.7/90.0 | 68.6/78.6/88.0 | 1.283 / 1.289 / 1.306 | -0.0100 [-0.0174, -0.0027] |
| Scotland Premiership | 1,547 | 73.0/82.2/91.3 | 73.2/82.5/91.5 | 69.9/79.8/89.6 | 1.454 / 1.455 / 1.491 | -0.0121 [-0.0190, -0.0045] |
| Spain La Liga | 2,660 | 73.0/82.5/91.4 | 73.1/82.9/91.7 | 70.0/79.6/89.3 | 1.355 / 1.360 / 1.400 | -0.0098 [-0.0143, -0.0051] |
| Spain Segunda Division | 3,234 | 73.4/82.8/91.6 | 73.3/83.0/91.4 | 69.7/79.9/89.5 | 1.349 / 1.354 / 1.366 | -0.0056 [-0.0100, -0.0017] |
| Turkey Super Lig | 2,445 | 72.5/82.2/91.2 | 72.6/82.5/91.5 | 68.9/79.4/89.1 | 1.454 / 1.461 / 1.478 | -0.0104 [-0.0155, -0.0050] |

## 3. High-threshold calls: P(total >= 24)

Each band cell is stated / realised (n).

| | production | undamped | **Surge** |
|---|---|---|---|
| band 0.5-0.6 | 54.9 / 55.2 (11,491) | 55.0 / 54.7 (11,042) | 55.0 / 55.1 (11,361) |
| band 0.6-0.7 | 64.7 / 64.9 (9,451) | 64.7 / 64.4 (9,513) | 64.7 / 64.7 (9,552) |
| band 0.7-0.8 | 74.2 / 73.3 (5,156) | 74.2 / 72.7 (5,647) | 74.2 / 73.5 (5,308) |
| **band 0.8-0.9** | 83.4 / 82.6 (1,373) | 83.5 / 81.5 (1,701) | 83.4 / **82.1** (1,461) |
| band 0.9-1.0 | 91.3 / 93.5 (62) | 91.7 / 93.8 (80) | 91.5 / 95.0 (60) |
| **70%+ calls, all seasons: n, stated, hit** | 6,591, 76.3, 75.4 | 7,428, 76.5, 74.9 | **6,829, 76.3, 75.5** |
| 70%+ calls, discovery | 2,875, 76.3, 74.7 | 3,435, 76.4, 73.9 | 3,036, 76.3, 75.0 |
| 70%+ calls, confirmation | 3,716, 76.3, 76.0 | 3,993, 76.5, 75.8 | 3,793, 76.4, 75.9 |
| 70%+ calls, 2025/26 | 1,123, 75.4, 77.9 | 1,208, 75.6, 77.4 | 1,160, 75.4, 77.4 |
| 70%+ calls, 2026/27 to date | 655, 78.7, 79.1 | 628, 78.4, 79.5 | 641, 78.6, 79.3 |

- Surge makes 3.6% more 70%+ calls than production, and they hit as often overall: +0.1pp pooled, +0.3pp on
  discovery, -0.1pp on confirmation, -0.5pp on 2025/26. The differences are well inside one standard error
  (about 0.6pp to 1.2pp).
- The 0.8-0.9 band moves from 2.0pp to 1.3pp over-stated. Production is 0.8pp over-stated in the same band. The
  remaining over-statement is shared with production: a selection effect on the most confident predictions,
  visible in both models.
- Whole-curve reliability: ECE 0.10pp (production 0.11pp, undamped 0.21pp).

## 4. Did Surge keep the over-shoot fixes?

Over-shoot (5+ above expected) realised / stated (excess, pp), monthly, odds route:

| segment | production | undamped | Surge |
|---|---|---|---|
| last 3 matches (either team) | 26.4 / 21.5 (+4.9) | 22.2 / 21.8 (+0.4) | 24.6 / 21.5 (+3.1) |
| dead rubber | 27.4 / 21.5 (+5.9) | 22.2 / 21.9 (+0.3) | 24.9 / 21.6 (+3.3) |
| h2h total - our expected >= +4 | 22.8 / 21.1 (+1.8) | 21.4 / 20.8 (+0.6) | 22.1 / 20.9 (+1.3) |
| goals in both teams' last 5, bottom fifth | 22.0 / 20.4 (+1.6) | 20.3 / 20.5 (-0.2) | 21.3 / 20.4 (+0.9) |
| shot-volume trend, top fifth | 22.5 / 21.4 (+1.2) | 21.5 / 21.2 (+0.2) | 22.1 / 21.3 (+0.9) |
| momentum (last 3), bottom fifth | 20.3 / 21.1 (-0.8) | 20.2 / 20.9 (-0.7) | 20.4 / 21.0 (-0.5) |
| all | 21.4 / 21.3 (+0.2) | 20.9 / 21.1 (-0.2) | 21.2 / 21.1 (+0.1) |

- Damping is uniform, so every signal is corrected by about 40% of the fitted step.
- Exempting the season-end terms from the damping was tested (section 1). It keeps the RPS gain, but its calls are
  only at production's level (74.7%) and it adds two Scottish cells below q. Not chosen.
- Season end stays the clearest remaining over-shoot pattern on the odds route. It is fine to show it as a product
  signal (the "Surge Watch" page).

## 5. Explanation factor "Surge signals"

- **Decomposition.** `src/shots/models/explain.py` splits the total-level factor of a model with out-of-sample
  terms into:
  - `shrinkage` (pull toward the league level);
  - `surge` (the correction), exactly: raw total = unshrunk × exp(s × (eta_shrink + sum t_j)), with damping s and
    the optional cap.
- **Signals.** The surge effect is shared among the signals in proportion to their log terms, stored as `sg_<term>`
  in `raw_explain`. `detail` lists up to four signals, largest first, in shots, for example
  "season end +0.9; low-goal run +0.4; momentum -0.2". The public names are `SURGE_SIGNALS`:

  | signals | terms |
  |---|---|
  | season end | last 3 / dead |
  | head-to-head | h2h |
  | low-goal run / high-goal run | goals; the name follows the sign |
  | momentum | mom, pace |
  | ratings | disagree |
  | league trend | ldrift |

- **Gating.** Only Surge (and the undamped `cand_odds_oos`) emit it, so it appears only with
  `SHOTS_ODDS_MODEL=cand_odds_surge`. fin_c and production explanations are unchanged (test).
- **Schema.** `docs/static-api.schema.json` (factor id enum) and `docs/static-api.md` gained `surge`. The change is
  additive; nothing emits it with the switch off. `src/shots/export` and `frontend/` were not touched.

## 6. Code

| file | change |
|---|---|
| `src/shots/models/candidates.py` | `FlexNB(oos_scale, oos_cap, oos_keep)`: damping of the whole fitted log correction, an optional cap on the surge term, optional undamped terms. `ratio_parts` / `surge_log` expose the split. New candidate `cand_odds_surge` (shrink + out-of-sample terms, about 2 seasons carved, `oos_scale = 0.4`). fin_c's path is unchanged (`oos_` is None). |
| `src/shots/models/explain.py` | factor `surge` ("Surge signals"), `_surge_split`, `_surge_text`, `SURGE_SIGNALS` |
| `src/shots/config.py` | `CANDIDATE_ODDS_MODELS = ("cand_odds_surge", "cand_odds_oos")`; `ODDS_MODEL` still `"negbin_pit_comp"` |
| `src/shots/predict/scan.py` | docstring only (the route already takes any candidate odds model) |

Tests:
- `tests/test_models_oos.py`:
  - damping;
  - cap;
  - undamped terms;
  - exact surge / shrinkage split and full additivity;
  - label and detail length;
  - no surge factor for fin_c;
  - signal text.
- `tests/test_odds_candidate_route.py`: now on `cand_odds_surge`.

Reproduce (scratch caches; `SHOTS_SCREEN_EXTRA` = a candidate build that includes the form family):

    python -m shots.models.screen run --models cand_odds_surge --variants odds --seasons 2019/2020,...,2026/2027 --refit month --cache <dir>
    python -m shots.evaluation.overshoot compare --cache <dir> --pairs cand_odds_surge:cand_nbmle:odds --refit month --segments --out <json>

## 7. Recommendation

- Surge is the odds-route candidate to adopt if you want one. It meets the decision rule's gain and guard
  conditions pooled, on the confirmation seasons and on 2025/26. Its confident calls are as accurate as
  production's, with about 4% more of them.
- Before switching `ODDS_MODEL` to `"cand_odds_surge"`, you need to decide on:
  1. **The Scotland Championship 90% cell** (89.5%; production 89.6% in the same cell). Accept it as "no worse than
     production", or require a fix for both models.
  2. **The protocol rule 4 exception:** odds route only, as recorded for fin_c.
  3. **An independent verification and a dry run:** `SHOTS_ODDS_MODEL=cand_odds_surge python -m shots update
     --days 30` against a scratch DB and board.
