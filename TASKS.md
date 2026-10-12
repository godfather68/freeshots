# Task plan

Phases run in order; tasks inside a phase marked ∥ can run in parallel.

## Phase 1: Data (agent: data-ingest, Sonnet)
- [x] 1.1 football-data.co.uk adapter: main leagues, seasons 2010/11 → current, canonical schema
- [x] 1.2 ∥ team-name alias table + unmapped-name report
- [x] 1.3 ∥ StatsBomb open-data adapter (validation only: do their shot counts agree?)
- [x] 1.4 Data-quality report: coverage by league/season, missing shots, duplicates

## Phase 2: Features (agent: feature-engineer, Sonnet)
- [x] 2.1 Rolling shots for/against (last 5/10/season), home/away splits, exponentially weighted
- [x] 2.2 ∥ Context: rest days, league level, promoted/new-team flags, pre-match odds-implied strength
- [x] 2.3 Leakage tests

## Phase 3: Models (agent: modeler, Opus)
- [x] 3.1 Naive baselines (league average, team average)
- [x] 3.2 Negative binomial GLM → distribution of total shots → lower quantiles
- [x] 3.3 Quantile LightGBM + calibration (conformal) on a held-out later period

## Phase 4: Evaluation (agent: evaluator, Opus), runs after every Phase 2/3 change
- [x] 4.1 Walk-forward backtest harness
- [x] 4.2 Coverage + pinball report per league / season, verdict

## Phase 5: Productization (later)
- [ ] 5.1 `predict(home, away, competition, date)` CLI / API returning bounds at q = 0.7/0.8/0.9
- [ ] 5.2 Opta adapter once the contract lands (same canonical schema; re-run Phase 4)
- [ ] 5.3 Extra competitions (cups, Europe, smaller leagues) as data allows

## Phase 3b: Model iteration (from 2026-10-08 backtest verdict: not ready to ship)
- [x] 3b.1 negbin: conformal shift fitted on latest training season (currently ~3 pts over-covered, bounds ~0.5 shot too low)
- [x] 3b.2 negbin + qgbm blend
- [x] 3b.3 Per-competition calibration check (qgbm under-covers in Greece, Scotland Championship)
- [x] 3b.4 Backtest with `--refit month` (matches production cadence) → ship decision (negbin_pit_comp: 1 of 63 checks short, see reports/2026-10-08_ship_decision.md)
- [x] 3b.5 Persist trained models with feature list, training window, metrics
- [x] 3b.6 Key teams on (country, team) to avoid future name collisions; test rejecting closing-odds columns
