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
  "counts": {"fixtures": 568, "with_odds": 0, "cold_start": 0, "results": 140, "provisional_results": 12},
  "awaiting_results": {"official": 169, "experimental": 0, "oldest": "2026-10-15"}
}
```
- `awaiting_results` (additive): matches with a pre-match prediction that had finished (lock moment + 2.5 h)
  by the latest board run's `built_at`, dated within 30 days of it, with no result yet. Measured against
  the board run, not the clock, so files stay deterministic. The site shows a "results pending" banner.

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

### Experimental fixtures (additive, schema 1; UEFA Champions League)
```json
{"id": "…", "league": "uefa-champions-league", "home": "Viking", "away": "Bayern Munich", "…": "…",
 "odds": false, "cold_start": true, "model": "negbin_pit_comp-noodds_2026-10-05-ucl-exp",
 "curve": [0.9999, 0.9997], "team": null,
 "experimental": true, "data_quality": "partial"}
```
- `experimental` is **`true` or absent** (never `false`). It marks a fixture of a competition we have no
  shot history for: the numbers are built from each club's domestic league form, their calibration on that
  competition is unknown, and it has no automatic results source: it is settled only when a result is
  entered by hand (`results_manual/`, see "Experimental results" below), and then appears in `results.json`
  flagged `experimental` and in `track.json` under `records.experimental`, **never** in `records.live`. The
  UI must label these fixtures and must not present them as equal to ordinary predictions. Method and limits: `reports/2026-10-13_champions_league.md`.
- `data_quality` (only with `experimental`): `"full"` = both clubs have domestic shot data; `"partial"` = one
  club has none and is replaced by a neutral stand-in (less informed; in practice barely wider); `"none"` = neither club has data (a
  generic estimate from the level of the known clubs' leagues). Absent on ordinary fixtures (= full).
- `team` is `null` for `partial` / `none` fixtures (a stand-in has no shots of its own); for `full` ones it is
  present and doubly experimental (cross-league strength is not modelled; the split carries extra spread).
- `league_avg` of such a fixture is the blend of the two clubs' domestic league averages (the mean of the
  two; the known club's league when one is unknown), not an average of the competition itself.
- `kickoff_local` / `date` are in the **home club's** country time zone.
- In `meta.leagues`, the competition has `"featured": true` (**`true` or absent**), `"country": "Europe"`,
  `"tier": 1`: `{"id": "uefa-champions-league", "name": "UEFA Champions League", "country": "Europe",
  "tier": 1, "fixtures": 52, "featured": true}`. The site pins featured competitions first.
- Ordinary fixtures and leagues carry none of these keys: their lines are byte-identical to before.

### `explain`: why the expected total is what it is (additive, schema 1)
```json
{"id": "…", "home": "Burnley", "away": "Charlton", "expected": 27, "odds": true, "…": "…",
 "explain": {
   "expected_mean": 27.4,
   "baseline": {"value": 26.8, "label": "Championship average this season"},
   "factors": [
     {"id": "away_defence", "side": "away", "label": "Charlton defence",
      "detail": "concedes 16.8 shots per match, last 10 (league 13.4)", "effect": 1.4},
     {"id": "away_attack", "side": "away", "label": "Charlton attack",
      "detail": "10.1 shots per match, last 10 (league 13.4)", "effect": -0.8},
     {"id": "market_strength", "side": "match", "label": "Match odds",
      "detail": "bookmaker odds make Burnley favourite", "effect": 0.2},
     {"id": "other", "side": "match", "label": "Smaller factors", "detail": "8 smaller factors combined", "effect": -0.1}
   ],
   "residual": -0.1,
   "signals": {"home_matches": 673, "away_matches": 745, "odds_used": true,
               "home_volatility": null, "away_volatility": null, "model_gap": 0.0}
 }}
