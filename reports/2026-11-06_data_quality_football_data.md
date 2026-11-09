# Data quality: football-data.co.uk (2026-11-06)

- Parsed rows: **125,350**. Kept with shot data: **95,274** (30,076 dropped for missing shots).
- Date range: 2010-08-06 → 2026-10-26
- Competitions: 22, teams: 574
- Total shots per match overall: mean 23.6, std 6.2, variance/mean 1.63 (>1 means overdispersed: plain Poisson is too narrow)

## Sanity checks (counts of suspicious rows)

| check | rows |
|---|---|
| home_sot > home_shots | 2 |
| away_sot > away_shots | 6 |
| goals > shots on target (home) | 274 |
| total_shots == 0 | 0 |
| total_shots > 60 | 0 |
| duplicate (competition, date, home, away) | 0 |
| missing pre-match 1X2 odds | 37 |

## Coverage by competition

| competition             | first_season   | last_season   |   rows |   with_shots | first_season_with_shots   |   pct_with_shots |   total_shots_mean |   total_shots_std |   total_shots_min |   total_shots_max |
|:------------------------|:---------------|:--------------|-------:|-------------:|:--------------------------|-----------------:|-------------------:|------------------:|------------------:|------------------:|
| England Championship    | 2010/2011      | 2026/2027     |   8976 |         8975 | 2010/2011                 |            100   |               24.3 |               5.9 |                 6 |                49 |
| England League Two      | 2010/2011      | 2026/2027     |   8870 |         8870 | 2010/2011                 |            100   |               22.2 |               5.9 |                 3 |                52 |
| England League One      | 2010/2011      | 2026/2027     |   8818 |         8818 | 2010/2011                 |            100   |               22.7 |               5.8 |                 3 |                47 |
| Spain La Liga           | 2010/2011      | 2026/2027     |   6179 |         6179 | 2010/2011                 |            100   |               24.3 |               5.9 |                 5 |                50 |
| England Premier League  | 2010/2011      | 2026/2027     |   6160 |         6160 | 2010/2011                 |            100   |               25.5 |               5.7 |                 5 |                51 |
| Italy Serie A           | 2010/2011      | 2026/2027     |   6160 |         6159 | 2010/2011                 |            100   |               25.1 |               6   |                 3 |                50 |
| France Ligue 1          | 2010/2011      | 2026/2027     |   5829 |         5828 | 2010/2011                 |            100   |               24.2 |               5.6 |                 4 |                54 |
| Germany Bundesliga      | 2010/2011      | 2026/2027     |   4959 |         4958 | 2010/2011                 |            100   |               25.9 |               5.8 |                 7 |                52 |
| Spain Segunda Division  | 2010/2011      | 2026/2027     |   7513 |         4258 | 2017/2018                 |             56.7 |               23.2 |               5.6 |                 5 |                49 |
| Scotland Premiership    | 2010/2011      | 2026/2027     |   3659 |         3659 | 2010/2011                 |            100   |               21.9 |               6.3 |                 3 |                48 |
| Italy Serie B           | 2010/2011      | 2026/2027     |   6778 |         3542 | 2017/2018                 |             52.3 |               24.8 |               6.3 |                 4 |                50 |
| England National League | 2010/2011      | 2026/2027     |   8775 |         3477 | 2010/2011                 |             39.6 |               19.4 |               5.6 |                 3 |                45 |
| France Ligue 2          | 2010/2011      | 2026/2027     |   5921 |         3257 | 2017/2018                 |             55   |               22.1 |               5.6 |                 3 |                50 |
| Turkey Super Lig        | 2010/2011      | 2026/2027     |   5311 |         3138 | 2017/2018                 |             59.1 |               24.4 |               6.2 |                 3 |                46 |
| Germany 2. Bundesliga   | 2010/2011      | 2026/2027     |   4977 |         2835 | 2017/2018                 |             57   |               26.8 |               5.8 |                 9 |                50 |
| Portugal Primeira Liga  | 2010/2011      | 2026/2027     |   4713 |         2835 | 2017/2018                 |             60.2 |               23.2 |               5.8 |                 7 |                45 |
| Netherlands Eredivisie  | 2010/2011      | 2026/2027     |   4912 |         2770 | 2017/2018                 |             56.4 |               27.2 |               6.1 |                 9 |                51 |
| Belgium Pro League      | 2010/2011      | 2026/2027     |   4325 |         2639 | 2017/2018                 |             61   |               24.1 |               6.4 |                 7 |                51 |
| Greece Super League     | 2010/2011      | 2026/2027     |   4016 |         2200 | 2017/2018                 |             54.8 |               19.4 |               6   |                 4 |                44 |
| Scotland Championship   | 2010/2011      | 2026/2027     |   2850 |         1590 | 2017/2018                 |             55.8 |               19   |               5.3 |                 5 |                42 |
| Scotland League One     | 2010/2011      | 2026/2027     |   2827 |         1566 | 2017/2018                 |             55.4 |               19.6 |               5.1 |                 7 |                38 |
| Scotland League Two     | 2010/2011      | 2026/2027     |   2822 |         1561 | 2017/2018                 |             55.3 |               19.1 |               5.4 |                 4 |                41 |

