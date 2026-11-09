# Four research ideas for the shot model (2026-11-06)

Author: modeler. Production is **unchanged**: `ODDS_MODEL = "cand_odds_surge"` (Scotland Championship on
`negbin_pit_comp`), `NOODDS_MODEL = "fin_c"`. Nothing was committed. The new candidates are opt-in only.

## Summary in plain language

We tested four ideas from the football-analytics literature. Each one could make our shot forecasts better.

| idea | where it comes from | what we found | verdict |
|---|---|---|---|
| 1. **Separate home and away ratings** for every team (home attack, home defence, away attack, away defence) | Wheatcroft's GAP ratings (Int. J. Forecasting 2020; J. Sports Analytics 2021) | As a forecaster on their own, split ratings are *worse* than one rating plus a league-wide home edge: team-specific home edges in shots are mostly noise. Used as **extra inputs** next to our model, they help a little on both routes. | **Odds route: promising, verify before adopting.** No-odds route: needs more (one league gets worse). |
| 2. **Closed-doors flag** for COVID-era matches without crowds | Reade, Schreyer & Singleton 2022; Wunderlich et al. 2021: empty stadiums removed referee bias and about halved home advantage | Confirmed in our data: with no crowd the home team's shot edge falls by about half, in all 11 countries. None of flag / down-weight / exclude improves forecasts: the model already adapts, and no future match is played behind closed doors. | **Don't adopt.** |
| 3. **Game-state adjustment**: discount shots taken while chasing a half-time deficit, and correct for red cards | Skripnikov et al. 2025 (arXiv 2508.04008); StatsBomb on score effects | The adjustment can be measured: a team trailing at half-time takes about 5% more shots than expected, and a red card costs about 15% of shots. But ratings built from adjusted shots predict no better than our current ratings. | **Don't adopt.** |
| 4. **Corners and shots on target** in the ratings | same GAP papers (shots + corners); our own SoT ratings | Corner ratings add nothing on either route. Shots-on-target ratings already sit inside fin_c; adding them to Surge does nothing either. | **Don't adopt.** |

**The best combination is idea 1 alone**, because nothing else helps. Candidates:
- `cand_odds_surge_v` = Surge + venue-split ratings;
- `cand_fin_v` = fin_c + venue-split ratings.

Monthly walk-forward, 2019/20 to 2025/26:
- **Odds route:** Surge + V beats production Surge in RPS by **-0.0060 [-0.0081, -0.0039]**. The gain holds in discovery
  and in confirmation. Pinball is better (-0.0028). No competition is worse. The ship rule passes. 70%+ calls of
  "24+ shots" hit at the same rate (75.6% vs 75.5%) with 4.5% more calls.
  - Caveat: about 70% of the gain comes from adding *any* opponent-adjusted rating to Surge (control: -0.0044). The
    venue split itself adds -0.0016 [-0.0031, -0.0002].
  - In 2025/26 alone the gain is not resolved (-0.0020, CI includes 0).
- **No-odds route:** fin_c + V gains -0.0020 [-0.0032, -0.0007]. That is below the protocol's resolution of about
  0.003, and England League One is significantly worse. Not ready.

## 1. Method

**Data.** Snapshot of `data/processed/matches.parquet` taken 2026-11-06, with the ingest agent's new columns
(`home_ht_goals`, `away_ht_goals`, `home_corners`, `away_corners`, `home_reds`, `away_reds`, ...). It covers 95,274 rows;
`features.parquet` covers the same rows. The existing candidate families (A, E, G, H) were rebuilt on this snapshot. They
are **identical** to the build behind the cached production references: 0 differing cells on 94,889 shared rows.

**New features.** `src/shots/features/research.py`. Every feature of a match uses matches strictly before its date,
and all matches of a day are read before any is written.
- **V, venue-split ratings.** An extended Kalman filter, the same NB observation model as family A. Each team's attack
  and defence = a general part + a venue deviation (home / away). The general part is exactly the existing EKF rating:
  with venue variance 0 the outputs are identical (tested).
- **Z, closed doors.** `src/shots/features/closed_doors.csv` gives per-country periods with notes, for all 11
  countries. It flags closed periods and, separately, reduced-capacity ("partial") periods.
