# Independent verification of `fin_c` for the no-odds route (2026-10-16)

Author: evaluator. Subject: the modeler's claim (`reports/2026-10-15_model_candidates.md`, section 3b) that `fin_c`
should replace production on the **no-odds route only**. `fin_c` is the production two-sided NB plus families A
(EKF ratings), E (head-to-head) and G (season context), plus out-of-sample shrinkage of the total toward the league
level, plus per-competition side dispersion.

No project code was changed. My scripts and outputs are in the session scratchpad under `verifier/`
(`repro.py`, `perturb.py`, `hand.py`, `trunc_start.py`, `last3.py`, `ship.py`, `bymonth.py`, `rerun.py`,
`robust.py`, `fixscot.py`, `cmp_rerun.py`).

## Verdict: adopt for no-odds, with conditions. Do not switch the current build as it is.

The gain is real and reproducible, and I found no leak. Two defects have to be fixed before the switch:

1. **A season-context bug.** The G family assumes a double round robin. That is wrong for Scotland, Greece and
   Belgium.
2. **A silent failure mode.** If the candidate features are missing or stale, `fin_c` keeps scoring. It is biased
   by about +2.3 shots, and its 80% bound covers about 72%.

The conditions are listed at the end.

## 1. Leakage

| check | result |
|---|---|
| **Perturbation.** All results (shots, SOT, goals) randomised on and after D = 2019-01-26, 2023-03-04 and 2025-12-20. Families A, E and G rebuilt with `build_candidates`. | **0 cells changed** on the 40,583 / 69,548 / 89,336 rows dated before D. **0 changed** on the 82 / 109 / 99 rows dated D (own and same-day results excluded). The rows after D did change (sanity check). |
| **Truncation.** Every row after D deleted, features rebuilt, same three dates. | 0 columns changed. |
| **Hand recompute** with plain pandas, 25 random rows from 2019/20 onward, 13 values each (h2h n / weighted sum / weighted mean / dev / shrunk total; ctx gp / points / games left per side; EKF league level; expected home shots). | **325 / 325 equal.** The EKF level uses the last 150 rows in (date, match_id) order. On a tied boundary date, which rows enter the window depends on the match_id hash. That is arbitrary, but always in the past. |
| Stored `features_candidates.parquet` vs a fresh build from the current `matches.parquet` | identical (A, E, G columns). The curves were not built on stale features. |
| Training strictly before the block (saved monthly curves) | `train_end` is before the month start on every row of both variants. |
| Duplicated fixtures / same-day doubles | none across sources (single source). No team plays twice on one day. The 4,215 repeated pairings within a season are legitimate (Scottish 3- and 4-round formats, playoffs). |
| Odds | no-odds route: every `mkt_*` / `mktx_*` column is blanked. A, E and G use no odds. |

**"Games left" and look-ahead.**
- `ctx_games_left = 2(n − 1) − gp`.
  - gp counts only matches played before the date.
  - n is the number of teams seen in the whole competition-season, a fixed-format fact.
- No result from the future enters the feature.
- A postponed match leaves the team with a game in hand, so its games left stays higher. That is correct in real
  time, not look-ahead.
- Seasons curtailed by COVID (2019/20) never trigger the flag. That is also what was knowable at the time.

**Two real problems, neither of them a leak:**
- **(a) Wrong season length for non-double-round-robin leagues.**
  - 51 of 260 competition-seasons play more than 2(n − 1) games:

    | league | games played | formula 2(n − 1) |
    |---|---|---|
    | Scotland Premiership | 38 | 22 |
    | Scotland Championship, League One, League Two | 36 | 18 |
    | Greece | 36 | 26 |
    | Belgium (3 seasons) | 40 | 30 |

  - Games left hits 0 at mid-season. `G_last3` is then on for **49% to 55% of Scottish rows** (true last-3 share: 9%),
    32% in Greece and 18% in Belgium. `ctx_dead` and `ctx_progress` are corrupted in the same way.
  - In the 15 double-round-robin leagues, the flag agrees with the true season end on 98.5% to 100% of rows.
  - The damage shows up in the scores. Monthly October-April RPS change vs production is worse in exactly these
    leagues: Scotland Championship +0.035, Belgium +0.026, Scotland League Two +0.025.
  - Diagnostic re-run of 2025/26 with only the Scottish season lengths corrected (38 / 36, known in advance):
    - overall ΔRPS improves from −0.0183 to −0.0200;
    - the 2025/26 competition guard flag on Scotland Championship (RPS and pinball) disappears.

  This run looked at a test season, so it is evidence for the bug, not a tuned result.
