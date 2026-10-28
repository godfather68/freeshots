# Platform layer: database, repository, loaders

The SQLite database `data/shots.db` (override with `SHOTS_DB`) is the **system of record for everything
the web app shows**. Parquet/CSV files stay as the modelling cache: the pipeline keeps writing them, and
loaders copy them into the DB. The web app reads the DB only, through `shots.db.repo.Repository`.

```
python -m shots.db upgrade          # create / migrate (idempotent)
python -m shots.db current          # schema revision vs head
python -m shots.db import [--full]  # load results, fixtures, current board, picks CSVs, backfill (idempotent)
python -m shots.db status           # counts + freshness
python -m shots update              # whole pipeline; its last 3 steps write to the DB and it records a job_run
```

Code: `src/shots/db/` (`engine.py`, `models.py`, `migrate.py`, `load.py`, `repo.py`, `access.py`, `jobs.py`,
`text.py`), migrations in `migrations/` (`alembic -c migrations/alembic.ini ...` also works).

## Connection settings

Every connection: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=10000`, `synchronous=NORMAL`,
`cache_size=64MB`. WAL lets the web app read while `shots update` writes.

## Schema (revisions `0001_initial`, `0002_pre_match`, `0003_run_integrity`, `0004_experimental`, `0005_explain`, `0006_manual_results`, `0007_provisional_results`)

Timestamps named `*_at` are **UTC** (naive). `matches.date` is the local match date and
`kickoff_local` the local "HH:MM", exactly as in the canonical schema. Board `meta.built_at` (and picks
`params.scan_ts`) keep the pipeline's local wall-clock strings for display.

### Football data

| table | key | notes |
|---|---|---|
| `competitions` | `id` | `name` unique (canonical `competition`, e.g. "Spain La Liga"), `country`, `tier`, `code` (football-data division) |
| `teams` | `team_id` | canonical `<country>:<slug>`; `name` = latest normalized name |
| `matches` | `match_id` | **one table for played and upcoming matches** (same canonical key; see below). Canonical columns + `kickoff_local`, `kickoff_utc` (calendar fixtures), `round`, `status`, `search_key` |

`matches.status`: `played` (row from `matches.parquet`, or a manual result with `source = 'manual'`, see
"Manual results", or a provisional result with `source = 'provisional'` until football-data publishes the
match, see "Provisional results"), `scheduled` (listed by a fixture
source), `unlisted` (was scheduled, no fixture source lists it any more: postponed/moved; set back to
`scheduled` if it reappears).

Why one `matches` table instead of `matches` + `fixtures`: fixtures and results share the canonical
`match_id`, predictions need one foreign key target, outcomes are a simple join, and a fixture becoming a
result is an update of `status` + result columns instead of moving rows. Rules enforced by the loaders:
a fixture source never overwrites a played match; a missing kick-off/round/odds never erases a known one.

### Predictions

| table | notes |
|---|---|
| `prediction_runs` | one batch: `kind` (`board` / `picks` / `backfill`), `source_key` (unique, makes imports idempotent: `board:<built_at>`, `picks:<file name>`, `backfill:<content hash>`), `built_at`, `confirmed_at`, `results_up_to`, `window_from`, `window_days`, `models` (JSON list of model labels), `params` (JSON: picks `n`, `min_prob`, `source` live/replay, `scan_ts`, `file`), `meta` (board.meta.json verbatim), curve ranges `curve_n_min/max`, `team_n_min/max`, `n_predictions`, `content_hash`, `status` |
| `predictions` | one match in one run (unique `run_id, match_id`): `model`, `odds_available`, `cold_start`, `median` (= board `expected_shots`), `lb_70/80/90`, `league_avg`, `above_league`, `mkt_p_over25`, `total_curve`, `points`, `selected` (picks), `home_/away_` `mean, median, lb_70, lb_80, lb_90`, `home_curve`, `away_curve` |

`predictions.explain` (migration `0005_explain`, JSON, NULL): board rows only. The additive decomposition
of the expected total ("why"), computed by `shots.models.explain` when the board is built, from the same
fitted model and feature row as the prediction, and stored with it (append-only like every other column).
It is persisted rather than recomputed at export because the exporter has neither models nor features, and
a later recomputation would use newer results or a refitted model, i.e. explain a different prediction. It
is part of the board's content hash only when the board has the column, holds no odds values, and no view
reads it. Contract: `docs/static-api.md`, "`explain`".

**Surge Watch labels** (no migration): `prediction_runs.meta["surge"]` of a board run =
`{"version": <catalogue hash>, "fixtures": {match_id: {"signals": [...], "against": [...]}}}`, written by the
board build (`shots.predict.surge`) into `board.meta.json` and stored with the run like the rest of the meta.
They are labels about the fixture (which over-shoot patterns of `reports/2026-10-26_overshoot_patterns.md` it
matches before kick-off), not predictions: no prediction row is touched, so the append-only rules are unchanged.
An identical rebuild refreshes them with the meta (`import_board`, "unchanged"). The catalogue (research
numbers per signal and route, cut points) is the committed `src/shots/evaluation/surge_signals.json`, generated
from the report JSON by `python -m shots.evaluation.surge table`; `Repository.board_surge()` reads the labels.
The same block holds `"context"` (2026-10-28): `{match_id: {"phase", "home": {pos, of, pts, gp, left, to_top,
to_safety}, "away": {...}}}` for the assessed fixtures, the league table before the match and the season phase
(`shots.predict.surge.table_context` / `season_phase`, from the candidate build's season-context family; published
as the fixture's `context`). `Repository.board_context()` reads it, `Repository.run_surge_labels(run_ids)` the labels
of any board runs (used for the labels of settled results: the run a result's prediction came from).

Curves: `total_curve[i] = P(total >= curve_n_min + i)` (board: n = 10..45),
`home_curve[i] / away_curve[i] = P(team >= team_n_min + i)` (n = 3..25). Sparse probabilities (picks:
one N; backfill: 22/24/26) are a JSON object `points = {"24": 0.71}`.

### Outcomes (views, not copies)

Results get corrected upstream, so outcomes are derived from the current `matches` row, never stored:

- `v_prediction_outcomes(prediction_id, run_id, kind, match_id, date, competition, status, actual_total, actual_home, actual_away)`;
  `status` = `pending` (not played) / `void` (played, no shots data) / `settled`.
- `v_prediction_points(prediction_id, run_id, kind, match_id, n, p)`: `points` as rows.
- `v_official_picks(..., n, p, scan_ts, source, status, actual_total, hit)`: the live track record. Per
  (match, N) the **last live picks scan taken on or before the match date that is pre-match** (made and
  inserted before kick-off; see "Pre-match integrity"). Replays are excluded (since `0003`).
  Equal to `track.settle` in tests whenever no replay is the latest scan (the file record still lists
  labelled replays).
- `v_replay_picks(...)`: the same columns for replay files (`replay_*.csv`, `params.source = 'replay'`):
  after-the-fact re-runs of past rounds, queryable separately, never official.
- `v_experimental_picks(...)` (`0004`): the official rule applied to experimental predictions; settled by
  manual results (`0006`), never part of the official record.

### Operations and accounts

- `job_runs(job, started_at, finished_at, status running|ok|failed|partial, counts JSON, error, host, pid)`:
  `python -m shots update` writes one row per run (`counts.steps` = step -> success, plus per-step loader counts).
- Auth-ready, **no logic yet**: `users` (normalized unique email, Argon2id `password_hash`, `is_active`,
  `is_admin`, `email_verified_at`, `locked_until`), `plans` (`key`, `name`, `is_default`, `is_active`),
  `plan_entitlements(plan_id, key, value JSON)`, `subscriptions` (user, plan, status, period, provider ids
  nullable for a future payment provider), `sessions` (`id_hash` = SHA-256 of the cookie id, `user_id`
  nullable for anonymous CSRF sessions, `csrf_hash`, `created_at`, `last_seen_at` (idle expiry),
  `expires_at` (absolute), `revoked_at`, `ip_hash`, `ua_hash`), `login_attempts(kind, account_hash,
  client_hash, success, at)` for rate limiting/lockout. Raw ids, emails or IPs are never needed in these
  lookup tables.

Seeded placeholder plans (data, change by migration or admin tooling, never by code):

| key | can_view_all_leagues | allowed_leagues | max_days_ahead | team_predictions | export | track_detail |
|---|---|---|---|---|---|---|
| free (default for new users) | false | the top 5 leagues | 3 | false | false | false |
| pro | true | null | 30 | true | false | false |
| premium | true | null | null (whole season) | true | true | true |

## Invariants

1. **Predictions are append-only** (SQLite triggers, so they hold for any client, including a plain
   `sqlite3` shell with foreign keys off): `UPDATE predictions` always aborts; `DELETE` aborts once the
   match is locked; an `INSERT` whose `id` or `(run_id, match_id)` already exists aborts, so
   `INSERT OR REPLACE` / `REPLACE` / UPSERT / `INSERT OR IGNORE` cannot rewrite a row (`0003`).
   `prediction_runs`: a run that has predictions cannot be deleted; `id, kind, source_key, built_at,
   params, models, content_hash, created_at` can never change (`confirmed_at`, `meta`, `n_predictions`,
   `status` stay writable for the loaders). `predictions.created_at` must equal the DB clock at insert
   (+-5 min). Lock moment = `kickoff_utc` if known, else the match date at 00:00 UTC (before any
   European kick-off); a `played` match is always locked. A new run inserts new rows; history is never
   rewritten. `load.delete_run` exists only to purge a broken import before any of its matches kicks
   off (it deletes the predictions first, then the now empty run).
2. Every loader is idempotent (`source_key`, change-detecting upserts). An identical board rebuild only
   bumps the latest board run's `confirmed_at` and `meta` (no duplicate 600 rows).
3. Schema changes only through Alembic migrations; `tests/test_db_migrations.py` fails if the ORM
   models and the migrations disagree.
4. Access control is by **entitlement**, enforced inside repository queries (`shots.db.access.Entitlements`;
   unknown/missing entitlement keys deny). Code never checks a plan key.
5. The web app never writes football or prediction data and never creates the DB file (missing file ->
   "No board yet" page). Its only writes are to the auth tables (`sessions`, `login_attempts`, `users`,
   `subscriptions`) through `shots.auth`; `tests/test_db_web.py` asserts that page requests and auth
   flows leave every other table untouched.

## Curve storage decision (JSON arrays)

Benchmark (`600 fixtures x 50 runs = 30,000 predictions`, total curve 36 points + two team curves of 23,
SQLite 3.50, this machine):

| layout | insert all | file size | board filter (latest run, P(total>=24) >= .7 and either team P(>=12) >= .6) | one match's curves | N=24 across all runs |
|---|---|---|---|---|---|
| JSON arrays (chosen) | 8.1 s | 20.8 MB | 8.9 ms | 0.49 ms | 547 ms |
| long table (pred, kind, n, p) | 312 s | 107.7 MB | 22.8 ms | 0.58 ms | 171 ms |
| wide columns (p_10..p_45, ph_*, pa_*) | 2.6 s | 25.0 MB | 1.4 ms | 0.50 ms | 269 ms |

JSON arrays: smallest file, fast enough for the UI's query shape (filter one run of ~600 rows by a
user-chosen N with `json_extract(curve, '$[N - n_min]')`, then read one match's whole curve), and the
N ranges live on the run, so widening a curve needs no migration. The long table is 5x larger and 40x
slower to write (82 rows per prediction) for no UI benefit; wide columns are fastest but tie the schema
to today's N ranges (82 columns, a migration per change). Cross-run analytics at one N (track-record
style) are slower with JSON, which is why the track record uses the sparse `points` + views.
On the real board (568 fixtures) every board page query runs in < 0.15 s end to end.

## Cross-border competitions (UEFA Champions League; migration `0004_experimental`)

Method, proxy validation and limits: `reports/2026-10-13_champions_league.md`. Platform side:

- **Ingest** (`shots.ingest.cross_border`, `club_countries.csv`, `calendar_ics`): a calendar file whose prefix
  is a cross-border competition (`uefa-champions-league_<season>.ics`) gives fixtures with competition
  "UEFA Champions League", country label "Europe", tier 1, and **each club's domestic team id**
  (`england:arsenal`), resolved through the reviewed `club_countries.csv` (`competition, club, country,
  history`). Clubs whose league we do not cover keep their own id (`norway:viking`) and no history; a club
  missing from the file gets `europe:<slug>`, is reported by `python -m shots.ingest.calendar_ics`
  (status `unknown`) and is scored without history until it is added. `fixtures.parquet` now also carries
  `kickoff_utc` (exact, from the calendar) and `home_country` / `away_country`; `read_fixtures` prefers that
  `kickoff_utc` (it is the only kick-off source for a competition whose "country" has no zone). Local date /
  kick-off are in the home club's zone. `teams.country` is always the club's own country.
- **Features / model**: these fixtures never enter `team_form.build_features`
  (`shots.features.cross_border` builds them from played matches), so domestic features, priors and board
  rows are identical with and without them. They are scored by `shots.models.cross_border.CrossBorderModel`
  on top of the no-odds model, saved under `models_store/cross_border/<no-odds label>-exp/`, board / picks
  label `<no-odds label>-ucl-exp`. The board and the picks CSV gain the columns `experimental` and
  `data_quality` only when such fixtures are present.
- **Database**: `predictions.experimental` (NOT NULL, default 0) and `predictions.data_quality`
  (`full | partial | none`, NULL = full), set at insert by the loaders (flagged by the row, or by its
  competition as a safety net) and immutable like every prediction column. `v_prediction_outcomes` exposes
  `experimental`; **`v_official_picks` and `v_replay_picks` exclude experimental predictions**, as do
  `Repository.official_results` and the file record (`track.dedupe`), so the live record, its counts and its
  metrics are unaffected. `v_experimental_picks` lists them apart (same columns): they stay `pending`
  until a manual result is entered (`0006`, see "Manual results"), then settle in that separate record. The migration adds the
  columns with `ALTER TABLE ADD COLUMN` (no table copy: triggers and existing rows untouched).
- **Lifecycle of an experimental fixture**: it stays `scheduled`, leaves the board the day after its date
  (board rows are `date >= today`), and becomes `played` only through a manual result. The worker's `results` reason fires once when it
  ends, as for any match.
- **Export**: fixture `experimental: true` + `data_quality`, league `featured: true` (additive, schema 1;
  `docs/static-api.md`). `shots.export.verify` checks both against the database.

## Manual results (migration `0006_manual_results`)

Competitions without an automatic results source (the UEFA Champions League: football-data.co.uk has none)
are settled from a committed, human-edited CSV per competition and season:
`results_manual/<calendar prefix>_<YYYY-YY>.csv` (e.g. `uefa-champions-league_2026-27.csv`).

- **Columns** (header required, all present, any order): `date` (YYYY-MM-DD, the match date as in our
  fixtures: venue-local, the home club's zone), `home`, `away` (calendar spellings or canonical names; both
  go through `team_aliases.csv` source `calendar`, as the calendar ingest does), `home_shots`, `away_shots`
  (total shots incl. blocked, 0..60), `home_sot`, `away_sot` (optional, both or neither, <= shots), `home_goals`,
  `away_goals` (0..20), `source` (http(s) URL the numbers were read from), `entered_at` (ISO 8601 UTC, `Z` or
  `+00:00`), `notes` (optional, e.g. a provider's definition caveat).
- **Checks** (`shots.ingest.manual_results.read_file`, then `shots.db.manual`): file name prefix must be a
  cross-border competition (a domestic league is refused); header exact; every value format / range above;
  no duplicate match, no team twice on one date, home != away; each row must resolve by canonical `match_id`
  (competition, date, normalised names) to a match of that competition in the DB, else the error lists the
  three closest candidates (`date home v away [match_id]`) and says when home / away are swapped; the
  competition must have no football-data division code and the match must not come from football-data;
  the match season must be the file's; the match must have finished (`kickoff_utc` + 105 min, unknown
  kick-off: 00:00 UTC of the date + 105 min) before `entered_at` and before now; `entered_at` not in the future
  (10 min tolerance). Any problem rejects that whole file (other valid files still load) and the update step
  `db-manual` fails, listing `file:line: problem` in `job_runs.error`.
- **Storage**: the result goes into the `matches` row (status `played`, `source = 'manual'`, shot / goal
  columns), so every outcome view settles from it. `manual_results` (one row per match) keeps what `matches`
  cannot: `source_url`, `entered_at`, `notes`, `file`, and the match's `prev_source` / `prev_status`.
  Results are **not** append-only: a corrected CSV row updates both; a removed row restores the match
  (`prev_status`, `prev_source`, empty result columns). Every insert / update / delete is recorded in
  `manual_result_edits` (`action`, `changes` = `{column: [old, new]}` of the changed columns, `file`,
  `entered_at`, `recorded_at`), append-only by trigger.
- **Guards (triggers)**: `matches.source` can become `'manual'` only in a competition without a division
  code, and never for a football-data match; `manual_results` rows only for such matches. So domestic
  results can never be overwritten by hand, and football-data's own loader never sees these matches
  (they are not in `matches.parquet`; fixture upserts never touch `played` rows).
- **Settlement**: experimental predictions settle through `v_prediction_outcomes` like any other and are
  counted by `v_experimental_picks` (live scans, pre-match, deduplicated); `v_official_picks` and
  `Repository.official_results` are unchanged (still `experimental = 0`). `Repository.base_rates` takes the
  overall share from automatic-source results only, and `load.incremental_since` ignores manual results,
  so domestic numbers do not move. The modelling pipeline never reads these results (it reads parquet).
- **Commands**: `python -m shots.db manual --check` (validate and list `line -> match_id, date, home, away,
  kickoff_utc, total`; exit 1 with every problem; writes nothing), `python -m shots.db manual` (load), and
  the update step `db-manual` (after `db`, before `db-board`). Loader counts:
  `{files, rows, inserted, updated, deleted, unchanged, fingerprint}`.
- **Worker**: reason `manual-results` = the files' fingerprint differs from `counts["db-manual"].fingerprint`
  of the last `ok` update (without one: when a file holds a data row), so a pushed CSV is picked up by the
  next scheduled slot; `results_manual/**` is also in the workflow's push paths, so a push publishes at once.
- **Export**: `results.json` lists settled experimental fixtures with `experimental: true`, every result has
  `result_source` (`football-data` | `manual`) and manual ones `source_url`; `track.json` has
  `records.experimental` and `daily` / `weekly` accuracy series (`docs/static-api.md`).

## Provisional results (migration `0007_provisional_results`)

football-data.co.uk sometimes stops publishing (no results 15-19 Oct 2026, ~170 predicted matches unsettled).
Matches of the competitions it covers may then be settled from a committed, human-edited CSV per competition
and season, **only until football-data publishes them**: `results_provisional/<league id>_<YYYY-YY>.csv`,
league id = slug of the canonical competition name (`england-premier-league_2026-27.csv`,
`germany-2-bundesliga_2026-27.csv`; the static export's league id).

- **Columns and checks**: exactly those of manual results (`shots.ingest.provisional_results.read_file` reuses
  `manual_results.parse_file`), except the file prefix must be a football-data competition
  (`shots.ingest.football_data.DIVISIONS`; a cross-border prefix is refused with a pointer to
  `results_manual/`) and the `source` URL must not be football-data itself. `shots.db.provisional` resolves
  rows like manual ones (closest candidates, swapped teams, season, finished before `entered_at`, not in the
  future), but requires the competition to HAVE a division code. Any problem rejects that file and fails the
  update step `db-provisional`.
- **Storage**: like manual results, the values go to the `matches` row (status `played`, `source =
  'provisional'`), so `v_prediction_outcomes` settles every prediction of the match, **the official live record
  included** (these are the very matches the record is about). `provisional_results` keeps the entered values,
  `source_url`, `provider` (host label: `www.espn.com` -> `espn`), `entered_at`, `notes`, `file`,
  `prev_source` / `prev_status`. Corrections update it; a removed row (or a removed file) restores a match that
  is still provisional. `provisional_result_edits` is the append-only audit (insert / update / delete as for
  manual results, plus **confirm**, see below).
- **football-data supersedes automatically**: the regular results load (`load.upsert_matches`, step `db`,
  which runs before `db-provisional`) overwrites a played row whenever anything differs, `source` included, so
  football-data's numbers replace the provisional ones the moment its CSV has the match; nothing
  provisional-specific runs. A trigger (`trg_matches_provisional_confirmed`) then appends a `confirm` audit
  row with `changes = {column: [provisional, football-data]}` for all six result columns. From then on the
  loader never writes that match again (it still records / corrects the provisional row; removing it leaves
  the confirmed match alone). Outcomes are views, so settlement follows each step. `result_status` is derived,
  never stored: `provisional` while `matches.source = 'provisional'`, else `confirmed`.
- **Provider agreement**: view `v_provisional_agreement` (per provisional row: `result_status` provisional |
  confirmed | unsettled, `prov_*` and `fd_*` shots / sot / goals, `diff_home_shots`, `diff_away_shots`,
  `diff_total` = football-data minus provider, `goals_agree`); `python -m shots.db provisional --agreement`
  prints it with a summary (`exact_total`, `mean_abs_diff_total`, `mean_diff_total`, `goals_agree`).
- **Guards (triggers)**: `matches.source = 'provisional'` only in a competition with a division code, never on
  INSERT (it only settles a fixture we already have), and never for a match already `played` from
  football-data; `provisional_results` rows only for matches of such competitions. 0006's guards are
  unchanged: `manual` stays reserved for competitions without a code.
- **Never in the modelling pipeline**: features, EKF ratings, league levels and the models read
  `matches.parquet`, built from football-data only; no modelling module imports the provisional code or the
  DB results (tested: `tests/test_db_provisional.py`, features and board byte-identical with provisional rows
  loaded). `Repository.base_rates` (Brier baseline) and `load.incremental_since` ignore provisional rows.
- **Commands**: `python -m shots.db provisional --check` (validate; list `line -> match_id, date, home, away,
  kickoff_utc, total, provider, status` (provisional | confirmed); exit 1 with every problem; writes
  nothing; works on a not-yet-migrated DB), `python -m shots.db provisional` (load), `--agreement`, and the
  update step `db-provisional` (after `db-manual`). Loader counts: `{files, rows, inserted, updated, deleted,
  unchanged, confirmed, fingerprint}`.
- **Worker / CI**: reason `provisional-results` (fingerprint vs `counts["db-provisional"].fingerprint`, as
  for manual results); `results_provisional/**` is in `update.yml`'s push paths and
  `shots.worker.ci.PIPELINE_PATHS`, so pushing a CSV publishes.
- **Export** (additive, schema 1): every `results.json` row has `result_status`; provisional rows have
  `result_source` = the provider (`espn`) and `source_url`; once confirmed, `football-data` and no URL.
  `meta.counts.results` / `.provisional_results`; `track.json` `records.live|experimental.provisional`
  (settled picks on a provisional result) and `daily` / `weekly` points `provisional` (matches).

### Results to-do (daily checklist issue, `.github/workflows/results-todo.yml`)
- Every day at noon Europe/Paris (crons 10:00 and 11:00 UTC; a guard keeps the slot at or after 12:00 Paris,
  manual dispatch takes an optional date) the workflow opens one issue "Results to enter — <date>" (label
  `results-todo`, created if missing) with up to 20 already-played matches that have a published prediction
  but no result yet. A person, or Claude in an interactive session, opens the ESPN links, types the numbers
  into `results_provisional/` (football-data leagues) or `results_manual/` (UEFA cups) and pushes: the push
  publishes (`update.yml`). No issue when nothing is missing; one per date (an existing title is skipped); open
  checklists older than 7 days are closed with a comment.
- **Nothing is fetched**: ESPN's terms forbid automated collection. `src/shots/results_todo.py` (standard
  library, no network module; `python -m shots.results_todo [--date] [--limit 20] [--data-dir] [--out]`) only
  reads our public JSON from a checkout of the `data` branch **with its history** (played matches have left
  the current board; the last `board.json` committed before kick-off is the published prediction) and builds
  links: `https://www.espn.com/soccer/schedule/_/date/YYYYMMDD/league/<slug>` (slugs:
  `src/shots/ingest/espn_leagues.csv`; unknown league = no link), then the match's team-stats page.
- Selection: match date in the 7 days before the run date, no row in `results.json`, no row already in the
  committed CSVs; official picks (P(24+) >= 0.70) first, then experimental cup picks, then the rest, by P(24+)
  then newest; leagues interleaved round-robin. The issue carries a pre-filled CSV block per target file in
  the manual-results column order, with our team names. Tests: `tests/test_results_todo.py`.

## Repository API (`shots.db.repo.Repository`)

`Repository(engine | path | URL | None)` (None = `SHOTS_DB` / `data/shots.db`). All methods take
`ent: Entitlements = FULL_ACCESS` where it matters; `today` is a `datetime.date`.

- `state() -> "ok" | "missing" | "unreadable"`; missing = no file, not migrated, or no board run.
- `board_meta() -> dict | None`: latest board run's `board.meta.json` keys (`built_at` local ISO,
  `results_up_to`, `window_days`, `window_from`, `models`, `n_fixtures`, `n_with_odds`, `n_without_odds`,
  `n_cold_start`, `n_leagues`, `date_min`, `date_max`, `thresholds`, `team_predictions`,
  `team_thresholds`, `surge`) + `run_id`.
- `board_surge() -> dict | None`: the latest board run's Surge Watch labels `{version, fixtures, run_id}`
  (None when the run has none).
- `board_context() -> dict | None`: the latest board run's table context `{run_id, fixtures: {match_id: {...}}}`.
- `run_surge_labels(run_ids) -> {run_id: {version, fixtures}}`: labels recorded with the given board runs (runs
  without labels are left out). `settled_results` rows carry `run_id` (the board run of the chosen prediction).
- `board_leagues(ent) -> tuple[str, ...]`: competitions on the latest board the caller may see.
- `latest_board(f, today, ent) -> BoardView` where `f` is `shots.web.filters.Filters` (`leagues, groups,
  n, smin, smax, pmin, when, odds, nocold, q, sort, side, tn, tp`). Filters, probability lookup and sort
  run in SQL and return exactly what `filters.apply` (the pandas reference) returns.
  `BoardView(rows, total, league_groups, has_team, run_id)`:
  - `rows`: list of dicts, display order. Keys: `match_id, date (date), kickoff ("" if unknown),
    competition, country, tier, round, home_team, away_team, expected_shots, lb_70, lb_80, lb_90,
    league_avg_shots, above_league, odds (bool), mkt_p_over25, cold_start, model, p` (= P(total >= f.n));
    with team access also `home_mean, home_median, home_lb_70/80/90, away_*`; with a team filter `tp, tside`.
    Missing values are `None`.
  - `total`: upcoming matches visible to the caller (before filters).
  - `league_groups`: `[{country, leagues: [{name, tier, count}]}]` over those upcoming matches,
    countries A-Z, leagues by tier then name.
  - `has_team`: run has team curves and caller has `team_predictions`.
- `match_detail(match_id, today, ent) -> dict | None`: one row of the latest board (any date) with the
  same keys plus `curve_ns` + `curve` (lists), and `team_ns`, `home_curve`, `away_curve` (None without
  team data/entitlement). None if not on the board or not visible (league / `max_days_ahead`).
- `track_records(source, ent) -> DataFrame`: `source="live"` -> `v_official_picks`; `"replay"` ->
  `v_replay_picks`; `"experimental"` -> `v_experimental_picks`; `"backfill"` -> the latest backfill run. Columns `match_id, date (datetime64), competition, n, p, status, actual (Int64),
  hit (boolean, NA unless settled), scan_ts, source`.
- `track_summary(source, n, min_prob, ent) -> dict`: `{src, counts (live status counts), avail (Ns with
  settled rows), n (resolved), has_data, settled (DataFrame), metrics (shots.predict.track.metrics; only
  `all`/`selected` without track_detail), total, n_comp}`.
- Static export (used by `shots.export` only, always with `FULL_ACCESS`; no odds values or source columns are
  selected): `board_export(today, ent)` (upcoming rows of the latest board with full curves and `kickoff_utc`),
  `official_results(since, until, ent)` (see "Stage 1"; rows also carry `experimental`, `result_source`,
  `source_url`), `settled_results(since | None, until, ent, experimental=None | False | True)` (the same rule
  for ordinary, experimental or all predictions), `base_rates(n, before)` (per-league share of past
  matches with >= n shots: the Brier baseline), `competitions()`, `last_finished_job(job)`, `record_since()`.
- `data_status(today) -> dict`: `{revision, matches_played, latest_result, upcoming_fixtures,
  competitions, teams, runs: {kind: {count, latest_built_at}}, predictions, latest_board_run, last_job}`.

`shots.db.access`: `Entitlements(can_view_all_leagues, allowed_leagues, max_days_ahead, team_predictions,
export, track_detail)`, `FULL_ACCESS`, `for_plan(session, plan_key)`, `default_plan_key(session)`.
The web app takes `create_app(db=None, today=None, entitlements=None)`, where `entitlements` is
`request -> Entitlements` (the auth phase plugs the session's plan in there).

## Loaders (`shots.db.load`) and the update flow

`python -m shots update` steps: results -> calendars -> picks scan -> board -> track dashboard ->
**db** (`sync_results`: migrate, upsert results of the last ~400 days (all on an empty DB) and the
upcoming fixtures from `fixtures.parquet` + the latest `data/raw/football-data/fixtures/fixtures_*.csv`) ->
**db-manual** (`sync_manual_results`: `results_manual/*.csv`, see "Manual results") ->
**db-provisional** (`sync_provisional_results`: `results_provisional/*.csv`, see "Provisional results") ->
**db-board** (`import_board_file`: the fresh board as a `board` run) -> **db-picks** (`sync_predictions`:
every picks CSV not yet imported as a `picks` run, the backfill as a `backfill` run, outcome counts).
Each step runs even if an earlier one failed; the run is recorded in `job_runs`.

Other entry points: `upsert_matches(engine, df, since=None)`, `upsert_fixtures(engine, df, today)`,
`read_fixtures(today)`, `import_board(engine, board_df, meta)`, `import_picks_file(engine, path)`,
`import_picks_dir(engine)`, `import_backfill(engine, df)`, `delete_run(engine, run_id)`, `sync_all(target)`.

## Adding a migration

Change `models.py`, then write `migrations/versions/000N_<name>.py` (`down_revision` = previous id; use
`op.batch_alter_table` for SQLite column changes), run `python -m shots.db upgrade`, and keep
`tests/test_db_migrations.py::test_models_match_migrations` green.

## Authentication

Code: `src/shots/auth/` (`service.py` AuthService, `web.py` FastAPI helpers, `passwords.py`, `messages.py`,
`settings.py` = every security constant, `common_passwords.txt`, `__main__.py` admin CLI). Uses the
account tables of `0001_initial` as they are; no schema change.

### Wiring (web app factory)

```python
from shots.auth.web import entitlements_provider, install_auth
app = create_app(db, entitlements=entitlements_provider)  # every page and repository query sees the caller's plan
install_auth(app, db)                                      # AuthService on app.state + /auth router + cookie middleware
```

`install_auth(app, db | AuthService, anonymous_plan=None) -> AuthService`. The served app
(`shots.web.app:app`) also adds `install_trusted_hosts` (Host allow-list); `create_app` always adds
`install_security_headers` (both in `shots.auth.http_security`, see "HTTP hardening").

`python -m shots.web` passes every proxy / logging setting to uvicorn explicitly (never uvicorn's
defaults): `SHOTS_PROXY_HEADERS=1` trusts `X-Forwarded-For` / `X-Forwarded-Proto` (default **off**: client
IP and scheme are the socket's; enable it only behind a TLS reverse proxy, which then decides the client IP
and the `https` scheme), `SHOTS_FORWARDED_ALLOW_IPS` = the proxy addresses allowed to set them (default
`127.0.0.1`), `SHOTS_ACCESS_LOG=1` enables uvicorn's access log (default **off**: it would write client
IPs and URLs to `reports/logs/`). `SHOTS_COOKIE_SECURE=1` forces the cookie `Secure` flag.

### Helpers (`shots.auth.web`)

| helper | use |
|---|---|
| `current_user(request) -> UserInfo \| None` | dependency or call; the session is validated once per request (cached) |
| `require_user` | dependency; anonymous -> **303** `/login?error=login_required&next=<this GET path>` |
| `require_admin` | dependency; `users.is_admin`, else 403 |
| `require_entitlement(key)` | dependency factory for boolean keys (`can_view_all_leagues`, `team_predictions`, `export`, `track_detail`); anonymous -> 303 to login, signed-in -> 403. Unknown or non-boolean key -> `ValueError` at import time |
| `entitlements_provider(request) -> Entitlements` | for `create_app(entitlements=...)`; cached per request |
| `csrf_token(request) -> str` | token for forms: the session's token, or for visitors without a session a stateless pre-login token (sets the `shots_prelogin` nonce cookie via the middleware if missing; no DB write) |
| `verify_csrf` | dependency for the app's own POST routes: 403 unless the `csrf_token` field or the `X-CSRF-Token` header matches the caller's **session** (pre-login tokens are accepted only by `/auth/login` and `/auth/register`) |
| `await form_data(request) -> dict \| None` | urlencoded body parser (max 16 KB, 50 fields, first value wins; python-multipart not needed) |
| `set_session_cookie(response, token, request, max_age=None)`, `clear_session_cookie(response, request)` | cookie with the flags below |
| `safe_next(value, default="/")` | local-path check for redirects |
| `auth_context(request, csrf=False) -> dict` | page context (below) |
| `get_service(request) -> AuthService` | the installed service |
| `auth_router(prefix="/auth")` | the endpoints below (included by `install_auth`) |

`AuthService` (`shots.auth.service`): `register`, `authenticate`, `login` (authenticate + rotation),
`create_session`, `validate`, `check_csrf`, `revoke` / `logout`, `revoke_all`, `change_password`,
`assign_plan`, `plan_for_user`, `entitlements_for_user`, `anonymous_entitlements`, `list_plans`,
`create_user`, `find_user`, `get_user`, `list_users`, `set_active`, `set_admin`, `purge`. Failures raise
`AuthError(code, reason)`.

### Endpoints (POST, `application/x-www-form-urlencoded`, all answer 303)

| endpoint | fields | success | failure redirect |
|---|---|---|---|
| `/auth/register` | `email`, `password`, `password_confirm` (optional), `next`, `csrf_token` | session cookie, -> `next` or `/` | `/register?error=...[&reason=...][&next=...]` |
| `/auth/login` | `email`, `password`, `next`, `csrf_token` | new session (previous one revoked), -> `next` or `/` | `/login?error=...[&next=...]` |
| `/auth/logout` | `csrf_token`, `next` | session revoked, cookie cleared, -> `next` or `/` | `/account?error=csrf` (anonymous: `/?error=csrf`) |
| `/auth/logout-all` | `csrf_token` | every session of the user revoked, cookie cleared, -> `/?ok=signed_out_everywhere` | `/account?error=csrf`; anonymous -> `/login` |
| `/auth/password` | `current_password`, `new_password`, `new_password_confirm` (optional), `csrf_token` | all sessions revoked, a fresh one set, -> `/account?ok=password_changed` | `/account?error=...[&reason=...]`; anonymous -> `/login?error=login_required&next=%2Faccount` |

`next` must be a local path (`/...`, not `//`, no scheme or host, no backslash or control characters,
at most 512 chars); anything else becomes `/`. Error codes: `invalid_credentials`, `rate_limited`,
`invalid_email`, `weak_password` (+ `reason` = `too_short | too_long | common | contains_email | same`),
`password_mismatch`, `email_taken` (`registration_failed` if `REVEAL_EMAIL_TAKEN = False`), `csrf`,
`invalid_request`, `login_required`. Ok codes: `password_changed`, `signed_out_everywhere`.

### Page-context contract (GET pages, owned by the web app)

`auth_context(request, csrf=...)` returns:
`user` (`UserInfo`: `id, email, display_name, is_admin, is_active, created_at`, or None), `plan`
(`{"key", "name"}`, display only, or None), `ent` (Entitlements), `csrf_token` (str; None for anonymous
visitors unless `csrf=True`), `csrf_field` (`"csrf_token"`), `auth_error` (a known code, or
`invalid_request` for anything else), `auth_error_message`, `auth_ok_message` (render these, **never
the raw query value**), `next` (a validated local path, or ""), `password_min_length`,
`password_max_length`.

- `/login`, `/register`: `auth_context(request, csrf=True)`; the form posts to `/auth/login` /
  `/auth/register` with hidden `csrf_token` and `next`; inputs `email` (`type=email
  autocomplete=username`), `password` (`autocomplete=current-password` / `new-password`), optional
  `password_confirm`.
- Every page: when `user` is set, a logout form `POST /auth/logout` with `csrf_token` (signed-in users
  always get a token, derived without a DB write). Only pages with a login/register form use
  `csrf=True` (it sets the pre-login nonce cookie; nothing is stored server-side).
- `/account` (`Depends(require_user)`): email, plan name, change-password form `POST /auth/password`,
  "sign out everywhere" form `POST /auth/logout-all`.
- `/pricing`: `get_service(request).list_plans()` -> `PlanInfo(key, name, sort_order, is_default,
  entitlements: dict)`; highlight the current `plan`.
- Messages: `shots.auth.messages.error_message(code, reason)` / `ok_message(code)` (already in the context).

### Security decisions (constants in `shots.auth.settings`)

- **Passwords:** Argon2id (argon2-cffi), t=3, m=64 MiB, p=4 (RFC 9106 low-memory profile); outdated
  hashes are re-hashed on the next successful login. At most `ARGON2_MAX_CONCURRENT` = 4 hashes /
  verifies run at once (a semaphore; others queue), so a login burst needs at most ~256 MiB. NFKC-normalized. Length 12..256. Rejected as
  `common`: the bundled SecLists top-10k list (lower-cased), a common word with up to 6
  leading/trailing digits or symbols (`Password1234!`), fewer than 4 distinct characters, a repeated
  unit (`abcabcabcabc`), keyboard / alphabet / digit runs; `contains_email` if it contains the email's
  local part (3+ chars). No network.
- **Emails:** trimmed, NFKC, lower-cased; one account per address; pragmatic syntax check (no
  deliverability check until email confirmation exists).
- **Enumeration and timing:** login answers `invalid_credentials` for an unknown email, a wrong password
  and a deactivated account alike, and always runs exactly one Argon2 verify (a dummy hash for unknown
  emails). Registration hashes before the existence check. Known trade-off: `email_taken` reveals that
  an address is registered (no email delivery yet for a "check your inbox" flow); it is rate-limited and
  switchable with `REVEAL_EMAIL_TAKEN`.
- **Sessions:** 256-bit `secrets.token_urlsafe(32)`; the DB stores only its SHA-256
  (`sessions.id_hash`). Cookie `shots_session`: `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` on HTTPS,
  `Max-Age` = lifetime. Idle timeout 7 days, absolute 30 days, `last_seen_at` written at most every 5
  minutes. No session row exists before sign-in (the login/register forms use the stateless pre-login
  token below); `create_session(None)` remains for API compatibility only. Rotation: login and registration revoke the previous
  session and issue a new id; a password change revokes every session of the user and issues a new one;
  deactivation revokes all. Sessions of deactivated users are rejected even if not revoked.
- **CSRF:** token = HMAC-SHA256(key = session id, "shots-csrf-v1"), base64url; checked in constant time
  against a live session, so it dies with the session and changes on login (`sessions.csrf_hash` keeps
  its SHA-256 for audit). **Pre-login** (login / register forms of visitors without a session): token =
  `<issued epoch seconds>.<base64url HMAC-SHA256(server key, "shots-prelogin-csrf-v1|nonce|issued")>`,
  bound to a random 256-bit nonce in the `shots_prelogin` cookie (HttpOnly, SameSite=Lax, Path=/, Secure
  on HTTPS, 2 h), valid `PRELOGIN_CSRF_TTL` = 2 h (5 min of future skew tolerated), compared in constant
  time; accepted only by `/auth/login` and `/auth/register`; the cookie is cleared on sign-in. Nothing is
  stored, so anonymous page views create no rows. Plus an `Origin` check (if sent, it must equal `Host`;
  `null` is rejected).
- **Rate limits and lockout** (`login_attempts`; keys are HMAC-SHA256, with a server key, of the
  normalized email / client IP): login per account: after 5 consecutive failures (24 h window, reset by a
  success) locked for 1 min x 2^(failures - 5), capped at 15 min, counted from the last failure; attempts
  during a lock are refused without checking the password and are not counted. Identical for unknown
  emails. Login per client: 20 failures / 15 min. Registration: 5 attempts / hour per client, 3 / hour per
  email. Password change: the account backoff (kind `password`). `users.locked_until` is not used.
  **Atomicity:** the limit check and the attempt record happen in one SQLite write transaction (a no-op
  UPDATE takes the database write lock first), and the attempt is recorded as a failure *before* the
  password is verified (then flipped to success), so N parallel guesses get exactly the free failures,
  across threads and processes.
- **Server key:** `SHOTS_AUTH_KEY`, else `<db dir>/auth.key` (random, created on first use, git-ignored
  via `data/*.key`). Losing it only resets rate-limit history.
- **Logging:** user ids only; never passwords, tokens, session ids, CSRF tokens, emails or IPs
  (`NewSession` / `SessionState` reprs hide tokens). uvicorn's access log is off unless
  `SHOTS_ACCESS_LOG=1`.
- **Housekeeping:** `AuthService.purge()` / `python -m shots.auth purge` deletes sessions dead for 7 days
  and attempts older than 30 days; every worker cycle runs it (`counts.purge` of the `worker` job row).

### HTTP hardening (`shots.auth.http_security`)

- **Host allow-list** (DNS rebinding): Starlette `TrustedHostMiddleware`, hosts from `SHOTS_ALLOWED_HOSTS`
  (comma-separated, default `127.0.0.1,localhost`); any other `Host` gets 400. Add the public host name
  here when the site goes public.
- **Headers on every response:** `Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self' 'unsafe-inline'; img-src 'self'; object-src 'none'; frame-ancestors 'none';
  base-uri 'self'; form-action 'self'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: same-origin`, `Permissions-Policy` (camera, microphone, geolocation, payment, usb,
  topics off). `Strict-Transport-Security: max-age=31536000` only when the request came over HTTPS.
- **Caching:** `Vary: Cookie` on every non-static response; `Cache-Control: no-store, private` on
  `/login`, `/register`, `/account`, `/auth/*`, on any request carrying a session cookie and on any
  response setting a cookie.
- **Scripts:** all JavaScript is external (`/static/boot.js`, `/static/app.js`); no inline scripts or
  event handlers (`tests/test_auth_hardening.py` checks the templates), so `script-src` has no
  `'unsafe-inline'`. `style-src` keeps `'unsafe-inline'` for four dynamic `style="width:..%"` bars
  (`board.html`, `match.html`; `app.js` copies such markup via `innerHTML`). Removing it later is
  feasible: emit the value as `data-w` and set `el.style.width` from `app.js` (CSSOM writes are not
  blocked by CSP), or use `<meter>` / 1%-step classes.
- **/track cache:** keyed on the resolved source (`backfill` / `live`) and the resolved N (from the
  available set), LRU-bounded to 32 entries, so junk query values cannot grow memory or force recomputation.

