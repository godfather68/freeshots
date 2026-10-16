# Diagnosis of the production model: where accuracy is lost, the ceiling, and edges (2026-10-15)

Author: evaluator. Model under study: `negbin_pit_comp`, odds variant and no-odds variant (all `mkt_*`
blanked in training and scoring, the model that scores most of the board). Numbers:
`reports/2026-10-15_diagnosis.json`. Reproduce:

    python -m shots.evaluation.curves            # ~15 min, 6 processes: walk-forward curves (odds, noodds, clim)
    python -m shots.evaluation.diagnosis         # ~25 min: every table below -> JSON

No production code, model or published output was changed. New files: `src/shots/evaluation/{scoring,curves,diagnosis}.py`,
`docs/evaluation-protocol.md`, `tests/test_evaluation_scoring.py` (8 tests), `tests/test_evaluation_diagnosis.py`
(3 leakage-guard tests). Data: `data/processed/curves_negbin_pit_comp_season.parquet` (git-ignored).

## Verdict (short)

1. **The odds model is close to its pre-match ceiling.** Within a competition-season it explains only 5% of the
   variance of total shots, but the rest is mostly in-match noise. Its own stated variance (30.0) equals its realised
   MSE (30.2). Hindsight oracles leave little: perfect knowledge of each team's within-season residual adds nothing,
   and the realistic remaining gain is about **0.004 to 0.01 RPS (0.1% to 0.3%)**.
2. **The no-odds model is where the accuracy is.** It is 0.023 RPS behind the odds model, and it is **systematically
   miscalibrated by its own predicted level** (calibration slope of the total within a competition-season is
   0.70 to 0.72). This is confirmed out of sample. A pre-match strength rating plus shrinkage of the predicted level
   recovers **0.015 to 0.020 RPS** on the confirmation seasons. That is about two thirds of the gap to the odds model.
   It also repairs the 80% bound in the top decile of predicted totals (randomized coverage 76.4% to 79.3%).
3. **One robust edge holds for both variants: end of season.** In each team's last 3 matches, totals are
   +0.5 to +1.2 shots above prediction, with the same sign in discovery and confirmation and in 6 of 7 seasons.
   Unders there are overstated by up to 5pp.
4. **Bugs and concerns:** see the "Bugs and product concerns" section. The issues are England National League in
   2026/27 (no history since 2015/16, 80% bound covers 68%), and a published median that disagrees with the
   published curve on 29% of fixtures.

## 1. Protocol and scoring API

- **Curves.** `curves.py` re-runs the season-refit walk-forward of the production class and keeps the full
  published curve P(total <= k), k = 0..80.
  - Each test season is fitted on rows dated strictly before its first match.
  - Targets are hidden from `predict`.
  - It reproduces the stored backtest bit for bit: on 2025/26, lb_70/80/90 are identical on 7,094/7,094 rows and
    |F(y) - pit_hi| <= 6e-17.
  - Rows: 2019/20 to 2025/26 complete (n = 49,643) plus 2026/27 to date (n = 1,583).
- **Discovery / confirmation.** Every pattern below was found or fitted on 2019/20 to 2022/23 (discovery). It is
  confirmed on 2023/24 to 2025/26 (confirmation), and 2026/27 is shown separately.
- **Scoring API.** `shots.evaluation.scoring`:
  - `score(df)` returns RPS (discrete CRPS), log score, pinball 70/80/90, coverage, mid-coverage, stated,
    randomized-PIT coverage, upper-bound coverage (under side), reliability ECE over lines 12..40, upper- and
    lower-tail gaps, PIT histogram, and median MAE/bias.
  - `score_by(df, by)` gives one row per group.
  - `compare(a, b)` gives paired differences with a day-block bootstrap.
  - `competition_guard(a, b)` checks "no competition worse beyond noise".
- **Protocol.** `docs/evaluation-protocol.md` sets the fixed walk-forward (season screening, monthly refit for
  finalists), the decision rule, and both variants.

### Headline (season refit)

