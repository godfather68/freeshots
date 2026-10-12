# League-drift features (feature side of improvement #2)

Code: `src/shots/features/team_form.py` (`_league_drift`, documented in the module docstring table).
Tests: `tests/test_features_team_form.py` (2 new tests: leakage incl. same-day, cold start and shift detection).
No model code, FEATURES list, `predict/` or `scripts/` touched. `data/processed/features.parquet` not overwritten.

## Features added (12 columns, appended at the end; all strictly before the match date)

prev = the competition's previous-season mean shots per team per match (only if that season ended
before the match date; else the competition all-time mean before the date, then global). K = 200 team-observations.

| column | definition |
|---|---|
| league_prev_level_shots | prev |
| league_prev_is_fallback | 1 if prev is a fallback |
| league_level_shrunk | (n*season-to-date mean + K*prev)/(n+K); n=0 gives prev |
| league_level_drift | league_level_shrunk - prev |
| league_recent28_n / _shots | obs and mean shots in the competition over the 28 days before (crosses season boundary) |
| league_recent28_drift | n/(n+K)*(recent28 - prev); 0 if n=0 |
| league_recent100_drift | mean of the last 100 prior competition matches minus prev (crosses boundary) |
| league_global_n28 / league_global_drift28 | all competitions, last 28 days: mean of (shots - own competition's prev), shrunk by n/(n+K); 0 if n=0 |
| days_since_season_start | date - first match date of the competition-season |
| league_season_matches_prior | competition-season matches dated before this one |

Cold start: the first match ever has NaN level (no history anywhere); count-based drifts are exactly 0 with n=0;
`league_recent28_shots` is NaN when no match in the window (n gives the reason).

## Checks

- Existing columns identical: rebuilt to a TEMP path, `assert_frame_equal` against `data/processed/features.parquet`
  on all 70 existing columns (same order, 94,678 rows): EQUAL.
- Leakage tests: changing same-day and later matches leaves the new features unchanged for earlier and same-day
  matches; changing a match's own (and same-day) result does not change its own features.

## Signal check (August-September, seasons 2019/20-2026/27, n = 10,052)

Residual A = total_shots - simple proxy (team shrunk for/against average, uses league_prior_shots).
Residual B = actual - median of `backtest_predictions_month_negbin_pit_comp` (production-style, joined by match_id, 100% joined).
Match-level correlation is tiny (match noise dominates); the signal is in the level, so the aggregated rows matter.

| level | n | corr(res) | level_drift | recent28_drift | recent100_drift | global_drift28 | days_since_start |
|---|---|---|---|---|---|---|---|
| match, A | 10,052 | | 0.013 | 0.021 | -0.008 | 0.042 | 0.002 |
| match, B | 10,052 | | 0.037 | 0.043 | 0.018 | 0.045 | -0.004 |
| comp-season, A | 166 | | 0.60 | 0.65 | 0.27 | 0.46 | 0.08 |
| comp-season, B | 166 | | 0.67 | 0.72 | 0.38 | 0.44 | 0.03 |
| season, A | 8 | | 0.88 | 0.89 | 0.57 | 0.92 | -0.11 |
| season, B | 8 | | 0.88 | 0.90 | 0.61 | 0.92 | -0.04 |

Slope of residual B (total shots) on the feature, match level: global_drift28 0.73, recent28_drift 1.26, level_drift 0.91.
The drifts are in per-team shots (total is 2x) and shrunk, so the model needs to learn a scale of about 1 to 2 on totals.

Season means (Aug-Sep): residual B and drifts move together (2024/25: B -0.48, global -0.39; 2023/24: B +0.45,
global +0.49; 2026/27: B +0.72, global +0.59).

### 2026/27 (Aug-Sep)

| month | level_drift | recent28_drift | recent100_drift | global_drift28 | res A | res B |
|---|---|---|---|---|---|---|
| Aug | 0.05 | 0.05 | 0.15 | 0.40 | 1.06 | 1.01 |
| Sep | 0.22 | 0.21 | 0.42 | 0.74 | 0.62 | 0.48 |

Both are residuals of actual minus prediction (positive = under-prediction; the ship decision's bias sign is the opposite).
In August the league-specific drifts are still near zero (little data, shrinkage), while the global drift is already
+0.4 per team (the broad rise is visible via other leagues and the end of the previous season). By September league-specific
drifts have caught up (+0.2 per team on average; Germany 2. Bundesliga +0.46, Serie A +0.39, Bundesliga +0.34).
Within 2026/27 alone, match-level correlations are about zero (-0.05 to 0.03), as expected.

## Recommendation for the modeler

1. `league_global_drift28` first: the only one that is informative in August, when the bias is largest (about 1 shot), and it is broad-based.
2. `league_recent28_drift` and `league_level_drift` (highly collinear, r about 0.99): pick one (recent28 had the best comp-season correlation).
3. `league_prev_level_shots` as the base level instead of the all-time fallback in `league_prior_shots`, and optionally `league_recent100_drift`.
4. Skip `days_since_season_start` as a stand-alone driver (about 0 correlation), though it is useful as an interaction with the drift terms.
Caveat: with only 8 Aug-Sep seasons, the season-level correlations (n=8) are suggestive, not proof. Try them on the monthly-refit backtest and check 2025/26 and 2024/25 (negative drift) do not get worse.
