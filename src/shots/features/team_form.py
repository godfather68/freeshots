"""Pre-match team shot-form features (TASKS 2.1) built from the canonical match table.

Usage:
    python -m shots.features.team_form        # data/processed/matches.parquet -> features.parquet

Teams are keyed by ``home_team_id`` / ``away_team_id`` (country-scoped), never by name.

Output: one row per match (``match_id``) with ``home_*`` / ``away_*`` features, match-level
context / market features, and targets ``home_shots, away_shots, total_shots``.

No-leakage rule: every feature of a match uses only matches with ``date`` strictly before the
match date. Same-day matches (even for the same team) never see each other. Missing shot values
are never imputed as 0; they are skipped inside the windows. Cold starts stay NaN for rolling
features, while ``n_prior_*`` and the league prior are always provided.

Per-side features (prefix ``home_`` / ``away_``; "for" = by this team, "against" = by opponents)

=====================================  =====================================================
name                                    definition
=====================================  =====================================================
{s}_{for,against}_l5 / _l10             mean shots for/against over the team's last 5 / 10
                                        prior matches (all competitions); fewer if less
                                        history, NaN if none. s in {shots, sot}.
{s}_{for,against}_std                   season-to-date mean (same team, same season label).
{s}_{for,against}_ewm                   exponentially weighted mean over all prior matches,
                                        halflife = 5 matches (a match 5 games ago has half the
                                        weight of the latest), ``ignore_na=True``.
{s}_{for,against}_venue_l5              mean over the team's last 5 prior matches at the same
                                        venue type (home-only for the home side, away-only for
                                        the away side).
rest_days                               days since the team's previous match (NaN if none).
n_prior_matches                         number of prior matches of the team (any competition).
n_prior_season                          number of prior matches of the team in this season.
tier_change                             tier of this match minus tier of the team's previous
                                        match (positive = dropped down a division); NaN if none.
shots_for_shrunk / shots_against_shrunk (n*m + K*p) / (n + K): m = last-10 mean, n = number of
                                        matches with data in that window (<=10), p = league
                                        prior, K = 5 pseudo-matches. n=0 gives exactly p.
=====================================  =====================================================

Match-level features (shared by both sides)

=====================================  =====================================================
league_prior_shots                      mean shots per team per match in the same competition
                                        and season over matches strictly before this date.
                                        Falls back to the competition's all-time mean, then
                                        the global mean, when fewer than 20 team-observations
                                        exist so far in the season.
league_prior_n                          team-observations behind the season-to-date prior.
mkt_p_home / mkt_p_draw / mkt_p_away    1/odds normalised to sum to 1 (overround removed);
                                        NaN if any of the three odds is missing/invalid.
mkt_p_over25                            1/odds_over25 normalised against odds_under25; NaN if
                                        either is missing/invalid.
=====================================  =====================================================

League-level drift features (match-level; catch league-wide shifts early in a season). "obs" = team
observations (2 per match). ``prev`` = the competition's previous season (previous label present in
the data) mean shots per team per match; it is only used if that season ended strictly before the
match date, else it falls back to the competition all-time mean before the date, then global.

=====================================  =====================================================
league_prev_level_shots                ``prev`` as above (never NaN once any history exists).
league_prev_is_fallback                1 if ``prev`` fell back to all-time/global mean, else 0.
league_level_shrunk                    (n*std + K*prev) / (n + K): season-to-date mean (n =
                                        obs so far this season) shrunk toward ``prev``, K = 200
                                        obs. n=0 gives exactly ``prev``.
league_level_drift                      league_level_shrunk - league_prev_level_shots.
league_recent28_n                       obs in the competition over the 28 days before the date.
league_recent28_shots                   mean shots per team-match over those 28 days (crosses
                                        the season boundary); NaN if n=0.
league_recent28_drift                   n/(n+K) * (recent28 - prev), K = 200; exactly 0 if n=0.
league_recent100_drift                  mean over the competition's last 100 prior matches
                                        (crosses the season boundary) minus ``prev``; 0 if the
                                        competition has no prior match.
league_global_n28                       obs over all competitions in the last 28 days that have
                                        a ``prev`` season to compare against.
league_global_drift28                   n/(n+K) * mean over those obs of (shots - own
                                        competition's ``prev``), K = 200; 0 if n=0. A
                                        cross-league "everything is up" signal.
days_since_season_start                 date minus the competition-season's first match date
                                        (schedule information, not a result).
league_season_matches_prior             matches of the competition-season dated before this date.
=====================================  =====================================================

Targets: ``home_shots, away_shots, total_shots`` (NaN when shots are missing).
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
IN_PATH = ROOT / "data" / "processed" / "matches.parquet"
OUT_PATH = ROOT / "data" / "processed" / "features.parquet"

EWM_HALFLIFE = 5          # matches
SHRINK_K = 5              # pseudo-matches of league prior
SHRINK_WINDOW = 10
PRIOR_MIN_OBS = 20        # team-observations needed to trust the season-to-date league prior
WINDOWS = (5, 10)
DRIFT_K = 200             # pseudo-observations for league-drift shrinkage
DRIFT_DAYS = 28
DRIFT_MATCHES = 100

REQUIRED = [
    "match_id", "competition", "tier", "season", "date", "home_team", "away_team", "home_team_id", "away_team_id",
    "home_shots", "away_shots", "home_sot", "away_sot",
    "odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25",
]
METRICS = ("shots", "sot")
TARGETS = ["home_shots", "away_shots", "total_shots"]


# --------------------------------------------------------------------------- helpers

def _stat(long: pd.DataFrame, keys: list[str], col: str, kind: str, window: int = 0) -> pd.Series:
    """Prior-only statistic of ``col`` within ``keys`` groups, aligned to ``long.index``.

    ``long`` must be sorted by (team, date, match_id). The value at row i uses rows before i in
    the group only (shift(1)); same-day leakage is removed afterwards by ``_day_first``.
    """
    shifted = long.groupby(keys, sort=False)[col].shift(1)
    g = shifted.groupby([long[k] for k in keys], sort=False)
    if kind == "roll":
        out = g.rolling(window, min_periods=1).mean()
    elif kind == "count":
        out = g.rolling(window, min_periods=1).count()
    elif kind == "expanding":
        out = g.expanding(min_periods=1).mean()
    elif kind == "ewm":
        out = g.ewm(halflife=EWM_HALFLIFE, ignore_na=True).mean()
    else:  # pragma: no cover
        raise ValueError(kind)
    return out.droplevel(list(range(len(keys)))).reindex(long.index)


def _day_first(long: pd.DataFrame, values: pd.Series, keys: list[str]) -> pd.Series:
    """Give every row of a (keys, date) group the value of its first row.

    The first row of a team's day only has strictly-earlier matches before it, so this removes
    any dependence on matches played the same day.
    """
    day_keys = [*keys, "date"]
    first = ~long.duplicated(day_keys)
    return values.where(first).groupby([long[k] for k in day_keys], sort=False).transform("max")


def _implied(odds: pd.DataFrame) -> pd.DataFrame:
    odds = odds.where(odds > 1.0)
    inv = 1.0 / odds
    return inv.div(inv.sum(axis=1, skipna=False), axis=0)


def _league_prior(m: pd.DataFrame) -> pd.DataFrame:
    """Strictly-before-date league priors at match level (index aligned to ``m``)."""
    valid = m["home_shots"].notna() & m["away_shots"].notna()
    tmp = pd.DataFrame({
        "competition": m["competition"], "season": m["season"], "date": m["date"],
        "s": (m["home_shots"] + m["away_shots"]).where(valid, 0.0),
        "n": valid.astype(float) * 2,
    })

    def excl_cum(group_cols: list[str]) -> pd.DataFrame:
        daily = tmp.groupby([*group_cols, "date"], sort=True)[["s", "n"]].sum().reset_index()
        grp = daily.groupby(group_cols) if group_cols else daily
        cum = grp[["s", "n"]].cumsum() - daily[["s", "n"]]
        daily[["s", "n"]] = cum
        return daily

    season = excl_cum(["competition", "season"]).rename(columns={"s": "s_season", "n": "n_season"})
    comp = excl_cum(["competition"]).rename(columns={"s": "s_comp", "n": "n_comp"})
    glob = excl_cum([]).rename(columns={"s": "s_glob", "n": "n_glob"})

    out = m[["competition", "season", "date"]].copy()
    out["_row"] = np.arange(len(out))
    out = out.merge(season, on=["competition", "season", "date"], how="left")
    out = out.merge(comp, on=["competition", "date"], how="left")
    out = out.merge(glob, on=["date"], how="left").sort_values("_row")

    def ratio(s, n):
        return (out[s] / out[n].where(out[n] > 0)).to_numpy()

    p_season, p_comp, p_glob = ratio("s_season", "n_season"), ratio("s_comp", "n_comp"), ratio("s_glob", "n_glob")
    use_season = (out["n_season"] >= PRIOR_MIN_OBS).to_numpy()
    prior = np.where(use_season, p_season, np.where(np.isfinite(p_comp), p_comp, p_glob))
    return pd.DataFrame(
        {"league_prior_shots": prior, "league_prior_n": out["n_season"].to_numpy()}, index=m.index
    )


def _before(dates: np.ndarray, cum: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Sum of per-date values (inclusive cumulative ``cum`` over sorted ``dates``) for dates < x."""
    pos = np.searchsorted(dates, x, side="left")
    c0 = np.concatenate([np.zeros((1,) + cum.shape[1:]), cum])
    return c0[pos]


