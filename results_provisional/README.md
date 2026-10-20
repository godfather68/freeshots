# Provisional results (football-data leagues)

Hand-entered final shot counts for matches of competitions football-data.co.uk covers, used only while
football-data has not published the match yet. As soon as it does, its numbers replace these automatically
(the result becomes "confirmed"); the entered values stay in the database for the provider-agreement audit
(`python -m shots.db provisional --agreement`). These rows settle predictions (official record included) but
are flagged "provisional" on the site, and they never enter model training or features.

- One file per competition and season: `<league id>_<YYYY-YY>.csv`, league id = slug of the competition name
  (`england-premier-league`, `spain-la-liga`, `germany-2-bundesliga`, `scotland-premiership`, ...). Cups and
  other competitions without football-data (Champions League, ...) go to `results_manual/` instead.
- Columns (header required): `date,home,away,home_shots,away_shots,home_sot,away_sot,home_goals,away_goals,source,entered_at,notes`
  - `date`: the match date as in our fixtures (venue-local); `home` / `away`: calendar or canonical names.
  - shots = total shots incl. blocked; `home_sot` / `away_sot` optional (both or neither).
  - `source`: the page URL (e.g. ESPN team stats); `entered_at`: ISO UTC, e.g. `2026-10-19T21:05:00Z`.
- Check before committing: `python -m shots.db provisional --check` (lists the match each row resolves to;
  exit 1 on any problem). Pushing a change here triggers a publish.

Details: `docs/platform.md`, "Provisional results".