### Plans and entitlements

`entitlements_for_user(user_id)`: the latest `active` / `trialing` subscription whose
`current_period_end` is NULL or in the future and whose plan is active -> `plan_entitlements` ->
`Entitlements.from_values` (a missing key denies: false / 0 days / no leagues). No such subscription ->
the default plan (`is_default`, Free). No usable plan at all -> everything denied. Registration assigns
the default plan (subscription `provider = "manual"`); `assign_plan` cancels the previous active
subscriptions. Code never checks a plan key.

**Anonymous visitors** get the default plan's entitlements (Free); configurable as config, not code:
`install_auth(..., anonymous_plan="<key>")` or `SHOTS_ANON_PLAN` (an unknown key denies everything).
Rationale: Free already exposes only the top-5 leagues, 3 days ahead and no team curves; a separate
guest tier would need a seeded plan (data migration) for little gain. To add one later: seed a `guest`
plan in a migration and set `SHOTS_ANON_PLAN=guest`.

Entitlement keys: `can_view_all_leagues` (bool), `allowed_leagues` (list, used when the former is false),
`max_days_ahead` (int, null = whole season), `team_predictions`, `export`, `track_detail` (bool). A new key
= a field in `shots.db.access.Entitlements` + rows in `plan_entitlements` (migration).

