# Over-shoot patterns: which matches beat our expected shots by 5+ (2026-10-26)

Author: modeler. Question from the user: "find common patterns in the fixtures that exceeded the expected shots by
5 or 6 shots, use them as potential features, highlight the patterns that would help exceed the shot count".

**Nothing in production changed.** The production switches (`NOODDS_MODEL = "fin_c"`, `MEDIAN_FROM_CURVE = True`) are
untouched. One new switch, `ODDS_MODEL`, was added with the production value (`"negbin_pit_comp"`); the candidate
behind it is opt-in only. Nothing committed or pushed.

## Plain-language summary

**Baseline.** In **21.4%** of matches (odds route; 21.2% on the no-odds route) the total ends **5 or more shots
above our published expected** (the curve median). 6+ above: 16.8%. 5+ below: 19.1%. The published curve says the
same thing (21.3% stated). So on average the model is right about how often this happens. The question is which
matches do it more often than the curve says.

**Patterns that help a match exceed our expected shots.** All are known before kick-off and were measured out of
sample (walk-forward, monthly refit, 2019/20 to 2025/26, 49,643 matches). "Stated" is what our own curve gave.

1. **End of season, especially with nothing at stake.** The biggest and most stable pattern, 7 out of 7 seasons.
   - Both teams have nothing left to play for: 5+ above expected in **27.4%** of matches. The curve said 21.5%.
     Baseline is 21.4%.
   - One team in its last 3 league matches: **26.4%** (curve 21.5%).
   - One "dead" team: 25.6%.
   - Late title race (within 3 points, 8 or fewer games left): 24.8%.
   - May overall: 25.2%.
   - The no-odds model (fin_c) already has a season-end flag and absorbs about two thirds of it: 23.4% vs 21.8%
     stated. The odds model has no season-end input and misses all of it.
2. **Head-to-head history above our number.** When the pair's past meetings averaged 4+ shots more than our
   expected: **22.8%** (curve 21.1%), 7 out of 7 seasons. When they averaged 4+ fewer: 20.0% (curve 21.9%).
3. **Both teams in a low-scoring run.** Bottom fifth of goals in both teams' last 5 matches: **22.0%** (curve 20.4%),
   7 out of 7 seasons. Few goals with ordinary shot volume means more shooting, not less. The market and our
   model read low goals as low shots. The top fifth (high-scoring runs) is the reverse: 21.2% vs 21.9% stated.
4. **Recent momentum: both teams have been beating the model.**
   - Top fifth of both teams' last-3 residuals: 23.2% (odds route; curve 22.0%).
   - 4+ over-shoots among both teams' last 5 matches: 23.8% (curve 22.3%).
   - Rising shot volume (last 5 vs last 10, top fifth): 22.5% (curve 21.4%).
   - The reverse holds and is strongest on the no-odds route: teams that recently under-shot the ratings go 5+
     over only 19.2% of the time (curve 21.1%; 0 of 7 seasons above the stated rate).
5. **Mismatch, when we have no odds.** Without odds, strong home favourites (market P(home) >= 0.70) go 5+ over in
   **25.4%** (curve 22.3%). The rating version, usable without odds (top decile of rating gap, home stronger):
   23.7% (curve 22.0%). With odds the odds model already captures this (22.6% vs 22.7%).
6. **Smaller signals.** Each is +1 to +2pp:
   - market expects more goals than our shot number implies (top fifth): 23.4% vs 21.8%;
   - a league currently running below last season's level: 22.1% vs 20.5%;
   - a promoted team involved: 22.4% vs 21.3%;
   - a high-shot referee (England and Scotland only): 22.5% vs 21.1%. A low-shot referee (England and Scotland
     only) gives 16.3% vs 21.0%, the strongest "under" signal (n = 603).

**Patterns that push the other way.**
- Winter: December and January give 19.5% to 19.7%.
- Our highest predictions (28+) over-shoot less than stated: 20.4% vs 23.3%.

**Not patterns** (within noise or sign flips between periods):
- rest days, short rest of the away team, congestion (4+ games in 14 days);
- weekday, kickoff time;
- early season (first 5 matches);
- newcomer or relegated teams;
- teams' recent shot volatility.

Derbies could not be tested: the data carry no city. A name heuristic finds only 19 matches.

**What became features, and what they do.**
- Patterns 2 to 6 became a new candidate family, "recent form" (`shots.features.recent_form`):
  - rating-residual momentum (last 3);
  - pace (last 3 vs last 10 match totals);
  - goal form (last 5);
  - a promoted flag.

  These sit alongside the existing head-to-head, season-context and rating families.
- **Added as ordinary model inputs, they barely help.**
  - Odds route: -0.002 to -0.003 RPS. That is at the resolution limit, and the confirmation period is not
    significant.
  - No-odds route: nothing (fin_c already has most of it).
- **Added as an out-of-sample correction of the predicted total, the odds route improves clearly.** The correction
  is fitted on the latest two training seasons, as predicted by a model that had not seen them. Candidate
  `cand_odds_oos`, monthly refit:
  - **RPS -0.0103 [-0.0134, -0.0072]**, pinball -0.0037 [-0.0053, -0.0020], log score -0.0039 [-0.0064, -0.0014];
  - confirmation seasons (not used for selection): -0.0078 [-0.0119, -0.0036];
  - the season-end, low-goals, momentum and head-to-head over-shoot excesses disappear.
