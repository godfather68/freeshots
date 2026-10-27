# Independent verification of "Surge" (`cand_odds_surge`) for the odds route (2026-10-27)

Author: evaluator. Subject: the modeler's claim (`reports/2026-10-27_surge_calibration.md`) that Surge, the production
odds NB plus an out-of-sample correction of the total damped to 40%, can replace `negbin_pit_comp` on the odds route.

No project code, config or test was changed. `ODDS_MODEL` is still `"negbin_pit_comp"`. Nothing was committed. My
scripts and outputs are in the session scratchpad under `verify_surge/`: `leak.py`, `leak_aug.py`, `dups.py`,
`runner.py`, `score.py`, `score2.py`, `cmp_modeler.py`, `terms.py`, `cmp_board.py`, and their `*.out` / `runs/`
files.

## Verdict: adopt on the odds route, with conditions

In plain language:
- The gain is real, small and honest. Over seven seasons Surge's forecasts are slightly better than production's.
- I found no leak.
- Its confident calls ("70%+ that the match has 24+ shots") are as good as production's. The difference in hit rate
  is +0.1pp, well inside the noise.
- It does not fix the one cell where the strict ship rule already fails for production: Scotland Championship at
  90%.
- In that league it is worse than production in 2025/26.

What decides it: the decision rule's gain and guard conditions pass pooled, on the confirmation seasons and on the
untouched 2026/27. The ship-rule shortfall is shared with production. Adopt only with the conditions at the end.

## 1. Leakage hunt

| check | result |
|---|---|
| **Perturbation.** All results (shots, SOT, goals) randomised on and after D = 2019-01-26, 2021-03-13, 2023-03-04, 2024-12-21, 2026-04-18. Then all 45 Surge inputs rebuilt: production features (`team_form`, incl. `league_prior_shots`, `league_level_drift`) plus candidate families adj / h2h / ctx / form. | **0 differing cells** on every row dated before D (40,583 to 92,397 rows). **0** on the rows dated D (82 to 109; their own and same-day results were changed). The rows after D change (sanity). |
| **Truncation.** Rows after D deleted, same five dates. | 0 differing cells before D and on D. |
| Early-season truncation (2024-08-17, 2025-08-10) | Only `ctx_games_left` differs, on 13 to 23 rows. League size comes from the full season, as found in the fin_c verification. These values are around 30 games left, so the `last3` and `dead` terms are unaffected (both need 10 or fewer). In production the league size comes from the calendars. |
| Fresh feature builds vs stored / modeler files | `features.parquet`: identical. Candidate columns used by Surge: identical to the modeler's `features_candidates_form.parquet` (0 differing cells on 94,889 rows). |
| Duplicated fixtures | Single source (football-data, 95,274 rows). 0 duplicate (date, home, away). 0 teams playing twice on one day. 0 same pairing within 3 days. |
| Odds | The ingest maps `Avg*` / `BbAv*` / `B365*` pre-match columns only. An import-time guard rejects closing-odds names. Surge adds no market input. |
| Training before the test block | Asserted on all 85 monthly blocks: `train.max(date) < block start`, and no shared rows. |
| **Out-of-sample correction fit** | `_fit_shrink` carves the latest training seasons (at least 12,000 rows, about 2 to 2.2 seasons) by season label, all inside the block's training rows. It scores them with a FlexNB fitted on the earlier seasons only. Then it fits (c, b, g) by NB maximum likelihood. Season date ranges overlap only once (2019/20 ran 6 days past the 2020/21 start). That affects the inner fit, never a test row. No test-season result enters any fit. |
| Damping | `oos_scale` multiplies the whole fitted log correction, shrinkage included, at prediction time only. It is not fitted. |

**Selection hygiene (damping 0.4).**
- The modeler's cache holds the damping and cap variants (`surge_l4/l6/l8/c2/l6c2/l4k`) for **2019/20 to 2022/23
  only**. Only the chosen 0.4 has confirmation-season files. That is consistent with the claim that the grid was
  scored on discovery only.