### Admin CLI

```
python -m shots.auth create-user --email you@example.com --plan premium --admin   # password prompted twice
python -m shots.auth set-plan --email E --plan pro
python -m shots.auth list-users
python -m shots.auth revoke-sessions --email E
python -m shots.auth deactivate --email E        # also revokes sessions; `activate` undoes it
python -m shots.auth purge
```

Passwords are never accepted on the command line. Admin = `users.is_admin` (no plan bypass: give the
admin account the plan it should see).

## Worker and deployment

### Worker (`src/shots/worker/`)

```
python -m shots.worker run-once [--force] [--min-interval H] [--days N]   # one cycle, then exit
python -m shots.worker status [--min-interval H]                          # lock, recent runs, what is due
```

One cycle = `shots.__main__.update` (results -> calendars -> picks -> board -> track -> db steps; not
duplicated) wrapped in:

1. **Overlap lock** `data/worker.lock` (next to the DB; `SHOTS_WORKER_LOCK` overrides): an OS lock (a
   byte-range lock on Windows, `flock` on Linux / macOS) held for the life of the process, so the OS frees
   it when the process exits, crashes or is killed.
   The file keeps the holder's pid/host/start time; a record left by a dead holder is reported as
   **stale** and taken over (logged); a live holder older than 6 h is flagged **suspect** in `status`.
   A cycle that finds the lock held exits 0 and records `skipped / locked`. A manual
   `python -m shots update` does not take the lock, but its `running` job row (live pid, same host,
   < 6 h old) makes the cycle skip (`busy`); `running` rows of dead processes are closed as `failed`
   ("abandoned").
