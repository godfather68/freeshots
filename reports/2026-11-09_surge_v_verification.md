# Independent verification of `cand_odds_surge_v` (Surge + venue-split ratings) for the odds route (2026-11-09)

Author: evaluator. Subject: the modeler's claim (`reports/2026-11-06_research_features.md`, commit f58fe96) that Surge
plus venue-split (GAP-style) ratings beats production Surge (`cand_odds_surge`) on the odds route.

No project code, config or test was changed. Nothing was committed. `data/processed/board.parquet`, `board.meta.json`,
`features.parquet` and `data/shots.db` hash the same as before. My scripts and outputs are in the session scratchpad,
under `vv/`:
- scripts: `build.py`, `leak.py`, `runner.py`, `score.py`, `analyse.py`, `ablate.py`, `dryrun.py`;
- outputs: `analyse.out`, `ablate.out`, `dry_*.log`, `runs/`;
- the scratch project copy used for the dry runs: `root/`.

I did not reuse the modeler's cached predictions as evidence. I used them once only, to check that my run reproduces
them.

## Verdict: DON'T ADOPT `cand_odds_surge_v`

In plain language:
- The headline gain is real and I reproduce it exactly: RPS -0.0060 against production Surge.
- **But it is almost all "Surge reads the opponent-adjusted ratings".** The venue split, the part that makes this
  model new, is not robust:
  - it adds -0.0015 RPS pooled, with a CI that just excludes 0;
  - it adds **nothing on pinball**, the primary metric (-0.0003, n.s.);
  - the gain comes only from 2022/23 to 2024/25. It is reversed in 2025/26 (+0.0007) and in the untouched 2026/27
    slice (+0.0017; pinball +0.0040);
  - it makes Portugal significantly worse than the control.
- **The explanations shown to users are broken** with this model. "League level" shows about -12 shots and "other"
  about +11 shots on every odds fixture. That alone blocks publishing.
- I found no leak.

What to do instead: the robust part is the single EKF ratings fed to Surge. I tested two forms:
- the modeler's control `Surge + A`;
- equivalently, `cand_odds_surge_v` with venue variance 0 (`p0v = 0`).

On the untouched 2026/27 slice it is the *better* variant:
- Surge + A: -0.0164 [-0.0284, -0.0050];
- V at `p0v = 0`: -0.0112 [-0.0223, -0.0008];
- full Surge + V: -0.0096 (n.s.).

If anything is adopted, it should be that simpler candidate, after its own verification (see the end). Its
confirmation CI for Surge + A touches 0.

## 1. Leakage audit

| check | result |
|---|---|
| Fresh rebuild | `features.parquet` rebuilt from `matches.parquet`: 0 differing cells (95,274 rows). Candidate + research columns: identical to the modeler's `extra.parquet` (70 columns, max abs diff 0.0). |
| **Perturbation.** All results (shots, SoT, goals, HT goals, corners, reds, fouls) randomised on and after D = 2019-11-30, 2021-08-21, 2023-01-14, 2025-03-15, 2026-09-27. Then all 55 inputs of `cand_odds_surge_v` rebuilt (production features, families adj / h2h / ctx / form, research V). | **0 differing cells** before D (46,367 to 94,614 rows) and **0 on D** itself (5 to 103 same-day rows). 100% of later rows change in V (sanity). |
| Truncation (rows after D deleted) | 0 differences, except 2021-08-21: `ctx_games_left` on 7 Italian opening-week rows. This is the known league-size-from-season effect (fin_c and Surge verifications). It is not related to V, and in production the size comes from the calendars. |
| Input order shuffled | 0 differing cells: features do not depend on row order. |
| Same-day results blanked (2024-04-13, 2026-10-25) | 0 differences on that day and before: a match never sees a same-day result. |
| Code review, `venue_ratings` | All matches of a date are read before any is written. Rows without shots never update a state, so upcoming fixtures are inert. The league level `league_level` uses strictly earlier matches only. The variance inflation uses the last *played* date. |
| Duplicated fixtures | One source (football-data). 0 duplicate match_id or (date, home, away). 0 teams playing twice on a day. The 18 same-pair matches within 3 days are reverse fixtures with different stats (Scottish, National League and Belgian play-off schedules), not duplicates. |
| Odds | Unchanged ingest. The import-time guard rejects closing-odds columns. V adds no market input. |
| Walk-forward | My runner asserts `train.max(date) < month start` and no shared rows, on all 88 monthly blocks. Result columns are dropped from the scored rows. |
| Production path (`scan.candidate_features` / `_candidate_odds_route`) | The research build runs on played + upcoming rows in the same causal pass. It is merged one-to-one. A missing V input makes a fixture fall back. It is built only when a selected candidate reads V; production settings give `()`. |

