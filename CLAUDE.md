# Shots — football total-shots lower-bound predictor

## Goal
For a given football match (any league / competition we have data for), predict a
**calibrated lower bound** on the number of **total shots** (on + off target, blocked included):
"With confidence q, the match will have at least N shots." We also predict per-team shots,
since the match total is built from home + away.

The LLM builds the pipeline; **predictions come from statistical models**, never from an LLM.

## Data sources
- **Bootstrap (now):** football-data.co.uk CSVs (columns `HS`, `AS`, `HST`, `AST`, plus odds).
  Free downloads meant for this use. Covers ~22 main leagues; "extra" leagues and cups have no shots.
- **Optional:** StatsBomb open data (event-level, a few competitions), for validation only.
- **Target (later):** Opta feed (contract pending). Every source is an adapter that outputs the
  canonical schema below, so swapping in Opta must not touch features/models.
- Respect site terms and rate limits. No scraping of sites whose terms forbid it.

## Canonical match schema (`data/processed/matches.parquet`)
`match_id, source, competition, country, tier, season, date (UTC date), home_team, away_team, home_team_id, away_team_id,
home_shots, away_shots, home_sot, away_sot, home_goals, away_goals,
odds_home, odds_draw, odds_away, odds_over25, odds_under25`
- Odds are **pre-match** market averages only, never closing odds.
- Defined in `src/shots/ingest/schema.py`.
- Build: `python -m shots.ingest.football_data` (add `--refresh-current` to re-pull the live season).
- Report: `python -m shots.ingest.quality` → `reports/`.
- `*_team_id` = `<country>:<slug of normalized name>` (stable key; features group by it, not by name).
- Team names normalized through `src/shots/ingest/team_aliases.csv`.
- Missing shot data → row dropped from training, never imputed as 0.

## Layout
- `src/shots/ingest/` one module per source → canonical schema
- `src/shots/features/` pre-match features only (rolling team attack/defence shot rates, home/away, rest days, league level, odds-implied strength)
- `src/shots/models/` count models (Poisson / negative binomial baseline, quantile GBM)
- `src/shots/evaluation/` backtests and calibration reports
- `data/raw/` untouched downloads (git-ignored), `data/processed/` canonical outputs (git-ignored)

## Non-negotiable rules
1. **No leakage.** A feature for match M may only use matches with date strictly before M's date.
2. **Time-based splits only** (train on past seasons, test on later ones). No random K-fold.
3. **Primary metrics:** coverage of the lower bound and pinball (quantile) loss. Also report MAE of
   the median vs. a naive "league average" baseline.
   **Ship rule (rounding-aware, decided 2026-10-08):** bounds are integers, so the pinball-optimal
   bound covers slightly more than q. A model ships when coverage ≥ q overall and in every
   competition, mid-coverage (avg of P(y ≥ N) and P(y > N)) is within ±2pp of q, and pinball is no
   worse than `team_avg`. Do not force exact-q coverage at the cost of pinball.
4. Every model change ships with a backtest report in `reports/`.

## Commands
- Install: `python -m pip install -r requirements.txt`
- **Routine update (one command):** `python -m shots update [--days 30]`: refresh results/odds → calendars (if
  changed) → save picks → rebuild UI board → settle track record. Steps continue past failures; exit code 1 if
  any failed. Scheduled via `scripts/scheduled_scan.ps1` (Tue/Fri 09:00, when football-data posts odds).
- Tests: `python -m pytest -q`
- Season calendars: `python -m shots.ingest.calendar_ics` parses `calendars/*.ics` →
  `data/processed/fixtures.parquet` + `fixtures.csv` (`--suggest` proposes aliases for new team names;
  review them, then add to `team_aliases.csv` with source `calendar`).
- Scan upcoming fixtures: `python -m shots.predict.scan [--shots 24] [--min-prob 0.7] [--days 7 | --season] [--all]`
  (replay a past round against real results: `--as-of YYYY-MM-DD`). Output → `reports/picks/`.
  Fixtures come from the calendars merged with football-data's fixtures.csv (which adds odds).
  Fixtures without odds are scored by a separate no-odds model; never feed missing odds to the odds model.
- Build the UI board: `python -m shots.predict.board [--days 30 | --season] [--no-refresh]` →
  `data/processed/board.parquet` (+ `board.meta.json`): every upcoming fixture with median, lb_70/80/90, the
  full curve P(total >= n), n = 10..45 (`p_10`..`p_45`) and experimental per-team columns. Same routing as scan.
- **Database (system of record for the app):** SQLite `data/shots.db` (`SHOTS_DB` overrides), SQLAlchemy +
  Alembic. `python -m shots.db upgrade | current | import [--full] | status`. `python -m shots update` loads
  results, fixtures, board and picks into it and records each run in `job_runs`. Predictions are append-only
  (SQLite triggers); outcomes are derived views, never stored. Schema, repo API and invariants: `docs/platform.md`.
- **Stage 1 public site (GitHub Pages + Actions, repo `godfather68/freeshots`, branch `main`):**
  `.github/workflows/update.yml` runs every 3 h: restore state from the Actions cache →
  `python -m shots.worker run-once --force` → `python -m shots.export --out <dir>` (JSON per
  `docs/static-api.md`, checked by `python -m shots.export.verify`) → build `frontend/` (Vite + React,
  `npm run lint | test | build`) → deploy to Pages and commit the JSON to the `data` branch (public audit
  trail). No accounts in Stage 1. **Never publish raw source data, odds, the DB or scratch files**; the
  public branch is `main`, local `master` holds the older detailed history and is not pushed. Details:
  `docs/platform.md` "Stage 1".
- **UEFA Champions League (experimental, added 2026-10-13):** fixtures from
  `calendars/uefa-champions-league_2026-27.ics`; clubs map to their domestic team ids through
  `src/shots/ingest/club_countries.csv`; predictions use each club's domestic form through the no-odds model
  (`...-ucl-exp`), flagged `experimental` with `data_quality` full/partial/none. There is no UCL history, no
  calibration evidence and no settlement source, so these never count in the official track record
  (`v_experimental_picks`). New clubs or matchdays: extend the `.ics` and the mapping. Report:
  `reports/2026-10-13_champions_league.md`.
- Web UI (local, server-side rendered): `python -m shots.web` → http://127.0.0.1:8000. Reads the DB through
  `shots.db.repo.Repository` only (never runs a model, no SQL in routes). Access control is by entitlement,
  enforced in the repository queries. Preview config: `.claude/launch.json` (`shots-web`).
