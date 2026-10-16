# Evaluation protocol for model candidates (from 2026-10-15)

Every candidate (new feature, new model structure, new calibration) is scored the same way, so
candidates can be compared directly. Owner: evaluator. Code: `src/shots/evaluation/scoring.py`
(scores), `src/shots/evaluation/curves.py` (walk-forward curves), tests
`tests/test_evaluation_scoring.py`.

## 1. What a candidate must produce

One row per (match) and per variant (`odds` = fixtures with market odds, `noodds` = all `mkt_*`
columns blanked in training and scoring, which is the production no-odds model). Each row has
`match_id`, `date`, `season`, `competition`, `actual` (realised total shots) and the **full
predictive curve** in one of two forms:

- `cdf_0 .. cdf_K` = P(total <= k), K >= 60 (`curves.py` writes K = 80), or
- `ge_1 .. ge_K` = P(total >= n), contiguous from n = 1.

The board's `p_10..p_45` is **not** enough for RPS or log score. The curve must be the one that
would be published, i.e. after any recalibration. Bounds are read off the curve
(L_q = max{n : P(total >= n) >= q}), so a model cannot report bounds that disagree with its curve.

For any model that implements `fit` / `predict` and either `prob_at_least` or `total_pmf`:

    python -m shots.evaluation.curves --model <registry name> --variants odds,noodds,clim --refit season
    python -m shots.evaluation.curves --model <registry name> --variants odds,noodds --refit month   # finalists

## 2. Fixed walk-forward

- **Season refit (screening):** test seasons 2019/20 to 2025/26 (complete, n = 49,643), plus
  2026/27 to date reported separately (never part of the verdict, too short and still moving).
  Each season is predicted by a model fitted on every row dated strictly before that season's first
  match. Target columns are removed from the rows passed to `predict`.
- **Monthly refit (finalists only):** same seasons, refit before every calendar month on every row
  dated strictly before that month (production cadence). The ship decision uses monthly refit.
- **Selection hygiene:** anything tuned (a hyper-parameter, a variant choice, a shrinkage constant)
  is tuned on 2019/20 to 2022/23 ("discovery") only. 2023/24 to 2025/26 ("confirmation") is used to
  report, not to choose. Say in the report which seasons were looked at before the choice.
- Candidates compared with each other must score **identical rows** (`scoring.compare` refuses
  otherwise).
- Reference rows: `variant = clim` (competition climatology, last 3 seasons) and the production
  model `negbin_pit_comp` from `data/processed/curves_negbin_pit_comp_season.parquet`.

## 3. Scores (`scoring.score(df)`, `scoring.score_by(df, by)`)

| score | definition | role |
|---|---|---|
| `rps` | ranked probability score over total shots = discrete CRPS, sum_k (F(k) - 1{y <= k})^2, in shots | **primary full-distribution score** (lower is better) |
| `logs` | -ln P(total = y), floored at 1e-6 | secondary; punishes over-confident tails |
| `pin_70/80/90`, `pin_mean` | pinball loss of L_q at tau = 1 - q | primary for the lower bounds (CLAUDE.md rule 3) |
| `cov_q`, `mid_q` | share y >= L_q; mid = avg of share(y >= L_q) and share(y > L_q) | ship rule |
| `stated_q`, `rand_q` | mean stated P(y >= L_q); randomized-PIT coverage | separates rounding margin from miscalibration |
| `ucov_q`, `umid_q` | same for the upper bound U_q = min{n : P(total <= n) >= q} | the "under" side |
| `rel_ece_pp`, `rel_max_gap_pp` | reliability of P(total >= n) pooled over lines 12..40, 10 bins | whole-curve calibration |
| `upper_tail_gap_pp` / `lower_tail_gap_pp` | realised minus stated where P(total >= n) <= 0.2 / >= 0.8 | tail reliability |
| `pit_hist`, `pit_max_dev_pp` | randomized PIT decile shares | shape check |
| `mae`, `bias` | of the integer median min{k : F(k) >= 0.5} | point accuracy (vs the naive baseline) |
| `mean_bias`, `rmse_mean` | of the curve mean | level checks |

## 4. Comparing two candidates

- `scoring.compare(a, b)`: paired per-match differences a - b of `rps`, `logs`, `pin_mean`,
  `abs_err`, mean and 95% CI from a **day-block bootstrap** (all matches of one date are
  resampled together; same-day matches share weather, referees' instructions, betting moves and
  league-wide shocks). 1,000 resamples.
- A difference is "real" only if the CI excludes 0. Report the CI, not only the point estimate.
- Report the difference on the confirmation seasons and on 2026/27 as well as on all seven seasons.

## 5. Decision rule

A candidate replaces production when, under **monthly refit** on 2019/20 to 2025/26:

1. **Ship rule (CLAUDE.md rule 3, unchanged):** coverage >= q overall and in every competition,
   overall mid-coverage within +-2pp of q, pinball no worse than `team_avg`, at q = 0.7 / 0.8 / 0.9.
2. **Better:** paired RPS difference vs production has a CI entirely below 0, and pinball is not
   significantly worse (CI of the pinball difference not entirely above 0).
3. **No competition worse beyond noise:** `scoring.competition_guard(candidate, production,
   metric="rps")` flags no competition (CI of the per-competition difference entirely above 0).
   Run it for `pin_mean` too. One flagged competition blocks the candidate until explained.
4. **Both variants:** conditions 2 and 3 hold for the `odds` variant and the `noodds` variant
   (most fixtures on the board are scored without odds).
5. **No new leak:** every new feature has a strictly-before-date construction and a test, and the
   candidate is not "too good" (an RPS gain above about 0.05 shots, i.e. well beyond the ceiling
   estimates in `reports/2026-10-15_diagnosis.md`, is treated as a bug until a leakage check passes).

Informational, not blocking: log score, reliability ECE, upper-tail gap, per-season RPS, and
2026/27.

## 6. Scale of differences (for planning)

From `reports/2026-10-15_diagnosis.md` (season refit, 2019/20 to 2025/26): production RPS 3.082
(odds) / 3.105 (no-odds), climatology 3.299. Odds versus no-odds is 0.023. Realistic gains from
one candidate are 0.002 to 0.02 RPS. The bootstrap CI half-width of a paired difference is
about 0.002 to 0.004 over seven seasons, so differences smaller than about 0.003 cannot be
resolved and should not drive decisions.
