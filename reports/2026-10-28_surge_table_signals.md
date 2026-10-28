# Surge Watch: league-table signals, season phase and stacking (2026-10-28)

Author: modeler. Question from the user: "find fixtures with more than one surge signal so the edge is better, using
each team's current table position and the period of the season in each competition".

**No prediction changed.** The production switches are untouched (odds route Surge, Scotland Championship on
`negbin_pit_comp`, no-odds route `fin_c`). Surge Watch only labels fixtures. Nothing committed or pushed.
Machine-readable numbers: `reports/2026-10-28_surge_table_signals.json`.

## Plain-language summary

A **surge** is a match that ends 5 or more shots above our published expected total. It happens in about 21% of
matches, and our curves say about the same. All numbers below are out of sample: walk-forward predictions refitted
every month on earlier matches only, 2019/20 to 2025/26, 49,643 matches.

1. **Two or more signals on the same match: the edge roughly adds up.**
   - **Odds route** (the curves of the old odds model): matches with 2+ signals surged **25.1%** of the time. The
     curve said **21.1%**. This held in **7 of 7 seasons**.
   - It also passes the strictest test. The signals were picked on 2019/20-2022/23 only, then the rule was scored on
     2023/24-2025/26: 25.6% vs 21.9%, held in 3 of 3 seasons.
   - **After the Surge correction**, 2+ signals still surged 24.1% vs 21.0% (7 of 7 seasons; strict test 25.6% vs
     22.1%).
   - **No-odds route** (fin_c): 2+ signals surged **24.1% vs 21.3%**, 7 of 7 seasons. But this route fails the
     strict test, because only three signals qualify on 2019/20-2022/23 alone. Those three rarely stack: 162
     matches, 19.8% vs 21.7%.
2. **Signals add up, but only partly.** A match with 2 or 3+ signals beats the curve by about **three quarters of
   the sum** of the single-signal edges. On the Surge curves, 3+ signals add nothing beyond 2 (saturation). Some
   pairs reinforce each other:
   - "one side with nothing to play for" + "top vs bottom": +7 to +9 points;
   - "promoted side near the bottom" + "top vs bottom": +6.
   Some pairs cancel:
   - "last three games" + "top vs bottom" or + "promoted side near the bottom": 0 or negative. A late match where both
     sides still have something to play for does not surge.
3. **Season phase.** Only the **run-in** (6 or fewer league games left) surges more than the curve says:
   - +2.8 points on the old odds model, 7 of 7 seasons;
   - +1.8 after Surge;
   - +1.1 on the no-odds route.
   The season-end signals already explain this: run-in matches without any signal are not significantly different.
   **Mid-season** surges slightly less than stated (-0.6 to -0.8 points). Most single signals are weakest in
   mid-season. **Two signals in mid-season give no edge** on any route.
4. **New table signals.** One definition per idea was chosen on 2019/20-2022/23 only:
   - **Top of the table against the bottom** (last third of the season, at least 60% of the table between the two
     sides). It surged 23.3% vs 21.5% on the no-odds route, 6 of 7 seasons. On the odds route: 23.7% vs 21.6%.
     **Added (boost, both routes).**
   - **Promoted side near the bottom** (last third, a promoted team in the bottom quarter). It surged 24.8% vs
     21.3% on the no-odds route, **7 of 7 seasons**. On the odds route: 23.9% vs 21.7%, 5 of 7 seasons, interval just
     above 0. **Added (boost, both routes).**
   - **Title race** (last third, both sides within 6 points of first). It surged only **11.6% vs 20.8%** on the
     no-odds route, lower in 6 of 6 seasons. This is a strong *damper*. Too few title-race matches were in the
     discovery seasons to choose the definition in advance (103 < 150), so it is **held back**: shown in the
     catalogue, never applied.
   - **No effect:**
     - relegation six-pointers;
     - "a team needing a win" (near the drop line in the run-in);
     - mid-table with nothing realistic at stake (beyond what "nothing to play for" captures).
     The last one is positive on the odds route only.