```
- `explain` is an **object or absent** (never `null`). It is computed when the prediction is made, from the
  same fitted model and the same feature row, and stored with the prediction; the exporter only copies it.
  Method, error sizes and limits: `reports/2026-10-13_under_side_and_explanations.md`, `src/shots/models/explain.py`.
- **It adds up exactly:** `baseline.value + sum(factors[].effect) + residual == expected_mean`, in tenths of a
  shot. `residual` is rounding only (at most a few tenths). The UI may show the sum without recomputing.
- `expected_mean` is the **mean** of the published distribution, one decimal. The fixture's `expected` is its
  integer **median**; the mean is usually 0 to 0.7 shots above it (the distribution is right-skewed). Show
  the breakdown as leading to `expected_mean`, not to `expected`.
- `baseline.value` equals the fixture's `league_avg` (shots per match in that league this season; the league's
  long-run average in the first rounds; for experimental fixtures the blend of the two clubs' domestic
  leagues). `baseline.label` says which.
- `factors`: at most six named factors of at least 0.2 shots, sorted by absolute effect (largest first), then
  at most one entry `other` = every smaller factor combined (always last). `effect` is in shots, one
  decimal, never 0. `side` is `home`, `away` (a property of that team) or `match`. `label` and `detail` are
  display text with the real team names and the real numbers behind the factor. Factor ids:

  | id | meaning (what the model reads) |
  |---|---|
  | `home_attack`, `away_attack` | that team's shots for: last 10 (shrunk to the league), recency-weighted, season to date, on target |
  | `home_defence`, `away_defence` | that team's shots conceded (same windows); a leaky defence adds shots for the opponent |
  | `home_venue`, `away_venue` | home team's latest home matches / away team's latest away matches (shots for and against) |
  | `market_strength` | bookmaker match-result odds (who is favourite); only when `odds` is true |
  | `market_goals` | bookmaker over/under 2.5 goals odds; only when `odds` is true |
  | `home_rest`, `away_rest` | days since that team's previous match |
  | `home_early`, `away_early` | early-season / cold-start adjustments (few season matches, no history, division change) |
  | `home_standin`, `away_standin` | experimental fixtures only: the club has no shot data and is averaged over typical teams |
  | `league_level` | the model's own rating of an average matchup at this league level, minus the raw league average |
  | `calibration` | shift of the mean from the recalibration of the distribution on past forecast errors |
  | `home_rating_attack`, `away_rating_attack` | only with the candidate no-odds model (`fin_c`, off by default): that team's opponent- and venue-adjusted attack rating |
  | `home_rating_defence`, `away_rating_defence` | same, defence rating (shots conceded vs an average team) |
  | `head_to_head` | same model: totals of previous meetings of the two clubs vs what the ratings expected |
  | `season_context` | same model: stage of the season, last three league matches, nothing left to play for |
  | `surge` | only on fixtures scored by the odds model "Surge" (`cand_odds_surge`, the odds route since 2026-10-27; not Scotland Championship, which stays on `negbin_pit_comp`): label "Surge correction"; the out-of-sample correction of the total, detail lists the signals behind it (season end, head-to-head, low/high-goal run, momentum, ratings, league trend) with their shots |
  | `shrinkage` | same model: the pull of the forecast total toward the league level |
  | `other` | all factors not listed one by one |
- **No odds values, ever.** Market factors are worded as categories ("strong favourite", "favourite", "slight
  favourite", "about level"; "high-scoring", "average", "low-scoring"); their `label` / `detail` contain no
  digit (checked by the verifier). `signals.odds_used` is a boolean equal to the fixture's `odds`.
- `signals`: `home_matches` / `away_matches` = prior matches behind each side's form (domestic matches for
  experimental fixtures; 0 = cold start). `model_gap` = expected mean of the odds model minus that of the
  no-odds model for the same fixture, in shots (`null` when the fixture was scored without odds): a large
  absolute value means the market disagrees with shot form. `home_volatility` / `away_volatility` are
  reserved (`"low" | "average" | "high" | null`) and **always `null` today**: a team's recent spread of
  match totals was tested and does not predict its future spread, so no label is published.
- These are attributions of the model (how its coefficients split the credit), not causes: form and odds
  overlap, so do not read a small `market_*` effect as "the odds do not matter".
- Experimental fixtures carry `explain` too (from the experimental model; the same caveats apply).

### `surge`: Surge Watch labels (additive, schema 1)
```json
{"id": "…", "home": "Como", "away": "Venezia", "expected": 32, "odds": false, "…": "…",
 "surge": {"signals": ["beating_ratings", "home_mismatch"], "against": []}}
