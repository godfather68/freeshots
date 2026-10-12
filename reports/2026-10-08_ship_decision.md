# Ship decision (TASKS 3b.4): monthly refit, rounding-aware rule, 2026-10-08

Author: evaluator agent. Candidates: `team_avg` (baseline), `negbin`, `negbin_cal_comp`, `negbin_pit_comp`.
`blend`, `qgbm` and `negbin_cal` were dominated in 3b and were not re-run.

- **Protocol.** Walk-forward expanding window with `--refit month`: 85 refits per model, each on
  every row dated strictly before the first match of that calendar month. The 7 complete test
  seasons are 2019/20 to 2025/26 (n = 49,643). 2026/27 to date (n = 1,385) is reported
  separately. The season-refit numbers were re-run with the same harness, plus distribution
  diagnostics. They reproduce the 3b predictions exactly (lb_80 identical on 2025/26).
- **Files.** Predictions: `data/processed/backtest_predictions_month_<model>.parquet` (one file per
  model, run in parallel), the combined `backtest_predictions_month.parquet`, and
  `backtest_predictions_season_diag.parquet`. Harness report: `2026-10-08_backtest_month.md`.
  Runtime with 4 parallel processes at 1 thread each: negbin 35 min; negbin_cal_comp and
  negbin_pit_comp 53 min each; team_avg 18 s.
- **New evaluation code.** `src/shots/evaluation/calibration_checks.py` has the randomized PIT,
  mid-coverage, reliability tables, a day-block paired bootstrap, the CLAUDE.md rule-3 ship check
  and monthly drift control limits. `src/shots/evaluation/ship_decision.py` builds the tables in
  this report. The harness (`backtest.py`) now records, for models that expose a predictive
  distribution, F(y-1), F(y), the stated P(total >= lb) and the pmf at each bound. These are
  computed at scoring time, after `predict` has run on target-free rows. Tests:
  `tests/test_evaluation_calibration.py` (9 new tests; 95 tests pass).

## Verdict

**Under the rule as written in CLAUDE.md rule 3, no candidate passes under monthly refit.
`negbin_pit_comp` misses by one cell: at q = 0.9 in Scotland Championship, coverage is 89.6% vs
90.0% (n = 1,172, -0.4pp, about 0.5 SE).** It passes every other check, and it passes the
whole rule under season refit (Scotland Championship 90.4% there). No other candidate comes
close: raw `negbin` fails 3 cells, `negbin_cal_comp` fails 8 checks.

My recommendation: **ship `negbin_pit_comp`, but only after one of these:**