- **(b) League size taken from hindsight early in the season.**
  - Historically, n uses every team of the season. A production build from played matches only sees fewer teams in
    the first week or two.
  - Truncating at 2025-08-03 and 2025-08-10 changes `ctx_games_left` and `ctx_rank_frac` on 5 to 18 rows.
  - The league size is genuinely known in advance, so in production n must come from the calendar or the competition
    configuration. Otherwise training and serving differ.

## 2. Shrinkage slope b and selection

- **b is out of sample.**
  - `FlexNB._fit_shrink` fits (c, b) by NB maximum likelihood on the latest training season(s) (`carve_calibration`,
    at least 3,000 rows). That season is scored by a model fitted before it. All of it is inside each block's training
    rows.
  - My 2025/26 re-run logs b per monthly block: **0.78 to 0.85**, with c ≈ 0. The report's 0.73 to 0.79 refers to
    other windows. b is never fitted on test rows.
- **Looks at the data before the choice:**
  - 2023/24 + 2024/25 were looked at many times: about 35 model specs × 2 variants in screening, then the finalists.
  - The modeler followed the coordinator's split, not `docs/evaluation-protocol.md` (which asks for tuning on
    2019/20 to 2022/23). The deviation is declared.
  - **2019/20 to 2022/23 are not fully untouched:**
    - the EKF hyper-parameters were tuned on 2012-08 to 2023-06 (4 parameters, NB log-likelihood of shots, not the
      candidate's RPS);
    - my own diagnosis (the source of the shrinkage and end-of-season ideas) used them as discovery seasons.
  - They were not used for the model choice. They do confirm it: `fin_b` season refit ΔRPS −0.0264
    [−0.0326, −0.0203].
  - **2025/26 is clean for the A/E/G/shrink choice**, and confirms it: −0.0182 / −0.0183. That is about 70% of the
    validation gain (−0.026 to −0.029), which is the size of winner's curse I would expect.
  - **2025/26 is not clean for `fin_b` → `fin_c`.** The per-competition side dispersion was added after the monthly
    run (2023/24 to 2025/26) showed one failing Scottish 90% cell. So the ship-rule pass of `fin_c` comes from the
    same window that motivated it.
  - `fin_c` itself has **no season-refit curves**: the "−0.0259, all 7 seasons" figure is `fin_b`. The added
    dispersion was neutral in screening (`cand_disp_side`: ΔRPS +0.0001), so the risk is small, but it is not
    evidence about `fin_c`.
- **Protocol gap.** The monthly decision run covers 2023/24 to 2025/26 only. The protocol asks for 2019/20 to 2025/26.
  The modeler gives memory as the reason. The monthly reference is `cand_nbmle` (the production NB re-estimated);
  its monthly bounds equal the production monthly backtest on 99.92% to 99.93% of 22,849 rows (odds variant).

## 3. Reproduction

- **From the saved curves** with `shots.evaluation.scoring`, no-odds, monthly, 2023/24 to 2025/26, n = 21,464: every
  claimed number reproduces exactly:
  - ΔRPS −0.0233 [−0.0316, −0.0157];
  - log score −0.0064 [−0.0108, −0.0017];
  - pinball −0.0086 [−0.0131, −0.0043];
  - MAE −0.034;
  - 2025/26: −0.0183 [−0.0304, −0.0062].
  - Season refit, `fin_b`, 7 seasons: −0.0259 [−0.0310, −0.0214]. Odds route: −0.0032 [−0.0070, +0.0009], not
    significant, with Scotland Championship flagged.
- **Independent re-run.** I wrote my own runner (`rerun.py`), not `screen.run_job`:
  - setup: 2025/26, no-odds, refit before every month on rows strictly before the month;
  - model spec: identified as `cand_nbmle` with `families=A,E,G, shrink=True, side_z=("comp",)`;
  - the `("comp","logmu")` alternative does not match.
  - Result: **max |ΔCDF| = 0.0 against the saved `fin_c` curves on 7,094 / 7,094 rows**, and the same scores.
- **Competition guard:** no competition is flagged on the pooled 3-season window. On 2025/26 alone, Scotland
  Championship is flagged worse for RPS and for pinball (the Scottish games-left bug, section 1a).

## 4. Ship rule (CLAUDE.md rule 3), no-odds, monthly, 2023/24 to 2025/26

| | fin_c | production (`cand_nbmle`) |
|---|---|---|
| coverage 70/80/90, overall | 73.4 / 82.5 / 91.5 | 73.4 / 82.5 / 91.4 |
| worst competition coverage | 72.0 / 81.1 / 90.4 (all ≥ q) | ≥ q everywhere |
| mid-coverage 70/80/90 | 70.1 / 79.8 / 89.6 (within ±2pp) | 70.2 / 79.8 / 89.5 |
| pinball (mean of 70/80/90) | **1.4399** | 1.4485 |
| `team_avg` pinball, same rows | 1.4634 | |
| under side ucov / umid 80 | 81.9 / 79.5 | 82.2 / 79.8 |
| upper / lower tail gap (pp) | +0.16 / −0.13 | +0.13 / −0.19 |
| reliability ECE (pp) | 0.24 | 0.16 |

**The rule passes.** Caveats:
- The tightest 90% cells are Scotland Championship (90.4%) and Belgium (90.5%), on n ≈ 540 and 934, where the
  standard error is about 1.0 to 1.3pp.
- Scotland Championship is the only competition where `fin_c` pinball is above `team_avg` (1.352 vs 1.336). The rule
  is overall, but that is the same league as the bug.
- Under side and tails are unchanged within 0.3pp. ECE is slightly worse but still small.

**Segments** (ΔRPS vs production; 80% coverage, `fin_c` with production in brackets):

| segment | n | ΔRPS | cov80 | umid80 |
|---|---|---|---|---|
| Aug/Sep | 3,984 | **−0.074** [−0.101, −0.048] | 82.5 (81.4) | 79.1 (80.0) |
| Oct-Apr | 15,862 | −0.011 [−0.018, −0.004]; pinball n.s. | 82.3 (82.4) | |
| May | 1,562 | −0.020 (n.s.) | 83.6 (86.0) | 76.6 (74.5); both bad, `fin_c` less so |
| cold start (< 5 prior matches) | 162 | −0.196 | 77.8 (78.4) | |
| promoted / relegated / new team in the match | 8,395 | −0.023 | 82.6 (82.7) | |
| same, Aug/Sep | | −0.067 | | |

- Cold start: both models are below 80%, n is tiny.
- **59% of the total gain comes from Aug/Sep** (19% of rows). It has the same sign in every season (August −0.07 to
  −0.14). That is where the carried-over ratings beat production's empty rolling form.
- The Aug/Sep segment exceeds the "too good" threshold (0.05). Every leakage check in section 1 passes, so I accept
  it. The protocol's threshold is defined on the overall gain (−0.023), which is within the expected range.

## 5. Robustness: missing or stale candidate features

Test: one block (March 2026, n = 740), with `fin_c` and production both fitted normally:

| scoring input | RPS | cov 70/80/90 | mean predicted (actual 24.59) |
|---|---|---|---|
| production | 3.057 | 75.0 / 85.3 / 93.1 | 24.42 |
| `fin_c`, complete features | 3.055 | 74.6 / 84.6 / 92.8 | 24.55 |
| `fin_c`, `adj_*` NaN | **3.403** (+0.35) | **59.2 / 71.6 / 85.3** | 26.94 |
| `fin_c`, all candidate columns NaN (stale build: a new fixture has no row) | 3.361 | 61.4 / 72.7 / 86.2 | 26.73 |
| `fin_c`, `ctx_*` NaN | 3.066 | 75.4 / 85.3 / 93.4 | 24.34 |
| `adj_*` columns absent | `KeyError` (hard failure) | | |

- **There is no safe fallback today.** NaN inputs are silently median-filled, and the `A_missing` flag was learned
  only on true cold starts. A stale or failed build would publish bounds that are too high, with 80% coverage near
  72%, and nothing would be reported.

## Conditions for adopting `fin_c` on the no-odds route

1. **Fix G season length.** Take each competition's scheduled games per team from configuration or the calendar
   (Scotland 38 / 36; for Greece and Belgium, the regular-season length or no flag), not 2(n − 1). In production,
   take n from the calendar as well. Then re-run the monthly decision run on the final code.
   - Preferably include 2019/20 to 2022/23 monthly, as the protocol requires.
   - Score `fin_c` season refit on 2019/20 to 2022/23.
   - Confirm that the competition guard is clean on 2025/26 alone, as well as pooled.
2. **Fail-safe routing.**
   - Build A/E/G in the same update run as scoring.
   - For each fixture, if `adj_*` is NaN while both teams have history (or `features_candidates` is older than the
     latest played match), score that fixture with the current production no-odds model and log it.
   - If more than about 2% of non-cold-start fixtures are affected, fail the step loudly (`job_runs`).
   - Add a test that a stale build falls back to production rather than median-filling.
3. Keep production on the odds route (not significant: −0.003 [−0.007, +0.001], and Scotland Championship flagged).
   This adopts on one variant only, which `docs/evaluation-protocol.md` rule 4 (both variants) does not literally
   allow. The coordinator or the user should record it as an explicit exception: the two routes are separate models.
4. Monitor Scotland Championship / League One and Belgium after the switch. They have the thinnest coverage margins.
