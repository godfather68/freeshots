# League-drift model candidate (`negbin_pit_comp_drift`), 2026-10-09

Model side of improvement #2. Feature side: `2026-10-09_league_drift_features.md`.

## Recommendation: **do not adopt. Keep `negbin_pit_comp` in production.**

- The drift candidate passes the ship rule exactly where production passes (season refit). It fails
  exactly where production fails (monthly refit: Scotland Championship at q = 0.9, 89.7% vs
  89.6% for production).
- It does not beat production on pinball. Monthly refit: -0.0002 [-0.0009, +0.0005]. On the
  seasons never used to select it (2025/26 and 2026/27), it is slightly worse: +0.0010
  [-0.0013, +0.0033].
- The problem it was meant to fix improves only a little. The August 2026 median bias goes from
  -1.00 to -0.90 shots and September from -0.48 to -0.30. But 2026/27 pinball and coverage get
  slightly worse (q = 0.8: 82.2% to 81.3%).
- It does help in the negative-drift season 2024/25 (Aug-Sep bias +0.48 to +0.33, cov90 87.6% to
  88.0%). That gain is small and its CI includes 0.

The class stays in the registry as a candidate. Production code and outputs are unchanged.

## What was built

- `src/shots/models/drift.py`
  - `DriftDesign` = the production NB design plus, per side, the relative drift terms
    `log1p(clip(drift / prev, -0.5, 1))`, where prev = `league_prev_level_shots`. With the log
    link, a coefficient of 1 means the drift passes fully into the rate.
  - Optional early-season interactions: the same terms times `clip(1 - days_since_season_start / E, 0, 1)`.
  - Optional `base="prev"`: where the season-to-date prior is a fallback (`league_prior_n` < 20),
    the previous-season level is used instead of the all-time competition mean.
  - Missing drift values become 0. Missing drift columns raise a `KeyError` that names the columns.
  - `NegBinDriftModel(NegBinModel)` and `NegBinPitCompDrift(NegBinPitComp)` (registry
    `negbin_pit_comp_drift`): same calibration (per-competition PIT map, 2-season window), same
    `predict` / `prob_at_least` / `bound_probabilities` / `team_*` API. The no-odds variant
    (`mkt_*` blanked) works unchanged.
  - Defaults are the variant chosen on validation: `drift_terms=("global", "level")`, base prior,
    no interaction.
- Behaviour-preserving hooks:
  - `NegBinModel._design(side)` in `negbin.py`;
  - `NegBinCal._new_nb()` in `calibrated.py`;
  - `persist.model_features` now lists the drift model's extra inputs.
  - The golden-hash tests of `negbin_pit_comp` (synthetic and real sample, odds and no-odds) pass,
    so production outputs are bit-identical.
- Tests:
  - New `tests/test_models_drift.py` (11 tests): contract with odds and no-odds; team methods;
    production ignores drift columns; missing columns raise; predict ignores targets; coefficient
    of about 1 and lower pinball on synthetic data with a hidden level shift; the prev base
    changes only fallback rows; the interaction vanishes after E days; save/load metadata lists
    the drift features.
  - `tests/test_models.py::synthetic` gained the drift columns. They are drawn from their own RNG
    stream and appended last, so all existing columns and golden hashes are unchanged.

## Variant selection (validation only; test seasons not used)

- Season refit; each validation season is fitted on all rows dated before it.
- Validation seasons are 2023/24 (positive drift) and 2024/25 (negative drift), n = 14,370. Only
  rows dated before 2025-07-01 were loaded.
- Pinball is the mean over q = 0.7 / 0.8 / 0.9. Δ = variant - production, day-block bootstrap 95% CI.