5. **Our 70%+ calls (P(24+) >= 70%).**
   - **Odds route:** calls with 2+ signals hit 80.3% vs 76.2% stated. In the strict test: **84.3% vs 76.8%** (n =
     185, interval +3.2 to +12.3 points). On the Surge curves: 79.2% vs 76.0% (interval includes 0).
   - **No-odds route:** 2+ signals do **not** raise the hit rate: 75.6% vs 76.6%. A side finding: no-odds 70%+
     calls with **no** boost signal hit 73.8% vs 76.1% stated, short in 6 of 7 seasons. See "Watch items".

**Recommendation.**
- Add the two table boosts and hold back title race (done in the catalogue).
- Publish the stacking table (counts and rates by number of signals, per route and phase), done in
  `surge_signals.json`.
- Use a **"strong surge candidate" (2+ boosts)** label **only on the odds route**, where it holds out of sample.
  - On today's routing that means only odds fixtures scored by `negbin_pit_comp`: Scotland Championship and Surge
    fallbacks, because Surge-scored fixtures are not labelled.
  - Do **not** use the label on the no-odds route yet: it fails the strict test, and it does not lift 70%+ calls
    there.
- Optional, needs a decision: the same 2+ edge remains after the Surge correction (measured against the Surge
  curves, so it is not double counting). The coordinator could allow a "strong" label on Surge-scored fixtures,
  using the `odds_surge` numbers of the JSON.

## 1. Setup

- **Data.** Same data and predictions as `reports/2026-10-26_overshoot_patterns.md` (scratch cache of that study):
  - `fin_c` / no-odds;
  - `cand_nbmle` / odds (= production `negbin_pit_comp`);
  - `cand_odds_surge` / odds (Surge).
  All are monthly refits, 2019/20-2025/26, 21 domestic leagues, 49,643 matches per set.
- **Definitions.**
  - Surge = actual >= expected + 5, where expected is the integer curve median.
  - Stated = P(total >= expected + 5) on the same curve.
  - Excess = realised - stated.
  - 95% CIs: day-block bootstrap, 1,000 resamples.
  - Seasons held = seasons with 20+ matches in which the excess has the signal's sign.
- **Causality.** Table inputs are the candidate build's season-context family (`ctx_*`). Its league table is built
  only from results dated strictly before the match. Promotion comes from the previous season's tier. Both are
  pre-match (rule 1).
- **Phase** (`surge_rules.season_phase`, format-aware):
  - Progress is the share of scheduled games played, the mean of the two teams. It uses `SEASON_FORMATS`: Scottish
    4-round leagues and the Premiership's 38 games.
  - run-in = either team has 6 or fewer games left. Otherwise late = progress >= 2/3, middle = progress >= 0.25,
    early = below that.
  - Playoff formats (Belgium, Greece) have no known season end. Their phase uses the regular-season progress, the
    playoff rounds count as late, and they never have a run-in.
- **Selection** (`surge_table.select`).
  - The grid of definitions was fixed in advance: 27 cells.
  - Per idea, the cell with the largest discovery |z| of the excess was chosen. This used the no-odds route only,
    seasons 2019/20-2022/23, and required discovery n >= 150.
  - A chosen signal applies on a route by the catalogue's rule: the CI excludes 0, the sign held in >= 5 of 7
    seasons, and the confirmation excess (2023/24-2025/26) has the same sign.

Reproduce:

```
python -m shots.evaluation.surge_table study --preds <scratch>/os/month --surge-preds <scratch>/os/surge
python -m shots.evaluation.surge table --preds <scratch>/os/month    # catalogue + stacking, re-checks every count
```

## 2. Season phase

Surge rate vs stated, per residual set (excess in points, 95% CI, seasons in which the sign held):