- **K, corner ratings.** The family A filter run on corners.
- **W, game state.** Each past side-observation's shots are divided by a causal factor for its state: half-time lead /
  level / deficit, and own / opponent red card (full-match counts; the timing is unknown). Factor of a state on date D
  = (realised / rating-expected shots in that state, over all earlier matches) / (the same for the neutral state),
  shrunk with K = 200 observations. The adjusted shots then go through the same filter.

**Candidates** (`src/shots/models/candidates.py`):
- New families V, K, S (SoT ratings only), Z and W.
- Out-of-sample terms `vdisagree`, `wdisagree`, `kdisagree`.
- `FlexNB(closed_weight=, partial_weight=)` to down-weight closed / partial matches (0 drops them).
- `CandidatePit(exclude_closed=True)` to drop closed matches from every fit, calibration included.
- All defaults are off.

**Walk-forward** (`shots.models.screen`, `docs/evaluation-protocol.md`):
- Screening uses a season refit; finalists get a monthly refit (fit on every row dated before the month).
- Seasons 2019/20 to 2025/26. Discovery = 2019/20 to 2022/23, confirmation = 2023/24 to 2025/26.
- 2026/27 was not run.

**References** = production on each route, on identical rows:
- **Odds:** Surge (`cand_odds_surge`), re-run monthly with the same runner. My earlier 2025/26 re-run against the
  cached Surge curves differed by at most 3e-4 in the CDF, 99.98% of bounds equal.
  - Scotland Championship is excluded from the odds comparisons. Production scores it with `negbin_pit_comp`, and
    every candidate would keep that exclusion, so its rows are identical by construction.
  - Odds rows only: n = 48,454.
- **No-odds:** fin_c, from the cached monthly curves of its adoption run. A 2025/26 re-run reproduces them to 1e-7
  with identical bounds. All rows: n = 49,643.

**Scores** (`shots.evaluation.scoring`):
- Paired differences, candidate minus production (negative = better), with 95% day-block bootstrap CIs.
- Competition guard on RPS and pinball.
- Ship rule (CLAUDE.md rule 3) with per-competition pinball against `team_avg` (monthly team_avg bounds).
- P(24+) reliability and 70%+ calls.

**Selection hygiene.**
- Fixed in advance, not tuned:
  - the venue variance (see 2.1);
  - the down-weight 0.33 (one third);
  - the game-state shrinkage K = 200.
- The screening tables were read pooled and by period. The finalists (V on both routes, the A control, the Z flag)
  were picked from screening that showed all seven seasons, so confirmation is not a fully untouched slice for the
  *choice of finalist*. The finalists are the obvious ones: nothing else had a non-zero effect.

**Leakage.**
- Unit tests (`tests/test_features_research.py`, 14 tests): rebuild with all results changed on and after three cut
  dates; truncation; upcoming fixtures never update a state; equivalence with family A.
- Real data: results (shots, SoT, goals, HT goals, corners, reds) randomised on and after 2020-11-07, 2022-02-12 and
  2024-12-21, then a rebuild. **0 differing cells** on every row dated on or before the cut (incl. 80 to 100
  same-day rows); every later row changes.
- No candidate is "too good": the largest gain, 0.006 RPS, is well below the protocol's 0.05 alarm.

## 2. Results by idea

### 2.1 Separate home and away ratings (GAP-style)

**As a forecaster.** One-step-ahead NB log-likelihood of each side's shots under the filter's own expectation,
matches 2019-07 to 2023-06, difference vs the single rating:

| venue share of the rating variance | Δ log-lik per side-match |
|---|---|
| tiny venue part (p0v = 0.001) | -0.0002 |
| p0v = 0.0025 / 0.005 / 0.01 on top of the general part | -0.0009 / -0.0017 / -0.0027 |
| 25% / 50% / 75% venue-specific (same total variance) | -0.0053 / -0.0079 / -0.0099 (pure GAP-like) |

Every venue share is worse, and the more venue-specific the worse. Team-specific home edges in *shots* are too noisy
to estimate from one season's ~19 home matches. One rating plus a league-wide home edge is the better filter. The
research build keeps p0v = 0.01 without drift only so that the venue deviation has content to test as a model input.

**As model inputs** (season refit screening, pooled 2019/20 to 2025/26, ΔRPS [CI]):

