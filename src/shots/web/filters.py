"""Query-string filters for the board: parsing (lenient, clamped), applying, and URL building.

Every filter is a GET parameter. Bad values never raise: numbers are clamped to their range,
unknown choices fall back to the default, unknown leagues are ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from shots.db.repo import TOP5, weekend  # noqa: F401  (one definition, shared with the SQL filters)
from shots.db.text import fold

N_LO, N_HI, N_DEFAULT = 15, 40, 24
S_LO, S_HI = 10.0, 45.0
P_MAX, P_STEP = 95, 5
TN_LO, TN_HI, TN_DEFAULT = 5, 25, 12  # team shot threshold slider (experimental team numbers)
SIDES = {"home": "Home team", "away": "Away team", "either": "Either team"}
STRONG = 0.70  # rows at or above this probability are emphasised

GROUPS = {"top5": "Top 5 leagues", "second": "Second tiers"}
WHEN = {"": "Whole board", "today": "Today", "3d": "Next 3 days", "weekend": "This weekend",
        "7d": "7 days", "30d": "30 days"}
SORTS = {"kickoff": "Kick-off", "prob": "Probability", "shots": "Expected shots",
         "above": "Above league average"}


@dataclass(frozen=True)
class Filters:
    leagues: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    n: int = N_DEFAULT
    smin: float | None = None
    smax: float | None = None
    pmin: int = 0
    when: str = ""
    odds: bool = False
    nocold: bool = False
    q: str = ""
    sort: str = "kickoff"
    side: str = ""  # team-threshold filter: "", "home", "away" or "either"
    tn: int = TN_DEFAULT
    tp: int = 0

    def items(self) -> list[tuple[str, str]]:
        """Non-default parameters, in a stable order: the canonical query string."""
        out = [("league", v) for v in self.leagues] + [("grp", v) for v in self.groups]
        if self.n != N_DEFAULT:
            out.append(("n", str(self.n)))
        if self.smin is not None:
            out.append(("smin", f"{self.smin:g}"))
        if self.smax is not None:
            out.append(("smax", f"{self.smax:g}"))
        if self.pmin:
            out.append(("p", str(self.pmin)))
        if self.when:
            out.append(("when", self.when))
        if self.odds:
            out.append(("odds", "1"))
        if self.nocold:
            out.append(("nocold", "1"))
        if self.q:
            out.append(("q", self.q))
        if self.sort != "kickoff":
            out.append(("sort", self.sort))
        if self.side:
            out.append(("side", self.side))
            if self.tn != TN_DEFAULT:
                out.append(("tn", str(self.tn)))
            if self.tp:
                out.append(("tp", str(self.tp)))
        return out

    def qs(self) -> str:
        return urlencode(self.items())

    def chips(self) -> list[tuple[str, "Filters"]]:
        """(label, filters-without-that-item) for each active filter."""
        c: list[tuple[str, Filters]] = []
        for v in self.leagues:
            c.append((v, replace(self, leagues=tuple(x for x in self.leagues if x != v))))
        for v in self.groups:
            c.append((GROUPS[v], replace(self, groups=tuple(x for x in self.groups if x != v))))
        if self.smin is not None:
            c.append((f"at least {self.smin:g} expected shots", replace(self, smin=None)))
        if self.smax is not None:
            c.append((f"at most {self.smax:g} expected shots", replace(self, smax=None)))
        if self.pmin:
            c.append((f"P ≥ {self.pmin}%", replace(self, pmin=0)))
        if self.when:
            c.append((WHEN[self.when], replace(self, when="")))
        if self.odds:
            c.append(("odds published only", replace(self, odds=False)))
        if self.nocold:
            c.append(("no cold starts", replace(self, nocold=False)))
        if self.q:
            c.append((f"team: {self.q}", replace(self, q="")))
        if self.side:
            who = {"home": "home team", "away": "away team", "either": "either team"}[self.side]
            label = f"{who} {self.tn}+ shots" + (f", P ≥ {self.tp}%" if self.tp else "") + " (experimental)"
            c.append((label, replace(self, side="", tn=TN_DEFAULT, tp=0)))
        if self.sort != "kickoff":
            c.append((f"sorted by {SORTS[self.sort].lower()}", replace(self, sort="kickoff")))
        return c


# Quick views shown above the match list: (key, label, what it does, filters, needs team numbers).
PRESETS = (
    ("likely", "Likely high-shot games", "At least a 70% chance of reaching your threshold, most likely first",
     {"pmin": 70, "sort": "prob"}, False),
    ("weekend", "Big leagues this weekend", "Top 5 leagues, Saturday and Sunday",
     {"groups": ("top5",), "when": "weekend"}, False),
    ("team15", "Team over 15 shots", "One side with a 70% chance of 15+ shots (experimental team numbers)",
     {"side": "either", "tn": 15, "tp": 70}, True),
)


def _num(raw, lo: float, hi: float) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return min(max(v, lo), hi)


def parse(params, known_leagues=()) -> Filters:
    """params: anything with .get / .getlist (starlette QueryParams)."""
    known = set(known_leagues)
    leagues = tuple(dict.fromkeys(v for v in params.getlist("league") if v in known))
    groups = tuple(dict.fromkeys(v for v in params.getlist("grp") if v in GROUPS))
    n = _num(params.get("n"), N_LO, N_HI)
    smin, smax = _num(params.get("smin"), S_LO, S_HI), _num(params.get("smax"), S_LO, S_HI)
    if smin is not None and smax is not None and smin > smax:
        smin, smax = smax, smin
    p = _num(params.get("p"), 0, P_MAX)
    when = params.get("when") or ""
    sort = params.get("sort") or "kickoff"
    tn, tp = _num(params.get("tn"), TN_LO, TN_HI), _num(params.get("tp"), 0, P_MAX)
    side = params.get("side") or ""
    return Filters(
        leagues=leagues, groups=groups,
        n=int(round(n)) if n is not None else N_DEFAULT,
        smin=smin, smax=smax,
        pmin=int(round(p / P_STEP) * P_STEP) if p is not None else 0,
        when=when if when in WHEN else "",
        odds=params.get("odds") in ("1", "on", "true"),
        nocold=params.get("nocold") in ("1", "on", "true"),
        q=(params.get("q") or "").strip()[:60],
        sort=sort if sort in SORTS else "kickoff",
        side=side if side in SIDES else "",
        tn=int(round(tn)) if tn is not None else TN_DEFAULT,
        tp=int(round(tp / P_STEP) * P_STEP) if tp is not None else 0,
    )


# prepare / apply / has_team: the pandas reference implementation of the board filters. The web app
# now filters in SQL through shots.db.repo.Repository.latest_board; tests check both agree.

def prepare(board: pd.DataFrame) -> pd.DataFrame:
    """Add the accent-folded search column and a plain date column once, at load time."""
    board = board.copy()
    board["_search"] = (board["home_team"] + " | " + board["away_team"]).map(fold)
    board["_day"] = pd.to_datetime(board["date"]).dt.date
    return board


def has_team(board: pd.DataFrame) -> bool:
    """True if the board carries the experimental per-team columns (older boards do not)."""
    return {"ph_3", "pa_3", "home_mean", "away_mean"} <= set(board.columns)


def apply(board: pd.DataFrame, f: Filters, today: date) -> pd.DataFrame:
    """Filtered and sorted copy with `p` = calibrated P(total >= f.n) read from the stored curve."""
    df = board[board["_day"] >= today].copy()
    df["p"] = df[f"p_{f.n}"].astype(float)
    if f.leagues or f.groups:
        keep = df["competition"].isin(f.leagues)
        if "top5" in f.groups:
            keep |= df["competition"].isin(TOP5)
        if "second" in f.groups:
            keep |= df["tier"] == 2
        df = df[keep]
    if f.side and has_team(df):
        ph, pa = df[f"ph_{f.tn}"].astype(float), df[f"pa_{f.tn}"].astype(float)
        if f.side == "home":
            df["tp"], df["tside"] = ph, "home"
        elif f.side == "away":
            df["tp"], df["tside"] = pa, "away"
        else:  # either team: the better of the two chances
            df["tp"] = np.fmax(ph, pa)
            df["tside"] = np.where(ph >= pa, "home", "away")
        if f.tp:
            df = df[df["tp"] >= f.tp / 100 - 1e-9]
    if f.smin is not None:
        df = df[df["expected_shots"] >= f.smin]
    if f.smax is not None:
        df = df[df["expected_shots"] <= f.smax]
    if f.pmin:
        df = df[df["p"] >= f.pmin / 100 - 1e-9]
    if f.when:
        if f.when == "weekend":
            sat, sun = weekend(today)
            df = df[df["_day"].isin([sat, sun])]
        else:
            last = {"today": 0, "3d": 3, "7d": 7, "30d": 30}[f.when]
            df = df[df["_day"] <= today + timedelta(days=last)]
    if f.odds:
        df = df[df["odds"]]
    if f.nocold:
        df = df[~df["cold_start"]]
    for t in fold(f.q).split():
        df = df[df["_search"].str.contains(t, regex=False)]
    base = ["_day", "kickoff", "competition", "home_team"]
    lead = {"prob": "p", "shots": "expected_shots", "above": "above_league"}.get(f.sort)
    if lead:
        df = df.sort_values([lead] + base, ascending=[False, True, True, True, True])
    else:
        df = df.sort_values(base)
    return df.reset_index(drop=True)