| | n | RPS | log score | pinball | MAE | cov 70/80/90 | mid 80 | rand 80 | under cov 80 | skill vs clim |
|---|---|---|---|---|---|---|---|---|---|---|
| odds, 2019/20 to 2025/26 | 49,643 | **3.0817** | 3.1338 | 1.4061 | 4.35 | 73.3 / 82.6 / 91.5 | 79.7 | 79.9 | 82.1 | 6.6% |
| no-odds | 49,643 | 3.1049 | 3.1414 | 1.4144 | 4.39 | 73.2 / 82.5 / 91.5 | 79.7 | 79.8 | 82.3 | 5.9% |
| climatology (comp., 3 seasons) | 49,643 | 3.2988 | 3.2159 | 1.4956 | 4.64 | 74.7 / 84.0 / 92.4 | 81.6 | 81.7 | 81.7 | 0 |
| odds, 2026/27 | 1,583 | 3.3852 | 3.2265 | 1.5462 | 4.83 | 73.5 / 82.7 / 92.0 | 79.8 | 80.0 | **79.2** | 5.3% |
| no-odds, 2026/27 | 1,583 | 3.4531 | 3.2417 | 1.5814 | 4.92 | 74.2 / 83.0 / 91.7 | 80.7 | 80.9 | **78.1** | 3.4% |

- Odds minus no-odds RPS: -0.0231 [-0.0270, -0.0195]. Odds minus climatology: -0.217 [-0.229, -0.206].
- Pinball 1.4061 matches the 2026-10-08 season-refit report exactly.
- Whole-curve reliability is excellent on complete seasons: ECE 0.09pp (odds) / 0.18pp (no-odds), and upper-tail
  gaps are within 0.1pp.
- 2026/27 reliability is worse (ECE 1.5 / 2.1pp, upper-tail gap +1.4 / +1.9pp). This is the known early-season
  upward drift: unders are overstated in August and September 2026.

## 2. Where RPS and calibration are lost

Residual = actual - mean of the published curve. pit = mean mid-PIT - 0.5 (positive: the model is too low).
r80 = randomized 80% coverage - 80 (pp). All tables are for the odds model unless marked otherwise.

**By competition** (complete seasons; skill vs climatology, residual in discovery / confirmation):

| competition | skill | resid disc / conf | r80 (pp) | sd(z) | worst season skill |
|---|---|---|---|---|---|
| Germany 2. Bundesliga | **1.5%** | -0.14 / -0.21 | -1.5 | 0.98 | **-2.3% (2024/25)** |
| England Premier League | 4.4% | +0.02 / -0.12 | -0.6 | 1.03 | 1.1% |
| Germany Bundesliga | 4.4% | -0.09 / -0.02 | -0.6 | 1.04 | 2.0% |
| Portugal | 4.6% | -0.12 / -0.10 | -0.5 | 0.99 | 2.1% |
| Scotland Championship | 6.3% | -0.13 / -0.17 | **-1.5** | **1.06** | 1.2% |
| Scotland League One | 5.7% | -0.12 / -0.02 | **-1.1** | **1.05** | 1.4% |
| Turkey | 5.7% | -0.29 / -0.06 | -1.0 | 0.99 | 2.0% |
| Italy Serie A | 11.1% | +0.26 / +0.05 | +0.8 | **1.05** | 2.4% |
| Greece | 17.5% | +0.48 / -0.29 | +1.0 | 1.00 | 3.0% |

- Per-competition level bias is small and mostly flips sign between periods, except 2. Bundesliga, Portugal and
  Scotland Championship (negative in both).
- The **Scottish Championship / League One failing cell of the ship rule is a dispersion problem, not a mean
  problem**: sd of standardized residuals is 1.05 to 1.06, so the predictive spread is about 5% too narrow.
- Widening per competition (fitted on discovery) improves the log score significantly: -0.0026 [-0.0040, -0.0008].
  It costs a negligible +0.0006 RPS.
- Fix it in the NB, with a per-competition or partially pooled alpha, for coverage reasons. Do not expect any RPS
  gain from it.

**By season phase, month and schedule:**

