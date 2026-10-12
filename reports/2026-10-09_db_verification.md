# DB verification (Phase C), 2026-10-09

Independent check of the SQLite system of record (`data/shots.db`, revision `0002_pre_match`) against the
file-based track record (`src/shots/predict/track.py`), plus integrity, kick-off, web and board sanity checks.

**Method.** The real DB was only ever read: it was copied with the SQLite backup API from a `mode=ro`
connection. Every experiment ran on throw-away copies in a temp dir. The real DB's WAL changed at 14:47 local
because of a session row written by another `python -m shots.web` (PID 22920, port 8000) that was not started
by this check; my own server on the copy made 23 GET requests and wrote no session rows.

DB contents at check time: 5 runs (board 568, picks 117 + 126 + 46 replay, backfill 7,094 matches x 3 N),
94,678 played matches, 3,306 scheduled.

## Verdicts

| # | Check | Verdict |
|---|---|---|
| 1 | DB track record = file track record (live and backfill) | **PASS** (identical today; one latent divergence, see 1c) |
| 2 | Append-only integrity | **FAIL**: two bypasses, plus replay rows counted as official |
| 3 | Kick-off UTC / local / DST, pre_match flags | **PASS** |
| 4 | Web pages = DB views; Free plan does not leak | **PASS** |
| 5 | Board sanity, 568 fixtures | **PASS** with one minor inconsistency (median vs curve) |

## 1. DB vs file track record: PASS

**Live** (`Repository.track_records("live")` vs `track.settle(out_path=None)` and the saved `track_record.parquet`):
- 172 official picks on both sides (45 settled, 127 pending, 0 void). The (match, N) keys are identical.
- p, status, actual, hit, scan_ts, source, date and competition match: 0 mismatches. The saved parquet equals a fresh `settle()`.
- The `pre_match` filter in `v_official_picks` drops nothing today. The tightest gap between a run's `built_at` and kickoff is 7.5 h, so on real data the filter has never had anything to drop.
- Web metrics, full access, live N=24: 45 settled in 7 leagues. 6 picks at P >= 70%, 5 hits (83.3%, Wilson 44% to 97%), mean P 73.1%. The reliability bands match a direct SQL group-by.

**Backfill** (latest `backfill` run vs `track_backfill.parquet`):
- 21,282 rows (7,094 matches x N = 22/24/26). Keys, dates, competitions, actuals and hits are identical; all rows settled.
- p differs by at most 5.0e-7, because `import_backfill` rounds p to 6 dp. No row crosses a band edge or the 0.6/0.65/0.7/0.75/0.8 thresholds.
- `track.metrics` vs `Repository.track_summary` for N in {22, 24, 26} and min_prob in {0.6, 0.7, 0.8}: `all` and `selected` are equal. Reliability, per-league and cumulative tables have identical counts and hits; their floats differ by at most 4.8e-7. Example: N=24, P >= 0.7 gives 846/1085 = 78.0% (CI 75.4 to 80.3) against a mean P of 75.3%.

**Differences by design, and whether they are correct:**
- (a) **Settlement source.** The DB settles against the current `matches` row. `track.py` settles the backfill against the `actual` frozen in the parquet. After an upstream correction the two would diverge. The DB is the correct side; I tested this in check 2.
- (b) **Pending vs void.** The DB marks a pick pending when the match is not `played` and void when it is played without shots. `track.py` marks it pending when the match is missing from `matches.parquet`. The two are equivalent as long as `matches.parquet` keeps rows that have no shots data. No void rows exist today.
- (c) **Latent divergence, the file side is wrong.** `track.dedupe` (`src/shots/predict/track.py:78`) keeps the last scan on or before the match **date**, so a scan taken after kick-off on match day counts. The DB filters on `pre_match` before ranking, so it falls back to the last pre-kick-off scan. I simulated this on a copy: a 13:00 WAT scan for a 10:30 UTC kick-off. The DB kept the 09:43 scan (p = 0.707). `track.py` took the post-kick-off scan (p = 0.999). The DB behaviour is correct.
  - **Fix:** retire the file-based record, or make `dedupe` drop scans with `scan_ts >= kickoff`.

## 2. Integrity: FAIL

**Blocked as intended.** All of these were tried on copies:

| Attempt | Result |
|---|---|
| Raw `UPDATE predictions` (played match, future match, `SET pre_match=1` on all rows) | ABORT |
| `INSERT ... ON CONFLICT DO UPDATE` | ABORT |
| `DELETE` of a played match's prediction | ABORT |
| `DELETE` for a scheduled match whose date has passed (Sabadell v Andorra, 2026-10-03) | ABORT |
| ORM bulk `update()`, attribute update + flush, `session.delete`, bulk `delete()` of run 4, `load.delete_run(4)` | all `IntegrityError` |
| ORM delete of a `prediction_runs` row (`foreign_keys=ON`) | FK error |

