# UEFA Champions League: experimental predictions from domestic form (2026-10-13)

**Status: experimental. No calibration evidence on the Champions League exists, and none can be
produced with the data we have.** Everything below that looks like a coverage number comes from
domestic proxies and says only what the proxy says.

Code: `src/shots/ingest/cross_border.py` + `club_countries.csv` (clubs), `src/shots/features/cross_border.py`
(features), `src/shots/models/cross_border.py` (model), `src/shots/evaluation/cross_border.py` (this
validation; raw numbers in `reports/2026-10-13_champions_league_validation.json` and
`..._validation_finals.json`). Reproduce: `python -m shots.evaluation.cross_border --cache <dir> --jobs 5`
(27 model fits) and `--finals`.

## What we have and what we do not

- Fixtures: `calendars/uefa-champions-league_2026-27.ics`, 52 league-stage matches (matchdays 2-4,
  13 Oct - 4 Nov 2026). No Champions League results, no shot counts, no odds, and no source to settle
  these matches afterwards.
- 29 of the 36 clubs play in a domestic league we have shot data for. 7 do not: Bodo/Glimt, Viking
  (Norway), Lask Linz (Austria), Sabah FA (Azerbaijan), Shakhtar Donetsk (Ukraine), Slavia Praha (Czechia),
  Slovan Bratislava (Slovakia). Of the 52 fixtures: 36 `full`, 14 `partial`, 2 `none` (Sabah v Slavia,
  Lask v Slovan).

## Method

1. **Clubs.** Each club is resolved to its domestic team id through the reviewed `club_countries.csv`
   (`england:arsenal`), so its form is its domestic matches. Clubs without a covered league get their own
   id and no history; a club missing from the file is reported by the calendar build and scored as
   "no data" until someone adds it.
2. **Features.** For each fixture, each club's production team features (last 5/10, season to date,
   exponentially weighted, venue split, shots on target, rest days) as of the fixture date, from played
   matches only. The snapshot code reproduces `team_form.build_features` exactly (2,258 team-rows of the 29
   clubs since 2024/25 compared on the real data: maximum difference 0). Champions League fixtures never
   enter the domestic feature pass.
3. **League level of a competition without history** = the mean of the two clubs' domestic league
   priors (season-to-date shots per team per match). Each club's shrunk rates shrink toward its own
   league. Reason: the production model is close to `rate_for^0.7 * opp_rate_against^0.65 / level^0.3`
   (coefficients of the fitted no-odds model sum to ~1), so with the mean of the two levels the two
   leagues' shot environments enter multiplicatively and symmetrically. This is the only
   "league-environment adjustment"; it is implied by the model, not fitted.
4. **Model.** The production no-odds model (`negbin_pit_comp`, market features blanked), unchanged and
   not refitted, wrapped by `CrossBorderModel`; label `<no-odds model>-ucl-exp`. `tier_change` is 0.
5. **Calibration layer.** The per-competition PIT table has no Champions League entry, so the
   **global** PIT map is used. That is a fallback, not a calibration.
6. **Spread added for what we cannot know** (mean-preserving, Gauss-Hermite mixtures):
   `level_sd = 0.023` on the match level, `split_sd = 0.134` on the home/away split. Both are estimated on
   validation seasons only (below). The first barely moves the total bounds; the second mostly widens
   the per-team curves.

### Cross-league comparability: what is and is not adjusted

A club's domestic rates reflect its league. A dominant side of a weaker league has inflated
shots-for and deflated shots-against relative to what it would do against an elite opponent.

- **No strength correction is applied.** Nothing in our data can estimate how strong one league is
  relative to another (no match between leagues exists in it), and importing a ranking (UEFA
  coefficients) would be an unvalidated external assumption.
- **Why the match total tolerates this:** the error is antisymmetric. Over-rating the weaker-league
  club raises its predicted shots and lowers its opponent's by a similar factor; the sum moves little.
  The "moved clubs" proxy measures it: with a full division step between the two clubs' leagues, the
  total's mean is off by +2.7% / -1.3% while each team's shots are off by 8-19%.
- **Why per-team numbers do not tolerate it:** same proxy, the club coming from the weaker league gets
  14% fewer shots than predicted and its opponent 19% more. `split_sd` widens the team curves for this,
  but a spread cannot remove a bias whose direction we do not know for a given pair of leagues.
- **Assumptions, stated plainly:** (a) a Champions League match between clubs X and Y is produced like a
  domestic match between clubs with the same recent shot rates, in a league whose level is the mean of
  the two; (b) leagues are of equal strength; (c) the home side has the usual domestic home effect;
  (d) clubs do not play differently in Europe (rotation, tempo, game state) than at home.