| segment | n | resid disc / conf | r80 | under cov 80 |
|---|---|---|---|---|
| team's last match of season | 1,336 | **+0.81 / +1.17** | +2.3 | **77.2** |
| 2-3 matches left | 2,723 | **+0.63 / +0.45** | +1.6 | 78.4 |
| 4+ left | 45,584 | -0.06 / -0.08 | -0.4 | 82.5 |
| matchdays 1-5 | 6,687 | +0.02 / -0.21 | -0.4 | 81.6 |
| Dec-Feb | 16,306 | -0.20 / -0.18 | -0.1 to -1.8 | 83 |
| Tue-Thu | 7,025 | -0.15 / -0.07 | -0.9 | 82.7 |
| newcomer team (no data before this season, 7.4%) | 3,683 | -0.15 | -0.6 | |

- The May effect is the season-end effect. Monthly residual in May is +0.40 to +1.39 in every season except COVID
  2019/20 (-1.43).
- Winter is -0.2 shots in 9 of 11 countries (not Italy or the Netherlands). It is small and fits in-sample only
  weakly (see section 3).
- Early season (matchdays 1-5) has no stable sign. It follows the league drift: positive in 2026/27, negative in
  2024/25.

**By match type (odds model):** favourite strength, closeness and market over-2.5 show no confirmed bias. The market
inputs absorb them. The top decile of predicted totals is 0.28 shots too high (calibration slope within
competition-season 0.90 in discovery, 0.96 in confirmation). This is small.

**By match type (no-odds model): systematic and confirmed.**

| no-odds segment | resid disc / conf | r80 disc / conf | z disc / conf |
|---|---|---|---|
| predicted total 28+ | **-1.13 / -0.96** | **-3.8 / -4.0pp** | -5.6 / -5.5 |
| predicted 26-28 | -0.62 / -0.30 | -2.4 / -1.6pp | -5.2 / -2.6 |
| predicted < 20 | +0.48 / +0.36 | +2.5 / +1.0pp | +4.4 / +2.1 |
| favourite strength 0.7-0.8 | +0.64 / +0.54 | +3.4 / +2.8pp | +3.3 / +2.4 |
| favourite > 0.8 | +1.05 / +1.14 | +6.7 / +4.5pp | +3.1 / +2.7 |
| lopsided (abs(pH - pA) > .45) | +0.46 / +0.54 | +2.5 / +2.2pp | +4.2 / +4.1 |
| market over-2.5 > .65 | +0.83 / +0.48 | +4.0 / +2.0pp | +3.4 / +2.0 |

- Deciles of the no-odds predicted total:
  - The top decile predicts 28.8 against an actual 27.8.
  - Its **randomized 80% coverage is 76.2%** and its under coverage is 86.3%.
  - The bottom decile under-predicts by 0.43.
- The total's within-season calibration slope is **0.70 (discovery) / 0.72 (confirmation)**, while each side's
  slope is 1.00 to 1.04.
- In words: the no-odds model separates "both teams shoot a lot" from "both shoot little" too sharply. It misses
  that lopsided matches produce more total shots, which is information the market carries and shot form does not.
- Top-5 teams of the previous season are under-predicted on their own shots by +0.75 per match in the no-odds model,
  against +0.0 in the odds model.

**Tails.**

- Extreme totals are as rare as stated: 40 or more occurs in 1.02% of matches (stated 1.05%), 11 or fewer in 1.41%
  (stated 1.38%).
- The model ranks them reasonably: AUC is 0.77 for 40+ and 0.83 for <=11.
- In the lowest-probability quintile, both tails happen about 3 times as often as stated (0.11% vs 0.03% for 40+,
  0.18% vs 0.06% for <=11). The absolute size is negligible.
- In the top quintile, 40+ is stated 3.2% and realised 2.7%, the same high-level over-prediction as above.
- Red cards are no more frequent in extreme matches (17.8% vs 17.5% overall). Low totals come with fewer goals and
  closer scorelines. Extremes are not predictable beyond what the curve already states.

## 3. Ceiling: how much is left

**Variance budget** (odds model, complete seasons):

| quantity | value (shots²) |
|---|---|
| Var(total) | 37.3 |
| Var within competition-season | 31.9 |
| MSE of the curve mean | 30.2 (R² 18.9% total, **5.2% within comp-season**) |
| model's own mean predictive variance | 30.0 (consistent with MSE: dispersion is right on average) |
| Poisson floor (mean of mu) | 23.9 |
| side MSE home / away, residual corr | 20.0 / 16.8, **-0.18** (game state: one side's shots replace the other's) |