1. **(Preferred; a small model change, the modeler's call.)** Fix the Scottish lower leagues,
   which are miscalibrated in a consistent way (see below). For example, pool Scotland
   Championship, League One and League Two into one PIT calibration group, or widen their
   calibration window. Then re-run `--refit month` for `negbin_pit_comp` only (about 50 min).
2. **(A decision for the user, not the evaluator.)** Accept a statistical tolerance on the
   per-competition check, e.g. coverage >= q - 1pp in each competition. With 21 competitions x
   3 levels, a model whose stated probabilities are *perfectly* calibrated still fails the
   literal "every competition >= q" rule **11% of the time** (simulation: 2,000 draws from
   `negbin_pit_comp`'s own stated P(total >= lb) per match). Under such a tolerance,
   `negbin_pit_comp` ships now.

I am not applying option 2 myself: the user fixed the rule today. The single failing cell is not
pure noise either. All three Scottish lower leagues sit about 2pp below their stated
probabilities, so option 1 is the honest fix.

## Rule (CLAUDE.md rule 3, adopted 2026-10-08) and how it is applied

Blocking checks, on the complete test seasons, at each of q = 0.7 / 0.8 / 0.9:

- coverage >= q overall;
- coverage >= q in every competition (all 21 have n >= 1,146);
- overall mid-coverage, mid = (P(y >= N) + P(y > N)) / 2, within ±2pp of q;
- pinball no worse than `team_avg`. The mean paired difference must be <= 0; the day-block
  bootstrap 95% CI is reported.

Informational only (not blocking): worst season >= q - 1.5pp, per-competition |mid - q| <= 4pp,
and the share of competitions where the model beats `team_avg`.

I agree with the rule's premise (next section). One weakness: the literal "every competition
>= q" clause has no allowance for sampling noise (see the 11% figure above).

## Assessment of the modeler's integer-rounding claim: **confirmed**

Claim: negbin's ~3pp over-coverage comes from integer bounds, not from a miscalibrated
distribution. The pinball-optimal integer bound of a calibrated discrete distribution covers with
probability >= q.

1. **The theory holds.** For integer Y with CDF F, the expected pinball loss at tau = 1 - q is
   piecewise linear between integers. It is minimised at k = min{k : F(k) > 1 - q}, which covers
   with probability 1 - F(k-1) >= q. The excess lies between 0 and the pmf at k. Exact-q
   coverage therefore requires moving some bounds above the optimum, which costs pinball. I
   checked this on synthetic calibrated NB data: coverage over q, randomized PIT = q, and
   coverage = stated probability (`test_pinball_optimal_integer_bound_of_calibrated_nb_over_covers_by_granularity`).
2. **It matches the backtest, independently checked.** For negbin under monthly refit, at q = 0.8:
   - observed coverage is 82.9%;
   - the model's own expected coverage of its integer bounds (mean stated P(total >= lb)) is
     82.6%;
   - mean pmf at the bound is 5.5%, so the margin is 2.6pp, about half the pmf;
   - randomized-PIT coverage (rounding removed) is **80.2%**;
   - mid-coverage is 80.0%.

   The same holds at 0.7 (73.3 observed, 73.2 stated, 70.0 randomized) and at 0.9 (91.9 / 91.7
   / 90.2). The randomized PIT histogram is flat within ±0.6pp per decile.
3. **Forcing exact q costs pinball, significantly.** Under monthly refit, `negbin_cal_comp`
   (exact q) minus `negbin_pit_comp` is +0.0071 pinball, day-block CI [+0.0053, +0.0088].
4. **There is a stronger argument than the modeler's.** Exact-coverage bounds are only valid *on
   average*. For `negbin_cal_comp` at q = 0.8 (season refit), the recalibrated distribution gives 23.8k of the
   49.6k matches (48%) a stated P(total >= N) in [0.75, 0.80). Realised frequency in that group
   is 78.7%. For half the matches, "at least N with 80% confidence" is false. For
   `negbin_pit_comp`, every bound has stated P >= q, and the stated P is reliable (next
   section).
5. **Caveat: the claim is true in aggregate, not in every competition.**
   - Raw negbin: randomized coverage at q = 0.8 is 75.8 / 77.5 / 76.0% in Scotland Championship /
     League One / League Two, and 82.6% in Segunda. That is real miscalibration, as the modeler
     said.
   - `negbin_pit_comp` reduces these gaps but does not close them for the three Scottish lower
     leagues: randomized coverage is still 78.2 / 79.0 / 78.7% at q = 0.8, and 87.9 / 89.2 /
     88.2% at q = 0.9. Stated P(total >= lb_90) averages 91.7 / 91.8 / 91.8%; realised is
     89.6 / 90.9 / 90.0%.
   - The sign is the same in all three leagues and at all three q. Each league has only 135 to
     180 matches per season, so the 2-season calibration window holds about 330 rows, barely
     above `min_group` = 300. When the window includes the COVID seasons (137 + 135 = 272 rows),
     it falls back to the global map.
   - This is the cause of the one failing cell. It is not a bug, but it is a known weak spot.

## Stated probabilities of `negbin_pit_comp` (`bound_probabilities` / `prob_at_least`): **calibrated**

Reliability of stated P(total >= lb) against realised frequency, monthly refit, complete seasons:

| q | bin of stated P | n | stated | realised | gap (pp) | SE (pp) |
|---|---|---|---|---|---|---|
| 0.7 | [0.70, 0.75) | 37,785 | 72.5 | 72.6 | +0.0 | 0.2 |
| 0.7 | [0.75, 0.80) | 11,857 | 75.9 | 75.9 | -0.0 | 0.4 |
| 0.8 | [0.80, 0.85) | 45,304 | 82.5 | 82.5 | +0.0 | 0.2 |
| 0.8 | [0.85, 0.90) | 4,338 | 85.6 | 85.0 | -0.5 | 0.5 |
| 0.9 | [0.90, 0.95) | 49,641 | 91.7 | 91.5 | -0.2 | 0.1 |

- Pooled over all q in 2pp bins, every bin with n > 1,000 is within ±0.8pp.
- 2026/27 (n = 1,385): q = 0.8 gives 82.4 stated vs 82.2 realised, and q = 0.9 gives 91.7 vs
  91.8. At q = 0.7, the high bin is 75.8 vs 71.1 (n = 235, -1.7 SE), which is noise-level.
- Exception: the Scottish lower leagues are about 2pp optimistic (see above).
- A side note on finalize: `finalize` makes bounds monotone across q, so 1 to 2 rows per q
  sit at the boundary (e.g. stated P = 0.80 at q = 0.7). This is harmless.

## Overall, complete seasons 2019/20-2025/26 (n = 49,643)

`rand80` is the randomized-PIT coverage. `stated80` is the model's mean P(total >= lb_80).
Pinball is the mean over the three q.

| refit | model | cov 70/80/90 | mid 70/80/90 | rand80 | stated80 | pinball | sharp80 | MAE | bias |
|---|---|---|---|---|---|---|---|---|---|
| month | team_avg | 70.0 / 80.0 / 89.8 | 66.5 / 77.0 / 87.8 | | | 1.4279 | 19.70 | 4.406 | -0.30 |
| month | negbin | 73.3 / 82.9 / 91.9 | 69.9 / 80.0 / 90.1 | 80.2 | 82.6 | **1.4039** | 19.22 | 4.349 | -0.20 |
| month | negbin_cal_comp | 70.1 / 80.1 / 89.9 | 66.6 / 77.1 / 87.9 | 80.0 | 80.1 | 1.4135 | 19.74 | 4.349 | -0.20 |
| month | **negbin_pit_comp** | 73.4 / 82.7 / 91.5 | 69.9 / 79.9 / 89.7 | 80.0 | 82.7 | 1.4065 | 19.26 | 4.349 | -0.20 |
| season | team_avg | 70.0 / 80.0 / 89.8 | 66.5 / 77.0 / 87.8 | | | 1.4281 | 19.70 | 4.406 | -0.30 |
| season | negbin | 73.3 / 82.9 / 91.9 | 69.9 / 80.0 / 90.1 | 80.2 | 82.6 | 1.4038 | 19.23 | 4.349 | -0.20 |
| season | negbin_cal_comp | 70.1 / 80.0 / 89.8 | 66.5 / 77.1 / 87.8 | 79.9 | 80.1 | 1.4128 | 19.75 | 4.349 | -0.20 |
| season | negbin_pit_comp | 73.3 / 82.6 / 91.5 | 69.9 / 79.7 / 89.6 | 79.9 | 82.7 | 1.4061 | 19.28 | 4.349 | -0.20 |

Paired pinball difference vs `negbin_pit_comp`, monthly refit (day-block bootstrap, 500 resamples):

- team_avg: +0.0215 [+0.0184, +0.0245]
- negbin: -0.0026 [-0.0039, -0.0013]
- negbin_cal_comp: +0.0071 [+0.0053, +0.0088]

## Ship-rule outcome (monthly refit; blocking checks only)

| model | cov >= q overall | cov >= q every competition | |mid - q| <= 2pp overall | pinball <= team_avg | result |
|---|---|---|---|---|---|
| negbin | pass | **fail** at 0.7 / 0.8 / 0.9: 68.7 (Scot. L2), 78.9 (Scot. Ch.), 87.1 (Scot. Ch.) | pass | pass (-0.0240) | FAIL |
| negbin_cal_comp | **fail** at 0.9 (89.9) | **fail** at 0.7 / 0.8 / 0.9: 68.7, 78.8, 88.0 (all Scot. Ch.) | **fail** (66.6 / 77.1 / 87.9) | pass (-0.0144) | FAIL |
| negbin_pit_comp | pass | 0.7: 71.3, 0.8: 80.8 pass; **0.9: 89.6 (Scot. Ch.) fail** | pass (69.9 / 79.9 / 89.7) | pass (-0.0215 [-0.0245, -0.0184]) | **FAIL by 0.4pp in 1 cell** |
| negbin_pit_comp (season refit) | pass | pass (71.4 / 81.0 / 90.4) | pass | pass (-0.0221) | PASS |

`negbin_pit_comp` also passes every informational check under monthly refit:

- worst season coverage is 72.4 / 81.7 / 90.7;
- worst per-competition |mid - q| is 2.8pp;
- it beats `team_avg` on pinball in 21/21 competitions.

## Per competition, worst case (complete seasons)

| refit | model | min cov70 | min cov80 | min cov90 | max \|mid80 - q\| | max \|rand80 - q\| | sd cov80 |
|---|---|---|---|---|---|---|---|
| month | team_avg | 67.3 (Ger 2.BL) | 77.4 (Ger 2.BL) | 87.8 (NED) | 5.6 | n/a | 1.54 |
| month | negbin | 68.7 (Scot L2) | 78.9 (Scot Ch) | 87.1 (Scot Ch) | 4.5 | 4.2 | 1.55 |
| month | negbin_cal_comp | 68.7 (Scot Ch) | 78.8 (Scot Ch) | 88.0 (Scot Ch) | 4.9 | 1.8 | 0.66 |
| month | negbin_pit_comp | 71.3 (Scot Ch) | 80.8 (Scot Ch) | 89.6 (Scot Ch) | 2.1 | 1.8 | 0.73 |
| season | negbin_pit_comp | 71.4 (TUR) | 81.0 (Scot Ch) | 90.4 (Scot Ch) | 1.8 | 1.5 | 0.75 |

Full per-competition q = 0.8 tables, for both refits, are reproduced by
`python -m shots.evaluation.ship_decision --out <file>`.

## Per season, q = 0.8 coverage and pinball (monthly refit)

| season | team_avg | negbin | negbin_cal_comp | negbin_pit_comp | pinball team_avg | pinball negbin_pit_comp |
|---|---|---|---|---|---|---|
| 2019/20 | 79.4 | 82.3 | 79.3 | 82.1 | 1.409 | 1.385 |
| 2020/21 | 80.7 | 82.0 | 79.8 | 82.5 | 1.400 | 1.388 |
| 2021/22 | 81.2 | 84.9 | 82.2 | 85.1 | 1.377 | 1.362 |
| 2022/23 | 79.9 | 82.9 | 79.4 | 81.8 | 1.419 | 1.396 |
| 2023/24 | 79.4 | 83.2 | 79.7 | 82.3 | 1.467 | 1.437 |
| 2024/25 | 79.1 | 81.5 | 79.1 | 81.7 | 1.469 | 1.444 |
| 2025/26 | 79.9 | 83.3 | 81.1 | 83.3 | 1.453 | 1.433 |

`negbin_pit_comp` beats `team_avg` on pinball in every season. Its q = 0.7 and 0.9 coverage is
>= q in every season (min 72.4 / 90.7).

## 2026/27 to date (n = 1,385; not in the verdict)

| refit | model | cov 70/80/90 | rand80 | pinball | MAE | bias |
|---|---|---|---|---|---|---|
| month | team_avg | 71.8 / 80.9 / 90.1 | | 1.5656 | 4.919 | -1.05 |
| month | negbin | 72.8 / 81.9 / 91.0 | 79.0 | 1.5766 | 4.900 | -0.70 |
| month | negbin_cal_comp | 70.8 / 79.6 / 90.3 | 79.6 | 1.5732 | 4.900 | -0.70 |
| month | negbin_pit_comp | 73.1 / 82.2 / 91.8 | 79.6 | 1.5661 | 4.900 | -0.70 |
| season | negbin_pit_comp | 73.3 / 82.2 / 91.9 | 79.6 | 1.5668 | 4.896 | -0.71 |
| season | team_avg | 71.8 / 80.9 / 90.1 | | 1.5654 | 4.919 | -1.05 |

By month, with bias = median - actual:

| month | n | team_avg cov80 / bias | negbin_pit_comp (month) cov80 / bias | negbin_pit_comp (season) cov80 / bias |
|---|---|---|---|---|
| 2026-08 | 1,210 | 82.3 / -1.31 | 84.1 / -1.00 | 84.1 / -1.00 |
| 2026-09 | 1,466 | 79.4 / -0.86 | 80.5 / -0.48 | 80.5 / -0.49 |
| 2026-10 (to 5 Oct) | 92 | 87.0 / -0.53 | 84.8 / 0.00 | 84.8 / -0.02 |

## Cold start (either team < 5 prior matches, n = 508, monthly refit)

| model | cov 70/80/90 | pinball | bias |
|---|---|---|---|
| team_avg | 73.0 / 82.5 / 92.1 | 1.4331 | -0.84 |
| negbin | 75.4 / 85.2 / 93.5 | 1.4362 | -0.64 |
| negbin_cal_comp | 71.9 / 81.7 / 90.9 | 1.4537 | -0.64 |
| negbin_pit_comp | 74.8 / 84.3 / 92.5 | 1.4525 | -0.64 |

- Cold-start bounds are valid (over-covered) but not sharp.
- On these rows `negbin_pit_comp` is about 0.02 worse than `team_avg` on pinball. The sample is
  small: about 1% of matches, n = 508.

## Season vs monthly refit

- **Monthly refit changes almost nothing.** Overall pinball moves by at most 0.0007, coverage by
  at most 0.2pp, and per-season coverage by at most about 1pp. Rankings are identical overall
  (negbin < pit_comp < cal_comp < team_avg) and on 2026/27 (team_avg ≈ pit_comp < cal_comp <
  negbin). The reason is that the season-to-date and EWM form features already carry new
  information into the predictions. A refit only re-estimates about 30 coefficients and the
  calibration map from a window that grows by one month, so it adds very little.
- **Monthly refit does not fix the 2026/27 under-prediction.** Bias is -0.70 (monthly) vs -0.71
  (season). The under-prediction is an *early-season* effect: -1.0 shots in August, -0.5 in
  September, about 0 so far in October. It fades as the rolling form features absorb the new
  level. A refit on data dated before 1 August cannot see that level.
  - Part of the "bias" is structural: the integer median of a right-skewed NB sits below the
    mean. Seasonal median bias has been -0.45 to +0.07 since 2019, so the excess in August 2026
    is about -0.7 to -0.8 shots.
  - The fix, if wanted, is a feature or level-adjustment change, e.g. a faster early-season
    weight on season-to-date shots, or a league-level drift term. That is the modeler's scope.
    Coverage held throughout: August 84.1%, September 80.5% at q = 0.8.
- **Production cadence.** Monthly refits are still the right production cadence. They are cheap
  in production (one fit per month is about 40 s), and they put the partial current season into
  the calibration window. But the backtest shows no measurable accuracy gain over a season refit.

## What must be monitored in production

1. **Monthly coverage drift**, overall and per competition, at each q. Compare against the
   model's *stated* expected coverage (mean `p_80`, about 82.7%), not against q. Otherwise the
   granularity margin hides about 2.7pp of drift. Alert when monthly coverage falls below
   expected - 3 SE (`calibration_checks.monthly_drift`).
   - In the monthly backtest, `negbin_pit_comp` raised 1 alert in 71 months: August 2024,
     77.5% vs expected 82.7%, n = 622, recovered in September.
   - `negbin_cal_comp` raised 2: December 2022 (the World-Cup month) and August 2024.
2. **Randomized-PIT coverage per competition** on a rolling 12 months (`granularity_table`):
   flag |rand - q| > 3pp. The Scottish lower leagues are the first to watch: they are currently
   about 2pp short.
3. **Reliability of the displayed probabilities.** If the product shows "at least N shots, 83%",
   track stated vs realised by 2pp bin, monthly.
4. **Early-season bias.** In August and September, watch the bias of the median against the
   actual total, per league. In 2026 it reached -1 shot in August.
5. **Calibration-window size.** Log whenever a competition falls back to the global PIT map
   because it has fewer than 300 calibration rows. This happens to the Scottish leagues after
   short seasons, and to any newly added competition (Phase 5.3).

## Bugs and concerns

- **No model bugs found.** The calibrated models' `prob_at_least` is consistent with their own
  bounds:
  - stated P >= q for every `negbin_pit_comp` bound, apart from the 1 to 2 monotonicity-clipped
    rows per q;
  - the randomized PIT of `negbin_cal_comp` and `negbin_pit_comp` is identical per competition,
    as expected, since they share the same PIT map;
  - re-running season refit on the rebuilt data reproduces the 3b predictions exactly.
- **Concern (Scottish lower leagues).** After PIT recalibration they remain about 2pp
  over-confident. This is the only thing standing between `negbin_pit_comp` and a clean pass.
- **Concern (the rule).** The "every competition >= q" clause gives a perfectly calibrated
  model an ~11% false-fail rate. I recommend adding an explicit noise allowance (e.g. q - 1pp,
  or q - 1 SE) when the rule is next revisited.
- **Harness note.** `backtest.py` now writes `pit_lo` / `pit_hi` / `p_*` / `pmf_*` columns for
  distributional models. The automatic verdict lines in the harness report still use the old
  two-sided 2pp rule. They are labelled as superseded in `2026-10-08_backtest_month.md`. The
  rule-3 check lives in `calibration_checks.ship_check`.