def _ns(a) -> np.ndarray:
    return np.asarray(a).astype("datetime64[ns]")


def _league_drift(m: pd.DataFrame) -> pd.DataFrame:
    """Strictly-before-date league level / drift features (index aligned to ``m``)."""
    valid = (m["home_shots"].notna() & m["away_shots"].notna()).to_numpy()
    tot = (m["home_shots"] + m["away_shots"]).where(valid, 0.0).to_numpy()
    n_obs = valid.astype(float) * 2
    comp = m["competition"].to_numpy()
    season = m["season"].to_numpy()
    date = _ns(m["date"].to_numpy())
    day = np.timedelta64(1, "D").astype("timedelta64[ns]")

    # season-level means and end dates per competition (previous season = previous label present)
    sg = pd.DataFrame({"c": comp, "s": season, "t": tot, "n": n_obs, "d": date})
    ss = sg.groupby(["c", "s"]).agg(t=("t", "sum"), n=("n", "sum"), start=("d", "min"), end=("d", "max"))
    ss = ss.reset_index().sort_values(["c", "s"])
    ss["prev_mean"] = (ss["t"] / ss["n"].where(ss["n"] > 0)).groupby(ss["c"]).shift(1)
    ss["prev_end"] = ss.groupby("c")["end"].shift(1)
    key = pd.MultiIndex.from_arrays([comp, season])
    ssi = ss.set_index(["c", "s"])
    prev_mean = ssi["prev_mean"].reindex(key).to_numpy(dtype=float)
    prev_end = _ns(ssi["prev_end"].reindex(key).to_numpy())
    start = _ns(ssi["start"].reindex(key).to_numpy())
    prev_ok = np.isfinite(prev_mean) & (prev_end < date)

    names = ("comp_mean", "std_t", "std_n", "r28_t", "r28_n", "r100_t", "r100_n", "season_m")
    out = {k: np.full(len(m), np.nan) for k in names}
    for c in pd.unique(comp):
        idx = np.flatnonzero(comp == c)
        x = date[idx]
        daily = pd.DataFrame({"d": x, "t": tot[idx], "n": n_obs[idx], "one": 1.0}).groupby("d").sum()
        dates, cum = _ns(daily.index.to_numpy()), daily.cumsum().to_numpy()
        b = _before(dates, cum, x)
        lo = _before(dates, cum, x - DRIFT_DAYS * day)
        out["comp_mean"][idx] = b[:, 0] / np.where(b[:, 1] > 0, b[:, 1], np.nan)
        out["r28_t"][idx], out["r28_n"][idx] = (b - lo)[:, 0], (b - lo)[:, 1]
        for sv in pd.unique(season[idx]):
            j = idx[season[idx] == sv]
            sd = pd.DataFrame({"d": date[j], "t": tot[j], "n": n_obs[j], "one": 1.0}).groupby("d").sum()
            bs = _before(_ns(sd.index.to_numpy()), sd.cumsum().to_numpy(), date[j])
            out["std_t"][j], out["std_n"][j], out["season_m"][j] = bs[:, 0], bs[:, 1], bs[:, 2]
        # last DRIFT_MATCHES prior matches; same-day matches excluded via searchsorted(left)
        order = np.argsort(x, kind="mergesort")
        ct = np.concatenate([[0.0], np.cumsum(tot[idx][order])])
        cn = np.concatenate([[0.0], np.cumsum(n_obs[idx][order])])
        e = np.searchsorted(x[order], x, side="left")
        s0 = np.maximum(e - DRIFT_MATCHES, 0)
        out["r100_t"][idx], out["r100_n"][idx] = ct[e] - ct[s0], cn[e] - cn[s0]

    # global deviation of every observation from its own competition's previous-season level
    # (prev_ok already requires that season to have ended before this row's date; the sum is over
    # earlier rows, whose own prev_ok is evaluated at their own date)
    dev = np.where(prev_ok & valid, tot - n_obs * np.nan_to_num(prev_mean), 0.0)
    dn = np.where(prev_ok & valid, n_obs, 0.0)
    gd = pd.DataFrame({"d": date, "dev": dev, "dn": dn}).groupby("d").sum()
    gdates, gcum = _ns(gd.index.to_numpy()), gd.cumsum().to_numpy()
    gb, glo = _before(gdates, gcum, date), _before(gdates, gcum, date - DRIFT_DAYS * day)
    g_dev, g_n = (gb - glo)[:, 0], (gb - glo)[:, 1]

    gall = pd.DataFrame({"d": date, "t": tot, "n": n_obs}).groupby("d").sum()
    ga = _before(_ns(gall.index.to_numpy()), gall.cumsum().to_numpy(), date)
    glob_mean = ga[:, 0] / np.where(ga[:, 1] > 0, ga[:, 1], np.nan)

    fallback = np.where(np.isfinite(out["comp_mean"]), out["comp_mean"], glob_mean)
    prev = np.where(prev_ok, prev_mean, fallback)
    K = DRIFT_K
    std_n = out["std_n"]
    std_mean = out["std_t"] / np.where(std_n > 0, std_n, np.nan)
    level = np.where(std_n > 0, (std_n * np.nan_to_num(std_mean) + K * prev) / (std_n + K), prev)
    r28_n = out["r28_n"]
    r28 = out["r28_t"] / np.where(r28_n > 0, r28_n, np.nan)
    r100 = out["r100_t"] / np.where(out["r100_n"] > 0, out["r100_n"], np.nan)
    g_mean = g_dev / np.where(g_n > 0, g_n, np.nan)
    return pd.DataFrame({
        "league_prev_level_shots": prev,
        "league_prev_is_fallback": (~prev_ok).astype(float),
        "league_level_shrunk": level,
        "league_level_drift": level - prev,
        "league_recent28_n": r28_n,
        "league_recent28_shots": r28,
        "league_recent28_drift": np.where(r28_n > 0, r28_n / (r28_n + K) * (np.nan_to_num(r28) - prev), 0.0),
        "league_recent100_drift": np.where(np.isfinite(r100), r100 - prev, 0.0),
        "league_global_n28": g_n,
        "league_global_drift28": np.where(g_n > 0, g_n / (g_n + K) * np.nan_to_num(g_mean), 0.0),
        "days_since_season_start": (date - start) / day,
        "league_season_matches_prior": out["season_m"],
    }, index=m.index)