**Bug 2a: `INSERT OR REPLACE` rewrites a locked, settled prediction.** On a copy, prediction 1343's `points` went from `{"24": 0.509}` to `{"24": 0.05}`, and `v_official_picks` followed. REPLACE deletes the conflicting row internally, and SQLite does not fire DELETE triggers for that unless `recursive_triggers` is on (it is off).
- Where: `migrations/versions/0001_initial.py:36-42` (`TRIGGERS`).
- Fix, in a new migration:
  ```sql
  CREATE TRIGGER trg_predictions_no_replace BEFORE INSERT ON predictions
  WHEN EXISTS (SELECT 1 FROM predictions WHERE id = NEW.id OR (run_id = NEW.run_id AND match_id = NEW.match_id))
  BEGIN SELECT RAISE(ABORT, 'predictions are append-only: replace is not allowed'); END;
  ```

**Bug 2b: `prediction_runs` is unprotected.** No triggers guard it.
- Deleting run 4 with a plain `sqlite3` client succeeded, because that client has `foreign_keys` off by default. It left 46 orphan predictions, and the settled live record dropped from 45 to 0.
- `UPDATE prediction_runs SET params = json_set(params, '$.scan_ts', ...)` (or `built_at`) silently changes which pick is official and whether it counts as pre-match. On a copy it took the settled official record from 45 to 0.
- Fix, in the same migration:
  - Add `BEFORE DELETE ON prediction_runs WHEN EXISTS (SELECT 1 FROM predictions WHERE run_id = OLD.id)` that raises.
  - Add `BEFORE UPDATE OF kind, source_key, built_at, params, models, content_hash ON prediction_runs` that raises. Leave `confirmed_at` and `meta` writable, because `import_board` updates them (`src/shots/db/load.py:509-511`).

**Finding 2c: predictions created after their match's lock moment still count as official.**
- `pre_match` is computed from the run's claimed `built_at`, which is the time in the CSV file name (`src/shots/db/load.py:568`, `:487`), not from the actual insert time `created_at`.
- All 46 replay predictions, 45 settled and 1 pending, were inserted at 2026-10-09 10:43:58 UTC, after their matches (2-5 Oct). They are all in `v_official_picks`, which the web shows as "Live scans".
- This is consistent with the documented rule ("replays included") and with `track.py`. But a CSV named with a back-dated live timestamp would be accepted the same way.
- All 126 live official picks were inserted before kick-off (earliest kick-off 16:30 UTC).
- Fix:
  - Keep replays out of the "live" record (separate tab or label), and require `created_at < lock moment` for `source = 'live'` in `v_official_picks` (`migrations/versions/0002_pre_match.py:71`).
  - Add a CHECK or trigger that `built_at <= created_at`.

**0002 back-fill.**
- Stored `pre_match` equals the migration formula and the insert-time `lock_moment` rule for all 7,951 rows. The view's re-check agrees as well (backfill 0, board 1, picks 1).
- A downgrade to 0001 and upgrade to 0002 on a copy reproduced every flag (0 differences), and the triggers came back.
- The only `UPDATE predictions` in the code base is `0002_pre_match.py:33`, wrapped in a trigger drop and re-create.
- Every row in a run shares one `created_at`, and the UPDATE trigger forbids anything else. So the back-fill was the only write. (SQLite keeps no write log, so this is inferred, not observed.)
- The backfill run has `pre_match = 0` by construction (`built_at = utcnow()`). The repository's backfill query ignores the flag, which is correct for an out-of-sample replay.

**Result corrections propagate.** On a copy I upserted a corrected result through `load.upsert_matches` for one settled hit:
- With the corrected shots, the pick became settled, actual 1, hit False.
- With shots removed, it became void and the counts became 44 settled + 1 void.
- A backfill match also picked up the new actual.

## 3. Kick-offs and pre_match: PASS

