# Per-team shots: calibrated team bounds and P(team >= n), 2026-10-09

Author: modeler agent. Model change: `NegBinCal` (so `negbin_pit_comp` and its no-odds variant)
now exposes calibrated per-team predictions. **Total-shots outputs are byte-for-byte unchanged.**

## API (`src/shots/models/calibrated.py`, class `NegBinCal` and subclasses)

| method | returns |
|---|---|
| `team_predict(rows, side, confidences=CONFIDENCES)` | frame indexed like `rows`: `median` (integer-valued float), `lb_70`, `lb_80`, `lb_90` (int, monotone in q), `mean` (float, expected shots under the recalibrated distribution, for a display like "17 - 13") |
| `team_prob_at_least(rows, side, n)` | recalibrated P(shots of `side` >= n) per row; `n` scalar or per-row array |
| `team_bound_probabilities(rows, side)` | `p_70`... = P(side >= own lb_q) |
| `has_team_calibration` | False for models pickled before this change |

`side` is `"home"` or `"away"`; anything else raises `ValueError`.

## Calibration design

- Each side's raw NB is `nb_.sides_[side]`, the same per-side NB2 the total is built from.
- Inside `fit`, the same carved calibration window as the total (`cal_df`, the latest 2 seasons,
  scored by the NB fitted on the earlier rows) is reused for each side:
  - a `PitLevel` calibrator for the bound levels at q = 0.5 (median), 0.7, 0.8 and 0.9;
  - a `PitMap` for the probabilities.
- Grouping is per competition with the same `min_group` = 300 global fallback. `shrink` and
  `half_life_days` are also the same. For `target="exact"`, `ConformalShift` on 1 - F(y) is used.
- The calibration never uses predict rows.
- Total-shots state (`calibrator_`, `pit_map_`, `nb_`) is not touched. `persist.build_metadata`
  writes a new `team_calibration` entry (thresholds plus calibration-window report per side).
- **Old pickles.** The models in `models_store/` dated 2026-10-05 still load. Their `predict`,
  `prob_at_least` and `bound_probabilities` work unchanged. The `team_*` methods raise a
  `RuntimeError` that asks for `python -m shots.models.train --model negbin_pit_comp`, and for
  the cached `negbin_pit_comp-noodds_*` dirs to be deleted so `scan` refits the no-odds variant.
- **Unchanged-total proof.** I fitted `negbin_pit_comp` (odds and no-odds) before and after the
  change on:
  - synthetic data;
  - a real sample: EPL + Championship, fit on 2018/19 to 2021/22, predict 2022/23.

  I compared `predict`, `bound_probabilities` and `prob_at_least(n)` for n = 10..45. All four
  frames are identical (`assert_frame_equal(check_exact=True)`). The test pins them with golden
  hashes, and the real-sample test skips itself if `features.parquet` changes.

## Backtest protocol

- **Fit and test seasons.** One season-level fit on every row dated before 2025/26 (86,199 rows,
  2010-08-06 to 2025-06-01). The calibration window is 2023/24 + 2024/25. Test set: all 2025/26
  matches (n = 7,094 per side).
- **Variants.** "odds" is the production model. "noodds" is the same class fitted and scored with
  every `mkt_*` column blanked, as `scan.no_odds_rows` does.
- **Data.** A frozen snapshot of `features.parquet` taken at the start of the task, because
  features are being edited concurrently.
- **Supplementary run.** The same protocol for 2024/25, as a robustness check.
- **Baselines.**
  - Per-side `team_avg`: point = (s shots-for shrunk + o shots-against shrunk) / 2, with bounds
    from training-residual thresholds, as in `TeamAverage`.
  - Per-side league average (3-season competition mean), for MAE.
- **Definitions.**
  - mid = (P(y >= N) + P(y > N)) / 2.
  - rand = randomized-PIT coverage of the recalibrated CDF.
  - stated = mean P(side >= lb_q).
  - Pinball is the mean over the three q.
  - The pinball CI is a day-block bootstrap (500 resamples) of model minus `team_avg`.

### Overall, 2025/26