- The same correction gives the no-odds route nothing (fin_c already absorbs these signals in-sample).

**Recommendation.** For the no-odds route, keep fin_c.

`cand_odds_oos` is the first odds-route candidate with a resolved gain. **Do not switch it on yet.** It is wired as an
opt-in candidate, default off (`SHOTS_ODDS_MODEL=cand_odds_oos` for dry runs). Before adoption it needs:
- an independent verification;
- a fix for its slightly over-confident top band of P(24+): the 80-90% calls hit 81.5% vs 82.6% for production;
- its own explanation factor;
- your recorded exception to protocol rule 4 (one route only, as for fin_c).

Details in section 6.

## 1. Method

- **Predictions.** Walk-forward, monthly refit, 2019/20 to 2025/26 (n = 49,643 per route). These are the published
  curves of the production routing, from the fin_c adoption decision run (`shots.models.screen`; each month fitted
  on rows dated strictly before it):
  - odds route: production NB re-estimated with nbmle (`cand_nbmle`, identical to `negbin_pit_comp` to 1e-4 RPS);
  - no-odds route: `fin_c`.
- **Data.** football-data rows only. `results_provisional/` and `results_manual/` were never read.
- **Definitions.**
  - expected = published integer median of the curve.
  - over-shoot = actual - expected >= 5 (also >= 6); under-shoot = actual - expected <= -5.
  - stated rate = P(total >= expected + 5) on the same curve.
  - **excess = realised - stated.** This is the residual signal: a segment can have more over-shoots only because
    its curve is wider or higher, and that is already captured.
- **Conditioning variables.** All pre-match:
  - features use only matches dated strictly before the match (same-day matches excluded) and pre-match odds;
  - the model-residual momentum uses out-of-sample residuals of earlier dates only;
  - quintile cut points come from the discovery seasons.
  - Code: `src/shots/evaluation/overshoot.py` (`study`).
- **Uncertainty.** Day-block bootstrap: all matches of a date are resampled together, 1,000 resamples.
  - "lift" = segment rate / base rate.
  - Stability is the number of seasons (with 20+ matches) in which the excess is positive.
  - Discovery = 2019/20 to 2022/23, confirmation = 2023/24 to 2025/26 (`docs/evaluation-protocol.md`).