| phase | n | no-odds (fin_c) | odds (production) | odds after Surge |
|---|---:|---|---|---|
| early (< 25%) | 13,008 | 21.4 vs 21.2, +0.2 [-0.6, +1.0] | 21.6 vs 21.3, +0.3 [-0.5, +1.1] | +0.4 [-0.5, +1.2] |
| middle | 20,858 | 20.5 vs 21.2, **-0.6 [-1.2, -0.0]**, 5/7 | 20.4 vs 21.3, **-0.8 [-1.4, -0.2]**, 5/7 | **-0.8 [-1.4, -0.2]** |
| late (last third) | 8,947 | 21.2 vs 20.9, +0.3 [-0.6, +1.3] | 21.3 vs 21.0, +0.2 [-0.7, +1.1] | +0.4 [-0.5, +1.3] |
| run-in (<= 6 left) | 6,830 | 22.5 vs 21.5, **+1.1 [+0.0, +2.0]**, 5/7 | 24.2 vs 21.4, **+2.8 [+1.7, +3.9]**, 7/7 | 23.1 vs 21.3, **+1.8 [+0.7, +2.8]**, 5/7 |

**Run-in matches without any season-end or other boost signal:**
- no-odds: +1.1 [-1.1, +3.3], n = 1,294;
- odds: +0.8 [-1.3, +3.0].
The run-in excess is carried by the season-end signals.

**Signals by phase (excess, no-odds route).** The boosts are weakest in mid-season:

| signal | early | middle | late | run-in |
|---|---:|---:|---:|---:|
| h2h_above | +3.5 | +0.6 | +0.5 | +1.4 |
| low_goal_run | +3.1 | -0.4 | +1.9 | +1.5 |
| home_mismatch | +1.6 | +0.8 | +3.9 | +1.9 |
| below_ratings (damper) | +0.5 | -2.7 | -1.7 | -3.3 |

The odds route shows the same shape. Full cells: JSON `<route>.signal_phase`.

**How much of each catalogue pattern remains after the Surge correction** (odds route, production -> Surge, excess
in points):

| signal | production | after Surge |
|---|---:|---:|
| dead_rubber | +5.9 | +3.3 |
| dead_team | +4.0 | +2.7 |
| last_three | +4.9 | +3.1 |
| h2h_above | +1.8 | +1.3 |
| low_goal_run | +1.7 | +1.0 |
| top_vs_bottom (new) | +2.1 | +1.9 |
| promoted_struggling (new) | +2.3 | +2.2 |
| winter (damper) | -1.6 | -1.6 |
| below_ratings (damper) | -0.8 | -0.5 |

Surge removes about a third to a half of the season-end patterns. It leaves the table and winter patterns almost
intact.

## 3. Table signals (chosen on discovery, confirmed on 2023/24-2025/26)

| idea | chosen definition (discovery) | route | n | surge vs stated | excess, 95% CI | seasons | confirmation excess | verdict |
|---|---|---|---:|---|---|---|---|---|
| top vs bottom | rank gap >= 0.6, late/run-in | no-odds | 2,815 | 23.3 vs 21.5 | +1.8 [+0.3, +3.3] | 6/7 | +1.6 [-0.6, +4.0] | **applies** |
| | | odds | 2,815 | 23.7 vs 21.6 | +2.1 [+0.6, +3.7] | 6/7 | +1.6 [-0.6, +4.2] | **applies** |
| promoted side near the bottom | promoted, bottom quarter, late/run-in | no-odds | 1,061 | 24.8 vs 21.3 | +3.5 [+1.2, +5.8] | 7/7 | +2.1 [-1.5, +5.5] | **applies** |
| | | odds | 1,061 | 23.9 vs 21.7 | +2.3 [+0.03, +4.7] | 5/7 | +0.4 [-3.0, +3.7] | **applies** (marginal) |
| title race (damper) | both within 6 pts of 1st, late/run-in (relaxed n >= 100) | no-odds | 215 | 11.6 vs 20.8 | -9.1 [-13.3, -4.8] | 6/6 | -6.7 [-12.5, -0.6] | **held back** |
| | | odds | 215 | 16.3 vs 20.6 | -4.3 [-8.9, +0.7] | 4/6 | -2.1 | no |
| relegation six-pointer | both within 3 pts of safety, late/run-in | no-odds | 570 | 21.9 vs 20.6 | +1.3 [-2.0, +4.9] | 3/7 | -1.5 | no |
| needing a win | either within 3 pts of safety, run-in | no-odds | 2,291 | 21.4 vs 21.3 | +0.1 [-1.4, +1.7] | 4/7 | -1.8 | no |
| mid-table, nothing realistic | middle ranks, > 1 x games left from title and drop | no-odds | 2,419 | 21.8 vs 21.5 | +0.3 [-1.4, +1.9] | 3/7 | +1.8 | no (discovery sign negative) |
| | | odds | 2,419 | 23.7 vs 21.2 | +2.5 [+0.8, +4.1] | 6/7 | +3.6 | n/a (direction chosen on no-odds) |