No leak. The gain is about 0.2% of RPS, nowhere near "too good".

## 2. Reproduction

My own monthly runner fits the registered classes and reads curves through the public `prob_at_least_curve`.

Rows: odds rows only, Scotland Championship excluded (it stays on `negbin_pit_comp` for both models, so its rows are
identical by construction). n = 48,454 for 2019/20 to 2025/26, plus 1,922 for 2026/27 to 26 Oct. The modeler did not
run 2026/27, so for the venue split it is an **untouched slice**.

Checks:
- My curves match the modeler's cache with a max |ΔCDF| of 6e-4, and 99.95 to 99.99% of bounds are equal.
- Published bounds and the curve median equal the CDF-derived ones on 99.99 to 100% of rows.

Scores: candidate minus reference, negative = candidate better. 95% CIs come from 1,000 day-block bootstrap resamples
(month-block in brackets for RPS).

| slice | n | RPS V / Surge | ΔRPS V - Surge [day] [month] | Δpinball | Δlog | ΔMAE median |
|---|---|---|---|---|---|---|
| **all 2019/20-2025/26** | 48,454 | 3.0721 / 3.0781 | **-0.0060 [-0.0081, -0.0038] [-0.0087, -0.0036]** | -0.0028 [-0.0040, -0.0015] | -0.0013 (n.s.) | -0.010 [-0.015, -0.005] |
| discovery 2019/20-2022/23 | 27,531 | 3.0193 / 3.0261 | -0.0068 [-0.0095, -0.0041] | -0.0033 | -0.0015 | -0.010 |
| confirmation 2023/24-2025/26 | 20,923 | 3.1417 / 3.1466 | -0.0049 [-0.0081, -0.0018] | -0.0021 [-0.0039, -0.0004] | -0.0011 | -0.009 |
| 2025/26 | 6,914 | 3.1212 / 3.1232 | -0.0020 [-0.0067, +0.0024] n.s. | -0.0015 n.s. | -0.0007 | -0.006 |
| **2026/27 (untouched)** | 1,922 | 3.3780 / 3.3875 | -0.0096 [-0.0227, +0.0029] n.s. | -0.0036 n.s. | -0.0098 | -0.013 |

- Per season, ΔRPS: -0.0066, -0.0096, -0.0045, -0.0066, -0.0060, -0.0067, -0.0020, -0.0096. This matches the
  modeler's report to 1e-4.
- Brier score of P(24+): -0.00044 [-0.00067, -0.00020].

## 3. Where the gain comes from (the decisive part)

The modeler's control, Surge + A, also carries the SoT ratings and the rating SD. The cleaner ablation is the same
model with venue variance 0 (`p0v = 0`). Then the general part equals family A's shot rating exactly (max diff 0.0)
and the deviations are 0. Only the split differs between it and `cand_odds_surge_v`.

Monthly refit, ΔRPS [day CI] (pinball in parentheses):

| comparison | 2019/20-2025/26 | discovery | confirmation | 2025/26 | 2026/27 untouched |
|---|---|---|---|---|---|
| Surge + A − Surge | -0.0044 [-0.0059, -0.0027] | -0.0059 | -0.0024 [-0.0047, +0.0002] | -0.0031 n.s. | **-0.0164 [-0.0284, -0.0050]** (pin -0.0097) |
| V at p0v=0 − Surge | -0.0044 [-0.0058, -0.0029] | -0.0055 | -0.0031 [-0.0053, -0.0007] | -0.0027 n.s. | **-0.0112 [-0.0223, -0.0008]** (pin -0.0067) |
| **split: V − V at p0v=0** | **-0.0015 [-0.0030, -0.0001]** (pin -0.0003) | -0.0013 n.s. | -0.0018 n.s. | **+0.0007** n.s. | **+0.0017** n.s. (pin +0.0032) |
| split vs A control: V − Surge + A | -0.0016 [-0.0032, -0.0001] (pin -0.0006) | -0.0009 | -0.0025 [-0.0050, -0.0000] | +0.0011 | +0.0068 [-0.0011, +0.0142] (**pin +0.0061 [+0.0011, +0.0118]**) |

Split's own ΔRPS per season: -0.0004, -0.0001, +0.0001, **-0.0047, -0.0033, -0.0029**, +0.0007, +0.0017. Three
seasons carry it. The two most recent seasons go the other way.

**Sensitivity to the venue variance** (season refit, ΔRPS vs V at p0v=0, pooled):

| p0v | ΔRPS |
|---|---|
| 0.005 | -0.0016 |
| 0.01 (chosen) | -0.0016 |
| 0.02 | -0.0015 |

So the value is not tuned to a knife edge. That rules out one kind of optimism, but not the main one: the gain is the
same whatever the venue variance, and it does not hold out of time.

