# fin_c adoption, prepared behind switches (2026-10-16, finished 2026-10-20)

Author: modeler. Follows `reports/2026-10-15_model_candidates.md` and the independent verification
`reports/2026-10-16_fin_c_verification.md` ("adopt for the no-odds route, with conditions").

**Nothing is switched on.** Both switches default to OFF, so production is unchanged. Nothing is committed or pushed.

## Switches (`src/shots/config.py`)

| switch | default (production) | after approval | scope |
|---|---|---|---|
| `NOODDS_MODEL` | `"negbin_pit_comp"` | `"fin_c"` | domestic fixtures without odds |
| `MEDIAN_FROM_CURVE` | `False` | `True` | `expected` on every route |

- Turning either on is a one-line edit of `config.py`.
- The switches are independent. `fin_c` reads the median the same way as production unless `MEDIAN_FROM_CURVE` is on.
- For dry runs, the environment variables `SHOTS_NOODDS_MODEL` and `SHOTS_MEDIAN_FROM_CURVE` override the file. The
  scheduled pipeline sets neither.
- `FALLBACK_MAX_SHARE = 0.02` is the fallback threshold of condition 2.

## Verdict

**fin_c passes every decision criterion on the no-odds route under the protocol's full monthly decision run,
2019/20 to 2025/26, on the final code.**

- **Ship rule (CLAUDE.md rule 3): passes.**
  - Coverage is at least q overall and in every one of the 21 competitions.
  - Overall mid-coverage is within ±2pp of q.
  - Pinball (1.4059) is below `team_avg` (1.4279).
- **Gain:** RPS −0.0252 with a CI below 0. Pinball improves (CI below 0).
- **Competition guard:** no competition is worse on RPS or pinball, pooled or on 2025/26 alone.
- **Exception:** this adopts on one variant (no-odds) only. `docs/evaluation-protocol.md` rule 4 asks for both
  variants, so the coordinator or the user has to record an explicit exception. The odds route keeps production:
  its gain was not resolved (−0.003, n.s.) and Scotland Championship was flagged.
- **Caveat:** on 2025/26 alone, fin_c has 5 competition cells below q (production has 2).

## 1. What was fixed

### Condition 1: season context (G family)

`src/shots/features/candidates.py`:
- **Season length per competition format.** `SEASON_FORMATS`, `season_format`, `scheduled_games` replace
  `2(n−1)`. Formats are fixed before the season, so they are a pre-match fact.

  | competition | format | games per team |
  |---|---|---|
  | Scotland Premiership | 33 + 5 after the split | 38 |
  | Scotland Championship, League One, League Two | four round robins | 36 |
  | Belgium (all seasons) | regular season + playoffs | games left undefined |
  | Greece (from 2019/20) | regular season + playoffs | games left undefined |
  | every other league | double round robin | 2(n−1) |

- **Why no flag in Belgium and Greece.** After the regular season, a team's remaining games depend on its final
  regular-season rank (playoff group sizes differ). The season end is therefore not known before the match.
  - `ctx_games_left` is NaN, so there is no last-3 flag; `G_last3` falls to 0.
  - `ctx_dead` is 0.
  - `ctx_progress` counts the regular season.
  - The last regular-season matches decide playoff placement, so they are not "season end" matches, and flagging
    them would mislabel them.
  - Greece 2017/18 and 2018/19 were plain double round robins and keep the flag.
- **Effect.** Share of rows flagged "last 3 matches", before → after:

  | league | before | after |
  |---|---|---|
  | Scotland Premiership | 49% | 7.4% |
  | Scotland lower divisions | 53–54% | 6.6–6.7% |
  | Greece | 27% | 2% (pre-2019 seasons only) |
  | Belgium | 16% | 0% |
  | the 15 double-round-robin leagues | unchanged | unchanged |

- **League size without hindsight.** n now comes from `league_sizes` (`calendar_league_sizes(fixtures.parquet)`):
  the season calendar in production. Teams of the calendar that have no row yet sit in the table with 0 points.
  Without a calendar, n falls back to the teams of the competition-season among the rows given. In the update path
  those rows are played matches plus the upcoming fixtures of the window, which in practice already list every team.