**Notes.**
- **top vs bottom.** It works whichever side is at home, in the last third only. Over the whole season from
  mid-season on it is +0.3, n.s.
- **promoted side near the bottom.** Promoted teams in the top quarter lean the other way: -3.5 [-7.2, +0.3],
  1 of 6 seasons. Not used.
- **title race.** No definition reached the pre-set discovery minimum (n >= 150; the best had 103). The definition
  was therefore chosen after the fact with n >= 100, and the signal is **held back**.
- **mid-table, nothing realistic.** It is mostly the "nothing to play for" signals in a looser form. On the odds
  route, k = 1.5 and k = 2.0 give +3.7 and +4.2, and they overlap heavily with `dead_rubber` / `dead_team`, which
  already apply there.
- **Full grid.** All 27 cells with discovery z-scores: JSON `selection.discovery_scores` and `<route>.segments`.

## 4. Stacking

**Counting rule.** Count the boosts that apply on the route: the catalogue's plus the two new ones, with at most one
season-end signal per match. Held-back signals and dampers are not counted.

| boosts | no-odds: share, surge vs stated, excess [CI], seasons | odds (production) | odds after Surge |
|---|---|---|---|
| 0 | 60.4%, 20.5 vs 21.2, -0.7 [-1.2, -0.3] | 66.6%, 20.7 vs 21.4, -0.7 [-1.1, -0.3] | 67.6%, -0.5 [-0.9, -0.0] |
| 1 | 30.8%, 21.7 vs 21.1, +0.6 [-0.1, +1.3], 4/7 | 26.8%, 22.4 vs 21.0, +1.4 [+0.7, +2.2], 7/7 | 26.9%, +0.9 [+0.2, +1.6], 7/7 |
| 2 | 7.2%, 23.6 vs 21.2, +2.4 [+1.2, +3.7], 7/7 | 5.6%, 24.8 vs 21.1, +3.7 [+2.3, +5.1], 7/7 | 5.0%, 24.2 vs 21.0, +3.2 [+1.8, +4.7], 7/7 |
| 3+ | 1.6%, 26.2 vs 21.8, +4.4 [+1.7, +7.0], 7/7 | 1.0%, 26.9 vs 21.4, +5.4 [+2.0, +8.9], 7/7 | 0.5%, 23.2 vs 20.9, +2.3 [-2.5, +7.2], 5/7 |
| **2+** | **8.8%, 24.1 vs 21.3, +2.8 [+1.7, +4.0], 7/7**, conf. +1.9 [+0.03, +3.8] | **6.6%, 25.1 vs 21.1, +4.0 [+2.7, +5.2], 7/7**, conf. +3.8 [+1.8, +5.9] | **5.5%, 24.1 vs 21.0, +3.1 [+1.7, +4.6], 7/7**, conf. +2.7 [+0.3, +5.1] |

**What the new signals add.** Catalogue signals only, 2+ group:
- no-odds: +2.3 [+0.9, +3.7] (n = 2,759);
- odds: +3.8 [+1.8, +5.7] (n = 1,741).
With the two table signals the 2+ group grows by about 60% (n = 4,384 / 3,268) at an equal or larger edge.

**Strict out-of-sample test.** The boost set was chosen on discovery alone: the discovery CI of each signal's excess
is above 0, and its excess was positive in 3+ of the 4 discovery seasons. The rule was then counted and scored on
2023/24-2025/26 only.
- **Odds.** All 7 boosts qualify. The 2+ group surged 25.6% vs 21.9%, **+3.8 [+1.8, +5.9]**, 3/3 seasons, n =
  1,280. The 3+ group: +7.9 [+2.1, +13.8].