**Competition guard for the split** (V − Surge + A, RPS):

| slice | competitions significantly worse |
|---|---|
| pooled | Portugal +0.0078 [+0.0024, +0.0134] |
| 2025/26 | England Championship +0.025 [+0.001, +0.050], Portugal +0.017 [+0.003, +0.032] |
| 2026/27 | Scotland League One +0.050, Scotland Premiership +0.047 (n = 58 to 60) |

**Size.** On 2025/26 rows the venue-deviation terms move the log mean with a standard deviation of only about 0.5%,
about 0.1 shot. That is consistent with the modeler's own finding that team-specific home edges in shots are mostly
noise (every venue share lowered one-step NB log-likelihood).

**Selection bias.**
- The finalists were picked from screening tables that showed all seven seasons, so "confirmation" is not clean for
  the choice of V.
- The only slice that was truly untouched is 2026/27. There the split is worse than the control on pinball (CI above
  0) and point-worse on RPS.
- Combined with the 2025/26 reversal, the split's -0.0015 is best read as a 2022-2025 artefact at the edge of
  resolution, not as a stable effect.

## 4. Ship rule (CLAUDE.md rule 3), monthly, odds rows without Scotland Championship

| | Surge + V | Surge | Surge + A | team_avg | league_avg |
|---|---|---|---|---|---|
| coverage 70/80/90 | 73.3 / 82.7 / 91.5 | 73.3 / 82.6 / 91.5 | 73.2 / 82.5 / 91.5 | 69.9 / 80.0 / 89.8 | 74.4 / 83.5 / 91.9 |
| mid-coverage 70/80/90 | 69.9 / 79.8 / 89.6 | 69.9 / 79.8 / 89.6 | 69.9 / 79.7 / 89.7 | | |
| pinball (mean) | **1.4019** | 1.4047 | 1.4026 | 1.4295 | 1.4784 |
| MAE of median | 4.343 | 4.352 | 4.344 | 4.412 | 4.591 |

**Pooled 2019/20 to 2025/26.**
- No competition cell is below q.
- Pinball is below team_avg in every competition.
- Rounding-aware rule: **passes**, as for production.
- Scotland Championship is not scored by either model; the earlier report has its 89.5% (90%) shared shortfall.

**Confirmation.** No cell below q; pinball below team_avg everywhere.

**Single seasons.** Both models fail several cells, about equally often, all within noise.
- 2025/26:
  - Surge + V: Portugal 89.5 and Scotland Premiership 89.9 at 90%;
  - Surge: Portugal 89.5 and Turkey 89.9 at 90%.
- 2026/27 (small n): both fail Scotland League One at all levels (V 56.9 / 70.7 / 84.5, Surge 58.6 / 74.1 / 86.2,
  n = 58) and England National League.

**Watch list at 90%** (V / Surge):

| competition | pooled | 2025/26 | 2026/27 |
|---|---|---|---|
| Turkey | 91.3 / 91.2 | 90.5 / 89.9 | 92.6 / 92.6 |
| Portugal | 90.8 / 90.6 | 89.5 / 89.5 | 96.3 / 96.3 |
| Scotland Premiership | 91.2 / 91.3 | 89.9 / 91.2 | 96.7 / 96.7 |
| Scotland League One | 91.0 / 91.0 | 90.0 / 91.1 | 84.5 / 86.2 |
| Scotland League Two | 90.1 / 90.1 | 92.2 / 93.3 | 92.7 / 90.9 |

Mid-coverage at 90% for Scotland League Two is 87.9 pooled for both models. It is low but shared with production.

**Competition guard vs production Surge (V − Surge):**
- Pooled: **France Ligue 1 is worse on pinball**, +0.0052 [+0.0002, +0.0105]. This is borderline, and the modeler
  reported "none worse". Better: Spain La Liga, Turkey (RPS and pinball), Scotland League One / Two, Italy Serie A and
  Portugal (pinball).
- Confirmation, 2025/26 and 2026/27: none worse.

## 5. P(24+) calls (what users see)

Each cell is n / stated / hit. Δhit is V − Surge with a day-block CI.

| slice | Surge | Surge + V | Δhit |
|---|---|---|---|
| 70%+ calls, 2019/20-2025/26 | 6,820 / 76.3 / 75.5 | 7,127 / 76.5 / 75.6 | +0.12 [-0.41, +0.62] |
| confirmation | 3,792 / 76.4 / 75.9 | 3,952 / 76.6 / 75.8 | -0.09 [-0.71, +0.58] |
| 2025/26 | 1,160 / 75.4 / 77.4 | 1,258 / 75.6 / 76.8 | -0.63 [-1.68, +0.47] |
| 2026/27 | 741 / 78.7 / 79.6 | 802 / 79.2 / 79.3 | -0.32 [-1.43, +0.89] |

