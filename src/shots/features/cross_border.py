"""Pre-match features for fixtures of cross-border competitions (the UEFA Champions League).

We have no shot history for these competitions, so a fixture is described by each club's DOMESTIC
form, looked up by its domestic team id:

* Team features (same definitions as ``team_form``): a snapshot of the club's history strictly
  before the fixture date, built from PLAYED matches only. ``tier_change`` is 0: no claim is made
  about the relative strength of leagues (assumption, see reports/2026-10-13_champions_league.md).
* ``{side}_shots_*_shrunk`` shrinks toward the club's OWN domestic league prior (the league its
  window comes from), as in training.
* ``league_prior_shots`` (the "league level" of a competition without history) = the mean of the two
  clubs' domestic league priors as of the fixture date. One club unknown -> the other's prior; both
  unknown -> the mean prior of the known clubs in the same fixture list, else the global mean.
* A club without usable history keeps NaN team features and ``{side}_known = False``; the
  experimental model replaces it by a neutral mixture of real team profiles. ``data_quality`` =
  ``full`` (both known) / ``partial`` (one) / ``none``.

These fixtures never go through ``team_form.build_features``: domestic features, league priors and
team histories are exactly what they are without them (tests/test_cross_border.py).

No-leakage rule: everything uses matches with ``date`` strictly before the fixture date.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shots.features import team_form as tf

FULL, PARTIAL, NONE = "full", "partial", "none"
EXTRA_COLUMNS = ["home_known", "away_known", "home_league_prior", "away_league_prior",
                 "home_domestic", "away_domestic", "data_quality"]
_RATES = [f"{m}_{d}" for m in tf.METRICS for d in ("for", "against")]
SIDE_FEATURES = [*(f"{c}_{w}" for c in _RATES for w in ("l5", "l10", "std", "ewm", "venue_l5")),
                 "rest_days", "n_prior_matches", "n_prior_season", "tier_change",
                 "shots_for_shrunk", "shots_against_shrunk"]
MATCH_FEATURES = ["league_prior_shots", "league_prior_n", "mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25"]
IDENTITY = ["match_id", "date", "season", "competition", "tier", "home_team", "away_team",
            "home_team_id", "away_team_id"]


def _prepare(matches: pd.DataFrame) -> pd.DataFrame:
    m = matches.reset_index(drop=True).copy()
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    for c in ("home_shots", "away_shots", "home_sot", "away_sot"):
        m[c] = m[c].astype("float64")
    return m


class LeagueLevels:
    """``team_form._league_prior`` for an arbitrary (competition, season, date): the season-to-date
    mean shots per team per match strictly before the date, falling back to the competition's
    all-time mean and then the global mean below ``PRIOR_MIN_OBS`` team-observations."""

    def __init__(self, matches: pd.DataFrame):
        valid = matches["home_shots"].notna() & matches["away_shots"].notna()
        m = matches[valid]
        self._date = m["date"].to_numpy().astype("datetime64[ns]")
        self._season = m["season"].astype(object).to_numpy().astype(str)
        self._total = (m["home_shots"] + m["away_shots"]).to_numpy(float)
        comp = m["competition"].astype(object).to_numpy()
        self._rows = {c: np.flatnonzero(comp == c) for c in pd.unique(comp)}
        self._cache: dict = {}

    def prior(self, competition: str | None, season: str, date) -> tuple[float, float]:
        """-> (league prior per team per match, team-observations behind the season-to-date value)."""
        key = (competition, str(season), pd.Timestamp(date))
        if key not in self._cache:
            day = np.datetime64(key[2], "ns")
            idx = self._rows.get(competition, np.empty(0, dtype=int))
            idx = idx[self._date[idx] < day]
            cur = idx[self._season[idx] == key[1]]
            n = 2.0 * len(cur)
            if n >= tf.PRIOR_MIN_OBS:
                value = self._total[cur].sum() / n
            elif len(idx):
                value = self._total[idx].sum() / (2.0 * len(idx))
            else:
                before = self._date < day
                value = self._total[before].sum() / (2.0 * before.sum()) if before.any() else float("nan")
            self._cache[key] = (float(value), float(n))
        return self._cache[key]


def team_snapshot(hist: pd.DataFrame, date, is_home: bool, season: str, prior: float,
                  tier: float | None = None) -> dict:
    """Team features (``team_form`` definitions) from ``hist`` = that team's rows of
    ``team_form._team_long`` (sorted by date, match_id), using only rows dated strictly before
    ``date``. ``prior`` = the league prior the shrunk rates shrink toward. ``tier`` = tier of the
    match for ``tier_change``; None -> 0 (cross-border: no statement about league strength)."""
    h = hist[hist["date"] < pd.Timestamp(date)]
    out: dict = {}
    same_season = h[h["season"].to_numpy() == str(season)]
    venue = h[h["is_home"] == is_home]
    for col in _RATES:
        for w in tf.WINDOWS:
            out[f"{col}_l{w}"] = h[col].tail(w).mean()
        out[f"{col}_std"] = same_season[col].mean()
        out[f"{col}_ewm"] = h[col].ewm(halflife=tf.EWM_HALFLIFE, ignore_na=True).mean().iloc[-1] if len(h) else np.nan
        out[f"{col}_venue_l5"] = venue[col].tail(5).mean()
    out["rest_days"] = float((pd.Timestamp(date) - h["date"].iloc[-1]).days) if len(h) else np.nan
    out["n_prior_matches"] = float(len(h))
    out["n_prior_season"] = float(len(same_season))
    if not len(h):
        out["tier_change"] = np.nan
    else:
        out["tier_change"] = 0.0 if tier is None else float(tier) - float(h["tier"].iloc[-1])
    n_win = float(h["shots_for"].tail(tf.SHRINK_WINDOW).notna().sum())
    for d in ("for", "against"):
        m10 = out[f"shots_{d}_l{tf.SHRINK_WINDOW}"]
        m10 = 0.0 if pd.isna(m10) else m10
        out[f"shots_{d}_shrunk"] = (n_win * m10 + tf.SHRINK_K * prior) / (n_win + tf.SHRINK_K)
    out["_n_window"] = n_win
    return {k: (float(v) if v is not None and not pd.isna(v) else np.nan) for k, v in out.items()}


def team_histories(matches: pd.DataFrame, team_ids) -> dict[str, pd.DataFrame]:
    """{team_id: its ``_team_long`` rows} for the given ids, from played matches (prepared frame)."""
    ids = set(team_ids)
    sub = matches[matches["home_team_id"].isin(ids) | matches["away_team_id"].isin(ids)]
    if sub.empty:
        return {}
    long = tf._team_long(sub)
    long = long[long["team"].isin(ids)].copy()
    long["season"] = long["season"].astype(object).astype(str).astype(object)  # plain strings: fast comparisons
    return {team: g for team, g in long.groupby("team", sort=False)}


def build_cross_border_features(matches: pd.DataFrame, fixtures: pd.DataFrame, columns=None) -> pd.DataFrame:
    """One row per fixture with the feature columns of ``team_form.build_features`` (targets
    excluded; league-drift columns NaN) plus ``EXTRA_COLUMNS``. ``matches``: played matches
    (canonical schema); ``fixtures``: cross-border fixtures with domestic team ids.
    ``columns``: optional column order to reindex to (e.g. the played feature frame's)."""
    m = _prepare(matches)
    fx = fixtures.reset_index(drop=True).copy()
    fx["date"] = pd.to_datetime(fx["date"]).dt.normalize()
    hists = team_histories(m, pd.concat([fx["home_team_id"], fx["away_team_id"]]).unique())
    comp_of = dict(zip(m["match_id"].astype(object), m["competition"].astype(object)))
    levels = LeagueLevels(m)

    sides: dict[tuple[int, str], dict] = {}
    for i, r in enumerate(fx.itertuples(index=False)):
        for side in ("home", "away"):
            hist = hists.get(getattr(r, f"{side}_team_id"))
            dom, prior, known = None, np.nan, False
            if hist is not None:
                h = hist[hist["date"] < r.date]
                if len(h):
                    dom = comp_of.get(h["match_id"].iloc[-1])
                    prior = levels.prior(dom, r.season, r.date)[0]
                    known = bool(h["shots_for"].notna().any())
            if known:
                snap = team_snapshot(hist, r.date, side == "home", r.season, prior)
            else:
                snap = {k: np.nan for k in SIDE_FEATURES}
                snap["n_prior_matches"], snap["n_prior_season"] = 0.0, 0.0
            snap.pop("_n_window", None)
            sides[(i, side)] = dict(snap, known=known, league_prior=prior if known else np.nan,
                                    domestic=dom if known else None)

    out = fx[[c for c in IDENTITY if c in fx.columns]].copy()
    for side in ("home", "away"):
        block = pd.DataFrame([sides[(i, side)] for i in range(len(fx))], index=fx.index)
        for c in SIDE_FEATURES:
            out[f"{side}_{c}"] = block[c].astype("float64") if len(block) else np.nan
        out[f"{side}_known"] = block["known"].astype(bool) if len(block) else False
        out[f"{side}_league_prior"] = block["league_prior"].astype("float64") if len(block) else np.nan
        out[f"{side}_domestic"] = block["domestic"] if len(block) else None
    both = out[["home_league_prior", "away_league_prior"]]
    prior = both.mean(axis=1, skipna=True)  # one club unknown -> the other's domestic level
    if prior.isna().any():  # neither club known: the level of the known clubs of the same competition
        long = pd.concat([out[["competition", "home_league_prior"]].set_axis(["c", "p"], axis=1),
                          out[["competition", "away_league_prior"]].set_axis(["c", "p"], axis=1)])
        comp_mean = out["competition"].map(long.groupby("c")["p"].mean())
        glob = pd.Series([levels.prior(None, s, d)[0] for s, d in zip(out["season"], out["date"])], index=out.index)
        prior = prior.fillna(comp_mean).fillna(glob)
    out["league_prior_shots"] = prior.astype("float64")
    out["league_prior_n"] = 0.0
    for c in ("mkt_p_home", "mkt_p_draw", "mkt_p_away", "mkt_p_over25"):
        out[c] = np.nan  # no odds source for these fixtures
    n_known = out["home_known"].astype(int) + out["away_known"].astype(int)
    out["data_quality"] = n_known.map({2: FULL, 1: PARTIAL, 0: NONE})
    if columns is not None:
        keep = [c for c in columns if c not in tf.TARGETS]
        out = out.reindex(columns=[*keep, *(c for c in EXTRA_COLUMNS if c not in keep)])
    return out.reset_index(drop=True)
