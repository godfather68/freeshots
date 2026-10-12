# Leakage audit: ingest + team-form features, 2026-10-08

Auditor: evaluator agent (did not write the pipeline). Scope: `src/shots/ingest/football_data.py`,
`src/shots/ingest/schema.py`, `src/shots/features/team_form.py`, `data/processed/{matches,features}.parquet`
(94,678 matches, 2010/2011 to 2026/2027, data to 2026-10-05).
Reproduce: `python -m shots.evaluation.leakage_audit` (about 11 min; prints JSON).

## Verdict: PASS, no leakage found

Every check came back clean. The independent brute-force recomputation matches the pipeline exactly on
10,830 values. Rebuilding with the future removed and same-day results scrambled changes nothing.
Odds are the pre-match columns, not closing. No feature looks too good. The findings below are
latent risks or production-parity notes. None of them are active leaks.

## Empirical checks

| # | Check | Result |
|---|-------|--------|
| 1 | **Brute-force recomputation.** All 57 features (l5/l10/std/ewm/venue_l5 for shots and SOT, for and against, rest days, n_prior, tier_change, shrunk, league prior, market probabilities) were recomputed from `matches.parquet` with code written from scratch. Only matches with date strictly before the match were used. Sample: 160 random matches plus 30 "first match of a team's season" matches. | 190 matches, 10,830 values, **0 mismatches** (tolerance 1e-9) |
| 2 | **Truncation and perturbation.** For 5 match days (2011-08-21, 2015-08-15, 2019-11-10, 2020-02-15, 2024-11-30) every later match was dropped, the shots/SOT of all matches on that day were replaced with random values, and features were rebuilt. Features of that day's matches must not change. | 300 matches, **0 changed values** |
| 3 | **Correlation with the same-match target.** Max \|Spearman\| with total_shots is 0.384 (`league_prior_shots`). Team-form features are 0.26 to 0.34, which is plausible for pre-match information. **Same-match vs next-match test:** a feature's correlation with the team's shots in this match (0.303 for l5) is no higher than with its next match (0.311). With leakage, the same-match value would jump. Matches inside the window show the expected high correlation (0.59). | No signal of leakage |
| 4 | **Odds are pre-match.** Seasons 2010/11 to 2018/19 use `BbAv*`, which is pre-match (those files have no closing columns). From 2019/20 on, `Avg*` is used. The parquet odds were compared with the raw CSVs for all 51,028 matches from 2019 on: `odds_home` equals `AvgH` in 51,028/51,028 rows, while it differs from closing `AvgCH` in 49,166 rows. `odds_over25` equals `Avg>2.5` in 51,028/51,028 rows. As a cross-check, closing P(over 2.5) correlates more with total shots (0.259) than the pre-match value does (0.234), which is consistent with pre-match being used. | Pre-match confirmed |
| 5 | **Duplicates and identity.** 0 duplicate `match_id`. 0 identical (date, home, away) rows across competitions. 0 cases of a team playing twice on one date. 0 team names shared across countries. 0 teams in two competitions in the same season. Repeated fixtures within a season occur only in Scotland (split format), Belgium and Greece (play-off rounds). These are real matches on different dates. | Clean |
| 6 | **Date sanity.** 0 matches outside the [1 Jul Y, 31 Aug Y+1] window of their season, so no day/month misparse. Max date is 2026-10-05, with no future-dated rows. | Clean |
| 7 | **Staleness.** A fresh `build_features(matches)` equals `features.parquet` exactly. | Up to date |
| 8 | **League prior.** Included in the brute force (#1), which covers the season-to-date path and the early-season fallbacks to the competition and global means, all strictly before the date. Also covered by #2. | Strictly-before confirmed |

## Code review notes (latent risks, not active leaks)

| Severity | Finding | Suggested fix (owner: feature/ingest agents) |
|---|---|---|
| Low | Teams are keyed by **name only** (`team_form._team_long`). Today no name is shared across countries (check #5). Adding leagues, cups or Opta names could silently merge two clubs' histories, for example two different "Sporting" or "Hamilton" clubs. | Key on `(country, team)` or a stable `team_id` from the alias table. Add a test that fails on cross-country name collisions. |
| Low | The odds fallback chain (`Avg` -> `BbAv` -> `B365`) is safe today because no `*C*` closing column is in it. A future edit could add one without anyone noticing. | Add a unit test asserting no candidate in `ODDS_COLUMNS` matches the closing pattern (`AvgC`, `B365C`, `MaxC`, `PSC`, ...). |
| Low (production parity) | football-data "pre-match" odds are a snapshot taken around Friday afternoon for weekend games and Tuesday for midweek games. That is pre-kickoff but not "at prediction time". | At inference, use odds captured at a similar lead time. Never use closing odds. Otherwise backtest and production differ. |
| Info | `features.parquet` stores the targets (`home_shots, away_shots, total_shots`) next to the features. | Mitigated: the backtest harness removes target columns from the rows passed to `predict`, and `models/common.py` asserts FEATURES does not overlap TARGETS. |
| Info | Matches without shots are dropped in ingest before features are built. `rest_days` and `n_prior_*` therefore ignore cup and missing-data matches. This is a feature-quality issue, not leakage. | Optional: build the team calendar (rest days) from all parsed rows, including those without shots. |
| Info | Dates are local calendar dates. "Strictly before" excludes earlier kick-offs on the same day. This is conservative and correct. `_day_first` handles same-day doubles. There are 0 such cases in the data today, so that path is covered by unit tests only. | None. |
| Info | Backtest seasons overlap slightly. 11 matches from 2019/20 (Serie A, Segunda) were played after 2020/21 started. The season-level split excludes them from 2020/21 training (strict date rule). | None. This is the harness working as intended. |