| variant | terms | pinball | Δ all | Δ Aug-Sep | cov80 | mid80 | Aug-Sep bias 23/24 | Aug-Sep bias 24/25 |
|---|---|---|---|---|---|---|---|---|
| V0 production | - | 1.4401 | | | 81.5 | 78.7 | -0.46 | +0.50 |
| V1 | global | 1.4394 | -0.0007 [-0.0020, +0.0006] | -0.0028 [-0.0068, +0.0011] | 81.9 | 79.1 | -0.33 | +0.34 |
| V2 | global + recent28 | 1.4397 | -0.0004 [-0.0020, +0.0010] | -0.0017 [-0.0048, +0.0017] | 81.9 | 79.1 | -0.34 | +0.35 |
| **V3** | **global + level** | **1.4392** | **-0.0009 [-0.0023, +0.0002]** | **-0.0035 [-0.0071, -0.0002]** | 81.9 | 79.1 | -0.34 | +0.35 |
| V4 | global + recent28, prev base | 1.4401 | +0.0000 [-0.0016, +0.0016] | +0.0006 [-0.0032, +0.0044] | 81.9 | 79.1 | -0.32 | +0.39 |
| V5 | global + recent28, × early (60 d) | 1.4391 | -0.0010 [-0.0025, +0.0007] | -0.0026 [-0.0064, +0.0013] | 81.9 | 79.2 | -0.31 | +0.35 |
| V6 | global, × early (60 d) | 1.4393 | -0.0008 [-0.0022, +0.0005] | -0.0029 [-0.0077, +0.0017] | 81.9 | 79.1 | -0.32 | +0.29 |

- **Chosen: V3 (global + level).** It is the only variant whose Aug-Sep gain has a CI that
  excludes 0, and it is joint best overall.
- The prev-level base (V4) removed the gain, so it was dropped.
- The early-season interactions (V5, V6) added parameters without a measurable gain.
- No variant was evaluated on 2025/26 or 2026/27 before this choice.

**Why the effect is small.** The fitted drift coefficients are small, refitting on all data before
2026-07-01:

| side | drift_global | drift_level |
|---|---|---|
| home | 0.20 | 0.01 |
| away | 0.10 | 0.01 |

- A coefficient of 1 would be full pass-through. In August 2026 (global drift +0.40 shots per
  team), these coefficients lift the median total by only about 0.1 shot.
- The likelihood over whole seasons says most league-level movement is already carried by the
  season-to-date prior and the team EWM and shrunk form. Note that `log_prior` itself has
  coefficients of only 0.09 and 0.01.
- The interaction variants let August carry more weight (`drift_level_early` about 0.7 to 0.8),
  but this did not improve validation pinball.
- With 8 Aug-Sep seasons, the strong season-level correlation in the feature report is
  real but too coarse to estimate a large, stable effect.

## Walk-forward backtest: `negbin_pit_comp` vs `negbin_pit_comp_drift`

- **Harness.** `shots.evaluation.backtest.walk_forward`, unmodified, one job per (model, season).
  It covers 7 complete seasons, 2019/20 to 2025/26 (n = 49,643), plus 2026/27 to date
  (n = 1,385).
- **Rows.** Both models and `team_avg` were run on today's features, so they predict identical
  rows.
- **Predictions.** `data/processed/backtest_predictions_season_drift.parquet` and
  `..._month_drift.parquet` are new files. Existing prediction files were not touched.
- **Selection overlap.** The 7-season window includes the two validation seasons. 2025/26 and
  2026/27 are the clean hold-out.

### Overall, complete seasons

| refit | model | cov 70/80/90 | mid 70/80/90 | pinball | MAE | bias |
|---|---|---|---|---|---|---|
| season | team_avg | 70.0 / 80.0 / 89.8 | | 1.4281 | 4.406 | -0.30 |
| season | negbin_pit_comp | 73.3 / 82.6 / 91.5 | 69.9 / 79.7 / 89.6 | 1.4061 | 4.349 | -0.20 |
| season | negbin_pit_comp_drift | 73.3 / 82.6 / 91.5 | 69.9 / 79.8 / 89.7 | 1.4054 | 4.348 | -0.19 |
| month | team_avg | 70.0 / 80.0 / 89.8 | | 1.4279 | 4.406 | -0.30 |
| month | negbin_pit_comp | 73.4 / 82.7 / 91.5 | 69.9 / 79.9 / 89.7 | 1.4065 | 4.349 | -0.20 |
| month | negbin_pit_comp_drift | 73.4 / 82.7 / 91.6 | 70.0 / 79.9 / 89.7 | 1.4063 | 4.348 | -0.20 |

