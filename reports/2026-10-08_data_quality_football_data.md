# Data quality: football-data.co.uk (2026-10-08)

- Parsed rows: **124,754**. Kept with shot data: **94,678** (30,076 dropped for missing shots).
- Date range: 2010-08-06 → 2026-10-05
- Competitions: 22, teams: 574
- Total shots per match overall: mean 23.5, std 6.2, variance/mean 1.62 (>1 means overdispersed: plain Poisson is too narrow)

## Sanity checks (counts of suspicious rows)

| check | rows |
|---|---|
| home_sot > home_shots | 2 |
| away_sot > away_shots | 6 |
| goals > shots on target (home) | 272 |
| total_shots == 0 | 0 |
| total_shots > 60 | 0 |
| duplicate (competition, date, home, away) | 0 |
| missing pre-match 1X2 odds | 37 |

## Coverage by competition

| competition             | first_season   | last_season   |   rows |   with_shots | first_season_with_shots   |   pct_with_shots |   total_shots_mean |   total_shots_std |   total_shots_min |   total_shots_max |
|:------------------------|:---------------|:--------------|-------:|-------------:|:--------------------------|-----------------:|-------------------:|------------------:|------------------:|------------------:|
| England Championship    | 2010/2011      | 2026/2027     |   8927 |         8926 | 2010/2011                 |            100   |               24.3 |               5.9 |                 6 |                49 |
| England League Two      | 2010/2011      | 2026/2027     |   8822 |         8822 | 2010/2011                 |            100   |               22.2 |               5.9 |                 3 |                52 |
| England League One      | 2010/2011      | 2026/2027     |   8770 |         8770 | 2010/2011                 |            100   |               22.7 |               5.8 |                 3 |                47 |
| Spain La Liga           | 2010/2011      | 2026/2027     |   6149 |         6149 | 2010/2011                 |            100   |               24.3 |               5.9 |                 5 |                50 |
| England Premier League  | 2010/2011      | 2026/2027     |   6130 |         6130 | 2010/2011                 |            100   |               25.5 |               5.7 |                 5 |                51 |
| Italy Serie A           | 2010/2011      | 2026/2027     |   6130 |         6129 | 2010/2011                 |            100   |               25   |               6   |                 3 |                50 |
| France Ligue 1          | 2010/2011      | 2026/2027     |   5802 |         5801 | 2010/2011                 |            100   |               24.2 |               5.6 |                 4 |                54 |
| Germany Bundesliga      | 2010/2011      | 2026/2027     |   4932 |         4931 | 2010/2011                 |            100   |               25.9 |               5.8 |                 7 |                52 |
| Spain Segunda Division  | 2010/2011      | 2026/2027     |   7480 |         4225 | 2017/2018                 |             56.5 |               23.1 |               5.6 |                 5 |                49 |
| Scotland Premiership    | 2010/2011      | 2026/2027     |   3641 |         3641 | 2010/2011                 |            100   |               21.9 |               6.3 |                 3 |                48 |
| Italy Serie B           | 2010/2011      | 2026/2027     |   6748 |         3512 | 2017/2018                 |             52   |               24.8 |               6.3 |                 4 |                50 |
| England National League | 2010/2011      | 2026/2027     |   8753 |         3455 | 2010/2011                 |             39.5 |               19.4 |               5.6 |                 3 |                45 |
| France Ligue 2          | 2010/2011      | 2026/2027     |   5894 |         3230 | 2017/2018                 |             54.8 |               22.1 |               5.6 |                 3 |                50 |
| Turkey Super Lig        | 2010/2011      | 2026/2027     |   5284 |         3111 | 2017/2018                 |             58.9 |               24.3 |               6.2 |                 3 |                46 |
| Portugal Primeira Liga  | 2010/2011      | 2026/2027     |   4694 |         2816 | 2017/2018                 |             60   |               23.2 |               5.8 |                 7 |                45 |
| Germany 2. Bundesliga   | 2010/2011      | 2026/2027     |   4950 |         2808 | 2017/2018                 |             56.7 |               26.7 |               5.8 |                 9 |                50 |
| Netherlands Eredivisie  | 2010/2011      | 2026/2027     |   4885 |         2743 | 2017/2018                 |             56.2 |               27.2 |               6.1 |                 9 |                51 |
| Belgium Pro League      | 2010/2011      | 2026/2027     |   4308 |         2622 | 2017/2018                 |             60.9 |               24   |               6.4 |                 7 |                51 |
| Greece Super League     | 2010/2011      | 2026/2027     |   3996 |         2180 | 2017/2018                 |             54.6 |               19.3 |               6   |                 4 |                44 |
| Scotland Championship   | 2010/2011      | 2026/2027     |   2834 |         1574 | 2017/2018                 |             55.5 |               19.1 |               5.3 |                 5 |                42 |
| Scotland League One     | 2010/2011      | 2026/2027     |   2813 |         1552 | 2017/2018                 |             55.2 |               19.7 |               5.1 |                 7 |                38 |
| Scotland League Two     | 2010/2011      | 2026/2027     |   2812 |         1551 | 2017/2018                 |             55.2 |               19.1 |               5.4 |                 4 |                41 |