- **What was already known** (reports 2026-10-15/16):
  - season end (diagnosis edge 1);
  - mismatch without odds (fixed by fin_c's ratings and shrinkage);
  - head-to-head (family E);
  - referee (family F, within noise);
  - congestion, corners, fouls, AH line (nothing);
  - team residual to date and previous season (nothing for the odds model);
  - goal form EWM (small).

  New here:
  - the over-shoot framing;
  - short-window momentum (last 3);
  - low-goal runs;
  - promoted teams;
  - league drift reversal;
  - the finding that these signals are out-of-sample signals (section 4).

## 2. Base rates

Overall, per season:
- odds route: 19.5% to 22.5% of matches 5+ over; the excess vs stated is -1.0 to +1.6pp;
- no-odds route: 19.8% to 22.6%.

Per league (2019/20 to 2025/26). The raw rate follows each league's spread; the excess column shows that no league
is mis-stated beyond noise:

| competition | n | over 5+ (odds route) | over 6+ | under 5+ | over 5+ (no-odds route) | excess vs stated (odds / no-odds) |
|---|---|---|---|---|---|---|
| Germany Bundesliga | 2,141 | 23.8% | 18.3% | 18.4% | 22.9% | +0.5 / +0.3 pp |
| England League One | 3,712 | 23.7% | 18.8% | 18.4% | 23.3% | +0.7 / +0.4 pp |
| Belgium Pro League | 2,079 | 23.1% | 17.9% | 18.1% | 22.6% | +0.7 / +0.6 pp |
| Italy Serie B | 2,659 | 22.9% | 18.7% | 20.5% | 22.9% | -0.7 / -0.9 pp |
| Italy Serie A | 2,660 | 22.2% | 17.5% | 18.2% | 21.6% | +1.4 / +1.0 pp |
| Netherlands Eredivisie | 2,068 | 22.1% | 17.2% | 21.0% | 21.8% | +0.6 / -0.3 pp |
| Turkey Super Lig | 2,445 | 22.0% | 16.9% | 20.9% | 21.7% | -0.1 / -0.5 pp |
| Portugal Primeira Liga | 2,142 | 21.8% | 17.6% | 18.6% | 21.6% | -0.8 / -0.9 pp |
| England Championship | 3,864 | 21.7% | 17.4% | 19.2% | 21.5% | -0.0 / -0.3 pp |
| Scotland Premiership | 1,547 | 21.7% | 17.1% | 20.4% | 20.9% | +0.1 / +0.1 pp |
| England Premier League | 2,660 | 21.4% | 17.1% | 21.2% | 21.6% | +0.2 / +0.3 pp |
| France Ligue 1 | 2,337 | 21.3% | 16.8% | 17.9% | 21.1% | -0.2 / -0.4 pp |
| Germany 2. Bundesliga | 2,142 | 21.2% | 16.2% | 22.5% | 20.7% | -0.5 / -0.7 pp |
| Spain La Liga | 2,660 | 21.2% | 16.6% | 17.9% | 20.6% | +1.2 / +0.6 pp |
| England League Two | 3,752 | 20.7% | 17.0% | 19.1% | 20.8% | -0.2 / -0.0 pp |
| Spain Segunda Division | 3,234 | 20.3% | 15.6% | 18.1% | 20.3% | +0.6 / +0.4 pp |
| France Ligue 2 | 2,407 | 19.5% | 15.4% | 19.2% | 19.3% | -0.5 / -0.4 pp |
| Scotland League One | 1,148 | 19.1% | 14.0% | 17.2% | 19.7% | +0.6 / +1.2 pp |
| Greece Super League | 1,668 | 18.9% | 14.1% | 16.1% | 18.8% | +0.4 / +0.8 pp |
| Scotland Championship | 1,172 | 18.4% | 14.1% | 18.8% | 17.3% | +0.2 / -0.4 pp |
| Scotland League Two | 1,146 | 16.6% | 12.6% | 18.1% | 17.7% | -1.7 / -1.2 pp |

## 3. Patterns

Columns:
- rate = share 5+ over;
- lift [95% CI] vs the route's base rate;
- stated = curve's P(5+ over);
- **excess [95% CI]** = rate - stated;
- 6+ and 5+-under rates with their excess;
- excess in discovery / confirmation;
- seasons with positive excess.

Full table (about 120 segments x 2 routes, per season): `reports/2026-10-26_overshoot_patterns.json`.

### 3a. Odds route (production `negbin_pit_comp`), base 21.4% / 16.8% / 19.1%

| segment | n | 5+ over | lift | stated | excess | 6+ over (excess) | 5+ under (excess) | disc / conf | seasons |
|---|---|---|---|---|---|---|---|---|---|
| dead rubber (both teams nothing to play for) | 1,341 | 27.4 | 1.28 [1.16, 1.38] | 21.5 | **+5.9 [+3.3, +8.2]** | 22.4 (+5.5) | 16.6 (-2.6) | +5.1 / +6.9 | 7/7 |
| last 3 matches (either team) | 3,418 | 26.4 | 1.23 [1.17, 1.30] | 21.5 | **+4.9 [+3.5, +6.4]** | 21.2 (+4.3) | 17.7 (-1.6) | +5.0 / +4.7 | 7/7 |
| one dead team | 2,575 | 25.6 | 1.20 [1.13, 1.27] | 21.6 | **+4.0 [+2.5, +5.6]** | 20.2 (+3.2) | 17.6 (-1.7) | +4.7 / +3.3 | 7/7 |
| late title race (gap <= 3, <= 8 left) | 1,333 | 24.8 | 1.16 [1.06, 1.27] | 21.5 | +3.3 [+1.1, +5.7] | 18.3 (+1.4) | 18.2 (-1.3) | +3.7 / +2.8 | 5/7 |
| month: May | 3,301 | 25.2 | 1.18 [1.11, 1.24] | 21.6 | +3.6 [+2.1, +5.1] | 20.4 (+3.5) | 17.5 (-1.9) | +2.8 / +4.5 | 6/7 |
| h2h mean total - our expected >= +4 | 4,804 | 22.8 | 1.07 [1.02, 1.11] | 21.1 | +1.8 [+0.6, +2.9] | 18.3 (+1.9) | 17.2 (-1.2) | +1.8 / +1.6 | 7/7 |
| h2h mean total - our expected +1 to +4 | 11,729 | 22.2 | 1.04 [1.01, 1.07] | 20.9 | +1.3 [+0.6, +2.1] | 17.0 (+0.8) | 17.2 (-1.2) | +1.3 / +1.4 | 7/7 |
| goals in both teams' last 5, bottom fifth | 7,570 | 22.0 | 1.03 [0.99, 1.07] | 20.4 | +1.6 [+0.7, +2.5] | 17.2 (+1.4) | 17.1 (-1.2) | +1.4 / +2.0 | 7/7 |
| market goals vs our expected, top fifth | 10,492 | 23.4 | 1.09 [1.06, 1.13] | 21.8 | +1.6 [+0.8, +2.4] | 18.6 (+1.4) | 17.8 (-1.0) | +1.3 / +2.1 | 6/7 |
| league level vs previous season, bottom fifth | 9,398 | 22.1 | 1.03 [1.00, 1.07] | 20.5 | +1.6 [+0.7, +2.5] | 17.3 (+1.5) | 17.4 (-0.9) | +1.8 / +1.3 | 4/6 |
| over-shoots (5+) in both teams' last 5: 4+ | 7,407 | 23.8 | 1.11 [1.07, 1.16] | 22.3 | +1.5 [+0.6, +2.4] | 19.3 (+1.6) | 18.9 (-1.0) | +2.3 / +0.6 | 4/7 |
| model-residual momentum (last 3), top fifth | 10,577 | 23.2 | 1.08 [1.05, 1.12] | 22.0 | +1.3 [+0.4, +2.1] | 18.5 (+1.1) | 18.5 (-1.2) | +1.7 / +0.7 | 5/7 |
| shot-volume trend (last 5 / last 10), top fifth | 9,945 | 22.5 | 1.05 [1.02, 1.09] | 21.4 | +1.2 [+0.3, +2.0] | 17.6 (+0.8) | 18.3 (-0.9) | +2.2 / -0.2 | 5/7 |
| promoted team involved | 6,392 | 22.4 | 1.05 [1.00, 1.09] | 21.3 | +1.1 [+0.1, +2.2] | 17.4 (+0.7) | 18.6 (-0.2) | +1.3 / +0.9 | 5/7 |
| referee shots tendency +0.3 to +1 (UK only) | 3,268 | 22.5 | 1.05 [0.98, 1.11] | 21.1 | +1.4 [-0.0, +2.9] | 18.3 (+1.9) | 18.7 (-0.4) | +1.6 / +1.1 | 5/7 |
| *referee shots tendency <= -1 (UK only)* | 603 | 16.3 | 0.76 [0.61, 0.92] | 21.0 | -4.8 [-7.8, -1.6] | 11.8 (-4.7) | 20.1 (+1.3) | -5.4 / -3.7 | 0/7 |
| *month: December / January* | 5,299 / 5,103 | 19.7 / 19.5 | 0.92 / 0.91 | 21.3 / 21.2 | -1.6 / -1.7 | | | | 1/7, 2/7 |
| *our expected 28+* | 2,966 | 20.4 | 0.95 [0.89, 1.01] | 23.3 | -2.8 [-4.3, -1.4] | 16.3 (-2.6) | 23.9 (+1.5) | -3.3 / -2.4 | 1/7 |
| strong home favourite (p_home >= .70) | 2,696 | 22.6 | 1.05 [0.98, 1.13] | 22.7 | -0.2 [-1.7, +1.5] | | | | 3/7 |
| early season (either team < 5 matches) | 6,687 | 21.7 | 1.01 | 21.2 | +0.4 [-0.8, +1.7] | | | | 4/7 |
| congested (>= 4 matches in 14 days) | 1,590 | 22.6 | 1.06 | 20.7 | +2.0 [-0.2, +4.1] | | | +1.2 / +5.0 | 6/7 |
| away team on short rest (<= 3 days) | 6,256 | 20.7 | 0.97 | 21.2 | -0.5 [-1.4, +0.5] | | | -1.3 / +1.1 | 4/7 |
| Friday / kickoff >= 19:30 | 4,272 / 11,579 | 22.6 / 22.0 | | 21.5 / 21.4 | +1.1 / +0.6 (n.s.) | | | | 4/7, 5/7 |

### 3b. No-odds route (production `fin_c`), base 21.2% / 16.6% / 19.4%

| segment | n | 5+ over | lift | stated | excess | disc / conf | seasons |
|---|---|---|---|---|---|---|---|
| strong home favourite (p_home >= .70; needs odds to identify) | 2,696 | 25.4 | 1.20 [1.13, 1.28] | 22.3 | **+3.1 [+1.5, +4.8]** | +2.7 / +3.6 | 6/7 |
| market total goals >= 3.3 (needs odds) | 2,695 | 25.5 | 1.20 [1.13, 1.28] | 22.9 | +2.6 [+0.9, +4.2] | +3.3 / +1.9 | 7/7 |
| market goals vs our expected, top fifth (needs odds) | 10,463 | 23.6 | 1.11 [1.08, 1.15] | 21.5 | +2.1 [+1.3, +2.9] | +1.4 / +2.8 | 6/7 |
| league level vs previous season, bottom fifth | 9,398 | 22.4 | 1.06 [1.02, 1.09] | 20.4 | +2.0 [+1.2, +2.9] | +2.2 / +1.8 | 4/6 |
| strong home vs weak away (rating gap top decile) | 5,096 | 23.7 | 1.12 [1.07, 1.17] | 22.0 | +1.8 [+0.6, +2.9] | +1.5 / +2.1 | 6/7 |
| one dead team | 2,575 | 23.5 | 1.11 [1.04, 1.18] | 21.8 | +1.7 [+0.2, +3.2] | +2.0 / +1.4 | 6/7 |
| last 3 matches (either team) | 3,418 | 23.4 | 1.10 [1.04, 1.16] | 21.8 | +1.6 [+0.2, +3.0] | +1.9 / +1.3 | 6/7 |
| over-shoots (5+) in both teams' last 5: 4+ | 7,157 | 23.5 | 1.11 [1.07, 1.15] | 22.1 | +1.4 [+0.3, +2.3] | +1.8 / +0.9 | 4/7 |
| rating-residual momentum (last 3), top fifth | 10,148 | 22.7 | 1.07 [1.04, 1.11] | 21.5 | +1.2 [+0.4, +2.0] | +0.8 / +1.7 | 5/7 |
| promoted team involved | 6,392 | 22.4 | 1.06 [1.01, 1.10] | 21.1 | +1.2 [+0.1, +2.3] | +1.0 / +1.5 | 6/7 |
| h2h mean total - our expected >= +4 | 4,541 | 22.2 | 1.05 [1.00, 1.10] | 20.9 | +1.3 [+0.1, +2.5] | +1.3 / +1.4 | 6/7 |
| goals in both teams' last 5, bottom fifth | 7,570 | 21.4 | 1.01 | 20.4 | +1.1 [+0.1, +2.0] | +0.9 / +1.4 | 6/7 |
| *rating-residual momentum (last 3), bottom fifth* | 10,120 | 19.2 | 0.91 [0.87, 0.94] | 21.1 | -1.9 [-2.6, -1.0] | -1.9 / -1.8 | 0/7 |
| *league level vs previous season, top fifth* | 9,895 | 20.8 | 0.98 | 22.0 | -1.1 [-1.9, -0.3] | -1.3 / -0.9 | 0/7 |
| *our expected < 20 / 28+* | 4,061 / 3,185 | 19.9 / 21.1 | | 17.4 / 23.3 | +2.5 / -2.2 | | 7/7, 1/7 |

Notes:
- The season-end excess is three times smaller on the no-odds route: fin_c's `G_last3` flag already works.
- The mismatch excess (+3.1pp) needs odds to identify. Its odds-free version (rating gap) is +1.8pp: fin_c's ratings
  and shrinkage absorb part of it, not all.
- fin_c's predicted level is still a little too extreme (low predictions over-shoot, high ones under-shoot, 7/7 and
  1/7 seasons). That is a shrinkage question for fin_c, not a new feature.