| variant | side | cov 70/80/90 | mid 70/80/90 | rand 70/80/90 | stated 70/80/90 | raw NB cov | team_avg cov |
|---|---|---|---|---|---|---|---|
| odds | home | 74.7 / 83.9 / 92.7 | 70.5 / 80.2 / 90.4 | 70.6 / 80.3 / 90.7 | 74.2 / 83.3 / 92.3 | 73.8 / 82.8 / 92.1 | 69.3 / 78.6 / 89.2 |
| odds | away | 74.7 / 83.7 / 92.4 | 70.2 / 79.7 / 89.6 | 70.2 / 80.0 / 89.8 | 74.6 / 83.7 / 92.5 | 73.7 / 82.6 / 91.4 | 68.5 / 77.9 / 88.1 |
| noodds | home | 74.4 / 83.6 / 92.5 | 70.2 / 80.1 / 90.3 | 70.4 / 80.2 / 90.4 | 74.1 / 83.2 / 92.2 | 73.9 / 83.0 / 92.1 | 69.3 / 78.6 / 89.2 |
| noodds | away | 74.3 / 83.8 / 92.4 | 70.0 / 79.9 / 89.7 | 70.5 / 80.4 / 89.9 | 74.4 / 83.7 / 92.5 | 74.0 / 83.0 / 91.9 | 68.5 / 77.9 / 88.1 |

| variant | side | pinball (cal) | pinball raw NB | pinball team_avg | cal - team_avg [95% CI] | MAE median cal / raw / team_avg / league avg | bias of `mean` |
|---|---|---|---|---|---|---|---|
| odds | home | 1.1389 | 1.1382 | 1.1982 | -0.0593 [-0.0697, -0.0494] | 3.644 / 3.642 / 3.838 / 4.076 | -0.10 |
| odds | away | 1.0389 | 1.0362 | 1.0919 | -0.0531 [-0.0618, -0.0440] | 3.316 / 3.305 / 3.447 / 3.691 | -0.07 |
| noodds | home | 1.1608 | 1.1596 | 1.1982 | -0.0374 [-0.0466, -0.0294] | 3.722 / 3.717 / 3.838 / 4.076 | -0.08 |
| noodds | away | 1.0521 | 1.0495 | 1.0919 | -0.0398 [-0.0481, -0.0311] | 3.366 / 3.364 / 3.447 / 3.691 | -0.08 |

- **The raw per-side NB is already close to calibrated.** Its 2025/26 coverage is about 1pp below
  the recalibrated one, and in the calibration window the recalibration added +0.8 to +1.5pp.
- **The recalibration trades a little pinball for validity.** It costs +0.001 to +0.003 pinball
  against the raw NB, and adds about 1pp of coverage.
- **Over-coverage is integer granularity, as for the total.** Team counts are about 12, so the pmf
  at the bound is about 10% and coverage sits 3 to 4pp above q. Mid-coverage and randomized
  coverage are within ±0.5pp of q.

### Per competition, 2025/26 (21 competitions, n = 180 to 552 per competition and side)

| variant | side | min cov70 | min cov80 | min cov90 | max \|mid-q\| 70/80/90 | cells < q | beats team_avg pinball@80 |
|---|---|---|---|---|---|---|---|
| odds | home | 71.9 (NED) | 80.9 (GRE) | 89.8 (GRE) | 8.0 / 5.3 / 3.8 | 1 | 21/21 |
| odds | away | **63.1** (FRA L1) | 78.4 (FRA L1) | 90.2 (FRA L2) | 10.0 / 7.0 / 3.4 | 4 | 21/21 |
| noodds | home | 69.3 (SCO Prem) | 80.6 (SCO L2) | 88.9 (POR) | 8.0 / 4.2 / 3.7 | 2 | 19/21 |
| noodds | away | 64.7 (FRA L1) | 77.8 (FRA L1) | 89.5 (FRA L2) | 8.6 / 7.5 / 4.0 | 4 | 20/21 |

Every cell below q, with its z-score against q:

- **odds, home:** 0.9 Greece 89.8 (-0.1 SE).
- **odds, away:**
  - 0.7 France Ligue 1 63.1 (stated 74.2, -2.6 SE);
  - 0.7 Greece 65.7 (-1.4 SE);
  - 0.8 France Ligue 1 78.4 (-0.7 SE);
  - 0.8 Greece 78.8 (-0.5 SE).
- **noodds, home:**
  - 0.7 Scotland Premiership 69.3 (-0.2 SE);
  - 0.9 Portugal 88.9 (-0.6 SE).
- **noodds, away:**
  - 0.7 France Ligue 1 64.7 (-2.0 SE);
  - 0.7 Greece 68.2 (-0.6 SE);
  - 0.8 France Ligue 1 77.8 (-1.0 SE);
  - 0.9 France Ligue 2 89.5 (-0.3 SE).

- **France Ligue 1 away is the only notable miss.** It is not a level bias: the mean of
  predicted away shots is 11.36 against 11.14 actual. The 2025/26 away counts were more
  dispersed than the model expected.
- **The 2024/25 run did not repeat it.** France Ligue 1 away passed there. The worst cells were
  different ones: EPL home at 0.7 (65.0, -2.1 SE), and Ligue 2 home at 0.9 in the no-odds
  variant (85.6, -2.6 SE).
- **The literal per-competition clause is much stricter here than for the totals.** I simulated
  outcomes from the model's own stated P(side >= lb_q) per match, so the model is perfectly
  calibrated by construction (2,000 draws). On one season, such a model passes "coverage >= q in
  every competition" at all three q only:
  - 6.2% of the time (odds, home);
  - 12.0% (odds, away);
  - 5.5% (noodds, home);
  - 10.4% (noodds, away).

  The reason is the sample size: about 300 matches per competition, against the 7-season,
  1,150+ per competition used for the totals.

### Reliability of P(team >= n), 2025/26 (home + away pooled, n = 14,188)

| variant | n | mean stated | realised | worst 10pp bin with n >= 200: \|gap\| (bin, n, z) | bins with \|gap\| > 2 SE |
|---|---|---|---|---|---|
| odds | 8 | 82.1 | 82.5 | 5.3 ([0.5, 0.6), 623, +2.6) | 1/6 |
| odds | 10 | 67.4 | 68.2 | 4.7 ([0.3, 0.4), 783, +2.7) | 2/8 |
| odds | 12 | 51.1 | 52.1 | 3.1 ([0.1, 0.2), 800, +2.4) | 2/10 |
| odds | 14 | 36.1 | 37.1 | 2.3 ([0.1, 0.2), 2,317, +3.1) | 2/9 |
| odds | 16 | 23.9 | 24.3 | 4.7 ([0.6, 0.7), 334, +1.8) | 1/7 |
| noodds | 8 | 82.1 | 82.5 | 6.7 ([0.5, 0.6), 535, +3.1) | 1/6 |
| noodds | 10 | 67.5 | 68.2 | 3.6 ([0.3, 0.4), 654, +1.9) | 1/8 |
| noodds | 12 | 51.2 | 52.1 | 2.9 ([0.0, 0.1), 230, +1.8) | 0/9 |
| noodds | 14 | 36.2 | 37.1 | 1.6 ([0.2, 0.3), 2,715, +2.0) | 1/8 |
| noodds | 16 | 24.0 | 24.3 | 1.6 ([0.6, 0.7), 310, +0.6) | 0/7 |

- **Mean stated P is 0.4 to 1.0pp below realised.** In 2025/26 teams shot slightly more than
  predicted, i.e. P is slightly conservative.
- **2024/25 shows the opposite sign.** Stated is 0.1 to 0.9pp above realised, and only 0 to 2 of
  6 to 10 bins exceed 2 SE.

Detail for n = 12 (odds; realised minus stated, per bin):

- **Home bins** [0.1, 0.2) to [0.9, 1.0): +3.8, +1.2, +2.6, +2.0, +0.4, +1.5, +0.7, -0.0, -0.7.
- **Away bins**, same order: +2.9, +2.0, +2.2, +0.1, +0.5, -0.6, -2.1, +0.1, +2.6.
- **Pattern.** There is mild under-confidence for low-probability teams: when the model says
  15 to 35%, it happens about 2 to 3pp more often. No bin is off by more than 4pp.