2. **Skip-if-fresh** (`shots.worker.due`): the update runs with `--force`, when there is no `ok` update
   yet, when the DB has no board (`no-board`), when the last `ok` update started >= `--min-interval` ago
   (default 2 h, `SHOTS_WORKER_MIN_INTERVAL_HOURS`), or when something became due since that update started:
   `results` (a scheduled match's expected end - kickoff_utc + 2 h, or the end of its date when the time is
   unknown - passed), `odds` (a football-data publication moment, Tue/Fri 09:00, passed and fixtures in the
   next 7 days lack odds), `new-day` (the date changed: the board window rolls forward), `calendars` (the
   content of `calendars/*.ics` changed). Only `ok` updates count: after a `partial`/`failed` update every
   cycle retries.
   - **Zones.** `new-day` uses the machine's zone; on the GitHub runner that is UTC (`TZ=UTC`), which is
     also the day boundary of the static export. The odds hour is read in `SHOTS_ODDS_TZ` (an IANA name;
     unset = the machine's zone). `update.yml` sets it to `UTC`: there the rule is "Tue / Fri 09:00 UTC".
     The hour is only a hint for when to look early; odds published later are picked up by the next
     interval run.
   - **Calendars.** Each worker cycle stores a fingerprint of the calendar files it started from
     (`job_runs.counts.calendars`, names + contents, line endings normalised); the next cycle compares
     contents. File modification times are only used while no fingerprint is recorded: on a fresh git
     checkout (every GitHub run) all files are "new", which would make every cycle due.
   - **Minimum gap** (`--min-gap MINUTES`, `SHOTS_WORKER_MIN_GAP_MINUTES`, default 0 = off): no update
     starts less than this after the previous attempt of any outcome (`recent`), whatever is due, except
     `forced`, `never` and `no-board`. Meant for a worker that is triggered often (`update.yml`: 90 min):
     it bounds the number of board rebuilds on a match day and spaces the retries after a failure.
   - Locally (Task Scheduler) the defaults are unchanged: the rule mainly stops the boot/logon run from
     repeating a run made minutes ago.
3. **Bookkeeping**: one `job_runs` row with `job = 'worker'` per cycle (`ok` / `partial` / `failed` /
   `skipped`; `counts.action` = `update` | `skipped` | `locked` | `busy`, `counts.reasons`,
   `counts.update_job` = the update's own `job_runs` id, `counts.purge` = `{sessions, attempts}` deleted
   by the auth housekeeping that runs in every cycle that holds the lock). Exit code 0 when skipped or
   ok, 1 otherwise.

**Moving on to the next match**: the board build only scores fixtures not yet in `matches.parquet` from
today on, so a rebuilt board drops played matches and its window rolls forward daily. The repository also
hides a match from the upcoming board as soon as its result is loaded (`status = 'played'`), even if the
board step of that cycle failed (`tests/test_db_integrity.py::test_played_fixture_leaves_the_upcoming_board`).

### Pre-match integrity (migration `0002_pre_match`)

`predictions.pre_match` (boolean, set once at insert, never updated) = the run was built strictly before
the match's lock moment: `kickoff_utc` when the DB knows it; else, when the source row carries an
"HH:MM" of unknown zone (board/picks frames mix league-local and UK times), the earlier of the
league-local and UK readings; else 00:00 UTC of the match date (`shots.db.load.lock_moment`). The
migration back-fills existing rows from `kickoff_utc` / 00:00 UTC (the only write ever made to existing
predictions: the UPDATE trigger is dropped and re-created around it). `v_prediction_outcomes.pre_match`
re-checks the flag against the current `kickoff_utc` (a kick-off learned or moved earlier later can only
make it stricter), and **`v_official_picks` counts only pre-match predictions** (then, per match and N,
the last such scan on or before the match date). The backfill run is built after its matches, so its
rows are `pre_match = 0`; the backfill track record is a labelled replay and does not filter on it.

**Insert time and replays (migration `0003_run_integrity`).** A claimed scan time is not proof: a file
can be named with any timestamp. The official rule is therefore:

- A prediction is pre-match only if the run was built **and the row was inserted** before the lock
  moment, and the match had no result yet. The insert time is `predictions.created_at`, taken from the
  DB clock by the loader (`load.db_now`) and enforced by a trigger (+-5 min of SQLite's `now`).
- The DB rejects a `pre_match = 1` insert it can disprove: match already `played`, or insert time or
  `built_at` at or after the latest possible lock (`kickoff_utc`, else match date + 36 h; the loader
  applies the precise lock moment with the row's "HH:MM" hint).
- `v_prediction_outcomes.pre_match` also requires `created_at` before that bound, which removes
  pre-`0003` rows inserted after their match (the 46 replay rows of 2026-10-09 were the case).
- **Replays never count as official live picks**: `v_official_picks` takes `params.source = 'live'`
  only; replays are in `v_replay_picks` / `track_records("replay")`. The web "Live scans" tab therefore
  shows only scans that were in the DB before kick-off.
- Consequence: a DB rebuilt from the files (`python -m shots.db import --full` on a new DB) cannot prove
  that past live scans were pre-match, so their matches leave the official record. The live DB is the
  auditable record; back it up (see "Before going public").
- Not enforced: `built_at <= created_at`. A run may claim a future build time, but it gains nothing
  (official status needs the insert time before the lock), and the loaders' tests build runs ahead
  of the clock.
- The file record (`shots.predict.track.dedupe`) applies the same kick-off rule to scans (scan time
  before the lock moment from the row's "HH:MM" and the league's zone, else 00:00 UTC of the match
  date), so a scan taken after kick-off on match day no longer wins. It cannot know insert times, and
  it still lists labelled replays in `reports/dashboard.html`.

### Kick-off times

`matches.kickoff_local` is the league-local "HH:MM", `kickoff_utc` the same instant in UTC. Calendars are
UTC at the source. football-data's `fixtures.csv` `Time` is UK time: `read_fixtures` converts it
UK -> UTC -> league local (`load.uk_kickoffs`; the match date stays as published because it is part of
`match_id`). When a fixture is in both sources, the calendar time wins and football-data only adds odds
(or the time, if the calendar has none). Board/picks imports never replace a known kick-off (their
`kickoff` column mixes zones); they only fill `kickoff_local` of a match that has none and never invent a
`kickoff_utc`. Known upstream issue (outside the DB layer): `shots.predict.scan.fetch_fixtures` still puts
football-data's UK time in the board/picks `kickoff` column for football-data-only fixtures.

### Windows Task Scheduler (`deploy/`)

| script | what it does |
|---|---|
| `install.ps1 [-AtStartup] [-StartNow] [-Port 8000] [-KeepLogDays 14] [-WorkerEveryHours 3] [-RemoveLegacyTask] [-WhatIf]` | registers / updates (idempotent) the two tasks below for the current user and prints every step; `-WhatIf` builds the full task definitions and prints them without registering anything |
| `uninstall.ps1 [-KeepRunning] [-WhatIf]` | stops and unregisters both tasks, then stops leftover `shots.web` processes; keeps logs and data |
| `status.ps1` | read-only: task state / last result / next run, web reachability, `python -m shots.worker status`, log tails |
| `run_web.ps1`, `run_worker.ps1`, `common.ps1` | the task actions (wrappers that log and supervise) and shared settings |

- **Shots Web**: at logon of the current user (or at startup with `-AtStartup`); runs `run_web.ps1` ->
  `python -m shots.web --host 127.0.0.1 --port 8000`; no time limit; one instance; the wrapper restarts
  the server if it exits (5 s backoff doubling to 5 min) and Task Scheduler retries a failed launch 5 times.
- **Shots Worker**: at logon/startup (2 min delay) plus daily from 00:00 repeating every 3 h; missed
  slots run as soon as possible; 30 min limit; no new instance while one runs. The wrapper stops a cycle
  and its child processes after 25 min (a venv `python.exe` is a launcher with a child interpreter, which
  Task Scheduler's own limit would not reliably stop).
- **Logs**: `reports/logs/web_<date>.log`, `worker_<date>.log` (one file per day); these and the old
  `update_*.log` files older than `-KeepLogDays` are deleted at each start. The web log has no access
  log (no client IPs / URLs) unless `SHOTS_ACCESS_LOG=1` is set for the task's user.
- **Admin**: not needed by default (logon trigger, interactive logon, limited rights; runs only while you
  are logged on). `-AtStartup` registers boot triggers with the S4U logon type (no stored password, runs
  before logon, no access to network shares), which Windows only allows from an elevated PowerShell; the
  script refuses `-AtStartup` when not elevated and explains the alternative.
- `scripts/scheduled_scan.ps1` is deprecated (its Tue/Fri odds runs are now due work of the worker);
  `install.ps1` warns if a "Shots scan" task still exists and removes it with `-RemoveLegacyTask`.

User commands (from the project folder; never run by the agents):

```
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install.ps1 -WhatIf     # preview
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install.ps1 -StartNow   # no admin needed
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\status.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\uninstall.ps1
```

### Storage growth and retention

Measured on the current DB (44 MB: 98k matches, one 568-row board run, a 7k-row backfill): a board
prediction with its three curves takes ~0.8 KB, so one board run is ~0.45 MB; a picks prediction (one
sparse point) ~0.2 KB. A board run is stored only when its predictions differ from the latest one
(identical rebuilds only bump `confirmed_at`), so growth follows how often inputs change, not the
schedule: odds days and match days produce new runs, quiet days mostly do not. Expect ~2-4 changed
boards per match day, i.e. ~1-2 MB/day or **~0.3-0.6 GB per season**, plus well under 1 MB/day of picks
runs (one scan file per cycle). SQLite handles this comfortably; `matches` grows by ~25k rows a season.

Retention approach (documented, not implemented; nothing destructive runs automatically):
1. Never touch predictions that are or may become official: picks runs and the latest board.
2. Optional archival of **old non-official board runs** (e.g. boards older than 60 days that are not the
   last pre-match board for any of their matches): copy the runs and their predictions into
   `data/shots_archive_<season>.db` (same schema, migrated by the same Alembic scripts), verify counts
   and content hashes, then remove them from the live DB. The append-only trigger forbids deleting
   predictions of kicked-off matches, so this would be a deliberate, reviewed tool that drops and
   re-creates the trigger inside one transaction, never a routine step; the archive stays queryable
   (`ATTACH`) for audits.
3. `job_runs` rows (~8 per day) are tiny; `skipped` worker rows older than a year could be pruned if ever
   needed. Run `VACUUM` after any archival to return the space.

## Stage 1 (GitHub Pages + Actions)

Free, serverless deployment decided on 2026-10-12: a static site on GitHub Pages (public repository) and
GitHub Actions as the backend that runs the models on a schedule and publishes JSON. No accounts in
Stage 1: everything published is public. The FastAPI app, auth and the Task Scheduler setup stay for
local use and a later Stage 2 API.

### Static export (`src/shots/export/`)

```
python -m shots.export --out <dir> [--meta changed|always] [--today YYYY-MM-DD]
python -m shots.export.verify --dir <dir> [--db data/shots.db] [--today D] [--no-schema]
python -m shots update --export-dir <dir>      # or SHOTS_EXPORT_DIR; off by default
```

Writes `meta.json`, `board.json`, `results.json`, `track.json` and `surge.json` exactly as specified in
`docs/static-api.md` (JSON Schema: `docs/static-api.schema.json`, closed objects, validated in
`tests/test_export.py`). Everything is read through `Repository` with `FULL_ACCESS`; the exporter never opens the
parquet / raw files (its only file read is the committed Surge Watch catalogue).

- **Surge Watch** (2026-10-27): `surge.json` is the committed catalogue (`src/shots/evaluation/surge_signals.json`)
  copied field by field; a fixture's `surge` is the label set recorded with the board run (see "Predictions"),
  published only when the run used the same catalogue version, and checked against it (a label that does not
  apply on the fixture's route aborts the export). The site's "Surge" page (`#/surge`) explains the patterns and
  lists upcoming fixtures with a boost; probabilities are never adjusted. Tests: `tests/test_predict_surge.py`
  (rule, routes, no leakage), `tests/test_export_surge.py` (export, schema, verifier).
- **Table signals, strong candidates** (2026-10-28, `reports/2026-10-28_surge_table_signals.md`): the board build
  adds the table inputs (`surge_rules.table_inputs`) so `top_vs_bottom` / `promoted_struggling` fire, records
  `"strong": true` on fixtures with 2+ boosts when `stacking.routes.<route>.strong_applies` (odds only today), and
  takes the context's phase from `surge_rules.phase_of` (one definition). `surge.json` carries `stacking`; results
  carry the recorded `context` too.
- **Surge results and table context** (2026-10-28): a `results.json` row carries `surge` = the labels its pre-match
  board run recorded for it, as recorded (not re-judged against today's catalogue; malformed ids abort the export);
  a board fixture carries `context` (table position, season phase) recorded with the board run, checked for range
  and consistency (else the export aborts). The Surge page charts how flagged matches turned out (weekly surge rate,
  1+ boost vs assessed without, per-signal counts, latest settled candidates) and shows the context and a phase filter.

- **What is published:** our predictions, fixture identity (teams, league, kick-off) and, for finished
  matches, the final total / home / away shot counts. Never odds values, market probabilities, goals, shots
  on target or source columns: the repository export queries do not select them, the schema rejects unknown
  keys, and a test seeds distinctive odds values and asserts none of them appears in any file. `odds` in the
  files is the boolean "scored by the odds-aware model".
- **Day boundary:** the board lists fixtures dated on or after the **UTC date of the export** that have no
  result yet (the server-rendered app uses the machine's local date; on the UTC runner both are the same).
- **`results.json` rule (official, live + pre-match):** per match dated in the last 30 days whose result has
  shots data, the **last `board` run whose prediction is pre-match** by `v_prediction_outcomes.pre_match`
  (run built and row inserted before the lock moment, re-checked against the current kick-off). Board runs
  are always live builds; replays exist only as picks files and never get here. `predicted_at` is the
  prediction's insert time (`predictions.created_at`, DB clock, trigger-enforced), not a claimed build time.
  This is the `v_official_picks` rule applied to board runs (a picks run only holds one probability; the
  board run holds the full curve the page needs).
- **`track.json`:** `backfill` = latest backfill run, `live` = `v_official_picks`, `experimental` =
  `v_experimental_picks` (same shape as `live`), each of `live` / `experimental` with `daily` / `weekly`
  accuracy series (matches / MAE / bias / floor-80 from the `results.json` rule, `by_n` from the record's own
  picks; `docs/static-api.md`); per threshold N the selected
  picks (p >= 0.70) with Wilson interval, reliability bands and Brier over all settled rows, the Brier
  baseline from `Repository.base_rates` (equal to the dashboard's `track.base_rates`; checked on the real
  data: N=24 gives 1085 picks / 846 hits / Brier 0.2205 / baseline 0.2419 in both), and the per-league table.
- **Determinism:** UTF-8, LF, keys in contract order, probabilities rounded to 4 decimals, one fixture /
  result per line (a changed prediction is a one-line git diff). A file is rewritten only when its bytes
  change. `meta.json` holds the only timestamps (`built_at`, `last_job.finished_at`): with the default
  `--meta changed` it is rewritten only when another file or its own non-volatile content changed, so an
  unchanged database gives **no diff at all**; `--meta always` refreshes `built_at` (used for the deployed
  site, so visitors see when the pipeline last ran).
- **Fail closed:** a missing database / board, or a curve that is not a non-increasing probability curve of
  the right length, raises `ExportError` (CLI exit 2) before anything is written; the previous files stay.
- **In `python -m shots update`:** with `--export-dir` / `SHOTS_EXPORT_DIR` the export runs after the run is
  recorded in `job_runs`, so `meta.last_job` is that run. A failed export turns the run `partial`
  (`counts.steps.export = false`) and the exit code 1.
- **Additions to the contract** (additive, schema 1): `meta.record.since` (insert time of the earliest
  pre-match prediction in the database behind the export: it jumps forward if that database was rebuilt),
  `track.records.live.void`. **Nullable where the contract's example shows a value:** `meta.last_job` (no
  finished update run yet), `meta.thresholds.team` and fixture `team` (board without team curves),
  `round`, `kickoff_local`, `kickoff_utc` (unknown), `track.records.backfill` (no backfill run),
  `brier_baseline` (no earlier results in the database). `meta.leagues` lists every league any file refers
  to, with `fixtures: 0` for leagues that only appear in results or the track record.

**Independent check (evaluator hand-off):** `python -m shots.export.verify` reads the SQLite file read-only
with its own SQL (no repository, no exporter code) and compares: the JSON Schema, a forbidden-key scan, every
curve, `board.json` against the latest board run row by row and in order, `results.json` against its own
selection of the last pre-match board prediction, `track.json` counts and per-N picks / hits against
`v_official_picks` and the backfill run, `meta.json` against all of these, `surge.json` against the committed
catalogue and every fixture's `surge` against the board run's labels and the catalogue's route rules, every
fixture's `context` against the run's recorded context (range and phase consistency too), and every result's
`surge` against the labels its own pre-match board run recorded. Exit 0 / 1
(differences listed) / 2. It runs after every export in the scheduled workflow and in `tests/test_export.py`, where
twelve kinds of tampering are each detected.

### Workflows (`.github/workflows/`)

`update.yml` (hourly schedule + push to `main` + manual; concurrency group `shots-update`: runs queue, never
overlap, never cancel a running one; triggers and what each does: "Triggers" below), three jobs with
separate permissions:

| job | token permissions | what it does |
|---|---|---|
| `update` | `contents: write`, `actions: write`, `deployments: read` | restore state -> plan -> `python -m shots.worker run-once [--force]` -> check + save state -> export -> `shots.export.verify` -> commit `data/*.json` to the `data` branch -> decide whether to deploy |
| `build` | `contents: read` | `npm ci && npm run build` in `frontend/` with the exported JSON in `frontend/public/data/`; refuses to publish `*.db, *.csv, *.parquet, *.joblib, *.ics, *.key` |
| `deploy` | `pages: write`, `id-token: write` | `actions/upload-pages-artifact` + `actions/deploy-pages` (environment `github-pages`) |
| `surge_issue` | `issues: write` | only when the run's Surge monitor said REVERT: open an issue labelled `surge-monitor`, or comment on the open one |

- Python **3.12** on `ubuntu-24.04`, installed from `requirements.lock` (exact versions, all wheels; resolved
  and tested on Ubuntu 24.04 / Python 3.12.3) plus `pip install --no-deps -e .` (editable is required: the
  package finds `data/`, `calendars/` and `models_store/` relative to its own source path).
  `requirements.txt` stays the loose spec. lightgbm needs the system library `libgomp1` (on GitHub's images;
  the workflow installs it if absent).
- Actions are the official `actions/*` ones, pinned to commit SHAs. Checkouts use
  `persist-credentials: false`; the token is given only to the single step that pushes, and the job that
  runs npm has a read-only token. No `${{ }}` expression is interpolated into a shell script.
- The update cycle is the worker (`run-once`, with `--force` only when the plan says so): same lock,
  bookkeeping (`job_runs`) and auth purge as on the local machine. If it reports failures (for example the source site is down) the state is still
  saved and whatever the database holds is still exported (`meta.last_job.status` says `partial`); the run
  is then marked failed at the very end so GitHub notifies the owner.
- **Audit branch `data`:** holds only `README.md` and `data/{meta,board,results,track}.json`. One commit per
  run **only when a file changed** (`--meta changed`), author `github-actions[bot]`, plain `git push`
  (never forced; the step refuses any other file). Git history with GitHub's push times is the public proof
  of what was predicted before kick-off. The deployed site gets the same files with a fresh `built_at`.
- `watchdog.yml` (hourly at :43; `contents: read`, `actions: write`): the second, independent schedule;
  see "Triggers".
- `surge-weekly.yml` (Monday 07:00 UTC + manual; `actions: write` only, no checkout): `gh workflow run update.yml
  -f force=true -f surge_monitor=true`, so the board, the Surge model's numbers and Surge Watch are rebuilt from the
  newest fixtures at least weekly (the hourly runs already rebuild them whenever data changes). The monitor needs
  the database, which only exists in `update.yml`'s state cache, so it runs there: step "Surge monitor" (input
  `surge_monitor`, default false: schedule, push and watchdog runs never run it), after "Check the database",
  read-only, report to the runner's temp directory and the job summary (never committed). Exit 2 = REVERT ->
  job `surge_issue`. A failing monitor is a warning, never a failed publish. **No config change is ever made
  automatically**: reverting the odds model is the owner's decision (`reports/2026-10-27_surge_adoption.md`).
  Limits: GitHub's schedule is best effort (a dropped Monday slot is not replaced; start it by hand), and a
  dispatched run that is still pending when a newer `update.yml` run queues is replaced by it (concurrency
  group `shots-update`), so that week has no monitor run either.
- `ci.yml` (push to `main` / `master`, pull requests; read-only token): `pytest -m "not a11y"` (includes the
  export contract, schema, privacy and verifier tests), an optional non-blocking Playwright a11y job, and
  frontend lint / unit tests / build when `frontend/package.json` exists.
- The frontend is built with Vite's relative base (`./`, hash routing), so the same build works under
  `https://<user>.github.io/<repo>/` and on a custom domain; nothing in the workflow depends on the URL.

### Triggers: publishing without anyone clicking (2026-10-13)

Observed on the first day: the 3-hourly `schedule` (`23 */3`) did not fire for eight hours, and a code push
did not reach the site until the workflow was started by hand. Over the first 24 h (GitHub API, 2026-10-14)
only 3 of 8 slots ran, each about 90 min late (created 16:59, 22:51, 05:01 UTC for the 15:23, 21:23 and
03:23 slots). GitHub documents the scheduler as best effort
("can be delayed during periods of high loads ... some queued jobs may be dropped", worst at the start of
the hour). The design now assumes that any single trigger can be missing:

1. **Many cheap schedule slots.** `update.yml` has a slot every hour at two odd minutes (`13 */2`,
   `17 1-23/2`, UTC). A dropped slot costs an hour instead of three. A scheduled run is never forced: the
   worker's own rules (above; on the runner: interval 2.5 h, minimum gap 90 min, all in UTC) decide.
   Result with every slot firing: a full update every third slot (~3 h) when nothing is due, at the second
   slot (~2 h) when something is (results, odds, new day, calendars); at most 12 a day.
2. **Push to `main`** for the paths in `shots.worker.ci.push_paths()` (`src/`, `migrations/`, `calendars/`,
   `requirements.lock`, `pyproject.toml`, `update.yml`, `frontend/`, the data-branch README). Documentation,
   tests and reports do not start it. Commits to the `data` branch cannot: other branch, and events caused
   by `GITHUB_TOKEN` do not start workflows.
3. **Watchdog** (`watchdog.yml`, hourly at :43, rules in `shots/worker/watchdog.py`, standard library only):
   if no run of `update.yml` was created in the last 50 min (and none is active) it starts one with
   `gh workflow run update.yml -f force=false`; if runs happen but the site's `data/meta.json` `built_at`
   is older than 12 h it starts a forced one, at most once per 6 h. `workflow_dispatch` is one of the two
   events that do start a run when sent with `GITHUB_TOKEN` (GitHub docs, "Triggering a workflow from a
   workflow": "`workflow_dispatch` and `repository_dispatch` events always create workflow runs"). The
   site URL is `https://<owner>.github.io/<repo>`; set the repository variable `SITE_URL` for a custom domain.
   It is a second schedule of the same scheduler: it protects against dropped slots, not against an
   outage of GitHub Actions scheduling as a whole (for that: the external fallback below).
4. **Manual** `workflow_dispatch`: forced by default; the input `force=false` makes it behave like a
   scheduled run (used by the watchdog and by an external cron).

`python -m shots.worker.ci plan` (step "Plan this run", read-only, after the state is restored and
guarded) turns the trigger into four outputs; `ci publish` decides the deployment after the export:

| trigger | update cycle | site build + deploy |
|---|---|---|
| `schedule`, nothing due | no: the run ends after the plan (about a minute); no cache saved or deleted, no export, no commit | no |
| `schedule`, something due or interval reached | `run-once` (not forced) | only if the export changed (= a `data` commit), or the last deployment is >= 5 h old (heartbeat) |
| `push`, pipeline files changed (or the changed files cannot be listed) | `run-once --force` | always |
| `push`, only `frontend/` or the data-branch README | only if due | always (same data, fresh `built_at`) |
| `workflow_dispatch` (default `force=true`) | `run-once --force` | always |
| `workflow_dispatch` with `force=false` (watchdog, external cron) | as `schedule` | as `schedule` |
| any unforced run, when the newest Pages deployment failed or was built from another commit than `main` ("catch-up") | as `schedule` | always |

- **Catch-up.** The concurrency group lets one run wait behind the running one; a newer waiting run
  replaces it (GitHub cancels the older pending run). A push that arrives during an update can therefore
  lose its run to the next scheduled slot. The plan reads the newest `github-pages` deployment (REST
  `deployments` + its latest status; token permission `deployments: read`) and deploys whenever it is not
  a success for the current commit. A documentation-only commit also moves `main`, so the next slot
  rebuilds the site once (a build, not an update). If the API cannot be read the check is skipped.
- **Heartbeat.** Skipping the deployment when nothing changed would let `built_at` on the site grow old
  in a quiet week and trip the staleness warning although the pipeline is fine. So an update with an
  unchanged export still deploys when the last deployment is >= 5 h old (`ci.HEARTBEAT`): while the
  pipeline is healthy `built_at` is at most about 6 h old (plus dropped slots).
- **Nothing else changed:** concurrency group, state guard, `db-check`, state saved before anything is
  published, append-only `data` branch, token only in the steps that need it.

**Freshness on the site.** `meta.json` already carries what a warning needs: `built_at` (time of the
deployed export) and `last_job` (`status`, `finished_at`). The frontend warns after 48 h
(`STALE_HOURS`). With the heartbeat a healthy site is never older than about 6 h, so 12 h is a safe,
much tighter threshold (two missed full cycles); the watchdog uses the same 12 h for its forced run.

**Usage (public repository: standard runners are free; still kept modest).** Measured before the change:
cold run 14 min, warm run 2.5 min for the three jobs. Not yet measured: the no-op run (checkout, cached
`pip install`, state restore, plan), expected around one minute. Per day: 24 slots of which 8-12 are full
updates (2-3 min billed) and 12-16 no-ops (1 min), 4-8 site builds + deployments (2 jobs, 1 min each
billed), 24 watchdog runs (seconds, billed 1 min). Roughly 70-90 billed minutes a day, 2,000-2,700 a month
(before: 8 x ~5 = 40 a day, 1,200 a month). If the repository ever became private that would exceed the
2,000 free minutes: halve the watchdog (`43 */2 * * *`) and the slots first. Database growth follows the
number of full updates (8-12 board runs a day instead of 8).

**Cache churn.** Only a run whose update cycle ran saves a state cache (new key) and prunes old ones;
a no-op run only reads the newest state cache, which also keeps it from expiring. The past-seasons,
current-season and model caches are now restored only by full updates (several a day, far inside the
7-day eviction window). `setup-python`'s pip cache is written only when `requirements.lock` changes.

**Not verifiable locally, to check on the first runs after the merge:** that the new slots fire at all;
the duration of a no-op run; `deployments: read` and the shape of the two API answers under
`GITHUB_TOKEN` (the same public endpoints were read without a token on 2026-10-13 and match the tests);
that `gh workflow run` from the watchdog starts a run (documented, see above); `git fetch` of
`github.event.before` in the plan step (falls back to "pipeline changed"). The step summary of every run
prints the plan and the deploy decision.

#### Optional external fallback (5 minutes, free, set up by the owner)

If GitHub's scheduler stays unreliable, an external cron service can start the workflow through the REST
API. Nothing in the repository depends on it.

1. GitHub -> Settings -> Developer settings -> Personal access tokens -> **Fine-grained tokens** ->
   Generate new token. Resource owner: your account. Repository access: **Only select repositories** ->
   `freeshots`. Permissions -> Repository permissions -> **Actions: Read and write** (Metadata: read-only
   is added automatically); nothing else. Set an expiry (a year at most) and a reminder to renew it.
2. In a free cron service that can send a POST with custom headers and a body (for example cron-job.org),
   create a job, every hour at minute 50 (or every 30 minutes):
   - URL: `https://api.github.com/repos/godfather68/freeshots/actions/workflows/update.yml/dispatches`
   - Method: `POST`; body: `{"ref":"main","inputs":{"force":"false"}}`
   - Headers: `Authorization: Bearer <the token>`, `Accept: application/vnd.github+json`,
     `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
   - Expected answer: HTTP 204 (no body). Turn on the service's failure notification.
3. Test once from a terminal (the run appears under Actions as "manually triggered"; with `force=false`
   it does nothing unless something is due):
   `curl -i -X POST -H "Authorization: Bearer <the token>" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/repos/godfather68/freeshots/actions/workflows/update.yml/dispatches -d '{"ref":"main","inputs":{"force":"false"}}'`

The token is stored at the cron service. It cannot read or push code or read secrets, but "Actions: write"
also allows cancelling runs and deleting Actions caches, which hold the live record: use a service you
trust, and revoke the token (same settings page) if in doubt. Extra calls are harmless: one run at a
time, and an unforced run that finds nothing due ends after the plan.

### Linux readiness (checked on Ubuntu 24.04, Python 3.12.3, 2026-10-12)

- The whole suite (without a11y) and two full update cycles were run in a clean copy of the working tree
  with a fresh virtual environment built from `requirements.lock`; the `update` job's shell steps were
  executed as written in `update.yml` against a local bare repository standing in for GitHub (branch
  creation, no commit when nothing changed, one fast-forward commit when the board changed, refusal of a
  stray file). Not exercised locally: `actions/cache`, `gh cache`, artifacts, the Node build and the Pages
  deployment. The machine had no system `libgomp1`; scikit-learn's bundled copy was used instead. No Windows-only code path is left in the update,
  worker, export or database code (`deploy/*.ps1` is the Windows wrapper and is not used on Linux).
- Found and fixed: the worker's overlap lock used POSIX record locks (`fcntl.lockf`), which a process
  loses as soon as it closes any other descriptor of the file; reading the holder record did exactly
  that, so on Linux the lock was never actually held. It now uses `flock`
  (`tests/test_worker_lock.py::test_reading_the_holder_record_does_not_release_the_lock`). `pid_alive`
  no longer counts a zombie (ended, not yet reaped) as a running process.
- Known platform difference, not fixed here (model tests are not platform code):
  `tests/test_models_team.py::test_total_outputs_unchanged_synthetic` compares a sha256 of fitted-model
  outputs rounded to 1e-9 with a value recorded on Windows; the fit differs in the last bits on Linux, so
  `ci.yml` deselects that one test (and a workflow test pins that it is the only exclusion). It should
  become a tolerance-based comparison.
- Times are UTC on the runner (`TZ=UTC`): run stamps, picks file names and `local_to_utc` all agree.
  Kick-off conversions use explicit zones (`zoneinfo`, from the system tz database on Linux).

### State between runs

The runner is ephemeral. Source data and the database must not be published (public repository, releases,
Pages, artifacts), so they live only in the repository's **Actions cache** (not downloadable from the web
UI; readable by workflows of this repository). `python -m shots.worker.ci keys` computes the keys / paths.

| cache | key | contents | written |
|---|---|---|---|
| past seasons | `shots-fd-past-v1-<season>` | `data/raw/football-data/<1011..previous season>/` (immutable upstream) | once per season, after a successful update |
| current season | `shots-fd-current-v1-<season>-<run>` | `data/raw/football-data/<season>/` with ETags (conditional requests: an unchanged file costs a 304) | every run whose update cycle ran; 2 kept |
| **state** | `shots-state-v1-<run id>-<attempt>` | `data/shots.db` (WAL checkpointed into the one file), `data/raw/football-data/fixtures/` (newest 3), `data/processed/matches.parquet`, `data/processed/track_backfill.parquet` | every run whose update cycle ran (never by a run that found nothing due); 5 kept |
| models | `shots-models-v1-py312-<lock hash>-<models hash>` | `models_store/` (pickles: valid only for the exact packages) | when retrained (missing, older than 30 days, or the lock changed) |

Caches are immutable, so state is saved under a new key each run and restored by prefix (newest wins); older
ones are deleted (`gh cache delete`), leaving five generations to fall back on by hand. The state path list
is constant on purpose: a cache is only restored by the exact path list that saved it, so a season-dependent
path would orphan the database every July (`tests/test_worker_ci.py`). GitHub evicts caches not read for 7
days and caps a repository at 10 GB: the state cache is read by every run (hourly), the others by every full
update (about every 3 hours), and the total stays well under 1 GB (database ~45 MB uncompressed today, +0.3-0.6 GB per season).

Everything else is rebuilt: `features.parquet` every run, the fixtures from the committed `calendars/*.ics`.
A cold start (no cache at all) downloads ~374 season files with the usual 1.5 s delay (~10 min), trains both
models and the backfill, and then behaves like any other run. Measured in the local dry run (8 cores; the
hosted runner has fewer): first cycle with the raw files present but no database and no models 4 min 40 s
(update 249 s, of which training 92 s and the backfill 41 s); a following cycle 1 min 45 s (update 67 s).
The second cycle rebuilt an identical board, so the export wrote nothing and no commit was made.

**Protection of the live record** (the database's insert times are what makes a prediction official):
1. `Guard the live record`: if no state cache was restored but one exists (`gh cache list`), the run stops
   instead of starting from an empty database (a transient restore failure must not restart the record).
   Starting empty is allowed only when no state cache exists at all, or when the run is started by hand
   with `allow_fresh_state`.
2. `python -m shots.worker.ci db-check`: WAL checkpoint, `quick_check`, foreign-key check, and the saved
   baseline: the state is saved only if the database is intact and has not lost prediction runs,
   predictions or its record start since it was restored.
3. State is saved before anything is exported or pushed, also when the update was partial.

**Recovery if the cache is lost** (deleted by hand, or evicted after 7 idle days because the schedule was
disabled): the next run starts with an empty database and rebuilds everything from the sources. The
pipeline degrades gracefully and says so: the run log carries a warning, `meta.record.since` moves to the
restart time, results before the restart leave `results.json`, the live track record restarts at zero, and
the board is unaffected. The `data` branch still holds every board published before, with commit times.

Follow-up, not implemented: `python -m shots.db restore-from-export <git dir | snapshot dir>` to re-import
the past official predictions from the `data` branch history. It cannot be done with today's schema, by
design: migration `0003` makes the database reject a pre-match claim whose insert time is after kick-off,
and that rule is exactly what a restore would have to bypass. It needs a migration (`0004`) that adds an
attestation to restored rows (for example `predictions.attested_at` = the commit time, with the commit SHA
in the run's `params`), lets the pre-match trigger and `v_prediction_outcomes` accept that time in place of
`created_at` only for runs of a new kind `restored`, and labels them in the export
(`meta.record.restored_from`). This changes the integrity rule the 2026-10-09 verification signed off, so it
should be reviewed by the evaluator before it is built. Until then the five cache generations are the only
copies; an off-site backup would have to be an **encrypted** file (never a plain artifact or release: in a
public repository those are public).

### Before the repository goes public (owner's checklist)

- [ ] `calendars/*.ics` are third-party files and must be committed (the fixtures are built from them).
      Confirm that redistributing them in a public repository is acceptable, or move them to a private
      source later (a private repository or a secret-protected download step in `update.yml`).
- [ ] Tracked files that carry source data or are not meant for the public: `tests/fixtures/fd_*.csv` (a
      few football-data rows including odds), `dblive.pkl` and `rec.pkl` at the repository root (stray
      pickles), `reports/picks/*.csv` (the `mkt_p_over25` column is odds-derived once odds are published).
      Removing a file in a new commit does not remove it from history; publish a fresh history if needed.
- [ ] Fork pull requests can read the default branch's caches. Keep "Require approval for all external
      contributors" on (Settings -> Actions -> General) so nobody can run a workflow that prints the cached
      database.
- [ ] Protect the `data` branch with a ruleset: block force pushes and deletions (the workflow never needs
      either).
- [ ] GitHub disables scheduled workflows after 60 days without repository activity and may delay or skip
      scheduled runs under load (see "Triggers": hourly slots, watchdog, optional external cron); commits
      to the `data` branch count as activity. If the schedule is ever off for more than 7 days the caches
      expire (see Recovery).

## Before going public

Remaining items of the 2026-10-09 security review (the local 127.0.0.1 deployment does not need them):

- [ ] Lockout per (account, client) pair, with a CAPTCHA or progressive slowdown instead of a hard
      refusal, so an attacker cannot lock a victim out by guessing their email.
- [ ] Real client IP from the reverse proxy (`SHOTS_PROXY_HEADERS=1` + `SHOTS_FORWARDED_ALLOW_IPS`), and
      IPv6 clients keyed by their /64 prefix in the per-client limits.
- [ ] Enforce the `Secure` cookie (`__Host-` prefix for `shots_session` / `shots_prelogin`) and HSTS in
      production (served only over HTTPS).
- [ ] Email verification, then `REVEAL_EMAIL_TAKEN = False` (no account enumeration at registration).
- [ ] Explicit expected `Origin` configuration (the public origin) instead of comparing with `Host`.
- [ ] Password reset, account deletion and data export (GDPR), list of active sessions on `/account`.
- [ ] Privacy notice (what is stored: email, Argon2 hash, keyed hashes of IP / user agent, retention).
- [ ] Encrypted backups of `data/shots.db` (`VACUUM INTO` or the SQLite backup API, never a plain copy of
      a live WAL database) and of the auth key; in production pass the key via `SHOTS_AUTH_KEY`.
- [ ] Pinned dependency versions (with hashes) and `pip-audit` in the routine.
- [ ] Add the public host to `SHOTS_ALLOWED_HOSTS`.
- [ ] Decide whether Free users may open past match pages.