```
- A **surge** is a match that ends 5+ shots above our expected total (the integer median `expected`). `surge`
  lists the over-shoot patterns of `reports/2026-10-26_overshoot_patterns.md` this fixture matches before
  kick-off: `signals` = boosts (surges were more common than our curve said), `against` = dampers (less
  common). Ids, labels and the research numbers are in `data/surge.json`. **Labels only: the fixture's
  probabilities are exactly the published ones**, nothing is adjusted.
- `surge` is an **object or absent** (never `null`). Present when the fixture was assessed: a domestic
  fixture of a competition the research covered, scored by the model the research studied for its route (with
  odds `negbin_pit_comp`, without odds `fin_c`). Since 2026-10-27 the odds route is scored by the Surge model,
  which already corrects for these patterns, so **odds fixtures are labelled only when Surge is not used**
  (Scotland Championship, or a Surge fallback): no double counting. Absent for experimental (cross-border)
  fixtures, Surge-scored fixtures, other fallback or candidate models, and when the board run recorded its labels
  with another catalogue version than the published `surge.json`. Both lists may be empty ("assessed, nothing
  found").
- A label appears only when the catalogue says it `applies` on the fixture's route (`odds` true = route
  `odds`, else `noodds`); ids are in catalogue order; at most one of the season-end signals (`dead_rubber`,
  `dead_team`, `last_three`).
- Computed when the board is built (`shots.predict.surge`), from pre-match features only (each uses matches
  dated strictly before the fixture's date), and stored in the board run's metadata, never in a prediction
  row. The verifier checks them against the run.
- League-table signals (2026-10-28, `reports/2026-10-28_surge_table_signals.md`): `top_vs_bottom` and
  `promoted_struggling` (boosts, last third of the season), from the same pre-match league table as `context`.
- `strong` (**`true` or absent**): a **strong surge candidate** = `stacking.min_boosts` (2) or more `signals`, and
  only on a route where `surge.json` `stacking.routes.<route>.strong_applies` is true (2+ signals beat our curve
  out of sample, in the confirmation seasons too). Today that is the odds route only, i.e. fixtures scored by
  `negbin_pit_comp` (Scotland Championship, Surge fallbacks); never the no-odds route. Surge-scored fixtures are
  not labelled at all, so they are never strong. The exporter and the verifier recompute the rule.

### `context`: table position and season period (additive, schema 1)
```json
{"id": "…", "home": "Como", "away": "Venezia", "…": "…",
 "surge": {"signals": ["home_mismatch"], "against": []},
 "context": {"phase": "middle",
             "home": {"pos": 3, "of": 20, "pts": 21, "gp": 9, "left": 29, "to_top": 2, "to_safety": 12},
             "away": {"pos": 17, "of": 20, "pts": 8, "gp": 9, "left": 29, "to_top": 15, "to_safety": -1}}}