**Reliability bands**, pooled 2019/20 to 2025/26, stated / realised (n):

| band | Surge | Surge + V |
|---|---|---|
| 0.7-0.8 | 74.2 / 73.4 (5,301) | 74.2 / 73.0 (5,426) |
| 0.8-0.9 | 83.4 / 82.2 (1,459) | 83.5 / 83.6 (1,618) |
| 0.9+ | 91.5 / 95.0 (60) | 91.5 / 91.6 (83) |

- Calls only V makes: n = 889, stated 71.5%, hit 70.2%. Calls only Surge makes: n = 582, stated 71.4%, hit 66.0%.
- About 4.5% more calls at the same hit rate. The call side is fine and is not the reason for the verdict.
- 2024/25 is weak for both. The 0.8-0.9 band realises 76.8% for V against 79.3% for Surge.

## 6. Dry run (`SHOTS_ODDS_MODEL=cand_odds_surge_v`)

Setup: a scratch copy of `src/`, `data/processed/`, `models_store/`, calendars and a copy of the DB, with
`PYTHONPATH`, `SHOTS_DB` and `SHOTS_BOARD` pointed at it. `board.build(30, refresh=False)` was run for production and
for V.

**(a) Today, 2026-11-09, online fixtures.csv.** 414 fixtures, but only **1 odds fixture** (Monday; football-data
posts odds Tuesday and Friday). Fallback 0. Not informative.

**(b) Replay of 2026-10-27** (cached fixtures file, network blocked; results to 26 Oct, the state of the last
published board): 482 fixtures, 45 odds fixtures on the route, 34 published.

| check | result |
|---|---|
| Fallback | **0 of 45** (`share_non_cold` 0.0, limit 0.02). 1 excluded (Scotland Championship). The research build ran (about 1.5 min per candidate build; 3 builds per board). |
| No-odds fixtures | 448, identical to production. |
| `expected` changed | 12 of 34 (8 ×+1, 3 ×−1, 1 ×+2) |
| `lb_70` / `lb_80` / `lb_90` changed | 8 / 8 / 11, all ±1 |
| ΔP(24+) | -0.025 to +0.041; max |Δ| on any line 0.089 |
| 70%+ calls | 28 → 28: one gained (Mantova–Cremonese 0.686 → 0.708), one lost (Sheffield Weds–Cambridge 0.702 → 0.683) |
| **Explanations** | **Broken.** Every odds fixture shows "League level: the model rates an average matchup at 15.8 shots (league average 28.2), effect -12.4", balanced by an "other" factor averaging **+11.4 shots** (max 14.8). Production: "other" averages 0.5. |

Cause of the broken explanations: `shots.models.explain._family_reference` sets every unknown family column's reference
to 0. `V_log_exp`'s reference should be the log league level, as for `A_log_exp`. `_family_groups` also has no
mapping for `V_*` columns, so they all fall into "other".

On historical rows, 25 to 30% of published bounds and 31% of medians would change by about 1. The mean |ΔP(24+)| is
0.020.

Side notes:
- `shots.surge_monitor` matches `predictions.model` by the prefix `cand_odds_surge`, so it would count `_v` calls as
  Surge (fine, but implicit).
- Surge Watch would still skip these fixtures, because the route prefix is `negbin_pit_comp_`. That is correct: no
  double counting.

## What is still missing for any adoption

1. **Explanation factor group** for V (or for whichever ratings variant is adopted):
   - reference `V_log_exp` = log league level;
   - `V_att_s` / `V_dev_s` → `{side}_rating_attack`;
   - `V_def_o` / `V_dev_o` → `{o}_rating_defence`;
   - a test that the "other" factor stays small on a Surge + V board.
2. A **`src/shots/export/model_history.json` milestone** and the CLAUDE.md rule 5 update (route, Scotland
   Championship exclusion kept, monitor prefix).
3. A decision on the borderline **France Ligue 1 pinball** flag vs production.
4. Recommended instead: verify **Surge + A** (or V at p0v=0, which is equivalent: ΔRPS -0.0001 between them) as its
   own candidate, with the same protocol:
   - It has the robust part of the gain: -0.0044 pooled and -0.0164 / -0.0112 on the untouched 2026/27.
   - It needs no research build and no new feature code in the update path.
   - Its weak point is confirmation: -0.0024 with a CI touching 0 for A; V0 is -0.0031 [-0.0053, -0.0007].
   - It needs its own dry run and an explanation check. Family A is already mapped in `explain`, from fin_c.
   - Let 2026/27 accumulate (about 2,000 rows now) before deciding.