| route | candidate | ΔRPS vs production | note |
|---|---|---|---|
| odds | Surge + A (control: single EKF ratings in-sample) | -0.0047 [-0.0065, -0.0031] | Surge has no in-sample ratings |
| odds | Surge + V | -0.0064 [-0.0086, -0.0043] | vs Surge + A: **-0.0017 [-0.0032, -0.0003]** |
| odds | Surge + A + V | -0.0062 [-0.0084, -0.0042] | vs Surge + A: -0.0015 [-0.0029, -0.0001] |
| odds | Surge with the `disagree` term on V instead of A | -0.0011 [-0.0016, -0.0006] | small |
| no-odds | fin_c + V | -0.0019 [-0.0033, -0.0006] | fin_c already has A |

**Monthly decision run, odds route** (n = 48,454; production RPS 3.0781):

| slice | ΔRPS Surge+V | Δpinball | Δlog | ΔMAE | ΔRPS Surge+A (control) |
|---|---|---|---|---|---|
| all 7 seasons | **-0.0060 [-0.0081, -0.0039]** | -0.0028 [-0.0040, -0.0016] | -0.0013 [-0.0032, +0.0005] | -0.010 | -0.0044 [-0.0060, -0.0026] |
| discovery | -0.0068 [-0.0097, -0.0042] | -0.0033 | -0.0015 | -0.011 | -0.0059 [-0.0080, -0.0038] |
| confirmation | **-0.0049 [-0.0081, -0.0019]** | -0.0022 [-0.0040, -0.0003] | -0.0011 | -0.009 | -0.0024 [-0.0047, +0.0003] |
| 2025/26 | -0.0020 [-0.0071, +0.0025] | -0.0015 (n.s.) | -0.0007 | -0.006 | -0.0031 [-0.0070, +0.0005] |

- Per season, ΔRPS is negative in all seven: -0.0066, -0.0096, -0.0045, -0.0066, -0.0060, -0.0067, -0.0020.
- Venue split over the A control, monthly: -0.0016 [-0.0031, -0.0002] pooled, -0.0025 in confirmation, +0.0012 (n.s.)
  in 2025/26. The guard flags Portugal (RPS) and England League One / Ligue 1 (pinball) against the control, not
  against production.
- **Competition guard vs production:** none worse on RPS or pinball; 5 of 20 significantly better.
- **Ship rule:**
  - coverage 73.3 / 82.7 / 91.5, mid 69.9 / 79.8 / 89.6 (production 73.3 / 82.6 / 91.5 and 69.9 / 79.8 / 89.6);
  - no competition cell below q;
  - pinball 1.4019 vs team_avg 1.4295, below team_avg in every competition. **Passes.**
- **P(24+)**, stated / realised (n):

  | | production | Surge + V |
  |---|---|---|
  | band 0.7-0.8 | 74.2 / 73.4 (5,301) | 74.2 / 73.0 (5,425) |
  | band 0.8-0.9 | 83.4 / 82.1 (1,459) | 83.5 / **83.6** (1,618) |
  | 70%+ calls: n, stated, hit | 6,820, 76.3, 75.5 | **7,126, 76.5, 75.6** |
  | ... in confirmation | 3,793, 76.4, 75.9 | 3,951, 76.6, 75.8 |
  | ... in 2025/26 | 1,161, 75.4, 77.3 | 1,258, 75.6, 76.8 |

  Brier score of P(24+) -0.00044.

**Monthly decision run, no-odds route** (n = 49,643; production fin_c RPS 3.0806):

| slice | ΔRPS fin_c+V | Δpinball | Δlog |
|---|---|---|---|
| all | -0.0020 [-0.0032, -0.0007] | -0.0010 [-0.0019, -0.0001] | -0.0008 |
| discovery | -0.0012 [-0.0028, +0.0006] | | |
| confirmation | -0.0030 [-0.0048, -0.0010] | -0.0012 | -0.0012 |
| 2025/26 | -0.0006 (n.s.) | +0.0010 (n.s.) | +0.0036 |

- **Guard:** England League One is worse on RPS (+0.0065 [+0.0002, +0.0123]) and on pinball (+0.0041). Italy Serie B,
  Scotland Championship and Scotland League One are better.
