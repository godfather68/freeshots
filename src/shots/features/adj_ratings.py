"""Opponent- and venue-adjusted attack / defence ratings (candidate feature family A).

Model (per statistic y = shots or shots on target; "for" = by the team, "against" = by its opponent)::

    log E[y_home] = log L_home(comp, t) + att_i + def_j        i = home team, j = away team
    log E[y_away] = log L_away(comp, t) + att_j + def_i

* ``L_home`` / ``L_away`` are the competition's trailing level (last 150 matches before the date, home and
  away separately, shrunk to the all-competition trailing level with K = 30 matches). The home
  advantage and the league drift live in ``L``; ratings are relative to the league average.
* ``att`` is how many more (log scale) shots a team creates than an average team, ``def`` how many more it
  concedes than average (positive = leaky defence). Both are stored per team id (so a promoted team keeps
  its rating).

Estimation is an extended Kalman filter (a state-space "dynamic Poisson/NB regression"), updated match by
match. Why this instead of a refit time-decayed GLM: it is strictly causal by construction (the state
before date D has seen dates < D only), same-day exclusion is trivial (all matches of a date are read
before any is written), it handles cold starts through the prior variance, it costs one scalar update
per match (about 1e5 updates for all leagues), and the process-noise term q is an explicit, tunable form
of time decay (rating variance grows by q per day without matches, so old information is down-weighted
and long gaps, e.g. the summer, make ratings adapt faster).

Observation update (NB with dispersion r, mean mu = exp(eta)): innovation z = min((y - mu) / mu, clip_hi),
observation variance omega / Fisher with Fisher = mu * r / (mu + r); with diagonal state covariance
S = P_att + P_def + omega / Fisher, att += P_att / S * z, def += P_def / S * z, P -= P^2 / S.
A team's first match in a different competition than its previous one adds ``move_var`` to both
variances (promotion / relegation: the rating was measured against another league). New teams start at 0
with variance ``p0`` (prior = league average; the variance is exposed so a model can discount it).

Rows without the statistic (NaN) get features but never update the state, so fixtures of the future can be
scored with the same function.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

DEFAULTS = dict(q=3e-5, p0=0.06, r=10.0, omega=1.0, move_var=0.02, window=150, k_level=30, clip_hi=1.5)


def _ns(a) -> np.ndarray:
    return np.asarray(a).astype("datetime64[ns]")


def trailing_sums(group: np.ndarray, date: np.ndarray, vals: np.ndarray, window: int,
                  pool: np.ndarray | None = None) -> np.ndarray:
    """Sums of ``vals`` (n x k) over the last ``window`` rows of the same group dated STRICTLY before
    each row's date (same-day rows excluded). Returns n x (k+1); the last column is the row count.
    ``pool`` (boolean, optional): only these rows can enter a window (every row still gets its sums), e.g.
    rows with a result, so that upcoming fixtures never take places in the trailing window."""
    n = len(date)
    out = np.zeros((n, vals.shape[1] + 1))
    date = _ns(date)
    for g in pd.unique(group):
        idx = np.flatnonzero(group == g)
        src = idx if pool is None else idx[pool[idx]]
        order = src[np.argsort(date[src], kind="mergesort")]
        x = date[order]
        v = np.column_stack([vals[order], np.ones(len(order))])
        c = np.vstack([np.zeros((1, v.shape[1])), np.cumsum(v, axis=0)])
        e = np.searchsorted(x, date[idx], side="left")
        s = np.maximum(e - window, 0)
        out[idx] = c[e] - c[s]
    return out


def league_level(m: pd.DataFrame, home_col: str, away_col: str, window: int, k: float):
    """Trailing home / away level of the statistic per match (strictly before the date)."""
    h = m[home_col].to_numpy(dtype=float)
    a = m[away_col].to_numpy(dtype=float)
    ok = ~(np.isnan(h) | np.isnan(a))
    vals = np.column_stack([np.where(ok, h, 0.0), np.where(ok, a, 0.0), ok.astype(float)])
    comp = m["competition"].to_numpy()
    date = m["date"].to_numpy()
    # only matches with the statistic fill the window: identical on history (no played row lacks shots), and
    # upcoming fixtures (no result yet) see the same trailing matches as a row of the training data would
    c = trailing_sums(comp, date, vals, window, pool=ok)
    g = trailing_sums(np.zeros(len(m), dtype=int), date, vals, window * 20, pool=ok)
    # columns: sum_h, sum_a, n_ok, n_rows
    gh = g[:, 0] / np.where(g[:, 2] > 0, g[:, 2], np.nan)
    ga = g[:, 1] / np.where(g[:, 2] > 0, g[:, 2], np.nan)
    n = c[:, 2]
    lh = (c[:, 0] + k * gh) / (n + k)
    la = (c[:, 1] + k * ga) / (n + k)
    return lh, la, n


def adjusted_ratings(m: pd.DataFrame, stat: str = "shots", **params) -> pd.DataFrame:
    """Pre-match ratings for every row of ``m`` (index preserved).

    Needs ``date, competition, home_team_id, away_team_id, home_<stat>, away_<stat>``.
    Returns ``home_/away_`` x ``att, def, att_sd, def_sd``, ``lvl_home, lvl_away`` (league level used),
    ``exp_home, exp_away`` (model expectation of the statistic) and ``exp_total``.
    """
    p = {**DEFAULTS, **params}
    q, p0, r, omega, move_var = p["q"], p["p0"], p["r"], p["omega"], p["move_var"]
    clip_hi = p["clip_hi"]
    n = len(m)
    mm = m.reset_index(drop=True)
    day = ((mm["date"].astype("datetime64[ns]") - pd.Timestamp("2000-01-01")) // pd.Timedelta(days=1)).to_numpy()
    order = np.argsort(day, kind="mergesort")
    lh, la, _ = league_level(mm, f"home_{stat}", f"away_{stat}", p["window"], p["k_level"])

    codes, uniq = pd.factorize(pd.concat([mm["home_team_id"], mm["away_team_id"]], ignore_index=True))
    hi, ai = codes[:n], codes[n:]
    ccode = pd.factorize(mm["competition"])[0]
    nt = len(uniq)
    att = [0.0] * nt
    dfn = [0.0] * nt
    pa = [p0] * nt
    pdf = [p0] * nt
    last: list = [None] * nt
    lastc = [-1] * nt

    yh = mm[f"home_{stat}"].to_numpy(dtype=float)
    ya = mm[f"away_{stat}"].to_numpy(dtype=float)
    feat = np.full((n, 10), np.nan)  # h_att,h_def,h_asd,h_dsd,a_att,a_def,a_asd,a_dsd,exp_h,exp_a

    def read(t: int, d: int, c: int):
        va, vd = pa[t], pdf[t]
        if last[t] is not None:
            gap = d - last[t]
            va, vd = min(va + q * gap, p0), min(vd + q * gap, p0)
            if lastc[t] != c:
                va, vd = va + move_var, vd + move_var
        return att[t], dfn[t], va, vd

    def update(t_a: int, t_d: int, d: int, c: int, mu: float, y: float):
        """t_a: team whose attack produced y; t_d: team whose defence conceded it."""
        va = read(t_a, d, c)[2]
        vd = read(t_d, d, c)[3]
        fisher = mu * r / (mu + r)
        z = min((y - mu) / mu, clip_hi)
        s = va + vd + omega / fisher
        att[t_a] += va / s * z
        dfn[t_d] += vd / s * z
        pa[t_a] = va - va * va / s
        pdf[t_d] = vd - vd * vd / s

    i = 0
    while i < n:
        j = i
        d0 = day[order[i]]
        while j < n and day[order[j]] == d0:
            j += 1
        grp = order[i:j]
        d = int(d0)
        # 1) read everything dated d (no state of date d is visible)
        for k in grp:
            h, a, c = hi[k], ai[k], ccode[k]
            ha, hd, hva, hvd = read(h, d, c)
            aa, ad, ava, avd = read(a, d, c)
            feat[k, :8] = (ha, hd, math.sqrt(hva), math.sqrt(hvd), aa, ad, math.sqrt(ava), math.sqrt(avd))
            feat[k, 8] = lh[k] * math.exp(ha + ad) if lh[k] == lh[k] else np.nan
            feat[k, 9] = la[k] * math.exp(aa + hd) if la[k] == la[k] else np.nan
        # 2) then write
        for k in grp:
            if not (feat[k, 8] == feat[k, 8] and feat[k, 9] == feat[k, 9]):
                continue
            if yh[k] != yh[k] or ya[k] != ya[k]:
                continue
            h, a, c = hi[k], ai[k], ccode[k]
            update(h, a, d, c, max(feat[k, 8], 1e-3), yh[k])   # home attack vs away defence
            update(a, h, d, c, max(feat[k, 9], 1e-3), ya[k])   # away attack vs home defence
            for t in (h, a):
                last[t], lastc[t] = d, c
        i = j

    out = pd.DataFrame(feat, index=m.index, columns=[
        "home_att", "home_def", "home_att_sd", "home_def_sd",
        "away_att", "away_def", "away_att_sd", "away_def_sd", "exp_home", "exp_away"])
    out["lvl_home"], out["lvl_away"] = lh, la
    out["exp_total"] = out["exp_home"] + out["exp_away"]
    return out