Paired pinball difference, drift minus production (negative favours drift):

| slice | n | season refit | month refit |
|---|---|---|---|
| complete seasons | 49,643 | -0.0007 [-0.0013, +0.0001] | -0.0002 [-0.0009, +0.0005] |
| complete, Aug-Sep | 8,714 | **-0.0016 [-0.0032, -0.0002]** | -0.0010 [-0.0026, +0.0003] |
| complete, Oct-May | 40,929 | -0.0005 [-0.0013, +0.0004] | -0.0000 [-0.0009, +0.0008] |
| 2019/20-2022/23 (unseen in selection) | 28,179 | -0.0011 [-0.0020, +0.0000] | -0.0004 [-0.0013, +0.0006] |
| 2023/24 + 2024/25 (validation) | 14,370 | -0.0009 [-0.0023, +0.0002] | -0.0004 [-0.0017, +0.0010] |
| 2024/25 | 7,125 | -0.0016 [-0.0038, +0.0008] | -0.0005 [-0.0028, +0.0018] |
| 2024/25 Aug-Sep | 1,283 | -0.0053 [-0.0114, +0.0017] | -0.0026 [-0.0092, +0.0037] |
| **hold-out 2025/26 + 2026/27** | 8,479 | **+0.0016 [-0.0002, +0.0040]** | **+0.0010 [-0.0013, +0.0033]** |
| 2026/27 | 1,385 | +0.0023 [-0.0042, +0.0084] | +0.0013 [-0.0048, +0.0070] |
| 2026/27 Aug-Sep | 1,338 | +0.0022 [-0.0041, +0.0079] | +0.0014 [-0.0045, +0.0066] |

Per competition (monthly refit), drift has lower pinball in 10 of 21 competitions. The range runs
from Italy Serie A at +0.0026 to Greece Super League at -0.0042.

### Ship rule (CLAUDE.md rule 3)

| refit | model | cov ≥ q overall | cov ≥ q every competition (worst at 70/80/90) | \|mid - q\| ≤ 2pp | pinball ≤ team_avg | verdict |
|---|---|---|---|---|---|---|
| season | negbin_pit_comp | pass | 71.4 (TUR) / 81.0 (Scot Ch) / 90.4 (Scot Ch) | pass | -0.0221 [-0.0249, -0.0190] | **PASS** |
| season | negbin_pit_comp_drift | pass | 71.5 (TUR) / 81.1 (Scot Ch) / 90.6 (Scot L1) | pass | -0.0227 [-0.0255, -0.0197] | **PASS** |
| month | negbin_pit_comp | pass | 71.3 / 80.8 / **89.6** (all Scot Ch) | pass | -0.0215 [-0.0244, -0.0187] | FAIL (1 cell) |
| month | negbin_pit_comp_drift | pass | 71.3 / 80.8 / **89.7** (all Scot Ch) | pass | -0.0217 [-0.0247, -0.0189] | FAIL (1 cell) |

- The informational checks pass for both models:
  - worst season is ≥ q - 1.5pp;
  - worst per-competition |mid - q| is ≤ 2.8pp;
  - each model beats `team_avg` in 21/21 competitions.
- The drift terms do nothing for the Scottish lower-league calibration gap. That gap is a
  calibration-window issue (see `2026-10-08_ship_decision.md`), not a level-drift issue.

### August-September by season (monthly refit; bias = median - actual, so negative = under-prediction)