- **Calendars.** I re-parsed every `DTSTART` independently: 3,305 events across 11 calendars. All 3,305 DB rows match on `kickoff_utc` (exactly the source UTC), `kickoff_local` (zoneinfo of the league's zone) and local `date`.
- **DST end on 25 Oct 2026 is handled.**
  - Premier League 12:30 local: 11:30 UTC on 24 Oct, 12:30 UTC on 31 Oct.
  - Bundesliga 15:30: 13:30 UTC, then 14:30 UTC.
  - La Liga 14:00: 12:00 UTC, then 13:00 UTC.
  - Turkey (no DST) 13:30: 10:30 UTC on both dates.
- **football-data UK times.** The latest `fixtures_*.csv` (all three downloads) only holds 2-5 Oct rows, so no DB fixture currently takes its kick-off from football-data. I tested `uk_kickoffs` directly:
  - 15:00 UK becomes 14:00 UTC on 24 Oct and 15:00 UTC on 31 Oct.
  - Spain, Greece and Turkey local times convert correctly.
  - The ambiguous 01:30 on 25 Oct resolves to BST, and a country with no zone gets local time None. All correct.
- **Played matches.** All have `kickoff_utc` NULL (`matches.parquet` has no times). They lock at 00:00 UTC of the match date, which is conservative for European leagues.
- **pre_match** is right for every board and picks row (minimum margin 7.5 h). A simulated post-kick-off scan got `pre_match = 0` and was excluded (see 1c).
- The machine's zone is W. Central Africa (UTC+1, no DST). `local_to_utc` is therefore a fixed -1 h, which matches the stored `built_at` values.

## 4. Web: PASS

I started the app on a copy (`SHOTS_DB`, port 8765) and made anonymous requests, so the Free plan applied (top 5 leagues, 3 days ahead, no team or detail data).

**`/track`.** Headline numbers equal SQL over the views restricted to the top 5 leagues:

| src | N | Matches | Hits / selected | Hit rate | CI | Mean P |
|---|---|---|---|---|---|---|
| backfill | 22 | 1,752 | 809/1024 | 79.0% | 76-81% | 78.5% |
| backfill | 24 | 1,752 | 206/264 | 78.0% | 73-83% | 75.6% |
| backfill | 26 | 1,752 | 22/28 | 78.6% | 60-90% | 74.9% |

- The Live nav shows "0 settled, 42 pending", matching SQL; the live record has no top-5 settled picks.
- No reliability rows and no other league names appear for Free.
- With full access (TestClient), live and backfill numbers and the reliability tables match the DB (section 1).

**`/match/{id}`.**
- For 5 visible matches, the median, the 80% floor and all 36 curve points equal the DB `total_curve`.
- The team section is only a lock card; the 15.9 found in the page was an SVG coordinate.
- Non-top-5 matches and top-5 matches more than 3 days ahead return 404.

**`/`.** Exactly the 39 visible match ids are shown. `?league=Turkey+Super+Lig`, `?when=season`, a team filter, and a combination of these show nothing beyond those 39.

## 5. Board sanity (568 fixtures, run 1): PASS, one minor issue

- **DB vs parquet.** `board.parquet` equals DB run 1 exactly (ids, the three curves, scalars, model, odds flag).
- **Curves.** Total (n = 10..45), home and away (3..25) curves have no NaN, stay within [0, 1], and never increase with n.
- **Bounds.**
  - Total and team `lb_70/80/90`: P(>= lb) >= q in 568/568.
  - P(>= lb+1) < q everywhere except 1-2 rows where the 4-dp rounding of p lands exactly on q (for example Rizespor v Fenerbahce, p_26 = 0.8000). This is harmless.
  - Ordering lb90 <= lb80 <= lb70 <= median holds everywhere.
- **Team vs total consistency.**
  - The total mean implied by the curve minus (home_mean + away_mean): mean -0.01, max 0.44.
  - 0 violations out of 145,408 of P(T >= a+b) >= P(H >= a) + P(A >= b) - 1.
  - The team medians are consistent with the team curves.
- **Routing.** `n_with_odds` = 0. All 568 rows are `odds = False`, have `mkt_p_over25` null and carry the label `negbin_pit_comp-noodds_2026-10-05`. Neither `fixtures.parquet` nor the DB has odds for any of them (the football-data fixture file is stale). The labels are correct.
- **Minor: the median does not agree with the calibrated curve in 127/568 rows (22%).**
  - In 114 rows the displayed median is 1 below the curve's 50% point; in 13 rows it is 1 above.
  - Cause: `src/shots/models/calibrated.py:258` takes the median from the raw negative-binomial CDF, while the curve and the bounds use the recalibrated (PIT-mapped) probabilities.
  - On the match page, "middle estimate X" can therefore sit next to a curve that says P(>= X+1) >= 50%.
  - Fix: compute the median from the recalibrated CDF, the same way the bounds are computed (`lb_at_level` at the level mapped from 0.5).
- **Note.** `above_league` compares a median with a league **mean**, so 69% of rows show as below the league (mean -1.1). This is right-skew, not signal. Compare like with like, or rename the field.

## Summary of bugs

| # | Where | Severity | Fix |
|---|---|---|---|
| 2a | `migrations/versions/0001_initial.py:36` | high | BEFORE INSERT trigger against REPLACE on predictions (new migration) |
| 2b | no triggers on `prediction_runs` | high | block DELETE of runs that have predictions and UPDATE of identity/timing columns |
| 2c | `src/shots/db/load.py:487,568`; `0002_pre_match.py:71` | medium | official live record: require `created_at < lock`; separate replays; enforce `built_at <= created_at` |
| 1c | `src/shots/predict/track.py:78` | medium (latent) | drop post-kick-off scans, or retire the file record in favour of the DB |
| 5 | `src/shots/models/calibrated.py:258` | low | median from the recalibrated CDF |
| 1a | `track.py` backfill uses a frozen `actual` | low | read actuals from current results (the DB already does) |

**Verdict: the DB is not ready to be the sole system of record for an auditable track record until 2a and 2b
are closed.** Its numbers are right today: it equals the file record and the web shows exactly the view values.
But its append-only guarantee can be bypassed with plain SQL (REPLACE, or editing or deleting runs). Once 2a and
2b are fixed, the DB should replace `track.py` as the source of truth, because it is already stricter on
post-kick-off scans (1c).