## Extra columns: % of matches with shots where home and away are both present

Columns: home/away_ht_goals (HTHG/HTAG), corners (HC/AC), reds (HR/AR), fouls (HF/AF). Missing stays null, never 0.

### By competition, last 7 seasons

| competition             |   ht_goals |   corners |   reds |   fouls |
|:------------------------|-----------:|----------:|-------:|--------:|
| Belgium Pro League      |        100 |       100 |    100 |   100   |
| England Championship    |        100 |       100 |    100 |   100   |
| England League One      |        100 |       100 |    100 |   100   |
| England League Two      |        100 |       100 |    100 |   100   |
| England National League |        100 |       100 |    100 |   100   |
| England Premier League  |        100 |       100 |    100 |   100   |
| France Ligue 1          |        100 |       100 |    100 |   100   |
| France Ligue 2          |        100 |       100 |    100 |   100   |
| Germany 2. Bundesliga   |        100 |       100 |    100 |   100   |
| Germany Bundesliga      |        100 |       100 |    100 |   100   |
| Greece Super League     |        100 |       100 |    100 |   100   |
| Italy Serie A           |        100 |       100 |    100 |   100   |
| Italy Serie B           |        100 |       100 |    100 |   100   |
| Netherlands Eredivisie  |        100 |       100 |    100 |   100   |
| Portugal Primeira Liga  |        100 |       100 |    100 |   100   |
| Scotland Championship   |        100 |       100 |    100 |   100   |
| Scotland League One     |        100 |       100 |    100 |    99.9 |
| Scotland League Two     |        100 |       100 |    100 |    99.9 |
| Scotland Premiership    |        100 |       100 |    100 |   100   |
| Spain La Liga           |        100 |       100 |    100 |   100   |
| Spain Segunda Division  |        100 |       100 |    100 |   100   |
| Turkey Super Lig        |        100 |       100 |    100 |   100   |

### By season, all competitions

| season    |   ht_goals |   corners |   reds |   fouls |
|:----------|-----------:|----------:|-------:|--------:|
| 2010/2011 |        100 |       100 |    100 |   100   |
| 2011/2012 |        100 |       100 |    100 |   100   |
| 2012/2013 |        100 |       100 |    100 |   100   |
| 2013/2014 |        100 |       100 |    100 |   100   |
| 2014/2015 |        100 |       100 |    100 |   100   |
| 2015/2016 |        100 |       100 |    100 |   100   |
| 2016/2017 |        100 |       100 |    100 |    99.9 |
| 2017/2018 |        100 |       100 |    100 |    88.2 |
| 2018/2019 |        100 |       100 |    100 |    93.3 |
| 2019/2020 |        100 |       100 |    100 |   100   |
| 2020/2021 |        100 |       100 |    100 |   100   |
| 2021/2022 |        100 |       100 |    100 |   100   |
| 2022/2023 |        100 |       100 |    100 |   100   |
| 2023/2024 |        100 |       100 |    100 |   100   |
| 2024/2025 |        100 |       100 |    100 |   100   |
| 2025/2026 |        100 |       100 |    100 |   100   |
| 2026/2027 |        100 |       100 |    100 |   100   |