- **After Surge.** 5 boosts qualify. The 2+ group: **+3.5 [+0.9, +6.2]**, 3/3 seasons, n = 773.
- **No-odds.** Only `dead_team`, `last_three` and `promoted_struggling` qualify, because h2h, low-goal and home
  mismatch are not resolved on discovery alone there. The 2+ group: n = 162, 19.8% vs 21.7%, -2.0 [-7.7, +3.8].
  **Fails.**

**Do signals add up?**
- The table compares the realised excess with the sum of the single-signal excesses (JSON `stacking.additive`):

  | route | 2 boosts | 3+ boosts |
  |---|---|---|
  | no-odds | 2.4 of 3.3 | 4.4 of 5.8 |
  | odds | 3.7 of 5.1 | 5.4 of 7.9 |
  | after Surge | 3.2 of 3.5 | 2.3 of 5.4 |

- **Partly additive, about 70-75%.** On the Surge curves, 3+ saturates.
- **Pairs that reinforce** (no-odds; odds similar): `dead_team` + `top_vs_bottom` +7.0 (additive 3.5, n = 564);
  `top_vs_bottom` + `promoted_struggling` +6.4 (n = 346); `h2h_above` + `home_mismatch` +6.4 (n = 389).
- **Pairs that cancel:** `last_three` + `top_vs_bottom` -1.7 (n = 222); `last_three` + `promoted_struggling` -7.3
  (n = 101). The same story as the title-race damper: late matches with stakes for both sides do not surge, late
  matches without stakes do.

**By phase, 2+ group (excess):**

| phase | no-odds | odds | after Surge |
|---|---|---|---|
| early | +6.6 (n = 316) | +9.2 (n = 133) | +10.1 (n = 124) |
| middle | +1.1 [-2.2, +4.3] (n = 637), confirmation -3.2 | +1.2 [-3.1, +5.4] | -0.3 |
| late | +3.7 [+1.6, +5.9] | +3.1 | +2.6 |
| run-in | +2.3 [+0.7, +4.0] | +4.5 [+2.7, +6.2] | +3.4 |

A pre-listed variant "2+ outside mid-season" is a little stronger everywhere:
- no-odds +3.1, confirmation +2.6 [+0.5, +4.6];
- odds +4.3.
It is not adopted: the mid-season weakness was seen in a first look at this table before the variant was listed.

## 5. 70%+ calls of P(total >= 24), by number of boosts

| boosts | no-odds: n, hit vs stated | odds (production) | odds after Surge |
|---|---|---|---|
| 0 | 4,240: 73.8 vs 76.1, **-2.3 [-3.7, -0.9]** (6 of 7 seasons short) | 5,021: 75.0 vs 76.4, -1.4 [-2.6, -0.2] | 5,101: 75.3 vs 76.4, -1.1 [-2.3, +0.1] |
| 1 | 2,134: 76.6 vs 76.7 | 1,265: 75.7 vs 75.7 | 1,430: 75.4 vs 76.1 |
| 2+ | 599: 75.6 vs 76.6, -0.9 [-4.5, +2.4] | 305: 80.3 vs 76.2, +4.2 [-0.2, +8.4]; **strict test 84.3 vs 76.8, +7.6 [+3.2, +12.3]** (n = 185) | 298: 79.2 vs 76.0, +3.2 [-1.5, +7.7]; strict test 80.6 vs 76.8 (n = 191) |
| all | 6,973: 74.8 vs 76.3, -1.5 [-2.6, -0.5] | 6,591: 75.4 vs 76.3 | 6,829: 75.5 vs 76.3 |

On the odds route, 2+ boosts raise the hit rate of our confident calls. On the no-odds route they do not.

## 6. Recommendation and rule

- **Catalogue (done, `src/shots/evaluation/surge.py`).**
  - New `Signal`s:
    - `top_vs_bottom` (boost);
    - `promoted_struggling` (boost);
    - `title_race` (damper, `HELD_BACK`).
  - They carry `source="table"`, and their numbers come from this report's JSON.
  - `SEASON_END` is unchanged; the new signals are not season-end signals.