**What hindsight could still recover.** Each oracle is the in-sample gain minus the gain from the same number of
random groups (permutation null):

| oracle (perfect knowledge of ...) | net RPS gain |
|---|---|
| each team's attack/defence residual, from the other half of the same season (split-half, k = 0..20) | **none**: side MSE gets worse by 2% to 11% |
| team residual season to next season | corr 0.15 (attack) / 0.17 (defence); noise sd 0.70 vs total sd 0.74, so the signal sd is about 0.25 shots per team |
| league level per competition-season | -0.0065 |
| calendar month (all leagues) | -0.0069 |
| league x month | -0.023 |
| match date (common same-day shocks) | -0.016 |
| decile of predicted total | -0.0006 (odds) / **-0.0077 (no-odds)** |

**Irreducible part.**

- Post-match information explains little of the residual:
  - final and half-time score cells explain 5.4% of the residual variance out of sample;
  - adding red cards and the favourite-lost flag explains 4.8%;
  - red cards alone explain 0.04%.
- Red cards move shots between sides (a team with 10 men: -2.0 own, +1.9 for the opponent; two reds: -3.3 / +3.5),
  not the total. Red cards are irreducible for team-shot products and irrelevant for the total.
- The goal-to-residual association (0 goals -1.9, 6+ goals +2.7) is mostly shots causing goals, so 5% is an upper
  bound on how much the "scoreline path" explains.
- Ceiling estimate: of the 30.2 shots² MSE, about 23.9 is Poisson noise and about 5.5 to 6 is extra in-match
  variance (game state, tactics on the day, finishing). The remaining **predictable signal is about 0.3 to 0.7
  shots²**: league x month level, season end, referee, and for no-odds, team strength.
- Using RPS ≈ sqrt(MSE / pi), which holds within 0.7% here, an MSE cut of 1% gives about -0.5% RPS (-0.015).
- **Odds model:**
  - realistic gain: -0.004 to -0.01 RPS;
  - theoretical gain if every residual mean signal were captured: about -0.02;
  - RPS of a perfect mean with Poisson-only noise: 2.75. That is an absolute floor, unreachable.
- **No-odds model:** realistic gain -0.015 to -0.02 (closing most of the 0.023 gap to the odds model).

## 4. Ranked improvement opportunities

Each candidate is a linear correction of the curve mean, fitted on discovery residuals and applied as a location
shift on confirmation. Δ is the paired RPS change against an intercept-only shift, with a day-block bootstrap 95% CI
(negative is better).

A shift on top of the existing model is a **lower bound** on what the same signal gives inside the GLM. The
feature-engineer's families map as follows:

- A `adj_*` (opponent-adjusted EKF ratings) maps to my Elo / strength signal;
- G `ctx_*` (games left, dead rubber) maps to season end;
- F `ref_*` maps to referee;
- B `style_*` maps to corners / fouls;
- C `mktx_*` maps to the AH line.

I tested my own strictly-prior versions; tests in `tests/test_evaluation_diagnosis.py` show that changing same-day
and later results leaves them unchanged.

