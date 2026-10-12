# Static data contract (Stage 1: GitHub Pages + GitHub Actions)

The static site reads these JSON files, published next to it under `data/`. They are produced by
`python -m shots.export --out <dir>` from the SQLite database (through `shots.db.repo`), so they
carry exactly what the server-rendered app shows to a full-access user. There is no login in
Stage 1: everything published is public.

Rules
- **Never publish raw source data** (football-data.co.uk rows, odds, calendars). Only our own
  predictions, fixture identity (teams, league, kick-off), and for finished matches the final total
  and per-team shot counts needed to mark a prediction hit or miss.
- Probabilities are the models' calibrated values, rounded to 4 decimals; never recomputed client-side.
- Every file has `"schema": 1`. Additive changes keep the number; breaking changes bump it.
- Files are deterministic for the same database state (stable ordering, no timestamps except `built_at`),
  so an unchanged board produces no git diff.
- All times: `kickoff_utc` ISO 8601 `Z`; `date` and `kickoff_local` are league-local.

## `data/meta.json`
```json
{
  "schema": 1,
  "built_at": "2026-10-12T09:00:00Z",
  "results_up_to": "2026-10-11",
  "last_job": {"status": "ok", "finished_at": "2026-10-12T08:59:40Z"},
  "models": ["negbin_pit_comp-noodds_2026-10-05"],
  "thresholds": {"total": [10, 45], "team": [3, 25], "default_total": 24, "default_team": 12},
  "leagues": [{"id": "england-premier-league", "name": "Premier League", "country": "England", "tier": 1, "fixtures": 50}],
  "days": ["2026-10-12", "2026-10-13"],
  "counts": {"fixtures": 568, "with_odds": 0, "cold_start": 0}
}
```

## `data/board.json` (upcoming fixtures, sorted by kickoff then league then home)
```json
{
  "schema": 1,
  "fixtures": [{
    "id": "ae9173bf740eb7b5",
    "league": "england-premier-league",
    "round": "Regular Season - 6",
    "date": "2026-10-10", "kickoff_local": "17:30", "kickoff_utc": "2026-10-10T16:30:00Z",
    "home": "Man United", "away": "Tottenham",
    "expected": 30, "lb": {"70": 26, "80": 25, "90": 22},
    "league_avg": 27.8, "above_league": 2.2,
    "odds": false, "cold_start": false, "model": "negbin_pit_comp-noodds_2026-10-05",
    "curve": [0.9999, 0.9997],
    "team": {
      "home": {"mean": 16.4, "median": 16, "lb80": 12, "curve": [1.0, 0.9998]},
      "away": {"mean": 13.2, "median": 13, "lb80": 9, "curve": [1.0, 0.9991]}
    }
  }]
}
```
- `curve[i]` = P(total shots >= thresholds.total[0] + i); non-increasing; length = total range size.
- `team.*.curve[i]` = P(that team's shots >= thresholds.team[0] + i). `team` is `null` when the model
  has no team calibration. Team numbers are **experimental** and must be labelled so in the UI.

## `data/results.json` (matches finished in the last 30 days that had an official pre-match prediction)
```json
{
  "schema": 1,
  "results": [{
    "id": "…", "league": "…", "date": "2026-10-04", "home": "Girona", "away": "Mallorca",
    "actual": {"total": 25, "home": 14, "away": 11},
    "predicted_at": "2026-10-02T09:43:00Z",
    "expected": 26, "lb": {"70": 22, "80": 21, "90": 19}, "odds": true,
    "curve": [0.9999, 0.9995]
  }]
}
```
The client marks hit/miss for the visitor's threshold N as `actual.total >= N` next to `curve[N]`.
Only predictions the database counts as official (`v_official_picks` rule: live, pre-match) appear.

## `data/track.json`
```json
{
  "schema": 1,
  "records": {
    "backfill": {"label": "2025/26 season (out of sample)", "by_n": {"24": {
      "selected_min_prob": 0.7, "n": 1085, "hits": 846, "hit_rate": 0.7797, "mean_stated": 0.7533,
      "wilson95": [0.754, 0.803], "brier": 0.2205, "brier_baseline": 0.2419,
      "reliability": [{"band": [0.7, 0.8], "n": 925, "stated": 0.739, "realised": 0.763}],
      "by_league": [{"league": "netherlands-eredivisie", "n": 239, "hit_rate": 0.77, "stated": 0.774}]
    }}},
    "live": {"label": "Live picks (saved before kick-off)", "settled": 0, "pending": 127, "by_n": {}}
  }
}
```

## Audit trail
Each scheduled run commits the regenerated `data/*.json` to the published branch. Git history (with
GitHub's server-side timestamps) is the public proof of what was predicted before kick-off.

## Exporter notes (implementation of this contract)
The shapes above are unchanged. Details an implementer of the site needs (JSON Schema:
`docs/static-api.schema.json`; design: `docs/platform.md`, "Stage 1"):
- **Nullable:** `meta.last_job` (no update run recorded yet), `meta.results_up_to`, `meta.thresholds.team`
  and a fixture's `team` (no team curves), `round`, `kickoff_local`, `kickoff_utc` (unknown: a match can
  have a local time without a UTC instant), `model`, `league_avg`, `above_league`,
  `track.records.backfill` (no backfill), and in a `by_n` entry `brier_baseline`, `hit_rate`, `mean_stated`,
  `wilson95` (no selected picks).
- **Additive fields:** `meta.record.since` (start of the live record of the database behind this export; it
  moves forward if the database had to be rebuilt) and `track.records.live.void`.
- `meta.leagues` lists every league id used in any file; `fixtures` is 0 when a league only appears in
  `results.json` or `track.json`. League `name` has no country prefix ("Premier League").
- `expected`, `lb.*`, `median`, `lb80` are integers; `mean` has 2 decimals; `league_avg` / `above_league` 1.
- `board.json` lists fixtures dated on or after the UTC date of `meta.built_at` that have no result yet,
  ordered by `date`, `kickoff_local` (unknown first), league (full competition name), `home`.
  `results.json` is ordered newest first. `predicted_at` is when the prediction was recorded in the
  database (always before kick-off).
- `reliability` lists only bands that contain picks; `by_league` is ordered by `n` descending.
- On the audit branch (`--meta changed`) `built_at` and `last_job.finished_at` only move when something
  else changed; the deployed site is exported with `--meta always`.