- **Rule 1 note (also in the module docstring).** On history, n = teams of the season = the scheduled league size.
  That is fixed in advance, not a result. The feature still uses only results dated before the match.
- **Serving consistency (`src/shots/features/adj_ratings.py`).** The EKF league level's trailing 150-match window now
  holds only matches with a result (`trailing_sums(pool=...)`). Upcoming fixtures in the same build no longer take
  places in the window, so a fixture 4 weeks ahead sees the same played window as a training row. On history this is
  a no-op: no played row lacks shots, and a full rebuild is identical, checked with `assert_frame_equal`
  (`check_exact`).
- `data/processed/features_candidates.parquet` was rebuilt. Changed columns: `ctx_games_left`, `ctx_dead`,
  `ctx_progress` and `ctx_dead_n`, and only for Scotland, Belgium and Greece. A, E and the other families are unchanged.
- **Tests** (`tests/test_fin_c_adoption.py`):
  - formats table;
  - Scottish four round robins (last-3 flag on the final three matchdays only);
  - Premiership 38;
  - Belgium and Greece playoff seasons (no flag, NaN is not treated as a missing build);
  - Greece before 2019/20;
  - calendar league size equal to the full-season build in week 1 (the old code gave `2(n−1)` with n = 2);
  - upcoming fixtures never fill the trailing level, and played rows are unchanged.

### Condition 2: fail-safe routing

`src/shots/predict/scan.py`, `src/shots/models/candidates.py`.
- **Per fixture.** `FinC.missing_inputs` / `missing_candidate_inputs` check every candidate input fin_c reads
  (`family_inputs(("A","E","G"))`). Only games left is exempt (NaN by design).
  - If an input is NaN, or the fixture has no row in the build (stale build, or a fixture added after it), the
    fixture is scored by the **production no-odds model**. It is never median-filled.
  - If the candidate build or the fin_c fit raises, every no-odds fixture falls back.
- **Counts.**
  - They are logged at WARNING when there is any fallback.
  - The board writes them to `board.meta.json["noodds_route"]`: model, fallback model, n no-odds, n candidate,
    n fallback, n non-cold-start, share, build error.
- **Loud failure.**
  - `Prepared.check_fallback()` raises `CandidateFallbackError` when the fallback share of non-cold-start no-odds
    fixtures exceeds 2%.
  - It is called after the picks CSV and the board (+ meta) are written: fixtures that fell back carry valid
    production numbers, so the site never goes stale. The step is marked FAILED in `job_runs`, and
    `python -m shots update` exits with 1.
- **Staleness.** The build is made in the same run from the same `matches` frame, so it cannot be older than the
  latest played match. Reuse within a run is keyed on the matches hash.
- **Tests:**
  - switch off: no build and production routing;
  - complete features route to fin_c;
  - a fixture missing from the build goes to production, and the run raises;
  - a failed build: everything goes to production, and the error is reported;
  - the 2% threshold boundary.

### Condition 3: final decision backtest (section 2)

### Condition 4: integration

- **Features once per run.** `scan.candidate_features` builds families A/E/G (`PRODUCTION_FAMILIES`) on played
  matches + domestic fixtures.
  - Inside `shared_run` it builds once, on the widest window, and the picks scan and the board reuse it
    (`SharedRun.candidates`, counter `candidate_builds`).
  - Training rows for fin_c take the candidate columns of played matches from the same build.
  - The model is saved as `models_store/fin_c-noodds_<date>` with the usual metadata. Its feature list includes the
    candidate inputs (`FlexNB.input_columns`). It is retrained under the same 30-day rule.
- **Routes** (`Prepared.routes`, candidate mode):

  | fixtures | model | label |
  |---|---|---|
  | with odds | production odds model | unchanged |
  | without odds, candidate inputs complete | fin_c | `fin_c-noodds_<date>` |
  | without odds, fallback | production no-odds | `negbin_pit_comp-noodds_<date>` |
  | cross-border (UCL / UEL / UECL) | production no-odds base | as today (`...-ucl-exp`, ...) |