### Clubs without data

The unknown club is replaced by a **mixture of 100 real team profiles** (that side's rate features as
ratios to the league level, drawn once with a fixed seed from top-tier matches of the last three
seasons; home profiles for a home side, away profiles for an away side). The predictive distribution
is the average over the profiles: the model's own distribution with "which kind of team is this"
integrated out. The centre stays near a league-average opponent; the spread widens by the real
between-team variation. With one club known the level is that club's league (`partial`); with none it is
the mean level of the known Champions League clubs (`none`), which makes the two `none` fixtures a
generic estimate that only says "a match at this level".

They are flagged `data_quality: "partial" | "none"`, always `cold_start: true`, and carry no per-team
numbers. Measured on the 52 loaded fixtures, the extra width is small: the 80% / 90% bounds sit 5.1 / 7.9
shots below the median for `partial` matches against 5.1 / 7.8 for `full` ones, and 6.0 / 8.0 for the two
`none` matches. The model's team features explain only a modest part of a match's shot count, so hiding
a club mostly removes information about the centre, not about the spread. The honest reading is "less
informed", not "visibly wider".

## Proxy validation

All models are the no-odds variant, fitted on matches before the scored season. Spread parameters come
from the validation seasons; the test season (2025/26) was scored once with them. "mid" = average of
P(y >= bound) and P(y > bound), the ship rule's rounding-aware coverage.

### (b) An unseen competition: leave one league out

One top-tier league removed from fitting and calibration, then predicted with the global PIT fallback
(`unseen`), against the normal model on the same matches (`in-league`).

| period | model | n | cov 70 | cov 80 | cov 90 | mid 80 | pinball | MAE | actual / predicted mean | leagues with cov 80 < 80% | lowest league cov 80 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| validation 2024/25 | in-league | 3476 | 70.9% | 80.0% | 89.5% | 77.3% | 1.466 | 4.574 | 0.998 | 3 of 11 | 73.4% |
| validation 2024/25 | unseen | 3476 | 72.0% | 81.5% | 90.7% | 78.9% | 1.458 | 4.573 | 0.998 | 3 of 11 | 76.8% |
| validation 2024/25 | unseen + spread | 3476 | 72.1% | 81.7% | 90.9% | 79.0% | 1.458 | 4.573 | 0.998 | 3 of 11 | 76.8% |
| test 2025/26 | in-league | 3445 | 74.5% | 83.1% | 91.7% | 80.5% | 1.465 | 4.486 | 1.011 | 0 of 11 | 80.4% |
| test 2025/26 | unseen | 3445 | 75.0% | 83.7% | 92.3% | 81.2% | 1.468 | 4.482 | 1.011 | 0 of 11 | 80.8% |
| test 2025/26 | unseen + spread | 3445 | 75.1% | 83.8% | 92.4% | 81.4% | 1.467 | 4.483 | 1.011 | 0 of 11 | 80.8% |

Reading: when a whole league is unknown to the model, nothing degrades. The league-level error of the
predicted mean is 1.0% (rms over 11 leagues, validation; largest 1.7%), no more than sampling noise,
because the level of a league is already carried by its teams' own rates. **This says the global
fallback is adequate for an unseen competition made of one league; it does not cover mixing leagues.**

### (a) Mixing leagues: clubs that changed division

In its first three matches after promotion or relegation a club's form comes from another division.
Those matches were scored in "cross-border mode" (no tier information, level = mean of the two
divisions, global PIT). Control = other matches at the same stage of the season.

Totals:

| period | group | n | cov 70 | cov 80 | cov 90 | mid 80 | pinball | actual / predicted mean |
|---|---|---|---|---|---|---|---|---|
| validation 2021/22-2024/25 | promoted club involved | 279 | 76.0% | 85.3% | 91.8% | 83.2% | 1.543 | 1.027 |
| validation | relegated club involved | 266 | 69.9% | 79.0% | 88.0% | 76.9% | 1.555 | 0.987 |
| validation | control | 1645 | 72.5% | 81.2% | 90.4% | 78.9% | 1.495 | 1.000 |
| test 2025/26 | promoted club involved | 58 | 75.9% | 89.7% | 94.8% | 87.1% | 1.343 | 1.022 |
| test 2025/26 | relegated club involved | 66 | 68.2% | 75.8% | 87.9% | 72.7% | 1.433 | 0.956 |
| test 2025/26 | control | 405 | 72.6% | 82.0% | 91.6% | 79.6% | 1.455 | 1.002 |

Per team (validation; calibrated team bounds, with `split_sd`):