**Multivariate check** (linear fit of the residual on discovery, t-statistics; out-of-sample R² of the residual on
confirmation):
- odds route: head-to-head gap t = 8.9 / 5.3 (discovery / confirmation), goals -4.1 / -3.0, season end 4.5 / 4.3,
  pace 3.9 / 1.4, referee 2.1 / 2.9; out-of-sample R² +0.47%;
- no-odds route: momentum 4.8 / 3.9, head-to-head 5.6 / 2.9, goals -3.6 / -2.1, league drift -3.4 / -2.6,
  mismatch 2.3 / 2.3; out-of-sample R² +0.36%.

That is the same order as family A was on 2026-10-15 (+0.5%): small, and real.

## 4. From patterns to features

**New family H, "recent form"** (`src/shots/features/recent_form.py`, built through
`shots.features.candidates.build_candidates(families=(..., "form"))`, per team):

| column | definition |
|---|---|
| `form_res_l3` | mean of (match total - `adj_exp_total_shots` at the time) over the last 3 matches |
| `form_tot_l3`, `form_tot_l10` | mean match total shots over the last 3 and last 10 matches |
| `form_goals_l5` | mean goals for + against over the last 5 matches |
| `form_promo` | +1 promoted, -1 relegated, 0 same tier, NaN when the team has no previous season in the data |