- **Cross-border decision: it stays on production's no-odds base.** The honest validation available says nothing in
  favour of fin_c there:
  - There is no cross-border shot history to calibrate or test on, and the route is experimental and never settled.
  - fin_c's inputs do not carry over:
    - head-to-head between clubs of different leagues is almost always empty;
    - season context describes the domestic table, not a UEFA match;
    - the EKF ratings are relative to each domestic league's level, which the cross-border wrapper already
      re-levels its own way.
  - `CrossBorderModel` needs an NB GLM (`nb.sides_`), which FlexNB does not have.
  - Moving it would be an unvalidated change to a route whose only evidence (`reports/2026-10-13_champions_league.md`)
    is on the production base.
- **Explanations** (`src/shots/models/explain.py`).
  - fin_c is supported (`supports()`), with an exact decomposition. The side models are log-linear in
    [production design, family block], and the same Shapley game is used.
  - New factor ids:

    | factor | what it carries |
    |---|---|
    | `home_rating_attack`, `away_rating_attack` | EKF attack ratings, incl. their share of the rating expectation |
    | `home_rating_defence`, `away_rating_defence` | EKF defence ratings |
    | `head_to_head` | E family |
    | `season_context` | G family |
    | `shrinkage` | total-level pull toward the league level: shrunk minus unshrunk raw mean |

  - Rating uncertainty (`A_sd`) is split exactly into the two teams' `*_early` factors.
  - Reference matchup: ratings 0 (expectation = the fixture's own league level), no head-to-head deviation, no
    season-end or dead flags, and training medians for progress, gap to safety and uncertainty.
  - `league_level`, `calibration` and the production factors keep their meaning.
  - `signals.model_gap` on odds fixtures compares against the no-odds model that fixture would have had (fin_c, or
    production where it would fall back).
  - The new ids were added to `docs/static-api.schema.json` (enum) and `docs/static-api.md`. Both changes are
    additive; nothing emits the new ids while the switch is off.
  - Tests:
    - additivity to 1e-9 (raw) and in published tenths;
    - the reference matchup has zero rating and head-to-head effects;
    - ids within `FACTOR_ORDER`;
    - detail ≤ 160 characters.
  - The existing additivity tests (`tests/test_explain.py`) pass.

### Condition 5: median switch

- `calibrated.curve_median` returns min{k : G(F(k)) ≥ 0.5} on the published curve, as `team_predict` /
  `CandidatePit` do. `NegBinCal.predict` and `CrossBorderModel.predict` use it when `MEDIAN_FROM_CURVE` is on.
- Bounds and probabilities are unchanged.
- Test: with the switch on, P(total ≥ median) ≥ 0.5 ≥ P(total ≥ median + 1) on the published curve; with it off, the
  median is the raw one, bit for bit.

## 2. Final decision backtest (final code)

**Setup:**
- Final code; features rebuilt with the fixes. No-odds variant: every `mkt_*` / `mktx_*` blanked in training and
  scoring.
- Runner: `shots.models.screen`, one job per season, so no out-of-memory. Each block is fitted on rows dated strictly
  before it (asserted).
- **Monthly refit (decision run, all 7 seasons)**, plus fin_c **season refit** for all 7 seasons.
- Monthly reference: `cand_nbmle` (production re-estimated with `nbmle`; identical to `negbin_pit_comp` to 1e-4
  RPS). The statsmodels monthly run did not fit in memory.
- Season reference: the evaluator's `negbin_pit_comp` curves.
- Scores: `shots.evaluation.scoring`; day-block bootstrap 95% CIs.

### Paired difference fin_c − production, no-odds

| refit | slice | n | ΔRPS | Δlog | Δpinball | ΔMAE | guard RPS / pinball |
|---|---|---|---|---|---|---|---|
| monthly | **pooled 2019/20–2025/26** | 49,643 | **−0.0252** [−0.0302, −0.0206] | −0.0077 [−0.0108, −0.0048] | −0.0088 [−0.0115, −0.0062] | −0.036 [−0.044, −0.027] | none / none (14/21 better on RPS) |
| monthly | 2019/20–2022/23 | 28,179 | −0.0252 [−0.0320, −0.0192] | −0.0086 [−0.0124, −0.0045] | −0.0090 [−0.0124, −0.0060] | −0.036 | none / none |
| monthly | 2023/24–2024/25 | 14,370 | −0.0280 [−0.0376, −0.0186] | −0.0069 [−0.0133, −0.0010] | −0.0087 [−0.0142, −0.0032] | −0.037 | none / none |
| monthly | **2025/26 alone** | 7,094 | **−0.0198** [−0.0327, −0.0079] | −0.0057 [−0.0114, −0.0003] | −0.0080 [−0.0140, −0.0014] | −0.031 | **none / none** |
| season | pooled 2019/20–2025/26 | 49,643 | −0.0269 [−0.0317, −0.0221] | −0.0088 [−0.0119, −0.0061] | −0.0104 [−0.0129, −0.0079] | −0.039 | none / none |
| season | 2019/20–2022/23 | 28,179 | −0.0272 [−0.0335, −0.0216] | −0.0087 | −0.0106 | −0.037 | none / none |
| season | 2025/26 alone | 7,094 | −0.0193 [−0.0315, −0.0079] | −0.0034 (n.s.) | −0.0073 [−0.0136, −0.0004] | −0.027 | none / none |

Notes:
- The Scottish season-length fix removed the 2025/26 guard flag on Scotland Championship that the verifier found.
  - Its 2025/26 difference is now +0.061 RPS with a CI that includes 0. That is still the largest positive
    difference: small n (180 matches).
  - Pooled over 7 seasons it is +0.004 (n.s.).
- The 2025/26 gain is about 80% of the pooled gain: the expected winner's curse, smaller than before the fix.

### Ship rule (CLAUDE.md rule 3), no-odds, monthly, pooled 2019/20–2025/26

| | fin_c | production |
|---|---|---|
| coverage 70 / 80 / 90 | 73.1 / 82.4 / 91.5 | |
| worst competition | 72.2 / 80.3 / 90.4, all Scotland Championship (n = 1,172) | 72.3 / 80.9 / 90.3 |
| mid 70 / 80 / 90 | 69.7 / 79.6 / 89.6 (within ±2pp) | |
| pinball (mean 70/80/90) | **1.4059** | 1.4147 |
| `team_avg` pinball | 1.4279 | |
| competitions with pinball above `team_avg` | none | |

**The ship rule passes.**

### Margins in the leagues the verifier asked about

Monthly, pooled; coverage in %:

| competition | n | cov 70 / 80 / 90 | ΔRPS vs production | pinball (production / team_avg) |
|---|---|---|---|---|
| Scotland Championship | 1,172 | 72.2 / 80.3 / 90.4 | +0.004 (n.s.) | 1.341 (1.344 / 1.351) |
| Scotland League One | 1,148 | 72.6 / 81.7 / 91.2 | −0.018 | 1.279 (1.288 / 1.289) |
| Scotland League Two | 1,146 | 72.5 / 81.9 / 90.8 | −0.007 | 1.273 (1.273 / 1.306) |
| Scotland Premiership | 1,547 | 72.4 / 82.0 / 91.0 | −0.053 | 1.456 (1.476 / 1.491) |
| Belgium | 2,079 | 74.0 / 82.9 / 91.3 | −0.030 | 1.445 (1.448 / 1.471) |
| Greece | 1,668 | 74.5 / 83.9 / 92.3 | −0.035 | 1.257 (1.271 / 1.267) |

Before the fix (verifier, 3 seasons), Scotland Championship pinball was above `team_avg` (1.352 vs 1.336). On the
pooled 7 seasons it is now below (1.341 vs 1.351).

### 2025/26 alone (not the decision period; reported as required)

- **Competition guard:** none, on RPS and on pinball.
- **Cells below q, with about 180 to 380 matches per competition** (standard error at 90% ≈ 1.6 to 2.2pp):

  | model | 70% | 80% | 90% | cells |
  |---|---|---|---|---|
  | fin_c | none | Turkey 79.7 | Portugal 88.9, Turkey 88.2, Scotland League One 89.4, Scotland Premiership 89.9 | 5 |
  | production, same rows | none | none | Portugal 88.9, Turkey 89.9 | 2 |

  Every shortfall is within about 1 SE. But fin_c's one-season per-competition margins are thinner than
  production's. That is the main risk to monitor (section 4).
- **Pinball above `team_avg` in 2025/26:**

  | competition | fin_c | production | `team_avg` |
  |---|---|---|---|
  | Scotland Championship | 1.367 | 1.343 | 1.313 |
  | Scotland League One | 1.346 | 1.336 | 1.326 |
  | Italy Serie B | 1.456 | 1.461 | 1.451 |

  Production is also above `team_avg` in all three. The rule is assessed on the pooled period, where no competition is
  above it.

### Median switch (MAE of `expected`, monthly, pooled 2019/20–2025/26)

| route / model | raw median (today) | curve median (switch on) | change | share of fixtures whose `expected` changes |
|---|---|---|---|---|
| odds, production | 4.3490 | 4.3541 | +0.005 | 29.8% |
| no-odds, production | 4.3785 | 4.3893 | +0.011 | 23.4% |
| no-odds, fin_c | 4.3407 | 4.3537 | +0.013 | 26.8% |

- 2025/26 alone: odds −0.000, no-odds fin_c −0.000, production +0.005.
- The cost in MAE is about 0.01 shots. The gain is consistency: `expected` then always agrees with the published
  probabilities and with the per-team medians.
- fin_c with the median switch on is still 0.025 MAE better than production today (4.354 vs 4.378).

## 3. What changes for users when switched on

### `NOODDS_MODEL = "fin_c"`

**Fixtures without odds (most of the board; the calendars reach weeks ahead, and odds only come a few days before)**
get fin_c's curve, bounds, `expected` and per-team numbers:
- **Accuracy and calibration:** RPS −0.025 shots and pinball −0.009. Coverage of the 70/80/90 bounds stays at least q.
- **August and September** (thin rolling form) change the most: ratings carry over the summer.
- **The `model` column / label** reads `fin_c-noodds_<date>`.
- **Explanations** of these fixtures show the new factors:
  - "<team> attack rating" / "<team> defence rating" (e.g. "creates 12% more shots than an average team (adjusted
    for opponents and venue)");
  - "Head to head";
  - "Season context" (season % played, last three league matches, nothing to play for);
  - "Pull toward league level".

  The old form factors ("<team> attack", last 10) remain, usually smaller, because the ratings take part of their
  weight.

**Fixtures with odds:**
- Unchanged, except `explain.signals.model_gap`, which is now measured against fin_c.
- When odds appear for a fixture (a few days before kick-off), it moves from fin_c to the odds model, as it moves from
  the production no-odds model today.

**Unchanged:**
- Champions League / Europa / Conference fixtures (experimental, production base).
- Picks settlement, the track record mechanics and the export format (the schema enum is already extended).

**Operations:**
- Every update builds the candidate features once (section 3c), and fin_c is retrained monthly.
- A missing or failed build never publishes median-filled numbers. Affected fixtures get production numbers, and above
  2% the run is marked failed.

### `MEDIAN_FROM_CURVE = True`

- `expected` (and `above_league`) of about 25% to 30% of fixtures moves by usually 1 shot, so that `expected` is the
  median of the curve the site shows.
- Bounds and probabilities are unchanged. MAE +0.005 to +0.013.

### 3c. Runtime of the update

Measured on 2026-10-20 with real data and frozen fixtures, on an idle machine. Same steps as `python -m shots update`
(picks scan + 30-day board inside one shared run, `--no-refresh`); all outputs went to the scratchpad.

| run | picks | board | total | extra vs off |
|---|---|---|---|---|
| switch off (production) | 55 s | 44 s | 100 s | |
| switch on, steady state (saved fin_c model) | 95 s | 47 s | 142 s | **+42 s** |
| switch on, first run or monthly retrain | 211 s | 42 s | 253 s | +153 s, about once a month |

- **Steady state.** The extra time is the candidate feature build: 43–45 s for 94,889 played + 374 upcoming matches,
  mostly the EKF. It runs once per update; the board reuses it.
- **Retrain.** Fitting fin_c on 94,889 rows takes about 110 s. It happens when the saved model is more than 30 days
  behind the latest result, as for the production no-odds model.
- **Dry-run board** (480 fixtures, switch on):

  | fixtures | model | count |
  |---|---|---|
  | without odds | fin_c | 366 |
  | with odds | production | 8 |
  | cross-border | production base | 106 |

  - Fallbacks: 0.
  - Odds and cross-border rows: identical to the switch-off board, except the `explain` model gap.
  - All 480 fixtures carry an explanation.
  - On the no-odds rows, `expected` changes on 64% of fixtures (mean +0.4, mean absolute change 0.8 shots), and
    `lb_80` rises by 0.37 on average (mid-October: ratings carry more information than 8-10 matches of rolling
    form).
- **Production identity with both switches off.** The switch-off board (480 × 114) and the picks CSV from this code
  equal those from the untouched HEAD code (git worktree of d318895) on the same inputs: `assert_frame_equal`,
  `check_exact`.

## 4. Risks

- **Thinner per-competition margins in a single season** (2025/26: 5 cells below q vs 2 for production, all within
  about 1 SE).
  - Monitor Turkey, Portugal and the Scottish leagues on the 90% bound.
  - The pooled 7-season decision period passes everywhere, with Scotland Championship at 90.4% the tightest cell.
- **Selection.**
  - The A/E/G + shrink design was chosen on 2023/24–2024/25.
  - The side-dispersion addition and the season-length fix were motivated by 2023/24–2025/26 diagnostics.
  - The season-length fix is a correction of fact, not a tuned choice: the formats are public.
  - 2019/20–2022/23 were not looked at for the choice and give the same gain (−0.025, CI below 0).
- **Feature pipeline dependency.** The gain lives in the EKF ratings. A broken build degrades to production numbers
  (not to wrong ones), and above 2% fallback the run fails loudly.
- **Format table maintenance.** A league that changes format needs a `SEASON_FORMATS` entry (e.g. Belgium's 18-team
  2026/27 season is still a playoff format; covered). An unlisted split league would be treated as a double round
  robin, as before the fix.
- **One variant only.** Adopting for no-odds while the odds route keeps production is an exception to protocol rule 4.
  It must be recorded by the coordinator or the user.
- **Early-season league size without a calendar.** For leagues without a calendar file (France, Netherlands, Portugal,
  Greece, the English lower leagues, ...), n comes from the teams in played rows plus the fixtures of the window.
  Every team appears once the first round is in fixtures.csv.

## Files

- `src/shots/config.py` (new): the switches.
- `src/shots/features/candidates.py`: `SEASON_FORMATS`, `season_format`, `scheduled_games`, `calendar_league_sizes`,
  `league_sizes` argument, `PRODUCTION_FAMILIES`.
- `src/shots/features/adj_ratings.py`: `trailing_sums(pool=...)`; the league level is built from played matches only.
- `src/shots/models/candidates.py`: `FinC` (registered as `fin_c`), `family_inputs`, `missing_candidate_inputs`;
  `FlexNB.input_columns` lists the candidate inputs.
- `src/shots/models/calibrated.py` / `cross_border.py`: `curve_median` and the median switch.
- `src/shots/models/explain.py`: `flex_effects`, the new factor groups and wording; `supports()` accepts fin_c.
- `src/shots/models/screen.py`: walk-forward jobs also store `median_raw` and `median_curve`.
- `src/shots/predict/scan.py`:
  - `candidate_features`, `with_candidates`, `_candidate_route`;
  - `Prepared` fallback fields, `check_fallback`, `CandidateFallbackError`;
  - routes.
- `src/shots/predict/board.py`: gap model per row, `noodds_route` in the meta, loud failure after writing.
- `docs/static-api.schema.json`, `docs/static-api.md`: the new factor ids (additive).
- Tests:
  - `tests/test_fin_c_adoption.py` (new, 19 tests);
  - `tests/test_models.py`: synthetic candidate columns on their own random stream, so existing columns and golden
    hashes are unchanged, and the registry contract tests now cover fin_c;
  - `tests/test_predict_shared.py`: the shared run asserts `candidate_builds == 0` with the switch off.
- Backtest outputs (scratchpad, not in the repo): `adopt/*__month.parquet`, `*__season.parquet`, `final.json`.
