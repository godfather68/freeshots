# Surge adopted on the odds route, except Scotland Championship (2026-10-27)

Author: modeler.

**Decision (user, 2026-10-27):** "Keep production for Scotland Championship, adopt Surge for the rest."

**Evidence:**
- `reports/2026-10-26_overshoot_patterns.md` (research);
- `reports/2026-10-27_surge_calibration.md` (the damped model);
- `reports/2026-10-27_surge_verification.md` (independent verification: "adopt on the odds route, with
  conditions").

Nothing is committed or pushed; the coordinator reviews and publishes.

## What changed for users

- **Fixtures with bookmaker odds** (domestic, published) are scored by **Surge** (`cand_odds_surge`), except
  Scotland Championship. Surge is the previous odds model plus an out-of-sample correction of the total, damped to
  40%.
- **Model label** on the board, the picks and the database: `cand_odds_surge-odds_<training date>`.
- **Expected value** (median) moves by ±1 on a few fixtures. P(24+) moves by -0.01 to +0.02.
- **"Why this number"** gains a factor "Surge correction" on Surge-scored fixtures only. Its detail lists the
  signals behind it, for example "high-goal run -0.9; momentum -0.1; ratings +0.1".
- **Unchanged:**
  - Scotland Championship odds fixtures stay on `negbin_pit_comp`;
  - fixtures without odds (fin_c);
  - cross-border fixtures (experimental route);
  - settlement and the track record rules.
- **Surge Watch** no longer labels Surge-scored odds fixtures, to avoid double counting. See "Surge Watch" below.

## Exceptions recorded

1. **Protocol rule 4 (both variants): odds route only.**
   - The no-odds route keeps fin_c. The out-of-sample correction gave it no resolved gain (`cand_fin_s`: +0.0008,
     n.s.).
   - Same kind of exception as fin_c's, which was adopted on the no-odds route only.
2. **Ship rule (CLAUDE.md rule 3), per-competition coverage: Scotland Championship is excluded from Surge.**
   - At 90%, its pooled monthly coverage is 89.5% for Surge and 89.6% for production. The strict rule fails for
     both.
   - Surge is also flagged worse there in 2025/26 (RPS +0.018 and pinball +0.017, CIs above 0, n = 180).
   - Its confirmation pinball is above `team_avg` there.
   - The competition stays on `negbin_pit_comp` (`config.SURGE_EXCLUDED_COMPETITIONS`). **It is added to the
     watch list** with Turkey, Portugal and the Scottish leagues at 90%.

## Implementation

- **`src/shots/config.py`:**
  - `ODDS_MODEL = "cand_odds_surge"`, with the comment "adopted 2026-10-27 (user approval); was negbin_pit_comp";
  - `SURGE_EXCLUDED_COMPETITIONS = ("Scotland Championship",)`;
  - `odds_excluded_competitions()`, which is empty unless the odds model is a candidate;
  - `SHOTS_ODDS_MODEL` still overrides: `negbin_pit_comp` gives the old routing, and then no exclusion applies.
- **`src/shots/predict/scan.py`:**
  - Excluded-competition odds fixtures are scored by `negbin_pit_comp` by design. They are counted as `n_excluded`
    in `board.meta.json["odds_route"]`, never as fallbacks, so they never fail the run.
  - The fallback rule is unchanged: a Surge fixture missing from the candidate build goes to `negbin_pit_comp`,
    and the step fails above 2% of non-cold-start odds fixtures.
  - When only excluded competitions have odds fixtures, no candidate build is made.
  - One candidate build (families A/E/G + recent form) serves fin_c, Surge and Surge Watch.
- **`src/shots/predict/board.py`:**
  - `board.meta.json["odds_route"]["shadow"]` = production's P(total >= 24) on every published Surge-scored
    fixture (`shadow_production`). It is the same rows, moment and model as the replaced numbers, so the monitor
    compares like with like.
  - It is board meta only: never in a prediction row, never exported (checked: no `shadow` or `odds_route` key in
    the export).
- **Explanation factor `surge`, label "Surge correction".**
  - Emitted only by Surge, so only on Surge-scored fixtures.
  - Listed in `docs/static-api.schema.json` (enum) and `docs/static-api.md` (factor table, updated for the
    adoption).
  - The export verifier passes on a full export of the dry run: 0 problems, 482 explanations checked.
- **Tests:**
  - `tests/conftest.py` pins `SHOTS_ODDS_MODEL=negbin_pit_comp` with the other production switches.
  - `tests/test_odds_candidate_route.py` covers: the adopted defaults; the env override; routing to Surge; the
    excluded competition staying on production and not being a fallback; an excluded-only window with no build;
    missing rows falling back and failing loudly; a failed build; the shadow numbers covering Surge-scored
    fixtures only.
  - `tests/test_predict_surge.py` covers the no-double-counting rule.

## Surge Watch (verification condition 3)

- **Rule** (explicit in `src/shots/predict/surge.py` and `docs/static-api.md`):
  - an odds fixture is assessed only when it was scored by `negbin_pit_comp`, that is Scotland Championship, or a
    Surge fallback;
  - a Surge-scored odds fixture is never labelled;
  - no-odds fixtures are assessed as before (fin_c).
- **Why.** The catalogue's odds-route excesses describe production's residuals, and Surge already removes part of
  them.
- **Mechanism.** The existing model-label test (`negbin_pit_comp_` prefix) already enforces the rule; it is now
  documented and tested.
- **Frontend.** One sentence was added to the caveat of `frontend/src/pages/SurgePage.tsx`: "Matches with bookmaker
  odds are already adjusted by our Surge model, so they are only labelled here when that model is not used."
  Frontend lint, tests (147) and build pass.

## Monitoring (verification condition 4) and the revert rule

`python -m shots.surge_monitor [--months 12] [--as-of YYYY-MM-DD] [--out-dir reports/surge_monitor]`.
It is not scheduled; run it monthly, after the month's results are in. It lives at `shots.surge_monitor`, not
`shots.evaluation.surge_monitor` as first asked: it reads settled outcomes from the database, which may include
provisional results, and the modelling packages must not touch the database (`tests/test_db_provisional.py`).

- **Population.** Settled official live picks at N = 24 (`v_official_picks`, pre-match) whose prediction was made
  by Surge.
- **Shadow.** Production's P(24+) for the same match, from the pre-match board run closest to the pick's scan
  (`odds_route.shadow`). Causal: both numbers were stored before kick-off.
- **Reported:**
  - 70%+ calls of Surge and of the shadow on the same matches: count, mean stated, hit rate;
  - the hit-rate gap with a day-block bootstrap CI;
  - the 0.8-0.9 band, stated vs realised, for both;
  - the odds-route fallback share (mean and max over board runs).
- **Output.** Writes `reports/surge_monitor/<YYYY-MM>.md`.
- **Revert rule.**
  - Over the last 12 months, with **at least 300 70%+ calls each** for Surge and the shadow: if Surge's hit rate
    trails the shadow's by **more than 1.5pp**, the report says **REVERT** and the command exits with status 2.
  - Fewer calls: "NOT ENOUGH CALLS", exit 0.
  - Why 300: a 75% hit rate has a 2.5pp standard error at n = 300. The paired gap is much tighter, because the two
    models call about 90% the same matches. The live record makes about 25 to 30 odds-route 70%+ calls per board
    window, so expect the first decision after several months.
- **Also watch** (not automatic):
  - the 0.8-0.9 band realised more than 2pp below stated;
  - the fallback share near 2% (the update already fails above it);
  - Scotland Championship, Turkey, Portugal and the Scottish leagues at 90%.

**Revert procedure** (user approval needed, as for any switch):
1. Set `ODDS_MODEL = "negbin_pit_comp"` in `src/shots/config.py`, or set `SHOTS_ODDS_MODEL=negbin_pit_comp` for the
   scheduled job as an immediate stopgap.
2. Run `python -m shots update`. The odds route goes back to the saved `negbin_pit_comp` model (it is still
   retrained monthly, because the excluded competition and the fallback use it). Surge Watch then assesses odds
   fixtures again automatically (label rule).
3. No data migration is needed. Predictions already published stay in the append-only record with their Surge label.

## Dry run (scratch copy; the real `data/shots.db` and `data/processed` were not touched)

**Setup.**
- A full copy of the project in the scratchpad, `PYTHONPATH` pointed at it, `SHOTS_DB` set to its copied DB, and
  an export to a scratch site directory.
- Command: `python -m shots update --days 30 --export-dir <scratch>/site`, with the adopted defaults.

**Outcome.**
- Exit 0, every step OK. Surge trained on 95,274 rows in about 30 s, saved as `cand_odds_surge-odds_2026-10-26`
  in the copy's `models_store`.
- **Fallback: 0 of 45 odds fixtures** (share 0.0, limit 0.02). One Scotland Championship odds fixture was excluded
  and scored by `negbin_pit_comp_2026-10-05`.
- **Published board:**
  - 34 odds fixtures: 33 Surge, 1 production (Scotland Championship).
  - **70%+ P(24+) calls: 28 with Surge vs 25 for production's shadow on the same 33 fixtures. 3 calls change**,
    all three added by Surge. P(24+) moves by -0.010 to +0.024.
  - 26 of 33 Surge explanations carry the "Surge correction" factor.
- **Surge Watch:** 361 fixtures assessed. The only odds fixture among them is the Scotland Championship one,
  scored by `negbin_pit_comp`.
- **Export verifier:** ok, 0 problems.
- **Monitor on the copy:** "NOT ENOUGH CALLS" (no Surge picks settled yet), report written, exit 0.

## Proposed CLAUDE.md rule 5 text

> 5. **Production models (switches in `src/shots/config.py`; env `SHOTS_ODDS_MODEL` / `SHOTS_NOODDS_MODEL` /
> `SHOTS_MEDIAN_FROM_CURVE` override, tests pin the old values in `tests/conftest.py`):** odds route **"Surge"**
> `cand_odds_surge` (production NB + out-of-sample correction of the total, damped to 40%; adopted 2026-10-27),
> except the competitions in `SURGE_EXCLUDED_COMPETITIONS` (Scotland Championship), which stay on `negbin_pit_comp`
> (not fallbacks); Surge fixtures whose candidate inputs are missing fall back to `negbin_pit_comp`, and the run
> fails if > 2% fall back. No-odds route `fin_c` (production NB + EKF opponent-adjusted ratings, head-to-head,
> season context, shrinkage toward league level, per-competition dispersion; falls back to `negbin_pit_comp` per
> fixture when candidate inputs are missing, and fails the run if > 2% fall back); cross-border (UCL/UEL/UECL)
> stays on `negbin_pit_comp`. Published `expected` = median of the published curve. **Recorded exceptions (user):**
> fin_c adopted on the no-odds route only (2026-10-20); Surge adopted on the odds route only and not for Scotland
> Championship, which fails the 90% cell for both models and where Surge was worse in 2025/26 (2026-10-27). Surge
> Watch labels odds fixtures only when Surge is not used (no double counting). **Monitor Surge monthly** with
> `python -m shots.surge_monitor` (vs production's shadow P(24+) stored in `board.meta.json`); revert
> (`ODDS_MODEL = "negbin_pit_comp"`) when its 70%+ calls trail the shadow by > 1.5pp over 12 months with >= 300
> calls each. Watch Turkey, Portugal and the Scottish leagues (Scotland Championship included) at 90% coverage.
> Reports: `reports/2026-10-15_model_candidates.md`, `2026-10-16_fin_c_verification.md`,
> `2026-10-16_fin_c_adoption.md`, `2026-10-27_surge_calibration.md`, `2026-10-27_surge_verification.md`,
> `2026-10-27_surge_adoption.md`.

## Open items (not blocking)

- The candidate features can be rebuilt more than once per board run, when Surge Watch asks for a wider fixture set
  (verification side effect 2). This is correct but costs time.
- Verification caveat 5: public copy should not present Surge mainly as an "over-shoot signals" model. About half of
  the correction is a pull toward the rating expectation, and season end is the second part. The factor label
  "Surge correction" and the per-signal detail reflect that.