- Windows hold only matches with a result dated strictly before the match date. Same-day matches never see each
  other. Upcoming fixtures neither enter a window nor take a slot in it.
- Tests (`tests/test_features_recent_form.py`, 8): own, same-day and later results changed at three cut dates;
  window values by hand; the residual definition; upcoming fixtures; promotion and relegation; NaN for newcomers.
- Existing families reused: head-to-head (E), season context (G: last 3 matches, dead rubber), ratings (A), and
  production's `league_level_drift`.

**Two ways to use them.**

1. **As ordinary covariates of the side NB models** (in-sample).
   - Odds route: `cand_odds_g` (G), `cand_odds_eg` (E+G), `cand_odds_h` (H), `cand_odds_egh` (E+G+H).
   - No-odds route: `cand_fin_h` (fin_c + H).
2. **As an out-of-sample correction of the total** (`FlexNB(oos_terms=OOS_TERMS)`, new in
   `src/shots/models/candidates.py`):
   - log mu' = log L + c + b (log mu - log L) + sum_j g_j z_j.
   - (c, b, g) are fitted by NB maximum likelihood on the latest training season(s) carved out of the training
     rows, as predicted by a model fitted before them. This is fin_c's shrinkage step with more terms.
   - The 8 terms z are:
     - disagreement log(rating expectation / our total);
     - head-to-head log(h2h total / our total);
     - momentum;
     - pace;
     - goal form;
     - league drift;
     - last-3 flag;
     - dead-rubber count.
   - Candidates:
     - `cand_odds_s` (1 season carved, no shrink);
     - **`cand_odds_oos`** (shrink + about 2 seasons carved, `oos_rows = 12000`);
     - `cand_fin_s` (fin_c + the terms).

**Why the second form.** Fitted in-sample, the momentum coefficient is about 0 (-0.002 per shot, `cand_fin_h`
coefficients on 2010 to 2023 data). Pace and goals get small weights, and the momentum excess of the walk-forward
residual does not move (-1.5pp, then -1.4pp). These signals describe how a model fitted on the past goes wrong on
the next period:
- league drift;
- teams whose shot rates move between refits;
- a model that disagrees with the ratings or the head-to-head.

They are therefore only visible, and only estimable, out of sample. The carved-season fit estimates exactly that.

## 5. Evaluation (`docs/evaluation-protocol.md`)

All comparisons are paired on identical rows. 95% CIs are day-block bootstrap with 1,000 resamples. Negative =
better than production.

### 5a. Screening, season refit, all 7 seasons (n = 49,643)

