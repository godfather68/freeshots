"""Candidate family H ("form", 2026-10-26): short-term team form signals found by the over-shoot study
(reports/2026-10-26_overshoot_patterns.md). Not used by production.

Built through ``shots.features.candidates.build_candidates(..., families=(..., "form"))`` because it needs the
rating expectation of family A. Side columns carry a ``home_`` / ``away_`` prefix (table lists the stem).

-----------------------------------  ------------------------------------------------------------------
form_res_l3                          mean over the team's last 3 matches with a result of (match total shots -
                                     ``adj_exp_total_shots`` of that match): recent over/under-shooting relative
                                     to what the opponent-adjusted ratings expected at the time ("momentum").
form_tot_l3 / form_tot_l10           mean match total shots (for + against) of the team's last 3 / 10 matches
                                     (pace; log(l3 / l10) is the recent trend).
form_goals_l5                        mean goals for + against of the team's last 5 matches with a goal count.
form_promo                           +1 promoted this season, -1 relegated, 0 same tier: tier of the team's last
                                     match of the previous season minus this match's tier (sign). NaN when the
                                     team has no match in the previous season (newcomer to the data).
-----------------------------------  ------------------------------------------------------------------

Rule 1. A window only holds matches dated strictly before the match date (``merge_asof`` with
``allow_exact_matches=False`` on the date): same-day matches never see each other, and rows without a result
(upcoming fixtures) neither enter a window nor take a slot in it, so a fixture weeks ahead gets the same value as
it would on the eve of the match if nothing is played in between. The previous season's last tier is a fact
dated before the season.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

RES_WINDOW = 3
TOT_WINDOWS = (3, 10)
GOALS_WINDOW = 5
NAMES = ("form_res_l3", "form_tot_l3", "form_tot_l10", "form_goals_l5", "form_promo")


def _long(m: pd.DataFrame, values: dict[str, tuple[np.ndarray, np.ndarray]]) -> pd.DataFrame:
    parts = []
    for side in ("home", "away"):
        d = {"row": np.arange(len(m)), "is_home": side == "home", "team": m[f"{side}_team_id"].to_numpy(),
             "date": m["date"].to_numpy(), "season": m["season"].astype(str).to_numpy(),
             "tier": (m["tier"] if "tier" in m else pd.Series(np.nan, index=m.index)).to_numpy(float)}
        for k, (h, a) in values.items():
            d[k] = np.asarray(h if side == "home" else a, float)
        parts.append(pd.DataFrame(d))
    return pd.concat(parts, ignore_index=True)


def prior_window_mean(long: pd.DataFrame, col: str, window: int) -> np.ndarray:
    """Per row of ``long``: mean of ``col`` over the team's last ``window`` non-NaN observations dated strictly
    before the row's date (NaN if none)."""
    valid = long[long[col].notna()].sort_values(["team", "date", "row"], kind="mergesort")
    roll = valid.groupby("team", sort=False)[col].rolling(window, min_periods=1).mean().droplevel(0)
    valid = valid.assign(_v=roll.reindex(valid.index).to_numpy())
    # value after the team's last observed date: the last row of that (team, date) holds the full window
    snap = valid.groupby(["team", "date"], sort=False).tail(1)[["team", "date", "_v"]].sort_values("date")
    q = long[["team", "date"]].reset_index().sort_values("date")
    hit = pd.merge_asof(q, snap, on="date", by="team", allow_exact_matches=False)
    return hit.set_index("index")["_v"].reindex(long.index).to_numpy(float)


def _promotion(long: pd.DataFrame) -> np.ndarray:
    """Sign of (tier of the team's last match of the previous season - this tier): +1 promoted, -1 relegated."""
    def prev_label(s: str) -> str:
        y = int(s[:4])
        return f"{y - 1}/{y}"

    played = long.sort_values(["team", "date", "row"], kind="mergesort")
    last = played.groupby(["team", "season"], sort=False)["tier"].last()
    key = pd.MultiIndex.from_arrays([long["team"], long["season"].map(prev_label)])
    prev = last.reindex(key).to_numpy(float)
    return np.sign(prev - long["tier"].to_numpy(float))


def family_form(m: pd.DataFrame, exp_total: np.ndarray) -> dict[str, np.ndarray]:
    """``m``: prepared matches (``candidates._prepare``: date-normalised, ``tier`` present); ``exp_total``: family A
    expectation of the total for each row of ``m``."""
    n = len(m)
    tot = (m["home_shots"] + m["away_shots"]).to_numpy(float)
    res = tot - np.asarray(exp_total, float)
    goals = (m["home_goals"] + m["away_goals"]).to_numpy(float)
    long = _long(m, {"res": (res, res), "tot": (tot, tot), "goals": (goals, goals)})
    feats = {"form_res_l3": prior_window_mean(long, "res", RES_WINDOW),
             "form_tot_l3": prior_window_mean(long, "tot", TOT_WINDOWS[0]),
             "form_tot_l10": prior_window_mean(long, "tot", TOT_WINDOWS[1]),
             "form_goals_l5": prior_window_mean(long, "goals", GOALS_WINDOW),
             "form_promo": _promotion(long)}
    out: dict[str, np.ndarray] = {}
    rows, home = long["row"].to_numpy(), long["is_home"].to_numpy()
    for name, v in feats.items():
        for side, mask in (("home", home), ("away", ~home)):
            a = np.full(n, np.nan)
            a[rows[mask]] = v[mask]
            out[f"{side}_{name}"] = a
    return out


__all__ = ["family_form", "prior_window_mean", "NAMES"]