- On my re-run, the pre-written rule on discovery gives:

  | damping | 70%+ calls hit | 0.8-0.9 band realised |
  |---|---|---|
  | production | 74.7 | 81.3 |
  | 0.4 | 75.0 | 81.3 |
  | 0.5 (not in the modeler's grid) | 74.7 | |
  | 0.6 | 74.5 | |
  | 1.0 | 73.9 | |

  So 0.4 is the largest value that passes (a). That is a knife-edge choice: the margins are about 0.3pp against a
  standard error of about 0.8pp.
- **Optimism is nil or negative.** On confirmation, ΔRPS falls steadily as damping rises: -0.0052 (0.3), -0.0064
  (0.4), -0.0073 (0.5), -0.0079 (0.6), -0.0078 (1.0). The confirmation call hit rate is flat at 75.8 to 75.9 for
  every scale. So 0.4 is not favoured by the confirmation seasons. If anything it gives up about 0.0015 RPS.
- **Two residual caveats.**
  - The decision to damp at all came from the top-band shortfall seen on pooled seasons, which include confirmation.
  - The 8 correction terms came from the over-shoot study, which reported every pattern's stability on all 7
    seasons.

  The confirmation seasons are therefore not fully clean for the *term set*. The only untouched slice is 2026/27 to
  date. It agrees: ΔRPS -0.0075 [-0.0149, -0.0008].

## 2. Reproduction

My own runner (`runner.py`):
- monthly refit on every row dated strictly before the month;
- the real production class `negbin_pit_comp`, not `cand_nbmle`;
- odds rows only (49,626 of the modeler's 49,643; the 17 rows without odds belong to the no-odds route).

Damping variants come from the same fitted bases, with the per-competition PIT calibration refitted for each scale.
At 0.4 the curve equals the model's own to 0.0 on all 81 blocks.

Cross-checks:
- My Surge curves equal the modeler's cached Surge curves on 51,417 rows: max |ΔCDF| 3e-8, bounds 100% equal.
- My production run equals the project's stored production monthly backtest on 100% of 51,010 rows.
- Scale 0 reproduces `cand_nbmle` exactly.
- `cand_nbmle` differs from the real production model by up to 0.009 in the CDF (99.9% of bounds equal), so the
  modeler's reference was close but not identical.

Surge minus production, my scores. 95% CIs from 1,000 bootstrap resamples, by match-day blocks and by month blocks.
Negative = Surge better.

| slice | n | RPS Surge / prod | ΔRPS [day] [month] | Δpinball (mean of 70/80/90) [day] | Δlog score [day] | ΔMAE median |
|---|---|---|---|---|---|---|
| **all 2019/20-2025/26** | 49,626 | 3.0738 / 3.0820 | **-0.0081 [-0.0094, -0.0069] [-0.0099, -0.0065]** | -0.0030 [-0.0039, -0.0021] | -0.0035 [-0.0049, -0.0021] | -0.008 [-0.012, -0.004] |
| discovery 2019/20-2022/23 | 28,163 | 3.0223 / 3.0318 | -0.0095 [-0.0113, -0.0078] [-0.0118, -0.0070] | -0.0040 [-0.0051, -0.0027] | -0.0044 | -0.011 |
| **confirmation 2023/24-2025/26** | 21,463 | 3.1414 / 3.1478 | **-0.0064 [-0.0081, -0.0047] [-0.0085, -0.0042]** | -0.0016 [-0.0029, -0.0004] | -0.0023 [-0.0043, -0.0003] | -0.005 (n.s.) |
| 2025/26 | 7,094 | 3.1190 / 3.1237 | -0.0046 [-0.0081, -0.0016] [-0.0086, -0.0018] | +0.0005 [-0.0015, +0.0025] (n.s.) | -0.0005 (n.s.) | -0.001 (n.s.) |
| 2026/27 to 26 Oct (untouched) | 1,791 | 3.3743 / 3.3818 | -0.0075 [-0.0147, -0.0004] [-0.0114, -0.0055] | -0.0062 [-0.0115, -0.0016] | +0.0019 (n.s.) | -0.003 (n.s.) |
| *undamped (`cand_odds_oos`), all* | 49,626 | 3.0717 / 3.0820 | -0.0102 [-0.0132, -0.0072] | -0.0037 | -0.0039 | -0.014 |

- These match the modeler's table to ±0.0002 everywhere.
- The gain is about 0.27% of RPS. That is far below the protocol's 0.05 "too good" threshold.

**Why it works.** The applied correction is small: its standard deviation is 0.95% of the total, about 0.23 shots.
Share of its variance by term (approximate):

| term | share |
|---|---|
| `disagree` (pull toward the opponent-adjusted rating expectation; coefficient 0.34 before damping, 0.14 applied) | **54%** |
| goal form | 11% |
| season end (last-3 and dead rubber) | 17% |
| head-to-head | 7% |
| momentum, pace, league drift | under 5% each |

So Surge is mostly a blend with the EKF ratings, plus a season-end correction. The gain is largest in May-June:
ΔRPS -0.020 [-0.027, -0.014], against -0.006 to -0.010 in other months.

## 3. Ship rule (CLAUDE.md rule 3), monthly, pooled 2019/20-2025/26

| | Surge | production | team_avg | league_avg |
|---|---|---|---|---|
| coverage 70/80/90 | 73.2 / 82.6 / 91.5 | 73.4 / 82.7 / 91.5 | 70.0 / 80.0 / 89.8 | 74.3 / 83.4 / 91.9 |
| mid-coverage 70/80/90 | 69.8 / 79.7 / 89.6 (within ±2pp) | 69.9 / 79.9 / 89.7 | | |
| pinball (mean) | **1.4032** | 1.4062 | 1.4277 | 1.4760 |
| MAE of the median | 4.345 | 4.353 | 4.405 | 4.583 |

**Cells below q.**

| slice | Surge | production |
|---|---|---|
| pooled 7 seasons | **Scotland Championship 90%: 89.5%** (n = 1,172) | same cell, 89.6% |
| confirmation | Scotland Championship 90%: 89.6% (n = 540) | none |
| 2025/26 alone | Portugal 89.5, Scotland Championship 89.4, Turkey 89.9 (all at 90%) | Ligue 2 70%: 69.8; 2. Bundesliga 80%: 79.7; Portugal 89.5; Turkey 89.9 |

All shortfalls are 0.5pp or less, against standard errors of 0.9 to 2.2pp. In single seasons the two models fail
about equally often.

**Pinball vs team_avg.**
- Pooled: Surge is below team_avg in all 21 competitions.
- Confirmation: one exception, **Scotland Championship**: 1.3399 vs team_avg 1.3361. Production is at 1.3352.

**Competition guard** (per-competition paired CI):

| slice | RPS | pinball |
|---|---|---|
| pooled | 18 of 21 better, **none worse**; 2. Bundesliga +0.0004 and Portugal -0.0034 are n.s. | 7 better, none worse |
| 2025/26 alone | **Scotland Championship worse**: +0.018 [+0.0005, +0.038], n = 180 | **Scotland Championship worse**: +0.017 [+0.005, +0.030] |

This is the same flag the modeler reported, and the same league fin_c had trouble with.

**Strictly, the per-competition coverage clause fails in one cell for both models**, in the pooled 7-season run.

## 4. High-threshold calls: P(total >= 24)

**Reliability**, stated / realised (n), pooled 7 seasons:

| band | production | Surge | undamped |
|---|---|---|---|
| 0.7-0.8 | 74.2 / 73.3 (5,144) | 74.2 / 73.4 (5,301) | 74.2 / 72.6 (5,641) |
| 0.8-0.9 | 83.4 / 82.6 (1,371) | 83.4 / 82.2 (1,459) | 83.5 / 81.5 (1,700) |
| 0.9-1.0 | 91.3 / 93.5 (62) | 91.5 / 95.0 (60) | 91.7 / 93.8 (80) |

- Standard error is 1.0pp in the 0.8-0.9 band.
- Brier score of P(24+), paired: **-0.00067 [-0.00080, -0.00053]**, Surge better.

**70%+ calls.** Each cell is n / stated / hit; the binomial standard error of the hit rate is about 0.5pp pooled
and 1.0 to 2.4pp per season. Day-block CI for the hit-rate difference.

| slice | production | Surge | Δhit Surge - prod [95% CI] |
|---|---|---|---|
| all 7 | 6,577 / 76.3 / 75.4 | 6,820 / 76.3 / 75.5 | +0.07 [-0.35, +0.52] |
| discovery | 2,862 / 76.3 / 74.7 | 3,028 / 76.3 / 75.0 | +0.23 [-0.41, +0.91] |
| confirmation | 3,715 / 76.3 / 76.0 | 3,792 / 76.4 / 75.9 | -0.04 [-0.59, +0.47] |
| 2019/20 | 918 / 77.6 / 74.1 | 944 / 77.5 / 73.7 | -0.35 [-1.50, +0.76] |
| 2020/21 | 344 / 75.5 / 74.4 | 374 / 75.4 / 73.0 | -1.42 [-3.66, +0.61] |
| 2021/22 | 673 / 75.3 / 76.8 | 737 / 75.3 / 77.5 | +0.66 [-0.82, +2.23] |
| 2022/23 | 927 / 76.0 / 74.0 | 973 / 76.2 / 75.0 | +1.02 [-0.08, +2.06] |
| 2023/24 | 1,567 / 77.3 / 77.2 | 1,617 / 77.4 / 77.5 | +0.34 [-0.53, +1.14] |
| 2024/25 | 1,025 / 75.8 / 72.0 | 1,015 / 75.7 / 71.7 | -0.28 [-1.36, +0.66] |
| 2025/26 | 1,123 / 75.4 / 77.9 | 1,160 / 75.4 / 77.4 | -0.50 [-1.53, +0.60] |
| 2026/27 | 653 / 78.7 / 79.0 | 640 / 78.6 / 79.2 | +0.20 [-0.73, +1.22] |

- **Call quality is at production's level; no season differs beyond noise.**
- Surge makes 3.7% more calls.
- The calls only Surge makes (n = 564) hit 68.1% at 71.1% stated. The calls only production makes (n = 321) hit
  61.1% at 70.9% stated. Both marginal sets are over-stated; Surge's set is the better one.
- Both models over-state their calls by about 0.85pp pooled.
- 2024/25 is the weak season for both: about -4pp.

## 5. Robustness

- **Per season.** ΔRPS is negative in all 7 seasons and significant in each, from -0.0046 (2019/20, 2025/26) to
  -0.0129 (2020/21). The pinball difference is n.s. only in 2019/20 and 2025/26.
- **Mean bias.** Unchanged within 0.07 shots per season. In 2026/27 to date both models predict low: -0.42
  (production) and -0.51 (Surge).
- **Per league.** See section 3. The pooled point estimates are worse only in 2. Bundesliga (+0.0004, n.s.).
- **Damping sensitivity on confirmation** (sanity only, not used to choose):

  | damping | ΔRPS | 70%+ calls hit | 0.8-0.9 band stated / realised |
  |---|---|---|---|
  | production | 0 | 76.0 | 83.3 / 83.6 |
  | 0.3 | -0.0052 | 75.9 | 83.4 / 82.6 |
  | 0.4 | -0.0064 | 75.9 | 83.3 / 82.8 |
  | 0.5 | -0.0073 | 75.9 | 83.4 / 82.6 |
  | 0.6 | -0.0079 | 75.9 | 83.4 / 82.7 |
  | 1.0 | -0.0078 | 75.8 | 83.5 / 81.6 |

  - The result is stable between 0.3 and 0.6.
  - Damping does protect the top band compared with no damping: +1.2pp.
  - Surge's top band still realises about 0.8pp less than production's on confirmation.
- **Dry run of the current board.** Setup:
  - a scratch copy of `src/`, `data/processed/`, `models_store/`, calendars and a read-only SQLite backup of
    `data/shots.db`;
  - `PYTHONPATH`, `SHOTS_DB` and `SHOTS_BOARD` pointed at the scratch copy;
  - `SHOTS_ODDS_MODEL=cand_odds_surge python -m shots.predict.board --days 30 --no-refresh`, against a production
    build of the same copy.
  - The repo's `board.parquet` and `features.parquet` are byte-identical before and after.

  Results:
  - Exit 0.
  - **Fallback 0 of 46 odds fixtures** (share 0.0, limit 0.02).
  - The 448 no-odds fixtures are unchanged.
  - Of the 34 published odds fixtures: `expected` changed on 5 (±1), `lb_70/80/90` on 7 / 3 / 7, and P(24+) moved by
    -0.010 to +0.024. 70%+ calls went from 25 to 28.
  - 27 of 34 explanations carry the `surge` factor.

  Three side effects to know about:
  1. **Surge Watch stops assessing the odds fixtures** (394 to 360 assessed), because `ROUTE_MODELS["odds"]` is
     hard-wired to `negbin_pit_comp`. Its odds-route catalogue describes production's residuals, and Surge already
     absorbs part of those signals.
  2. The candidate features are rebuilt three or four times per board run (about 1 minute each). This is slow but
     correct.
  3. The factor label in the code is "Surge correction". The modeler's report says "Surge signals".

## Conditions for adopting Surge on the odds route

1. **Record the decision on Scotland Championship 90%.** It is 89.5% pooled for Surge and 89.6% for production, so
   the strict rule fails for both. Surge is also flagged worse there in 2025/26 (RPS and pinball), and its
   confirmation pinball is above team_avg. Either accept "no worse than production" explicitly, or exclude Surge from
   Scotland Championship (route it to production) until a fix. Add it to the CLAUDE.md watch list (Turkey, Portugal,
   Scottish leagues at 90%).
2. **Record the protocol rule 4 exception:** odds route only. The no-odds route keeps fin_c, as for fin_c's adoption.
3. **Decide what Surge Watch does on odds fixtures** before switching. Either keep labelling them, with a catalogue
   re-derived on Surge's own walk-forward residuals, or document that odds fixtures are no longer labelled. Do not
   reuse production's odds-route excesses for a Surge-scored fixture: that would double count the part Surge
   already corrects.
4. **Monitor after the switch** (monthly, on settled results):
   - the 0.8-0.9 band of P(24+), which realises 0.4 to 0.8pp below production;
   - the 70%+ call hit rate against production's shadow numbers;
   - the fallback share in `board.meta.json["odds_route"]`.

   Revert if the call hit rate trails production's by more than about 1.5pp over a season.
5. **Be honest in the wording.** Most of the correction is a pull toward the rating expectation (about half its
   variance). Season end is the second-largest part. Public copy should not present Surge mainly as an
   "over-shoot signals" model.