| candidate | variant | ΔRPS all | ΔRPS discovery | ΔRPS confirmation | Δpinball all | guard (all) |
|---|---|---|---|---|---|---|
| `cand_odds_g` (season context) | odds | -0.0013 [-0.0027, +0.0001] | -0.0016 [-0.0036, +0.0002] | -0.0008 [-0.0029, +0.0012] | +0.0001 | none |
| `cand_odds_eg` (+ head to head) | odds | -0.0016 [-0.0033, -0.0001] | -0.0022 [-0.0045, +0.0000] | -0.0008 [-0.0033, +0.0014] | -0.0003 | Scotland Championship worse |
| `cand_odds_h` (recent form) | odds | -0.0024 [-0.0041, -0.0009] | -0.0028 [-0.0051, -0.0008] | -0.0019 [-0.0041, +0.0003] | -0.0013 [-0.0025, -0.0003] | Scotland League One worse |
| `cand_odds_egh` | odds | -0.0028 [-0.0050, -0.0007] | -0.0035 [-0.0066, -0.0005] | -0.0020 [-0.0050, +0.0009] | -0.0005 | none |
| `cand_odds_s` (out-of-sample terms, 1 season) | odds | -0.0066 [-0.0096, -0.0034] | -0.0066 [-0.0107, -0.0021] | -0.0066 [-0.0111, -0.0022] | -0.0023 [-0.0039, -0.0006] | none |
| + shrink, 1 season (spec `odds_s2`) | odds | | -0.0074 [-0.0118, -0.0030] | | -0.0038 (disc.) | none |
| 2 seasons carved (spec `odds_s3`) | odds | | -0.0127 [-0.0168, -0.0090] | | -0.0069 (disc.) | none |
| **`cand_odds_oos`** (shrink + 2 seasons) | odds | **-0.0105 [-0.0135, -0.0074]** | **-0.0133 [-0.0176, -0.0095]** | **-0.0068 [-0.0110, -0.0026]** | -0.0040 [-0.0057, -0.0024] | none |
| `cand_fin_h` vs fin_c | no-odds | -0.0004 [-0.0016, +0.0008] | +0.0003 | -0.0012 [-0.0029, +0.0004] | +0.0003 | none |
| `cand_fin_s` vs fin_c | no-odds | +0.0008 [-0.0014, +0.0029] | +0.0025 [-0.0003, +0.0057] | -0.0015 | +0.0007 | Turkey worse |
| fin_c + terms, 2 seasons carved (spec `fin_s2`) | no-odds | | -0.0025 [-0.0052, +0.0004] | | -0.0011 (disc.) | Turkey worse |

**Selection, in the order the looks happened.**
1. The in-sample families and `cand_odds_s` / `cand_fin_s` were defined from the discovery-period residual study.
   They were scored once on all seven seasons.
2. After seeing those, the three carve and shrink variants were scored on discovery only. `cand_odds_oos` was
   picked on the discovery RPS (-0.0133, vs -0.0127 without shrink).
3. Confirmation and the monthly run were computed after the choice.
4. No-odds: `cand_fin_s` and the 2-season variant are not significant on discovery and flag Turkey, so no no-odds
   finalist.

### 5b. Decision run: monthly refit, `cand_odds_oos` vs production, odds route

| slice | n | RPS cand / prod | ΔRPS | Δlog score | Δpinball | ΔMAE |
|---|---|---|---|---|---|---|
| **all 2019/20-2025/26** | 49,643 | 3.0721 / 3.0824 | **-0.0103 [-0.0134, -0.0072]** | -0.0039 [-0.0064, -0.0014] | -0.0037 [-0.0053, -0.0020] | -0.014 |
| discovery 2019/20-2022/23 | 28,179 | 3.0203 / 3.0325 | -0.0122 [-0.0167, -0.0081] | -0.0047 [-0.0080, -0.0012] | -0.0060 [-0.0086, -0.0037] | -0.018 |
| confirmation 2023/24-2025/26 | 21,464 | 3.1402 / 3.1479 | -0.0078 [-0.0119, -0.0036] | -0.0029 [-0.0068, +0.0011] | -0.0006 [-0.0029, +0.0017] | -0.009 |
| 2025/26 | 7,094 | 3.1190 / 3.1237 | -0.0047 [-0.0126, +0.0028] | -0.0019 | +0.0002 [-0.0035, +0.0043] | -0.004 |
| 2026/27 to 21 Oct (informational) | 1,792 | 3.3709 / 3.3808 | -0.0099 [-0.0276, +0.0071] | +0.0040 | -0.0067 [-0.0191, +0.0037] | -0.008 |

- **Ship rule (CLAUDE.md rule 3), pooled monthly.**
  - Coverage 72.9 / 82.3 / 91.4 (production 73.4 / 82.7 / 91.5).
  - Mid-coverage 69.6 / 79.5 / 89.5, within ±2pp.
  - Pinball 1.4027, below `team_avg` 1.4279, and below `team_avg` in all 21 competitions.
  - **One cell below q:** Scotland Championship at 90%, 89.8% (n = 1,172; production 89.6% in the same cell, so
    production has that cell below q too). Strictly, the rule is not met in that cell by either model. The
    shortfall is 0.2pp against a standard error of about 0.9pp.