| group | side | n | actual | predicted | actual / predicted | cov 70 | cov 80 | cov 90 |
|---|---|---|---|---|---|---|---|---|
| promoted | the promoted club | 279 | 10.50 | 12.22 | 0.859 | 61.6% | 74.9% | 86.7% |
| promoted | its opponent | 279 | 14.00 | 11.74 | 1.193 | 88.5% | 94.3% | 96.1% |
| relegated | the relegated club | 266 | 12.56 | 11.67 | 1.076 | 79.7% | 86.1% | 93.2% |
| relegated | its opponent | 266 | 11.04 | 12.35 | 0.894 | 65.4% | 77.1% | 88.0% |

Reading: a full division step is a larger gap than most Champions League pairings, so this is close
to a worst case. Totals stay within about 3% in the mean; total coverage ranges from 5 points above to
1 point below target on validation, and 4 points below (75.8%, n = 66, standard error 5 points) on the
test season for one group. Per-team bounds of the side that comes from the weaker league cover 75%
where 80% is claimed. **Totals: usable with caution. Per-team: indicative only.**

### Clubs without data: one or both teams hidden

Top-tier matches with one side (or both) hidden and replaced by the profile mixture (800 matches a season).

| period | case | cov 70 | cov 80 | cov 90 | mid 80 | pinball | mean 80% bound |
|---|---|---|---|---|---|---|---|
| validation 2024/25 | both known | 72.4% | 81.1% | 90.5% | 78.6% | 1.459 | 20.5 |
| validation | away hidden | 75.6% | 83.8% | 92.0% | 81.2% | 1.471 | 20.1 |
| validation | home hidden | 73.6% | 83.3% | 92.8% | 80.8% | 1.450 | 20.1 |
| validation | both hidden | 75.1% | 84.0% | 92.5% | 81.5% | 1.460 | 20.0 |
| test 2025/26 | both known | 76.3% | 83.4% | 92.0% | 81.6% | 1.468 | 20.5 |
| test | away hidden | 76.8% | 85.5% | 92.5% | 82.7% | 1.496 | 20.2 |
| test | home hidden | 75.8% | 84.3% | 92.1% | 81.7% | 1.516 | 20.2 |
| test | both hidden | 76.4% | 84.8% | 93.1% | 82.3% | 1.528 | 20.1 |

Reading: when the hidden club is an ordinary club of the same league, the mixture is calibrated and
slightly conservative, at a pinball cost of 2-4% on the test season. The seven real unknown clubs are
not ordinary clubs of their opponent's league (champions of smaller leagues, often away at an elite
side), so this validates the mechanism, not those seven cases.

### (c) Champions League finals with public shot counts (anecdote, n = 7)

StatsBomb open data has the finals. The real pipeline was run on the 2013-2019 finals (model fitted on
matches before each final; shots of the first 90 minutes).

| final | actual (90 min) | median | 70% bound | 80% bound | 90% bound |
|---|---|---|---|---|---|
| 2013 Dortmund v Bayern Munich | 30 | 24 | 21 | 19 | 17 |
| 2014 Real Madrid v Ath Madrid | 21 | 26 | 23 | 21 | 19 |
| 2015 Juventus v Barcelona | 32 | 26 | 23 | 21 | 19 |
| 2016 Real Madrid v Ath Madrid | 31 | 26 | 23 | 21 | 19 |
| 2017 Juventus v Real Madrid | 27 | 28 | 25 | 23 | 21 |
| 2018 Real Madrid v Liverpool | 28 | 24 | 22 | 20 | 18 |
| 2019 Tottenham v Liverpool | 30 | 24 | 22 | 20 | 18 |

The 80% and 90% bounds held in 7 of 7, the 70% bound in 6 of 7; the actual totals average 28.4 against
a mean median of 25.4. Seven matches at a neutral venue, counted by a different provider with a
different shot definition, cannot support or refute anything. It is listed because it is the only
Champions League shot data we can reach, and it does not contradict the model.

## Decisions taken from the proxies

- **No general widening for "unseen competition"**: the leave-one-league-out error is 1%.
- **`level_sd = 0.023`** = that 1.0% combined (root sum of squares) with the 2.1% rms error of the total's
  mean when leagues are mixed. Its effect on the bounds is marginal (in the leave-one-league-out table it
  adds 0.1-0.2 points of coverage). A larger margin would have been a guess, not a measurement.
- **`split_sd = 0.134`** = rms log error of per-team means across a division step. It widens the team curves.
- **Per-team numbers are published only for `full` fixtures**, and the report above is the reason
  they must stay labelled experimental twice over.