- **Ship rule** passes: coverage 73.2 / 82.4 / 91.5, mid 69.8 / 79.6 / 89.6, pinball 1.4048 vs team_avg 1.4279.
- **Calls:** 7,061 at 74.8% vs 6,973 at 74.8%. The 0.8-0.9 band realises 81.8% vs 81.0%.
- The gain comes from 2022/23 to 2024/25 (about -0.004 each). The other seasons are about 0.

### 2.2 Closed doors

**Home edge in shots** (home / away shot ratio, matches since 2015/16):

| status | n | home/away | home share | mean total |
|---|---|---|---|---|
| open, before Mar 2020 | 27,472 | 1.219 | 54.9% | 23.2 |
| **closed** | 7,833 | **1.107** | 52.5% | 22.6 |
| partial capacity | 2,411 | 1.198 | 54.5% | 24.1 |
| open, after | 36,249 | 1.207 | 54.7% | 24.3 |

- On the log scale the edge falls from 0.19 to 0.10, **about half**, as Reade et al. and Wunderlich et al. found for
  goals and referee decisions.
- It falls in every country. Closed vs open-before ratio: Belgium 1.07 vs 1.23, England 1.09 vs 1.21, France 1.15 vs 1.22,
  Germany 1.10 vs 1.19, Greece 1.13 vs 1.38, Italy 1.11 vs 1.22, Netherlands 1.11 vs 1.27, Portugal 1.14 vs 1.20,
  Scotland 1.08 vs 1.14, Spain 1.11 vs 1.28, Turkey 1.17 vs 1.27.
- Partial-capacity matches behave like open ones.

**Home-advantage parameter** (no-odds production NB fitted on rows before 2023-07, implied mean log home edge on
2023/24 rows; realised 2023/24 = 0.188):

| training | implied edge | note |
|---|---|---|
| default (closed matches included) | 0.192 | |
| (a) flag in the design | 0.203 | flag coefficient -0.091 on the log edge (home -0.047, away +0.044) |
| (b) closed matches weight 0.33 | 0.199 | |
| (c) closed matches excluded | 0.202 | 0.203 if partial-capacity matches are excluded too |

Including closed matches pulls the edge down by about 0.01. The current model's edge is actually the closest to
2023/24.

**Forecasts** (ΔRPS vs production):

| variant | odds, season | no-odds, season | odds, monthly | no-odds, monthly |
|---|---|---|---|---|
| (a) flag | +0.0009 [-0.0001, +0.0020] | +0.0026 [+0.0013, +0.0038] | +0.0010 [-0.0002, +0.0023] | +0.0008 [-0.0005, +0.0022] |
| (b) weight 0.33 | +0.0000 | -0.0001 | | |
| (c) exclude | +0.0003; pinball -0.0012; log +0.0023; ship fails (Turkey 70%: 69.9) | +0.0004; log +0.0025; ship fails (Scotland Championship 90%: 89.7) | | |

**Why nothing helps:**
- The league levels (trailing 150 matches) and the rolling team rates adapt within weeks.
- The flag's coefficient is learned from few periods, and the guard flags Serie A / Ligue 2 / Serie B.
- No match that we will predict is played behind closed doors.
- The table is still worth keeping as documented data (it reproduces the expected home-edge collapse). The dates come
  from public announcements recalled without re-checking primary sources; they are within about a week. The notes
  say where capacity was mixed.

### 2.3 Game state

**Causal factors**, estimated on all matches before the latest date: HT deficit **1.059**, HT lead 1.003, own red card
**0.855**, opponent red card **1.155**.
- The "lead" factor is about 1, not below 1. Teams that lead at half-time are on average stronger on the day than
  their rating says, which offsets the suppression of shots while protecting a lead.
- With only HT and FT data we can measure the net effect, not the in-game one Skripnikov et al. estimate from event
  data.

**Forecasts:**

| route | candidate | season ΔRPS vs production | vs the matching control |
|---|---|---|---|
| odds | Surge + W | -0.0052 [-0.0070, -0.0038] | vs Surge + A: -0.0006 [-0.0012, +0.0001] |
| odds | Surge + A + W | -0.0049 | vs Surge + A: -0.0003 [-0.0010, +0.0003] |
| odds | `disagree` on W | -0.0002 | |
| no-odds | fin_c + W | -0.0005 [-0.0012, +0.0003] | monthly: -0.0003 [-0.0011, +0.0004]; Eredivisie pinball worse |