- **Stacking table (done).** `python -m shots.evaluation.surge table` now also writes `surge_signals.json` keys
  `table_report` and `stacking`. Both are additive; every earlier key and signal entry is byte-identical.
  - `stacking.routes.<route>.by_count` holds "0", "1", "2", "3+" and "2+", each with n, over_rate, stated_rate,
    excess_ci, seasons_held and seasons.
  - The same route block also holds `by_phase`, `season_phase`, `share`, `calls_70` and `signals` (the boosts
    counted).
  - `strong_applies` is **true on odds and false on no-odds**. The rule is: the 2+ CI is above 0, held in >= 5
    seasons, the confirmation excess is > 0, and the strict-test excess is > 0.
- **"Strong surge candidate" rule.** `surge_rules.strong(n_boosts)`, meaning 2 or more applying boosts. Show it only
  where `table["stacking"]["routes"][route]["strong_applies"]` is true.
- **Catalogue version.** The content changed, so the catalogue version (`table_version`) changes too. Board runs
  recorded with the old version publish without labels until the next board build.

## 7. Hooking it into `predict/surge.py` (not edited here; another agent owns it)

1. Add the promotion columns to the form columns, so `with_form` brings them in. `recent_form.family_form` already
   computes `form_promo`:
   `FORM_COLS = (..., "home_form_promo", "away_form_promo")`.
2. In `inputs(feats, expected, dates)`, append the table inputs (same index):
   ```python
   from shots.evaluation import surge_rules as sr
   x = pd.DataFrame({...existing...}, index=expected.index)
   return x.join(sr.table_inputs(f))   # f = feats.reindex(expected.index); ctx_* come from the candidate build
   ```
   `sv.flags` then sets `top_vs_bottom` / `promoted_struggling` / `title_race`; `sv.assign` is unchanged. Without
   step 2 the new columns are all False, so today's labels are unchanged.
3. Strong label (optional): per fixture `strong = sr.strong(len(entry["signals"]))[()] and
   table["stacking"]["routes"][route]["strong_applies"]`. Store it next to the labels. It is the export's call
   whether to publish it.
4. **Phase definitions differ.** `predict/surge.season_phase` (the new `context.phase`) returns None for playoff
   formats. `surge_rules.season_phase` (the research and the signal rule) uses the regular-season progress there,
   with playoff rounds counted as late. Elsewhere the two agree: same boundaries 0.25 / 2/3 / 6 games left.
   - For a consistent page, use `sr.phase_of(sr.table_inputs(f))` for the context, or accept that Belgium / Greece
     show no phase while their table signals can still fire.

## Watch items

- **No-odds 70%+ calls without any boost signal are over-stated:** 73.8% vs 76.1%, short in 6 of 7 seasons. The
  no-odds calls overall are 74.8% vs 76.3%. This is outside Surge Watch, and it is worth a look on the track record.
- **No-odds "strong" label:** re-test when 2026/27 is complete; the h2h and low-goal signals may then resolve on
  the discovery period.
- **Title race:** re-test with more seasons (n = 215 so far) before applying it.

## Files

- `src/shots/evaluation/surge_rules.py` (new):
  - phase;
  - table inputs;
  - the six families;
  - the live definitions `SELECTED`;
  - `stack_group`, `strong`.
- `src/shots/evaluation/surge_table.py` (new): the study (`python -m shots.evaluation.surge_table study`).
- `src/shots/evaluation/surge.py`:
  - the new signals, `TABLE_SIGNALS`, `HELD_BACK["title_race"]`;
  - optional table inputs in `flags` and `research_inputs`;
  - `build_table(report, cuts, table_report=None)`, `stacking_table`;
  - `cut_points` re-checks the table signal counts;
  - `table --table-report`.
- `src/shots/evaluation/surge_signals.json`: regenerated with `--preds`; every segment count re-derived by the live
  rule.
- `tests/test_surge_rules.py` (new).