| rank | opportunity | variant | Δ RPS confirmation [CI] | Δ 2026/27 | evidence / note |
|---|---|---|---|---|---|
| 1 | **Team strength + level shrinkage for the no-odds model** (Elo or `adj_*`, plus shrink of (mean - league level), coefficient -0.29) | no-odds | **-0.0152 [-0.0210, -0.0097]** | -0.063 [-0.083, -0.045] | level shrink alone -0.0103; mismatch -0.0119; Elo alone -0.0028. Fixes top-decile coverage 76.4 to 79.3%. Combined with 2-4: **-0.0203 [-0.0265, -0.0141]** |
| 2 | **Season end** (team's last match / 2-3 left; coefficients +0.86 / +0.68 shots) | both | -0.0021 [-0.0039, -0.0003] (odds); -0.0043 [-0.0069, -0.0015] (no-odds) | n/a (no season-end rows yet) | confirmed edge, section 5; needs the fixture list (known pre-season); `ctx_*` games-left from the feature-engineer is the production form |
| 3 | **Referee tendency** (shrunk past excess shots, K = 20; coefficient 0.29) | both, UK leagues only (39% of rows) | -0.0008 [-0.0015, -0.0001] overall; -0.0020 [-0.0042, +0.0003] on referee rows | +0.0003 | small and real; fouls tendency adds nothing |
| 4 | Goal form (EWM goals for/against) | both | -0.0013 [-0.0028, +0.0001] (odds); -0.0025 [-0.0043, -0.0009] (no-odds) | -0.003 | partly a strength proxy (overlaps with 1) |
| 5 | Per-competition dispersion (NB alpha by competition, or pooled) | both | RPS +0.0006 (worse), **log score -0.0026 [-0.004, -0.001]** | | for the ship rule (Scottish cells, Serie A), not for RPS |
| 6 | Winter / calendar month | both | -0.0008 [-0.0022, +0.0005] (odds); month dummies on no-odds -0.0030 [-0.0057, -0.0003] | -0.004 / -0.007 | the league x month oracle is -0.023 but mostly not forecastable |
| - | AH line | odds | -0.0002 [-0.0008, +0.0004] | | redundant with 1X2 + O/U 2.5 |
| - | Corners (rolling for/against) | both | **+0.0008 [+0.0002, +0.0013]** (worse) | | no incremental signal; do not add |
| - | Fouls / cards rolling | both | -0.0002 [-0.0008, +0.0006] | | none |
| - | Team residual to date (this season) | odds | +0.0004 [-0.0013, +0.0021] | | none for odds (the split-half oracle agrees); -0.0030 for no-odds = strength again |
| - | Team residual previous season | both | -0.0004 [-0.0013, +0.0006] | | persistence 0.15 is real but too small to use |
| - | Division change (first 10 matches) | both | -0.0001 | -0.0035 | tiny sample |
| - | European congestion (top-5 of last season x European months) | both | DiD -0.035 shots (odds) | | none |

Odds model, all useful signals combined (season end + referee + goal form): **-0.0043 [-0.0063, -0.0022]** on
confirmation, +0.004 [-0.012, +0.017] on 2026/27 (n.s.; there are no season-end rows yet and the intercept differs
in a drifting season).

Recommendation to the modeler and feature-engineer, in priority order:

1. Put a strength rating (`adj_*` or Elo) into the no-odds model and let the total's level shrink. A structural
   alternative is a term in |strength difference|, or modelling home/away dependence on the mismatch. Gate it on
   the no-odds deciles: top-decile rand80 must be >= 79%.
2. Add games-left / season-end.
3. Add the referee for UK leagues.
4. Use a per-competition alpha to clear the Scottish ship-rule cell.

Do not spend effort on corners, fouls, cards, the AH line, European congestion or per-team residual corrections for
the odds model.

## 5. Edges: persistent, directional mis-calibration

Screen: about 120 pre-match segments × 2 statistics (mean mid-PIT offset, mean residual). A segment counts as
discovered at |z| >= 3 on 2019/20 to 2022/23, with the SE inflated ×1.3 for same-day dependence. It is confirmed at
the same sign and |z| >= 2 on 2023/24 to 2025/26. Full lists: `edges_odds`, `edges_noodds` in the JSON.

**Confirmed, odds model (published with odds):**

- **End of season.** In each team's last 1 to 3 matches, totals are higher than stated.
  - Last match: +0.81 / +1.17 shots (discovery / confirmation), n = 773 / 563.
  - 2 to 3 left: +0.63 / +0.45.
  - Under-bound coverage at 80% is 77.2% (last match) and 78.4% (2-3 left).
  - Equivalent calendar form: May +0.59 / +0.69 (z +3.4 / +3.5); 6 of 7 seasons positive (exception: 2019/20 COVID
    restart).
  - Direction: overs are under-stated and unders over-stated by about 2 to 5pp.
- Nothing else in the odds model survives confirmation. April (+0.46 discovery) and predicted 26-28 (-0.39
  discovery) were not confirmed (+0.02, -0.01): they were noise.

**Confirmed, no-odds model (most of the board):**

- **High predicted totals are too high.** Predicted 28+: -1.13 / -0.96 shots, r80 -3.8 / -4.0pp. Predicted 26-28:
  -0.62 / -0.30. Confident overs on high-total no-odds fixtures are overstated by about 4pp.
- **Lopsided and strong-favourite matches are too low.** Favourite > 0.7: +0.54 to +1.14. abs(pH - pA) > .45:
  +0.46 / +0.54. Market over-2.5 > .65: +0.83 / +0.48. These segments need odds to be identified, so on the board
  they are only usable through a no-odds strength proxy (opportunity 1).
- **Low predicted totals are too low.** Predicted < 20: +0.48 / +0.36; predicted 20-22: +0.45 / +0.42.

**Reliably sharp (skill vs climatology, stable across seasons):**

- strong favourites > 0.8: skill 14.7%;
- predicted total 28+ in the odds model: 14.2%;
- Scotland League Two: >= 7.4% every season;
- La Liga, Ligue 2, Segunda: >= 3.8% every season.

**Weakly sharp:**

- 2. Bundesliga: skill 1.5%, -2.3% in 2024/25, below climatology. It has a 12% newcomer share (teams promoted from
  3. Liga, which we do not cover).
- Premier League: 4.4%.
- Bundesliga: 4.4%.

**Hypotheses (not confirmed, do not act):**

- winter -0.2 (consistent sign but not significant as a feature);
- Tue-Thu -0.15 / -0.07;
- newcomer teams' home shots -0.3 (rand80 74% to 77% in Scotland League Two, Ligue 2, 2. Bundesliga);
- per-team persistence (e.g. Rangers / Celtic / PSV / Galatasaray attack under-predicted 1.7 to 3.3 shots in
  consecutive seasons; the pooled correlation is only 0.15).

**Signs that flip (noise):** Greece (+0.48 / -0.29), La Liga (+0.29 / -0.09), Scotland League Two (-0.51 / +0.34),
matchdays 1-5, rest-day bins.

## 6. Bugs and product concerns (for the owners; not fixed here)

1. **England National League, 2026/27 (n = 155).**
   - The competition has no shot data for 2016/17 to 2025/26, so it never appears in a test season.
   - In 2026/27 the odds model predicts 19.9 against an actual 17.8 (bias -2.1).
   - The 80% bound covers **68.4%** against a stated 83.0%. Climatology covers 75.5% and has a better RPS (2.886 vs
     2.946).
   - 11 National League rows are in `track_record.parquet`.
   - Recommendation: flag as experimental or exclude until it has a calibration history.
2. **Published median is not the median of the published curve.**
   - `NegBinCal.predict` reads the median from the raw NB CDF, but the curve (`prob_at_least`) is PIT-recalibrated.
   - On 28.7% of fixtures the board's `expected_shots` differs (by one shot, mean -0.07) from min{k : P(total <= k) >= 0.5}.
   - MAE is identical (4.364 vs 4.367), but the board can show "expected 24" next to P(total >= 24) < 50%.
   - Fix: read the median at the recalibrated 0.5 level, as `team_predict` already does.
3. **Under side in 2026/27:** 80% upper-bound coverage is 79.2% (odds) / 78.1% (no-odds). This is the early-season
   drift already reported on 2026-10-13. Keep the caveat.
4. **Working-tree note:** other agents have uncommitted edits in `src/shots/models/{negbin,calibrated,persist}.py`
   and elsewhere. My curves were generated from the code as it was this morning and reproduce the stored production
   backtest exactly. Any candidate built on the edited code must re-check that production outputs are unchanged
   (the golden tests).
5. **Leakage check of this work.**
   - Candidate features use strictly earlier dates. Same-date matches of a team, a referee or the Elo update never
     see each other (asserted and tested).
   - Residual-history features use only out-of-sample residuals of earlier dates.
   - The games-left feature uses the season's fixture list, which is known pre-season. Here it is computed from
     played matches, so in production it must come from the calendar.
   - No result in this report looks too good. The largest gain (-0.063 for no-odds on 2026/27) is the early-season
     level of the no-odds model in a drifting season, measured on 1,583 matches; I treat it as an upper end, not as
     the expected gain.