# --------------------------------------------------------------------------- main

def _team_long(m: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for side, opp in (("home", "away"), ("away", "home")):
        parts.append(pd.DataFrame({
            "match_id": m["match_id"], "side": side, "is_home": side == "home",
            "team": m[f"{side}_team_id"], "date": m["date"], "season": m["season"], "tier": m["tier"],
            "shots_for": m[f"{side}_shots"], "shots_against": m[f"{opp}_shots"],
            "sot_for": m[f"{side}_sot"], "sot_against": m[f"{opp}_sot"],
        }))
    long = pd.concat(parts, ignore_index=True)
    long = long.sort_values(["team", "date", "match_id"], kind="mergesort").reset_index(drop=True)
    long["tier"] = long["tier"].astype("float64")
    return long


def build_features(matches: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in REQUIRED if c not in matches.columns]
    if missing:
        raise ValueError(f"matches is missing columns: {missing}")
    m = matches.reset_index(drop=True).copy()
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    for c in ("home_shots", "away_shots", "home_sot", "away_sot"):
        m[c] = m[c].astype("float64")

    long = _team_long(m)
    prior = _league_prior(m).assign(match_id=m["match_id"].to_numpy())
    long = long.merge(prior[["match_id", "league_prior_shots"]], on="match_id", how="left")
    long = long.sort_values(["team", "date", "match_id"], kind="mergesort").reset_index(drop=True)

    feats: dict[str, pd.Series] = {}
    team, team_season, team_venue = ["team"], ["team", "season"], ["team", "is_home"]
    for met in METRICS:
        for d in ("for", "against"):
            col = f"{met}_{d}"
            for w in WINDOWS:
                feats[f"{col}_l{w}"] = _day_first(long, _stat(long, team, col, "roll", w), team)
            feats[f"{col}_std"] = _day_first(long, _stat(long, team_season, col, "expanding"), team_season)
            feats[f"{col}_ewm"] = _day_first(long, _stat(long, team, col, "ewm"), team)
            feats[f"{col}_venue_l5"] = _day_first(long, _stat(long, team_venue, col, "roll", 5), team_venue)

    # context
    prev_date = long.groupby("team", sort=False)["date"].shift(1)
    feats["rest_days"] = _day_first(long, (long["date"] - prev_date).dt.days.astype("float64"), team)
    feats["n_prior_matches"] = _day_first(long, long.groupby(team, sort=False).cumcount().astype("float64"), team)
    feats["n_prior_season"] = _day_first(
        long, long.groupby(team_season, sort=False).cumcount().astype("float64"), team_season
    )
    prev_tier = long.groupby("team", sort=False)["tier"].shift(1)
    feats["tier_change"] = _day_first(long, long["tier"] - prev_tier, team)

    # shrinkage toward league prior using last-10 window
    long["_obs"] = long["shots_for"].notna().astype("float64")
    n_win = _day_first(long, _stat(long, team, "_obs", "count", SHRINK_WINDOW), team).fillna(0.0)
    n_win = np.minimum(n_win, SHRINK_WINDOW)
    p = long["league_prior_shots"]
    for d in ("for", "against"):
        mean10 = feats[f"shots_{d}_l{SHRINK_WINDOW}"].fillna(0.0)
        feats[f"shots_{d}_shrunk"] = (n_win * mean10 + SHRINK_K * p) / (n_win + SHRINK_K)

    ff = pd.DataFrame(feats)
    ff["match_id"] = long["match_id"].to_numpy()
    ff["side"] = long["side"].to_numpy()
    home = ff[ff["side"] == "home"].drop(columns="side").set_index("match_id").add_prefix("home_")
    away = ff[ff["side"] == "away"].drop(columns="side").set_index("match_id").add_prefix("away_")

    out = m[["match_id", "date", "season", "competition", "tier", "home_team", "away_team",
                 "home_team_id", "away_team_id"]].copy()
    out = out.join(home, on="match_id").join(away, on="match_id")
    out = out.join(_league_prior(m))

    imp = _implied(m[["odds_home", "odds_draw", "odds_away"]].astype("float64"))
    out["mkt_p_home"], out["mkt_p_draw"], out["mkt_p_away"] = imp.to_numpy().T
    ou = _implied(m[["odds_over25", "odds_under25"]].astype("float64"))
    out["mkt_p_over25"] = ou["odds_over25"]

    out["home_shots"], out["away_shots"] = m["home_shots"], m["away_shots"]
    out["total_shots"] = m["home_shots"] + m["away_shots"]
    out = out.join(_league_drift(m))  # appended last so existing column order is unchanged
    return out.reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, default=IN_PATH)
    ap.add_argument("--output", type=Path, default=OUT_PATH)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()
    feats = build_features(pd.read_parquet(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    feats.to_parquet(args.output, index=False)
    log.info("wrote %d rows x %d cols to %s in %.1fs", len(feats), feats.shape[1], args.output, time.time() - t0)


if __name__ == "__main__":
    main()