- **Not done:** forcing extra conservatism to "be safe". The ship rule values pinball as well as
  coverage; with no Champions League outcomes there is nothing to tune such a margin on.

## Limitations

1. **No calibration evidence on the Champions League.** The per-competition check of the ship rule
   cannot be run. These predictions are outside the ship rule and are labelled so everywhere.
2. **No settlement.** No source of Champions League shot counts: the fixtures stay pending forever,
   never enter the track record, and we will not learn from them.
3. **7 clubs without data** (16 of 52 fixtures `partial` or `none`).
4. **Relative league strength is not modelled.** Totals are robust to it only to first order; a lopsided
   match (elite side against a much weaker one) may behave differently from any domestic match.
5. **Form is domestic only.** Matchday 1 of the Champions League itself, and any European match, is
   invisible to the model; early in the season a club's window is a handful of league matches plus last
   season's. (Domestic results were refreshed to 12 October for the run below.)
6. **Only matchdays 2-4 are loaded**; later rounds need the calendar file to be extended. Knockout
   ties (two legs, extra time, game-state effects) would need separate thought.
7. Kick-off local times use the home club's country; Shakhtar Donetsk play their home matches abroad,
   so their local time (not the UTC instant) is wrong.
8. `league_avg` shown for these fixtures is the blend of two domestic league averages, not a
   Champions League average.

## What would make it trustworthy

A Champions League shots source with history (Opta, per the pending contract). With two or three
seasons of results: (1) add the competition to training and to the per-competition PIT calibration,
(2) measure and fit the cross-league effect instead of assuming it away, (3) run the ship rule on a
held-out season, (4) settle live picks and report a track record. Until then the label stays
"experimental".

## Local run of 2026-10-13 (matchday 2, results up to 12 October)

`python -m shots update --export-dir frontend/public/data`: all steps ok, 517 board fixtures of which 52
experimental (36 full with team curves, 14 partial, 2 none), `python -m shots.export.verify` ok. Kick-off UTC.

| kick-off | match | expected shots | 80% bound | P(>= 24) | data |
|---|---|---|---|---|---|
| 13 Oct 16:45 | Lens v Sp Lisbon | 25 | 21 | 62% | full |
| 13 Oct 16:45 | Sabah FA v Slavia Praha | 28 | 22 | 74% | none |
| 13 Oct 19:00 | Arsenal v Lille | 24 | 20 | 55% | full |
| 13 Oct 19:00 | Ath Madrid v Man United | 28 | 23 | 78% | full |
| 13 Oct 19:00 | Galatasaray v Barcelona | 27 | 22 | 71% | full |
| 13 Oct 19:00 | Inter v Club Brugge | 33 | 27 | 92% | full |
| 13 Oct 19:00 | RB Leipzig v PSV Eindhoven | 31 | 25 | 87% | full |
| 13 Oct 19:00 | Viking v Bayern Munich | 29 | 24 | 81% | partial |
| 13 Oct 19:00 | Villarreal v Napoli | 28 | 23 | 78% | full |
| 14 Oct 16:45 | Feyenoord v Como | 30 | 25 | 85% | full |
| 14 Oct 16:45 | Lask Linz v Liverpool | 27 | 22 | 70% | partial |
| 14 Oct 19:00 | Aston Villa v Fenerbahce | 29 | 24 | 82% | full |
| 14 Oct 19:00 | Betis v Porto | 29 | 24 | 83% | full |
| 14 Oct 19:00 | Bodo/Glimt v Dortmund | 27 | 22 | 72% | partial |
| 14 Oct 19:00 | Man City v Paris SG | 28 | 23 | 76% | full |
| 14 Oct 19:00 | Roma v Real Madrid | 29 | 24 | 81% | full |
| 14 Oct 19:00 | Shakhtar Donetsk v AEK | 25 | 20 | 59% | partial |
| 14 Oct 19:00 | Slovan Bratislava v Stuttgart | 32 | 26 | 89% | partial |

The `none` / `partial` rows are driven mostly by the league level of the known club (German clubs' league
runs at about 31 shots a match this season), not by anything known about the other side.

## Integration summary

- Board / scan route these fixtures to the experimental model; domestic rows are unchanged (a board
  built by the pre-change code on today's data equals the domestic rows of the new board, 465 rows x 111
  columns, exactly; `features.parquet` is identical).
- Database migration `0004_experimental`: `predictions.experimental`, `predictions.data_quality`;
  `v_official_picks` / `v_replay_picks` / official results exclude experimental rows; `v_experimental_picks`
  keeps them apart.
- Export (schema 1, additive): fixture `experimental: true`, `data_quality`; league `featured: true`.