```
- `context` is an **object or absent** (never `null`), present on the fixtures Surge Watch assessed (the same set
  as `surge`, see above; it does not depend on the catalogue version, so it stays when the labels are withheld).
  Context only: it never changes a probability.
- Our own computation from results dated strictly before the fixture (the season-context features of the
  candidate build, `shots.features.candidates` family G), recorded with the board run (its meta, next to the surge
  labels) when the board is built. No odds, no source rows.
- Per team: `pos` = league position, 1 = top, ranked by points then goal difference (equal teams are ordered
  arbitrarily within their block; `null` before the team's first game); `of` = teams in the league (the season
  calendar's); `pts`; `gp` = games played; `left` = scheduled games left (format-aware: double round robin by
  default, four rounds in the Scottish Championship / Leagues One and Two, 38 in the Scottish Premiership; `null` for
  playoff formats, Belgium and Greece); `to_top` = leader's points minus own (0 = level or leading);
  `to_safety` = own points minus those of the best-placed team in the drop zone (the bottom round(15%) places,
  e.g. 3 of 20; **<= 0 = in the drop zone**; an approximation: real relegation and play-off places vary).
- `phase` (the competition-season at that fixture) is **the same definition the table signals use**
  (`shots.evaluation.surge_rules.phase_of`): `run_in` = a team has 6 or fewer league games left, else `late` = the
  last third (>= 2/3 of the scheduled games played, mean of the two teams), `middle` = >= 25%, `early` below.
  Playoff formats (Belgium, Greece; `left` is `null`) use the share of the regular season, count the playoff rounds
  as `late` and never have a `run_in`. `null` only when progress is unknown (never when `left` is known).

### Under probabilities (client-side, no new field)
`P(under N.5) = P(total <= N) = 1 - curve[N + 1 - thresholds.total[0]]`, valid for N = 9..44 with the
published range 10..45. Validation of this side, its limits and the recommended caveats:
`reports/2026-10-13_under_side_and_explanations.md`. The under-side track record needs no extra field
either: in `track.json` a reliability band `[lo, hi]` of P(total >= N) with `stated` s and `realised` r is
the under-(N-1).5 band `[1 - hi, 1 - lo]` with stated `1 - s` and realised `1 - r`.

## `data/results.json` (matches finished in the last 30 days that had an official pre-match prediction)
```json
{
  "schema": 1,
  "results": [{
    "id": "…", "league": "…", "date": "2026-10-04", "home": "Girona", "away": "Mallorca",
    "actual": {"total": 25, "home": 14, "away": 11},
    "predicted_at": "2026-10-02T09:43:00Z",
    "expected": 26, "lb": {"70": 22, "80": 21, "90": 19}, "odds": true,
    "curve": [0.9999, 0.9995],
    "result_source": "football-data"
  }, {
    "id": "…", "league": "uefa-champions-league", "date": "2026-10-13", "home": "Arsenal", "away": "Lille",
    "actual": {"total": 26, "home": 15, "away": 11},
    "predicted_at": "2026-10-13T08:12:00Z",
    "expected": 25, "lb": {"70": 22, "80": 20, "90": 18}, "odds": false,
    "curve": [0.9999, 0.9995],
    "result_source": "manual", "source_url": "https://www.example.org/match/arsenal-lille", "experimental": true
  }]
}
```
The client marks hit/miss for the visitor's threshold N as `actual.total >= N` next to `curve[N]`.
Only predictions the database counts as official appear (`v_official_picks` rule: live, pre-match; per match
the last pre-match board prediction), plus experimental ones settled by the same rule (see below).

### Surge labels of settled matches (additive, schema 1)
```json
{"id": "…", "league": "italy-serie-a", "date": "2026-10-27", "home": "Como", "away": "Venezia",
 "actual": {"total": 34, "home": 20, "away": 14}, "expected": 27, "…": "…",
 "surge": {"signals": ["home_mismatch"], "against": []}}
```
- `surge` (**object or absent**, never `null`): the Surge Watch labels the fixture carried in the board run its
  prediction comes from (the pre-match run of the `results.json` rule), exactly as recorded then, so a visitor can
  see how surge candidates turned out (surged = `actual.total >= expected + margin`, `margin` from `surge.json`).
  Absent when that run did not assess the fixture or recorded no labels (labels started 2026-10-27).
- Recorded with the catalogue of that day: they are not re-checked against today's `surge.json` (a catalogue
  update must not rewrite history). Clients ignore ids the current catalogue does not list. `strong` (true or
  absent) is copied as recorded too.
- `context` (**object or absent**): the table context the same run recorded for the fixture (shape of the board
  fixture's `context`), so outcomes can be read by season phase.

### Experimental results (additive, schema 1; UEFA Champions League)
- `result_source` (every result): `"football-data"` (automatic) or `"manual"` (typed in from a public match
  page, for competitions without an automatic source), or a provider name for a provisional result (below).
  `source_url` (only with `"manual"` or a provisional result): that page.
- `experimental: true` (**`true` or absent**) marks a result of an experimental prediction. Such results are
  listed so visitors can compare, but they belong to `track.json` `records.experimental`, never to
  `records.live`. A manual result can be corrected later: the file then shows the corrected numbers.
- Providers count shots slightly differently (blocked shots, headers off target); the number published is
  the one entered from `source_url`, no adjustment is applied.

### Provisional results (additive, schema 1; football-data leagues)
```json
{"id": "…", "league": "spain-la-liga", "date": "2026-10-18", "home": "Betis", "away": "Getafe",
 "actual": {"total": 26, "home": 15, "away": 11}, "…": "…",
 "result_source": "espn", "result_status": "provisional",
 "source_url": "https://www.espn.com/soccer/team-stats/_/gameId/1"}
