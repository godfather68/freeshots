# Under-side validity and per-fixture explanations (2026-10-13/14)

Two deliverables for the over/under use case:

- **A. Under side.** Is P(total <= N) = 1 - P(total >= N + 1), read off the published curve, as
  trustworthy as the over side that has been validated so far?
- **B. Explanations.** For every board fixture, an additive breakdown of the expected total that comes
  from the fitted model itself (`explain` in `board.json`).

No model, coefficient or published probability changed. Domestic predictions are byte-identical (golden
tests pass); the board only gains an `explain` column and the export an additive `explain` object.

---

## A. Under side ("under N.5")

### Method

`python -m shots.evaluation.under_side` (code: `src/shots/evaluation/under_side.py`, numbers:
`reports/2026-10-13_under_side_validation.json`, curves cached in `data/processed/under_side_curves.parquet`).

1. **Season replay**, the protocol of the published backfill: for each of 2023/24, 2024/25 and 2025/26 the
   production model class (`negbin_pit_comp`) is fitted once on every match dated before that season's first
   match and scores the whole season (up to 10 months stale, harder than production's monthly refit). Two
   variants: `odds` (market features, fixtures with odds only) and `noodds` (market features blanked in training
   and scoring). 21,463 matches per variant; full curve kept, so every line N = 16..30 is checked.
2. **Monthly walk-forward** already on disk (`backtest_predictions_month_negbin_pit_comp`, odds variant, refit
   every month, 2019/20 to 2026/27, 51,028 matches): its PIT bounds give the exact coverage of any upper bound
   (y <= U_q iff F(y-1) < q), so upper and lower tails can be compared with the same predictions.

Definitions: upper bound U_q = min{n : P(total <= n) >= q}; `mid` = average of P(y <= U) and P(y < U) (the
rounding-aware ship rule, mirrored). "Picks" = every (match, line) with a stated probability >= 70%, as in
`track.json`; their standard errors are clustered by match.

### Results

**Bounds (both tails).**

| | upper cov 70 / 80 / 90 | upper mid 70 / 80 / 90 | lower cov 70 / 80 / 90 | lower mid 70 / 80 / 90 |
|---|---|---|---|---|
| walk-forward 2019/20 to 2026/27 so far (n 51,028) | 72.7 / 82.0 / 91.1 | 69.6 / 79.6 / 89.6 | 73.4 / 82.7 / 91.6 | 70.0 / 79.9 / 89.7 |
| replay, odds (3 seasons) | 73.2 / 82.3 / 91.3 | 70.1 / 80.0 / 89.9 | 73.1 / 82.1 / 91.2 | 69.7 / 79.4 / 89.2 |
| replay, no-odds (3 seasons) | 73.0 / 82.4 / 91.4 | 70.0 / 80.0 / 89.9 | 73.0 / 82.1 / 91.0 | 69.8 / 79.5 / 89.1 |
| walk-forward 2026/27 so far (n 1,385) | **68.3 / 78.7 / 89.2** | **65.1 / 76.0 / 87.8** | 73.1 / 82.2 / 91.8 | 70.8 / 79.4 / 90.0 |

Every completed season (walk-forward, per season) has upper coverage >= q at all three levels, mid within
2 pp, and the same holds per competition at 80% (81.0 to 84.0, mid 78.6 to 81.1); at 90% Serie A is at 89.8
(mid 88.5), the only cell below q. The upper tail is as well calibrated as the lower one.

**Reliability of P(under) by band, pooled over lines 16..30 (gap = realised - stated, pp).**

| band | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| odds | +0.4 | +0.6 | +0.3 | +0.1 | -0.3 | -0.2 | -0.2 | +0.1 | -0.1 | -0.6 |
| no-odds | +0.9 | +0.9 | +0.5 | +0.1 | 0.0 | -0.2 | -0.2 | -0.6 | -1.2 | -1.4 |

(Each band has 18,000 to 44,000 match-lines; binomial s.e. 0.2 to 0.5 pp.)

**Picks at >= 70%, under vs over (stated -> realised, gap, clustered s.e.).**

| | under picks | over picks |
|---|---|---|
| odds, all lines | 83.0 -> 82.9 (-0.2 +- 0.2), n 82,340 | 85.6 -> 85.2 (-0.4 +- 0.2), n 117,664 |
| odds, lines 16-22 | 79.4 -> 78.0 (-1.5 +- 1.1), n 3,351 | 86.2 -> 85.8 (-0.4) |
| odds, lines 23-26 | 81.3 -> 81.0 (-0.3 +- 0.4) | 75.9 -> 74.8 (-1.1 +- 0.7) |
| odds, lines 27-30 | 83.7 -> 83.6 (0.0 +- 0.2) | n = 71 |
| no-odds, all lines | 83.4 -> 82.4 (**-1.0 +- 0.3**) | 85.7 -> 84.9 (**-0.8 +- 0.2**) |
| no-odds, lines 16-22 | 80.0 -> 76.4 (**-3.6 +- 1.1**), n 3,689 | 86.4 -> 85.9 (-0.5) |
| no-odds, lines 23-26 | 81.4 -> 79.1 (**-2.3 +- 0.4**) | 76.6 -> 72.7 (**-3.9 +- 0.7**) |
| no-odds, lines 27-30 | 84.2 -> 83.8 (-0.4 +- 0.2) | 75.1 -> 61.9 (-13.2 +- 4.5), n 194 |

The published backfill (2025/26, `track.json`) gives the same picture for the lines it stores: under 21.5
77.4 -> 72.9 (n 350), under 23.5 82.3 -> 81.0 (n 574), under 25.5 81.1 -> 80.4 (n 1,385).

**By season (no-odds, under picks >= 70%):** 2023/24 -0.8, 2024/25 -1.0, 2025/26 -1.1 pp; odds: +0.4, -0.4,
-0.4. Lines 16-22, no-odds: -4.2, -1.1, -6.0; odds: -2.1, +1.0, -4.1.

**By competition (under picks, gap pp, odds / no-odds):** within +-1.5 pp for most leagues. Outliers:
Germany 2. Bundesliga -5.2 +- 2.0 / -7.8 +- 2.1, Belgium -2.8 / -4.0, Netherlands -2.1 / -3.2, Bundesliga
-1.4 / -3.1. These are high-shot leagues where an under at >= 70% means a line far below the league level
(the same "line far from the centre" effect as above, not a league-specific bias of the curve).

### Verdict

- **Structurally yes:** the under side is the same calibrated distribution. Upper-bound coverage meets the
  mirrored ship rule overall and per competition over seven walk-forward seasons, and the reliability of
  P(under) in every 10% band is within 0.6 pp (odds model). For the odds model, under probabilities are as
  trustworthy as over probabilities.
- **No-odds model (most of the board):** confident probabilities are slightly too confident **on both
  sides** (about 1 pp at >= 70%), which shows up more on the under side because under picks sit higher in
  the curve: P(under) 80-90% band realised 1.2 pp lower, 90-100% band 1.4 pp lower.
- **Where unders are not as trustworthy:**
  1. **Low lines, confident unders** (under 16.5 to 22.5 stated >= 70%): overstated by 3.6 pp (no-odds,
     significant) and 1.5 pp (odds, not significant), 4 to 6 pp in 2025/26. The mirror on the over side (a
     confident over at a high line, 23.5+) is overstated too, by 4 pp and more for the no-odds model. Rule:
     the further the line is from the fixture's centre, the more the stated probability should be read as an
     upper limit.
  2. **High-shot leagues** (2. Bundesliga, Belgium, Eredivisie, Bundesliga): confident unders 3 to 8 pp
     overstated (same mechanism).
  3. **Early season when the league level moves.** In 2026/27 shot levels rose and the model lags: upper
     coverage at 80% was 77.0% in August and 79.5% in September (lower side 84.1 / 80.5), i.e. unders about
     3 to 5 pp overstated, overs conservative. In a season where shots fall (2024/25) it is the reverse (over
     picks -1.5 / -2.1 pp). Direction depends on the drift; it fades by October.
  4. **Champions League (experimental):** neither tail is validated (no shot history, no results source).

**UI recommendation.** No hard restriction of the line range is needed on calibration grounds beyond the
published curve (lines 9.5 to 44.5). Do show:
- a general note that "under" is read from the same curve as "over" (1 - P(at least N+1));
- a caveat when the stated under probability is >= 70% at a line <= 22.5, or in the four leagues above, or
  for a fixture scored without odds ("historically 2 to 4 points lower than stated");
- the existing experimental label for Champions League fixtures, which applies to both sides;
- optionally an early-season caveat (first 8 weeks of a season) on both sides.

**Track-record numbers for the under side** are not added to the export: they are already derivable from
`track.json` (a reliability band [lo, hi] of P(>= N) with stated s / realised r is the under-(N-1).5 band
[1 - hi, 1 - lo] with 1 - s / 1 - r; this reproduces the figures above exactly). Documented in
`docs/static-api.md`.

---

## B. Explanations (`explain`)

### Decomposition and why

The production model has two log-linear NB GLMs, mu_s = exp(x_s . beta_s); the published distribution is the
PIT-recalibrated total. Code: `src/shots/models/explain.py`.

    expected_mean = baseline + sum(effects) + residual

- `expected_mean`: mean of the **published** (recalibrated) distribution. The board's `expected` is its
  integer median; the mean is on average 0.47 shots higher (right skew).
- `baseline` = 2 x league prior = the fixture's `league_avg` (499 of 499 equal). Label says "this season",
  "long-run average" (fewer than 20 team-observations this season) or, for Champions League fixtures, the blend
  of the two clubs' domestic leagues.
- **Reference fixture**: league-average team vs league-average team (every rate feature = league prior, SOT =
  prior x training SOT share, median rest, >= 10 season matches, no division change, no missing flags, market
  at training means). Each design column contributes beta_j (x_j - x0_j) to log(mu_s / mu0_s); columns are
  summed into factors (`column_group`).
- **Log to shots: exact Shapley value** of the multiplicative game v(S) = mu0 prod_{g in S} exp(delta_g),
  computed in closed form (Owen's multilinear extension, Gauss-Legendre, exact for a polynomial). Chosen
  because it is order-independent (the average over all orders of switching factors on), symmetric, keeps
  the sign of each log contribution, and is exactly efficient (sum = mu - mu0), so there is no linearisation
  error. Mid-point / log-mean linearisation would also add up but splits the interaction terms by a convention
  rather than by averaging; first-order linearisation would leave a residual that grows with the effects.
- `league_level` = mu0_home + mu0_away - baseline: what the model makes of that league level (intercept,
  league-prior coefficient, home advantage, reference market). It is -0.7 for the Championship (the odds model
  rates an average matchup at 26.1 vs a raw 26.8) and +0.3 for the no-odds model in most leagues.
- `calibration` = recalibrated mean - raw NB mean (|value| <= 0.37 on today's board).
- **Residual before rounding**: <= 3.5e-14 on the domestic routes (float noise), <= 1.6e-6 on the Champions
  League route (Gauss-Hermite quadrature and support truncation of its mixture). **After rounding** to one
  decimal: the residual carries rounding only: on today's 499 fixtures it is 0.0 (217), +-0.1 (257),
  +-0.2 (25). The verifier rejects anything above 0.5.
- **Cross-border model:** a club without data is replaced by n real team profiles; effects are averaged over
  the scenarios (the decomposition is linear in the scenario) and all of that club's factors are reported as
  one `*_standin` factor.
- **Published list:** the six largest factors with |effect| >= 0.2, sorted by |effect|, then one `other`
  ("Smaller factors", n combined) so nothing is hidden in the residual. Effects in shots, one decimal.

These are attributions of this model, not causal claims: correlated inputs (form, odds) share credit the way
the coefficients split it.

### Factors

`home_attack` / `away_attack` (shots for: shrunk last 10, recency-weighted, season to date, on target),
`home_defence` / `away_defence` (shots conceded, same windows), `home_venue` / `away_venue` (latest home /
away matches, for and against), `market_strength` (1X2 odds, worded as favourite categories),
`market_goals` (over/under 2.5, worded high / average / low-scoring), `home_rest` / `away_rest`,
`home_early` / `away_early` (season matches < 10, no history, division change), `home_standin` /
`away_standin` (experimental only), `league_level`, `calibration`, `other`. Every `detail` carries the real
numbers (e.g. "concedes 16.8 shots per match, last 10 (league 13.4)"); market details never contain a number
(checked by the verifier, team names excepted, e.g. "Schalke 04").

Signals: `home_matches` / `away_matches` (prior matches; 0 = cold start), `odds_used`, `model_gap` (odds model
minus no-odds model expected mean, only when the odds model scored the fixture; today -0.5 to -0.1 on the 5
odds fixtures). **Team volatility is not published (always null)**: a team's standard deviation of match
totals over its last 10 matches does not predict the next 10 (Spearman 0.04; 0.00 after controlling for the
team's level; 92,865 team-matches since 2019; last 20: 0.08), and teams flagged "high" (> 1.25 x league median)
had a next-match absolute deviation of 4.73 vs 4.57 for "low": not a signal worth a label.

On today's board, factors listed most often: home_attack 449, away_defence 446, away_attack 444, other 434,
home_defence 430, league_level 359, home_early 249, home_venue 248.

### Persistence

Stored with the prediction: migration `0005_explain` adds a nullable JSON column `predictions.explain`
(ALTER TABLE ADD COLUMN; no row rewritten; append-only triggers cover it; no view reads it). Reasons: the
exporter has no models or features (CI exports from the database only), and recomputing later would use
newer results or a refitted model, i.e. explain a different prediction than the one stored. The board
parquet carries it as a JSON text column computed in the same pass as the prediction; the content hash
includes it only when the column exists (older boards keep their hash). A failure in the explanation code
is logged and leaves the fixtures without `explain`; the predictions are still published.

### Export and size

`board.json` fixtures gain `explain` (object or absent). The exporter re-emits it key by key and refuses an
explanation that does not add up; `python -m shots.export.verify` checks additivity, ordering, residual size,
no numbers in market wording, `odds_used == odds`, and equality with the database. JSON Schema and
`docs/static-api.md` updated (schema stays 1). `meta.json` of the board gains `n_explained` (board.meta only).

Size on the 2026-10-14 board (499 fixtures): `board.json` 1,110 KB (was 539 KB for 517 fixtures yesterday;
about +1.05 KB per fixture, +90%); gzip 178 KB (was 127 KB, +40%). If that is too much, the cheapest cut is
fewer listed factors (`MAX_FACTORS`) or shorter `detail` strings.

### Example (real, 2026-10-14)

Norwich v Southampton (Championship, odds model): expected_mean 28.1 = Championship average 26.8 + goals
odds "high-scoring match" +1.4, Norwich defence (concedes 12.1, league 13.4) -0.7, league level -0.7,
Southampton defence (concedes 15.2) +0.6, Southampton attack (15.6) +0.4, Norwich attack (14.4) +0.2,
8 smaller +0.1, residual 0.0; model_gap -0.5.

Feyenoord v Como (Champions League, experimental, both clubs known): 30.4 = domestic blend 29.5 + Feyenoord
defence (concedes 9.8, reference 14.7) -3.3, Feyenoord attack (20.3) +3.0, Como attack (20.1) +2.4, Como
defence (11.1) -2.0, early-season +0.4 / -0.4, 6 smaller +0.8, residual 0.0.

### Tests

`tests/test_explain.py` (25 tests): closed-form Shapley equals the brute-force average over all orders and is
exactly efficient; additivity within 1e-6 (actually < 1e-9) for odds and no-odds routes and < 0.01 residual for
the cross-border route; the GLM part reproduces the model's own means and the expected mean equals the mean of
the published curve; reference fixture has zero effects; sign sanity (raising a team's attack raises its attack
effect, a leakier defence raises the opponent's); published rounding adds up exactly, ordering, caps; market
wording has no numbers; cold-start / early-season wording; unknown clubs' features are not used; explaining
leaves every prediction unchanged; board routes and failure isolation; storage, append-only, content hash,
export, JSON Schema, verifier tamper cases, migration 0005 upgrade/downgrade in place. Golden-hash tests of the
production model are unchanged and pass.

### Limitations

- Attributions depend on the reference chosen (league-average teams); a different reference moves effects
  between `league_level` and the team factors without changing the total.
- Correlated features share credit as the coefficients do; small market effects do not mean odds are useless.
- `detail` shows the last-10 average; the factor also includes recency-weighted, season-to-date and on-target
  rates, so the number and the effect will not always move together.
- Champions League explanations inherit every caveat of the experimental model.