| season | n | bias prod | bias drift | cov80 prod | cov80 drift | cov90 prod | cov90 drift | pinball prod | pinball drift |
|---|---|---|---|---|---|---|---|---|---|
| 2019/20 | 1,433 | -0.03 | -0.02 | 81.9 | 81.7 | 91.6 | 91.7 | 1.3784 | 1.3772 |
| 2020/21 | 563 | -0.12 | -0.13 | 81.9 | 82.1 | 90.6 | 90.4 | 1.4220 | 1.4239 |
| 2021/22 | 1,368 | -0.65 | -0.54 | 87.4 | 86.7 | 94.2 | 93.8 | 1.3458 | 1.3424 |
| 2022/23 | 1,366 | +0.04 | +0.03 | 81.6 | 81.7 | 91.1 | 91.1 | 1.3977 | 1.3973 |
| 2023/24 | 1,387 | -0.44 | -0.33 | 82.6 | 82.6 | 91.8 | 91.6 | 1.4636 | 1.4636 |
| 2024/25 | 1,283 | +0.48 | +0.33 | 79.0 | 79.4 | 87.6 | 88.0 | 1.5062 | 1.5037 |
| 2025/26 | 1,314 | -0.32 | -0.35 | 84.3 | 84.2 | 92.8 | 92.6 | 1.3983 | 1.3988 |
| 2026/27 | 1,338 | -0.72 | -0.57 | 82.1 | 81.3 | 91.8 | 91.1 | 1.5765 | 1.5779 |

- The drift model moves the early-season bias in the right direction in the seasons with a
  clear shift: 2021/22, 2023/24, 2024/25 and 2026/27.
- It moves the bias by only 0.1 to 0.15 shots, a fraction of the shift.
- Season-refit numbers are within 0.02 of these.

### 2024/25 (negative-drift season), monthly refit

| model | cov 70/80/90 | mid80 | pinball | bias |
|---|---|---|---|---|
| negbin_pit_comp | 72.4 / 81.7 / 90.7 | 78.8 | 1.4441 | -0.07 |
| negbin_pit_comp_drift | 73.3 / 82.4 / 91.2 | 79.6 | 1.4436 | -0.23 |

Nothing gets worse. Coverage and mid-coverage improve; the latter moves closer to q. The
full-season bias moves away from 0 because the integer median sits below the mean, which is the
structural part of the bias.

### 2026/27 to date, monthly refit

| month | n | team_avg cov80 / bias | prod cov80 / bias / pinball | drift cov80 / bias / pinball |
|---|---|---|---|---|
| 2026-08 | 605 | 82.3 / -1.31 | 84.1 / -1.00 / 1.5632 | 83.6 / -0.90 / 1.5614 |
| 2026-09 | 733 | 79.4 / -0.86 | 80.5 / -0.48 / 1.5875 | 79.4 / -0.30 / 1.5915 |
| 2026-10 (to date) | 46 | 87.0 / -0.53 | 84.8 / 0.00 / 1.2486 | 80.4 / +0.13 / 1.2464 |
| season to date | 1,385 | 80.9 / -1.06 | 82.2 / -0.70 / 1.5661 | 81.3 / -0.55 / 1.5673 |

- In September, drift lowers the bias but lowers coverage too: cov90 is 91.3% for production and
  90.2% for drift, and pinball is slightly worse.
- A shifted median does not help pinball unless the lower tail moves with it.

## Conclusion

- The drift features carry a real, correctly signed signal. The candidate is calibrated and passes
  the same ship checks as production.
- But the NB learns only a small pass-through (about 0.1 to 0.2). The resulting gain is below the
  noise of the backtest, and it does not show up on the untouched seasons 2025/26 and 2026/27.
- Adopting it would add a dependency on 4 more feature columns (also needed on upcoming fixtures)
  with no measurable product gain. **Not adopted.**

Options if early-season bias is still a priority:

1. Monitor it as `2026-10-08_ship_decision.md` §4 already recommends. Coverage held in August and
   September 2026 (84.1% and 80.5% at q = 0.8).
2. Try an explicit, non-learned season-start level reset: rescale team form features by
   current/previous league level for the first N matchdays. This would be a feature-engineer
   change, evaluated the same way.
3. Fix the Scottish calibration window, which is the only thing blocking the monthly ship rule for
   either model.

Reproduce:

- the validation table: `walk_forward(..., ["2023/2024", "2024/2025"], refit="season")` on rows
  dated before 2025-07-01, with the V0-V6 kwargs above passed to `NegBinPitCompDrift`;
- the backtest: `walk_forward(MODELS[m], features, [season], refit=...)` for m in `team_avg`,
  `negbin_pit_comp` and `negbin_pit_comp_drift`;
- the tables: `calibration_checks.ship_check` / `granularity_table` / `paired_bootstrap` on the
  saved prediction files.