```
- `result_status` (every result): `"provisional"` = entered by hand from `source_url` because football-data
  has not published the match yet; `"confirmed"` = the competition's own source (football-data, or the manual
  results of a competition without an automatic source). A provisional result **does** settle the
  prediction, in `records.live` too, and is replaced by football-data's numbers automatically when they
  arrive: the row then says `"result_source": "football-data"`, `"result_status": "confirmed"`, has no
  `source_url`, and its `actual` (hence hit / miss and every aggregate) may change by a shot or two.
- `result_source` of a provisional result is the provider's name (the host label of `source_url`, lowercase,
  e.g. `"espn"`); clients should treat any value other than `"football-data"` / `"manual"` as a provider name.
- `meta.counts.results` = rows in `results.json`, `meta.counts.provisional_results` = those with
  `result_status: "provisional"`. In `track.json`, `records.live.provisional` / `records.experimental.
  provisional` = how many of the record's `settled` picks rest on a provisional result, and every `daily` /
  `weekly` point has `provisional` = how many of its `matches` do.
- Suggested UI wording: a "Provisional" tag, "Entered by hand from ESPN; replaced by the official data when it
  arrives."

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
    "live": {"label": "Live picks (saved before kick-off)", "settled": 104, "pending": 113, "void": 0, "by_n": {},
             "daily": [{"date": "2026-10-13", "matches": 9, "mae": 4.1, "bias": 1.2, "floor80_held": 0.8889,
                        "by_n": {"24": {"picks": 7, "hits": 6}}}],
             "weekly": [{"date": "2026-10-12", "matches": 41, "mae": 4.4, "bias": 0.6, "floor80_held": 0.8293,
                         "by_n": {"24": {"picks": 30, "hits": 23}}}]},
    "experimental": {"label": "Champions League (experimental, manual results)", "settled": 0, "pending": 36,
                     "void": 0, "by_n": {}, "daily": [], "weekly": []}
  }
}
```
- `records.experimental` (additive, schema 1): same shape as `live`, from `v_experimental_picks` (experimental
  predictions, live scans, pre-match, deduplicated like the official record), settled by manual results. It is
  never merged into `live`; its `by_n` entries have the same fields (`mean_stated` included) once picks settle.
- `daily` / `weekly` (additive, schema 1, on `live` and `experimental`): accuracy over time, oldest first.
  `daily` = the 30 most recent **match dates** (venue-local, as `date` everywhere) with at least one settled
  match or pick; `weekly` = the 52 most recent weeks, `date` = the **Monday** of the week (venue-local match
  dates, weeks Monday to Sunday). Per point:
  - `matches`, `mae`, `bias`, `floor80_held`: over the settled matches of that day / week, one prediction per
    match chosen by the `results.json` rule (the last pre-match board prediction; experimental ones for
    `experimental`). `mae` = mean |actual - expected| (expected = the integer median `expected`), `bias` =
    mean (expected - actual) (**positive = the model expected more shots than were played**), 2 decimals;
    `floor80_held` = share of matches with `actual.total >= lb.80`, 4 decimals. `null` when no match (`matches`
    can be 0 on a day that only has settled picks).
  - `by_n`: **exactly the thresholds of the record's own `by_n`** (live scans save N = 24 today, so `{"24": …}`;
    `{}` for an empty record), with the record's pick definition: picks = settled picks of the same view
    (`v_official_picks` / `v_experimental_picks`) whose published P(total >= N) >= `selected_min_prob` (0.70),
    hits = those with actual >= N. Summed over all days, `picks` / `hits` equal `by_n[N].n` / `.hits` (when the
    record is within the window), so a weekly hit rate can be compared with `by_n[N].mean_stated`.