### Supplementary: 2024/25, same protocol (fit before 2024/25)

| variant | side | cov 70/80/90 | mid 70/80/90 | pinball cal vs team_avg | cells < q |
|---|---|---|---|---|---|
| odds | home | 73.5 / 82.7 / 91.8 | 69.3 / 79.0 / 89.3 | 1.1350 vs 1.2023 | 10 |
| odds | away | 73.4 / 82.3 / 91.6 | 68.6 / **78.3** / 88.5 | 1.0101 vs 1.0809 | 12 |
| noodds | home | 73.4 / 82.8 / 91.7 | 69.3 / 79.2 / 89.2 | 1.1574 vs 1.2023 | 10 |
| noodds | away | 72.9 / 82.3 / 91.4 | 68.0 / **78.4** / 88.4 | 1.0286 vs 1.0809 | 12 |

- Away mid-coverage in 2024/25 is 1.4 to 2.0pp under q (bias of `mean` +0.14 to +0.18), which is
  close to the ±2pp limit. It is driven by an away-shot level drop in that season, not by the
  dispersion.

## Ship-rule verdict (CLAUDE.md rule 3) for per-team bounds, 2025/26

| check | odds home | odds away | noodds home | noodds away |
|---|---|---|---|---|
| coverage >= q overall | pass | pass | pass | pass |
| coverage >= q in every competition | **fail** (1 cell) | **fail** (4) | **fail** (2) | **fail** (4) |
| \|mid - q\| <= 2pp overall | pass (max 0.5) | pass (0.4) | pass (0.3) | pass (0.3) |
| pinball <= team_avg | pass (-0.059) | pass (-0.053) | pass (-0.037) | pass (-0.040) |
| **verdict** | **FAIL** | **FAIL** | **FAIL** | **FAIL** |

**Verdict: per-team bounds do not ship under the rule as written.** They pass every overall
check, beat `team_avg` clearly, and the probabilities are calibrated to about 1pp on average.

The per-competition failures are mostly at noise level:

- 9 of the 11 failing cells are within 1.4 SE of q;
- a perfectly calibrated model fails the clause on one season 88 to 95% of the time;
- France Ligue 1 away at q = 0.7 is the only real outlier (-2.6 SE), and it did not repeat in
  2024/25.

My recommendation:

1. Before any ship decision on team bounds, run a 7-season walk-forward backtest of the team
   outputs, as was done for the totals. That gives about 1,150+ matches per competition and side.
   Then apply the rule. Under the current one-season protocol the clause cannot discriminate.
2. Until then, the UI can show team medians or means and P(team >= n) as **unshipped /
   experimental**. Overall reliability is within about 1pp. Team lb_q should not be labelled with
   a guaranteed confidence per competition.
3. The per-competition-tolerance question raised in the ship-decision report (q - 1pp or q - 1 SE)
   applies here even more. That decision belongs to the user.

## Reproduction

- **Scripts.** The backtest scripts are in the session scratchpad: `team/bt_team.py` (with
  `TEST_SEASON` env, default 2025/2026) and `team/an_team.py`, run on a features snapshot. They
  use only public model API plus `_side_cdf` for the raw-NB reference.
- **Run time.** The odds fit took 46 s and the no-odds fit 24 s.
- **Tests.** `tests/test_models_team.py` covers:
  - contract: shape, dtypes, monotone bounds, median >= lb_70, index alignment, per-row n;
  - no leakage: predict-row targets ignored, target-less fit rows ignored, calibration window
    inside train;
  - calibration on synthetic data: well-specified data, a side-specific level shift corrected
    while the other side stays calibrated, and the exact target;
  - the no-odds variant;
  - a save/load round-trip with metadata;
  - the old-pickle error;
  - golden hashes of the total outputs, synthetic and real sample, odds and no-odds.
- **Result.** The full suite gives 182 passed.
