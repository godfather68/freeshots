# Candidate feature families (2026-10-15)

Production outputs unchanged: `features.parquet` rebuilt from `matches.parquet` with the untouched `team_form.build_features`
and compared with `assert_frame_equal(check_exact=True)`: IDENTICAL (94,876 x 82). `team_form.py` has no diff.
Only `ingest/football_data.py` changed: the closing-odds regex now also blocks `AHCh` and the `1XB` bookmaker prefix
(no configured pre-match column is affected; tests prove it).

## Files
- `src/shots/ingest/match_extras.py`: `python -m shots.ingest.match_extras` -> `data/processed/match_extras.parquet` (124,952 rows, all
  parsed raw rows incl. unplayed; match_id equals the canonical adapter's, tested). Columns: kickoff_time, referee, corners, fouls,
  yellow, red (home/away), `ah_line` (AHh, older BbAHh), `ah_home_odds/ah_away_odds` (AvgAHH/A, BbAvAHH/A, B365AHH/A). Closing columns
  are never read (import-time guard + test with AHCh/AvgCAHH/B365CH present in the input).
- `src/shots/features/adj_ratings.py` (family A engine), `src/shots/features/candidates.py` (all families, definition table in the docstring),
  builder `python -m shots.features.candidates` -> `data/processed/features_candidates.parquet` (94,876 rows x 120 cols, keyed by `match_id`,
  95 s here; column names do not collide with production, tested).
- Tests: `tests/test_features_candidates.py` (16: leakage with own + same-day + later results changed at 3 cut dates, same-day isolation,
  cold start, opponent adjustment, future-fixture rows, market inversion, schedule, UCL, H2H, referee, context table, production unchanged),
  `tests/test_match_extras.py` (7). 45 pass together with `test_football_data` and `test_features_team_form`.

## Families (full definitions in the `candidates.py` docstring)
| fam | columns | idea |
|---|---|---|
| A `adj_*` | per side att/def (+sd) for shots and for SOT, accuracy att/def (sot minus shots rating), expected shots/SOT per side and total, league level | opponent- and venue-adjusted log ratings, extended Kalman filter on an NB model, updated match by match |
| B `style_*` | corners/fouls/card points for+against (l10, ewm), SOT ratio for/against, shots sd10, match-total sd10 | style/volume proxies |
| C `mktx_*` | AH line, abs, de-vigged AH prob, market total goals (O/U inverted through Poisson), GD from 1X2 (Skellam inversion) and from AH, lambda per side, abs GD | market structure |
| D `sched_*` | n matches in 7/14/28 d, rest before previous match, short-rest share, Sun/Mon share, midweek-after-weekend, weekday, rest diff, days to next fixture (schedule only), UCL days since/to (current season) | congestion |
| E `h2h_*` | n, weighted n, decayed mean total, deviation vs ratings (shrunk K=2), total shrunk to current expectation; tau 3 years | head to head |
| F `ref_*` | n, shots / fouls / cards deviation of the referee's matches (K=15) | referee |
| G `ctx_*` | games played, points, rank, gap to title, gap to drop zone, games left, "dead rubber" flag; match progress | season context |

Why an EKF for A: strictly causal by construction, all matches of a date are read before any is written, one scalar update per match
(about 12 s for all 95k), cold start through an explicit prior variance, and time decay is the process-noise rate q (variance grows per day
without matches, so summers and long gaps make ratings adapt faster). Promotion/relegation adds variance when the team's competition changes.
Hyperparameters tuned only on matches 2012-08..2023-06 by NB log-likelihood of shots (level-only -2.919, tuned -2.844; MAE 3.64 -> 3.29); the
optimum is flat: q=3e-5/day, prior var 0.03, observation noise factor 0.7, move variance 0.02. Rows without shots (future fixtures) get features and never update, so the same function can score upcoming matches.

## Coverage (share non-null, matches.parquet rows)
Seasons 2010/11..2026/27: A, C, D (except UCL) and G are 98-100% every season (first matches of the data have NaN expectation by design).
B 98-100% (corners/fouls/cards exist in all seasons; fouls 88-93% in 2017/18-2018/19 for some leagues).
E (any prior meeting) 51% in 2010, 83-95% afterwards (England National League only 30%, Serie B 81%, Segunda 84%).
F referee: only England (all 5 tiers) and Scotland (tiers 2-4 plus Premiership) carry a name: 37-40% of all matches since 2017, 66% before
2017; 0% for Belgium, France, Germany, Greece, Italy, Netherlands, Portugal, Spain, Turkey. Spain 2012/13 9%.
D UCL: 6% of 2026/27 rows (clubs in the calendar), 0% otherwise by design (NaN = unknown, not 0). No historical European fixtures exist in the data, so the historical Euro proxies are only indirect (Sun/Mon share, short-rest share).
Per-competition table (2019+): all families 100% except those above (`cov_comp.csv` figures; rows identical across competitions for A, B, C, G).

## Signal check against the production walk-forward residual
Residual = actual - median of `backtest_predictions_month_negbin_pit_comp` (100% join, 2019/20-2026/27); PIT residual = mid(pit_lo, pit_hi) - 0.5.
Found = 2019/20-2022/23 (n = 28,179); confirmed = 2023/24-2025/26 (n = 21,464). Mean residual +0.18 / +0.23 (production under-predicts the median).
Spearman at match level (positive = more shots than the model said):

| feature (match level) | rho found | rho confirmed | PIT found | PIT confirmed |
|---|---|---|---|---|
| adj_exp_total_shots - median (A) | 0.080 | 0.063 | 0.058 | 0.043 |
| h2h_total_shrunk - median (E; contains the A expectation) | 0.063 | 0.047 | 0.061 | 0.040 |
| ref_shots_dev (F; England + Scotland only, n 10.6k / 8.4k) | 0.022 | 0.035 | 0.023 | 0.034 |
| adj_att sum / adj_def sum / adj_sot_att sum (A) | 0.011 | 0.021 | 0.021 | 0.024 |
| style_fouls_against_l10 sum (B) | 0.018 | 0.029 | 0.001 | 0.008 |
| ctx_pts / gap_safety / dead flag sums (G) | 0.01-0.02 | 0.02 | 0.01-0.02 | 0.02 |
| sched_sunmon_share, n28, rest_prev (D) | 0.01-0.015 | 0.016-0.021 | 0.01 | 0.006-0.02 |
| mktx_* (C): abs GD, AH abs, total goals, GD | |rho| < 0.017 | < 0.013 | < 0.02 | < 0.02 |

Out-of-sample incremental fit (fit on found, evaluate on confirmed, control = production median; ridge | gradient boosting; R2 is the share of residual variance, RMSE of the median is 5.630 on confirmed):

| family | corr(pred, residual) | OOS R2 ridge | OOS R2 hgb |
|---|---|---|---|
| A ratings | 0.072 | +0.50% | +0.50% |
| E h2h | 0.057 | +0.33% | +0.25% |
| D schedule | 0.034 | +0.10% | +0.08% |
| G context | 0.027 / 0.046 | +0.06% | +0.14% |
| F referee | 0.027 | +0.06% | +0.07% |
| B style | 0.017 | -0.03% | -0.15% |
| C market | 0.006 | -0.04% | -0.03% |
| all 120 columns | 0.090 | +0.78% | +0.64% |

Correction check on A: residual slope on (adj_exp_total - median) is 0.34 (found); applying `median + 0.085 + 0.34 * (adj - median)` to confirmed lowers RMSE 5.630 -> 5.613
but not MAE (4.438 -> 4.442, the median is an integer and the shift hurts L1); the gain per season is mixed in MAE. Unfitted `adj_exp_total_shots` alone: RMSE 5.695 vs 5.630 for production;
the average of the two 5.621. So A is a near-substitute with a little complementary information.

Aggregated levels (Spearman of group means with mean residual, found / confirmed):
- Team-season (n = 1,515 / 1,121, at least 10 matches), pre-match rating at that time vs mean residual of the team's matches: att+def (shots) **0.62 / 0.62**; def 0.32 / 0.23; att 0.21 / 0.23;
  SOT att+def 0.40 / 0.43; accuracy att+def -0.19 / -0.13; corners for+against 0.18 / 0.21; total sd10 0.19 / 0.25; fouls 0.05 / 0.05; SOT ratio -0.13 / -0.11.
  Production leaves between-team signal in the residual (teams with high rated volume are under-predicted); it is large in rank terms but small in match terms (about 0.3 shots sd vs 5.6 match noise).
- Competition-month (n = 734 / 601): mktx_total_goals -0.17 / -0.14 (months where the market expects more goals are over-predicted by production), mktx_ah_abs -0.06 / -0.06, adj_def sum 0.09 / 0.07,
  corners sum -0.09 / -0.09, sched_n14 0.04 / 0.11 (not stable), adj_exp_total 0.04 / 0.07.

## Recommendation for the modeler (ranked)
1. **A adj ratings**: only family with a consistent, sizeable signal at every level (match 0.08/0.06, team-season 0.62/0.62, OOS R2 0.5%). Use `adj_att`, `adj_def` (sums and diffs), `adj_exp_total_shots`,
   and the disagreement `adj_exp_total_shots - base prediction` as a feature or as an offset/ensemble member. Include `adj_*_sd` for cold starts. Expect a small, real gain, mostly in RMSE.
   Next step to push it: add venue-specific or team-specific home advantage and league-quality offsets on promotion in the EKF, and use it as a stronger prior in the structural model.
2. **E h2h** (`h2h_total_shrunk`, `h2h_dev_shrunk`): consistent (0.06/0.05) but largely the A expectation recycled; test after A is in, it may be redundant.
3. **F referee** where available (England, Scotland only; ~40% of matches): consistent but weak (0.02/0.035); use `ref_shots_dev` with `ref_n`, NaN elsewhere. Cheap to try on those leagues.
4. **G context / D schedule**: signs stable but |rho| about 0.02 and R2 under 0.15%; try ctx_dead_n and sched_n14/sunmon only as interactions in late season. UCL features cannot be validated (6% of 2026/27 rows, no residuals yet).
5. **B style**: nothing at match level (OOS R2 negative); corners and match-total sd10 show team-season correlations (0.2) that A already captures. Skip unless used for the dispersion/shape model (`style_total_sd10`).
6. **C market**: match-level nothing (production already uses odds); the comp-month correlation of `mktx_total_goals` (-0.15) is a league-level calibration hint, not a match feature.

Caveats: signals are small relative to match noise, so the modeler must confirm on the lower-bound objective (pinball/coverage), not on the mean residual; the production median is integer so MAE gains are hard to see.
`sched_days_to_next` and the UCL columns use fixture dates (schedule, published in advance), not results; they are the only columns that depend on future rows existing, so decide explicitly whether they are allowed under rule 1 before using them (excluded from my signal fits for UCL; days_to_next is in the schedule family, no meaningful signal).
Machine note: in this environment imports are very slow (about 40 s), so the extras table takes several minutes to rebuild.