- **Competition guard, pooled:** no competition worse, on RPS or on pinball.
  - RPS is significantly better in 8 of 21 competitions (England League One and League Two, Ligue 2, Serie A,
    Scottish Premiership, La Liga, Turkey).
  - Point estimates are worse only in 2. Bundesliga (+0.009, n.s.) and Portugal (+0.003, n.s.).
  - 2025/26 alone: Scotland Championship flagged (+0.052 RPS, and pinball; n = 180); Segunda pinball. 2026/27: La
    Liga flagged (about 100 matches).
- **No new leak.** The gain (0.010) is below the protocol's "too good" threshold (0.05) and inside the diagnosis
  ceiling (odds model, theoretical about 0.02). An independent real-data check passed:
  - every input of the correction terms was rebuilt with all results dated on or after a cut date removed (cuts
    2023-07-01, 2024-01-15, 2025-03-08);
  - all rows dated on or before the cut are identical: 0 differing cells, and `league_level_drift` max difference
    0.0;
  - a fresh full build equals the stored candidate file on all 94,889 shared rows (max absolute difference 0.0).

**Did it absorb the patterns?** Over-shoot excess (realised / stated, pp) before and after, monthly, odds route:

| segment | n | production | `cand_odds_oos` |
|---|---|---|---|
| last 3 matches (either team) | 3,418 | 26.4 / 21.5 (+4.9) | 22.2 / 21.8 (+0.4 [-1.0, +1.6]) |
| dead rubber | 1,341 | 27.4 / 21.5 (+5.9) | 22.2 / 21.9 (+0.3 [-2.0, +2.4]) |
| one dead team | 2,575 | 25.6 / 21.6 (+4.0) | 22.1 / 21.8 (+0.3 [-1.4, +1.7]) |
| h2h total - our expected >= +4 | 4,804 | 22.8 / 21.1 (+1.8) | 21.4 / 20.8 (+0.6 [-0.7, +1.8]) |
| goals in both teams' last 5, bottom fifth | 7,570 | 22.0 / 20.4 (+1.6) | 20.3 / 20.5 (-0.2 [-1.1, +0.6]) |
| shot-volume trend, top fifth | 9,945 | 22.5 / 21.4 (+1.2) | 21.5 / 21.2 (+0.2 [-0.5, +1.1]) |
| promoted team involved | 6,392 | 22.4 / 21.3 (+1.1) | 21.9 / 21.1 (+0.8 [-0.4, +1.8]) |
| league level vs previous season, bottom fifth | 9,398 | 22.1 / 20.5 (+1.6) | 21.8 / 20.4 (+1.3 [+0.5, +2.2]) |
| league level vs previous season, top fifth | 9,895 | 22.0 / 21.9 (+0.0) | 20.9 / 21.9 (-1.0 [-1.7, -0.2]) |
| our expected < 20 | 3,907 | 19.0 / 17.5 (+1.5) | 19.4 / 17.3 (+2.1 [+0.9, +3.3]) |
| our expected 28+ | 2,966 | 20.4 / 23.3 (-2.8) | 20.4 / 23.1 (-2.7 [-4.1, -1.5]) |
| all | 49,643 | 21.4 / 21.3 (+0.2) | 20.9 / 21.1 (-0.2) |

- Season end, low-goal runs, head-to-head and momentum are absorbed.
- League drift is overcorrected at the top (-1.0pp).
- The extremes of the predicted level are not fixed (that needs a different shrinkage, not a feature).

**High-threshold calls.** P(total >= 24), the scan default:

| | production | `cand_odds_oos` |
|---|---|---|
| overall stated / realised | 50.4% / 51.0% | 50.9% / 51.0% |
| band 0.7-0.8: stated / realised (n) | 74.2 / 73.3 (5,156) | 74.2 / 72.7 (5,647) |
| band 0.8-0.9: stated / realised (n) | 83.4 / 82.6 (1,373) | 83.5 / 81.5 (1,701) |
| 70%+ calls: n, stated, hit | 6,591, 76.3%, 75.4% | **7,428 (+13%)**, 76.5%, 74.9% |
| 70%+ calls, confirmation: n, stated, hit | 3,716, 76.3%, 76.0% | 3,993, 76.5%, 75.8% |
| 70%+ calls, 2025/26: n, stated, hit | 1,123, 75.4%, 77.9% | 1,208, 75.6%, 77.4% |

- The candidate makes 13% more confident calls.
- Its hit rate is 0.2 to 0.8pp lower, and its top band is about 1pp more over-stated than production's.
- Production's own calls are already about 1pp over-stated.
- **It does not improve the high-threshold calls.** That is the main reservation for this product (picks are 70%+
  calls).

## 6. Recommendation

- **No-odds route (most of the board): keep fin_c, do not adopt anything here.**
  - fin_c already absorbs most of these patterns: season end (via `G_last3`), head-to-head, part of the mismatch.
  - Neither the new family (`cand_fin_h`, -0.0004 n.s.) nor the out-of-sample terms (`cand_fin_s`, +0.0008 n.s.;
    2-season variant -0.0025 n.s. with a Turkey guard flag) resolve a gain.
  - Open item for fin_c: its predicted level is still slightly too extreme (section 3b). Look at the shrinkage
    window, not at features.