## `data/surge.json` (Surge Watch catalogue; additive file, schema 1)
```json
{
  "schema": 1,
  "report_date": "2026-10-26",
  "margin": 5,
  "matches": 49643,
  "period": "2019/20 to 2025/26",
  "method": "Out of sample: walk-forward predictions of our production models ...",
  "routes": {"odds": {"label": "Scored with odds", "base_rate": 0.2142},
             "noodds": {"label": "Scored without odds", "base_rate": 0.2117}},
  "signals": [{
    "id": "dead_rubber", "label": "Nothing to play for", "direction": "boost",
    "explanation": "Neither team can still reach the title or drop into the relegation places ...",
    "routes": {
      "odds": {"n": 1341, "over_rate": 0.2737, "stated_rate": 0.2146, "base_rate": 0.2142,
               "excess_ci": [0.0332, 0.0824], "seasons_held": 7, "seasons": 7, "applies": true},
      "noodds": {"n": 1341, "over_rate": 0.2267, "stated_rate": 0.2195, "base_rate": 0.2117,
                 "excess_ci": [-0.0165, 0.0284], "seasons_held": 4, "seasons": 7, "applies": false}
    }
  }]
}
```
- The research's own numbers, never recomputed at export time: the committed table
  `src/shots/evaluation/surge_signals.json` is generated from `reports/2026-10-26_overshoot_patterns.json` by
  `python -m shots.evaluation.surge table` and copied field by field. Deterministic: no timestamp, so the file only
  changes when the catalogue does.
- Per signal and route (`odds` = fixtures scored with odds, `noodds` = without): `n` matches in the segment,
  `over_rate` = share that ended `margin`+ shots above our expected total, `stated_rate` = what our own published
  curves gave for that event on the same matches, `base_rate` = the share over all matches of the route ("usual"),
  `excess_ci` = 95% interval of `over_rate - stated_rate` (day-block bootstrap; `winter` pools two months with a
  normal approximation), `seasons_held` of `seasons` (with 20+ matches) in which the difference had the signal's
  direction, `applies` = the interval excludes 0 **and** it held in at least 5 seasons. A signal that does not
  apply on a route is still listed (its numbers show why: usually our model already accounts for it) but never
  labels a fixture on that route.
- `direction`: `boost` (surges more common than our curve said) or `damper` (less common). Signal ids today:
  `dead_rubber`, `dead_team`, `last_three`, `h2h_above`, `low_goal_run`, `beating_ratings`, `home_mismatch`
  (boosts), `winter`, `below_ratings` (dampers); clients must ignore unknown ids.
- The site is expected to say it plainly: signals come from about 50,000 past matches out of sample; they are a
  lean, not a promise; the probabilities shown are unchanged.
- Optional for clients: a site without `surge.json` simply hides Surge Watch.
- `stacking` (additive, 2026-10-28): matches by number of applying boost signals, per route, the research's numbers
  copied from the catalogue: `min_boosts` (2), `note`, `groups` (`"0"`, `"1"`, `"2"`, `"3+"`, `"2+"`), `phases`,
  and per route `signals` (the boosts counted), `share` (share of matches per group), `by_count` and `season_phase`
  (cells `n`, `over_rate`, `stated_rate`, `excess_ci`, `seasons_held`, `seasons`; an empty group has only `n`) and
  `strong_applies` (whether fixtures of that route get the `strong` label). Example: odds route `by_count["2+"]` =
  `{"n": 3268, "over_rate": 0.2512, "stated_rate": 0.2115, "excess_ci": [0.0268, 0.0525], "seasons_held": 7,
  "seasons": 7}`, `strong_applies: true`; no-odds `strong_applies: false` (fails the strict out-of-sample test).

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
  moves forward if the database had to be rebuilt), `track.records.live.void`, `track.records.experimental`, `daily` / `weekly` on the live and experimental
  records, result `result_source` / `source_url` / `experimental` (see "Experimental results"), result
  `result_status`, `meta.counts.results` / `provisional_results`, record and point `provisional` (see
  "Provisional results"), and, for cross-border
  competitions only, fixture `experimental` / `data_quality` and league `featured` (see "Experimental
  fixtures"), fixture `explain` (see "`explain`"), fixture `surge` and the file `surge.json` (see "`surge`" and
  "`data/surge.json`"), fixture `context` (see "`context`") and result `surge` (see "Surge labels of settled
  matches"). `meta.counts.cold_start` includes experimental fixtures with a club without data.
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