The game-state-adjusted ratings are a near substitute for the existing ratings, with no extra information.

### 2.4 Corners and shots on target

| route | candidate | season ΔRPS vs production |
|---|---|---|
| odds | Surge + corner ratings (K) | +0.0001 [-0.0005, +0.0008]; guard flags Championship, 2. Bundesliga |
| odds | Surge + corner/shot volume term (`kdisagree`) | +0.0001 |
| odds | Surge + SoT ratings (S) | -0.0007 [-0.0017, +0.0003]; guard flags Serie A / B |
| no-odds | fin_c + corner ratings | +0.0001 [-0.0007, +0.0008] |
| no-odds | SoT ratings | already in fin_c (family A: `A_sot_att_s`, `A_sot_def_o`) |

Corner volume carries no information on total shots that shot ratings do not already have. This matches the 2026-10-15
finding that corner style features had team-season but no match-level signal.

## 3. Combined candidate

V is the only idea with a gain, so the combined candidate is V alone. Adding Z, W or K to it was not run: each one is
null or harmful on its own, both against production and against the matching control.

## 4. Recommendation

- **Odds route, `cand_odds_surge_v`: promising; adoption needs your decision after an independent verification and a
  dry run.**
  - It meets decision-rule conditions 1 to 3 pooled and in confirmation: CI below 0, pinball better, no competition
    worse, ship rule passes. Its calls are as accurate, with more of them.
  - 2025/26 alone is not resolved.
  - Be honest in any wording: most of the gain is "Surge also reads the team ratings directly". The venue split adds
    about a quarter of it.
  - A simpler alternative, Surge + A, gives -0.0044 but is not resolved in confirmation.
  - Scotland Championship would stay on `negbin_pit_comp`.
- **No-odds route, `cand_fin_v`: needs more. Do not switch.**
  - The gain (-0.0020) is below the protocol's resolution.
  - The guard flags England League One.
  - 2025/26 is flat.
- **Closed doors: don't adopt.** Keep the table and the analysis as documentation.
- **Game state: don't adopt.** If event data with in-game timing arrives (Opta), revisit: the HT proxy cannot separate
  game state from team strength on the day.
- **Corners: don't adopt.**

## 5. Code and tests

| file | change |
|---|---|
| `src/shots/features/research.py` | new: families V / Z / K / W, `venue_ratings`, `closed_door_flags`, `gamestate_factors`, `gamestate_ratings`, `build_research`, CLI |
| `src/shots/features/closed_doors.csv` | new: 39 dated closed / partial periods for 11 countries, with notes |
| `src/shots/models/candidates.py` | families V, K, S, Z, W; OOS terms `vdisagree` / `wdisagree` / `kdisagree`; `closed_weight` / `partial_weight` (FlexNB), `exclude_closed` (CandidatePit); registry `cand_odds_surge_v`, `cand_fin_v`. Defaults unchanged. |
| `src/shots/config.py` | the two names added to the *allowed* candidate lists only; `ODDS_MODEL` / `NOODDS_MODEL` untouched |
| `src/shots/predict/scan.py` | `research_families()`: the research columns are built in `candidate_features` only when a selected candidate reads them (() in production); missing research inputs make an odds fixture fall back like any other missing input |
| `tests/test_features_research.py` | 14 tests: leakage (3 cut dates, truncation, fixtures), equivalence with family A, closed-door table and flags, planted game-state effect, opt-in defaults, crowd weights, registry, research build off in production / on when opted in |

Not done for `cand_odds_surge_v`: an `explain` factor group for V, and a dry run of `python -m shots update` with
`SHOTS_ODDS_MODEL=cand_odds_surge_v`. An adoption would also need a `model_history.json` milestone.

Reproduce: the scratch scripts `drive.py` (runner specs) and `evalrs.py` (scores), and the commands
`python -m shots.features.research --output <path>` and
`python -m shots.models.screen run ... --refit month` with `SHOTS_SCREEN_EXTRA` pointing at the merged candidate +
research build.