- **Odds route: `cand_odds_oos` is promising but needs more work. Do not adopt yet.**
  - For it:
    - the first resolved odds-route gain since the evaluator's ceiling estimate (-0.0103 RPS monthly, CI below 0,
      also in confirmation);
    - pinball and log score better;
    - no competition worse pooled;
    - leakage check clean;
    - it removes the season-end blind spot of the odds model (+4.9pp, then +0.4pp).
  - Against:
    - the 70%+ P(24+) calls are not better: more calls, hit rate about 0.5pp lower, top band about 1pp more
      over-stated;
    - 2025/26 alone is not significant, and Scotland Championship is worse in 2025/26 alone;
    - the strict per-competition ship rule fails in one cell (Scotland Championship 90%: 89.8%), as it does for
      production in this run (89.6%);
    - it applies to one route only (protocol rule 4 exception, as fin_c);
    - its explanation would report the correction inside the "Pull toward league level" factor (needs its own
      factor id first).
  - Next steps if you want to pursue it:
    1. An independent verification (as for fin_c).
    2. Recalibrate the 0.7+ band of P(24+): for example the per-competition PIT window after the correction, or
       restrict the correction to the mean and keep production's curve shape.
    3. A dedicated explain factor.
    4. A dry run: `SHOTS_ODDS_MODEL=cand_odds_oos python -m shots update --days 30` against a scratch DB and board.
- **Product-side, no model change needed:** a "season end / nothing at stake" badge on fixtures whose expected total
  comes from the odds model would be honest today (+5pp over-shoot excess, 7 of 7 seasons).

## 7. Opt-in wiring (default off)

- `src/shots/config.py`:
  - new switch `ODDS_MODEL = "negbin_pit_comp"` (production; `CANDIDATE_ODDS_MODELS = ("cand_odds_oos",)`);
  - env `SHOTS_ODDS_MODEL`;
  - `odds_model()`, `candidate_odds()`.
  - Existing switches are untouched.
- `src/shots/predict/scan.py`:
  - one candidate build serves both candidate routes (`candidate_families()`); with only fin_c on, it is exactly
    the previous build (families A/E/G);
  - `_candidate_odds_route` scores odds fixtures by the candidate when the build has them, and by the production
    odds model otherwise (`missing_odds_candidate_inputs`); it never median-fills a missing row;
  - `latest_candidate_odds_model` saves `models_store/cand_odds_oos-odds_<date>` with feature list, training window
    and metadata, and retrains under the same 30-day rule;
  - `Prepared.odds_info` is counted, written to `board.meta.json["odds_route"]`, and fails the step after writing
    above `FALLBACK_MAX_SHARE`.
  - The no-odds and cross-border routes are unchanged.
- Tests: `tests/test_odds_candidate_route.py` (7):
  - default is production;
  - switch off means no candidate build and production labels;
  - complete build routes to the candidate;
  - a missing row falls back and fails loudly;
  - a failed build falls back;
  - one build for both routes;
  - missing-input detection.

## Files

- New:
  - `src/shots/evaluation/overshoot.py` (`study`, `compare`);
  - `src/shots/features/recent_form.py`;
  - `tests/test_features_recent_form.py`;
  - `tests/test_evaluation_overshoot.py`;
  - `tests/test_models_oos.py`;
  - `tests/test_odds_candidate_route.py`;
  - `reports/2026-10-26_overshoot_patterns.json` (all segments, both routes, per season and per competition).
- Changed:
  - `src/shots/features/candidates.py`: family `"form"` (docstring H), `ODDS_CANDIDATE_FAMILIES`;
  - `src/shots/models/candidates.py`: family H, `OOS_TERMS` / `oos_inputs` / `oos_raw`, `FlexNB(oos_terms, oos_rows)`,
    candidates `cand_odds_{g,eg,h,egh,s,oos}`, `cand_fin_h`, `cand_fin_s`, `missing_odds_candidate_inputs`;
  - `src/shots/config.py` (new switch, production value);
  - `src/shots/predict/scan.py`, `src/shots/predict/board.py` (opt-in odds route);
  - `tests/test_models.py`: synthetic family-H columns on their own random stream, so earlier columns and golden
    hashes are unchanged.
- Reproduce:

      python -m shots.models.screen run --models "fin_c;cand_nbmle" --variants noodds --refit month ...   # production curves
      python -m shots.evaluation.overshoot study --preds <dir with the monthly production curves>
      SHOTS_SCREEN_EXTRA=<features_candidates incl. form> python -m shots.models.screen run --models "cand_odds_oos" \
          --variants odds --seasons 2019/2020,...,2025/2026 --refit month --cache <dir>
      python -m shots.evaluation.overshoot compare --cache <dir> --pairs cand_odds_oos:cand_nbmle:odds --refit month --segments --out <json>

  The candidate file with the form family is `python -m shots.features.candidates`, which now includes `"form"` by
  default. `data/processed/features_candidates.parquet` was not rebuilt here; the evaluation used a scratch copy
  (stored file + form columns, A identical to 0.0).
