"""Independent leakage audit of the ingest + team-form feature pipeline.

Usage:
    python -m shots.evaluation.leakage_audit          # prints results as JSON-ish text

Checks (all on real data):
1. brute-force recomputation of every feature for a sample of matches, using only matches dated
   strictly before the match (written from scratch, shares no code with team_form.py);
2. truncation/perturbation: rebuild features with all later matches removed and same-day results
   scrambled; features of that day must not change;
3. correlation of features with the same-match target vs. the team's next-match target;
4. odds: parquet odds equal the raw pre-match columns (Avg*/BbAv*), not closing (AvgC*/B365C*);
5. duplicates / identity collisions / date-season consistency;
6. features.parquet is up to date with matches.parquet.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from shots.features.team_form import build_features
from shots.ingest.football_data import DIVISIONS, RAW_DIR, read_raw, to_canonical

ROOT = Path(__file__).resolve().parents[3]
MATCHES = ROOT / "data" / "processed" / "matches.parquet"
FEATURES = ROOT / "data" / "processed" / "features.parquet"

HALFLIFE, K, PRIOR_MIN = 5, 5, 20


# --------------------------------------------------------------------------- 1. brute force

def _team_history(m: pd.DataFrame, team: str, before: pd.Timestamp) -> pd.DataFrame:
    h = m[((m.home_team == team) | (m.away_team == team)) & (m.date < before)]
    h = h.sort_values(["date", "match_id"], kind="mergesort")
    is_home = (h.home_team == team).to_numpy()
    return pd.DataFrame({
        "date": h.date.to_numpy(), "season": h.season.to_numpy(), "tier": h.tier.to_numpy(dtype=float),
        "is_home": is_home,
        "shots_for": np.where(is_home, h.home_shots, h.away_shots).astype(float),
        "shots_against": np.where(is_home, h.away_shots, h.home_shots).astype(float),
        "sot_for": np.where(is_home, h.home_sot, h.away_sot).astype(float),
        "sot_against": np.where(is_home, h.away_sot, h.home_sot).astype(float),
    })


def _mean(x) -> float:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return float(x.mean()) if len(x) else np.nan


def _league_prior(m: pd.DataFrame, comp: str, season: str, d: pd.Timestamp) -> float:
    def ratio(rows):
        return rows.home_shots.sum() + rows.away_shots.sum(), 2 * len(rows)
    s, n = ratio(m[(m.competition == comp) & (m.season == season) & (m.date < d)])
    if n >= PRIOR_MIN:
        return s / n
    s, n = ratio(m[(m.competition == comp) & (m.date < d)])
    if n > 0:
        return s / n
    s, n = ratio(m[m.date < d])
    return s / n if n else np.nan


def brute_force(m: pd.DataFrame, row: pd.Series) -> dict[str, float]:
    d = row.date
    out = {}
    prior = _league_prior(m, row.competition, row.season, d)
    out["league_prior_shots"] = prior
    for side in ("home", "away"):
        team = row[f"{side}_team"]
        h = _team_history(m, team, d)
        venue = h[h.is_home == (side == "home")]
        seas = h[h.season == row.season]
        for met in ("shots", "sot"):
            for dd in ("for", "against"):
                c = f"{met}_{dd}"
                p = f"{side}_{c}"
                out[f"{p}_l5"] = _mean(h[c].iloc[-5:])
                out[f"{p}_l10"] = _mean(h[c].iloc[-10:])
                out[f"{p}_std"] = _mean(seas[c])
                out[f"{p}_venue_l5"] = _mean(venue[c].iloc[-5:])
                out[f"{p}_ewm"] = (h[c].ewm(halflife=HALFLIFE, ignore_na=True).mean().iloc[-1]
                                   if len(h) else np.nan)
        out[f"{side}_rest_days"] = float((d - h.date.iloc[-1]).days) if len(h) else np.nan
        out[f"{side}_n_prior_matches"] = float(len(h))
        out[f"{side}_n_prior_season"] = float(len(seas))
        out[f"{side}_tier_change"] = float(row.tier) - h.tier.iloc[-1] if len(h) else np.nan
        last10 = h.shots_for.iloc[-10:]
        n = float(last10.notna().sum())
        for dd in ("for", "against"):
            mean10 = out[f"{side}_shots_{dd}_l10"]
            mean10 = 0.0 if np.isnan(mean10) else mean10
            out[f"{side}_shots_{dd}_shrunk"] = (n * mean10 + K * prior) / (n + K)
    inv = 1 / np.array([row.odds_home, row.odds_draw, row.odds_away], dtype=float)
    inv = np.where(np.array([row.odds_home, row.odds_draw, row.odds_away], dtype=float) > 1, inv, np.nan)
    p = inv / inv.sum()
    out["mkt_p_home"], out["mkt_p_draw"], out["mkt_p_away"] = p
    o, u = float(row.odds_over25), float(row.odds_under25)
    out["mkt_p_over25"] = (1 / o) / (1 / o + 1 / u) if o > 1 and u > 1 else np.nan
    return out


def check_brute_force(m: pd.DataFrame, f: pd.DataFrame, n_random: int = 160, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    idx = list(rng.choice(len(m), n_random, replace=False))
    # targeted hard cases: same-day doubles, first match of a team's season, earliest-in-season dates
    long = pd.concat([m[["match_id", "date", "home_team"]].rename(columns={"home_team": "t"}),
                      m[["match_id", "date", "away_team"]].rename(columns={"away_team": "t"})])
    same_day = long[long.duplicated(["t", "date"], keep=False)].match_id.unique()
    first_season = m.sort_values("date").groupby(["season", "home_team"]).head(1).match_id.to_numpy()
    targeted = list(rng.choice(same_day, min(10, len(same_day)), replace=False)) if len(same_day) else []
    targeted += list(rng.choice(first_season, 30, replace=False))
    sample_ids = list(m.match_id.iloc[idx]) + targeted
    fi = f.set_index("match_id")
    mi = m.set_index("match_id", drop=False)
    mism = []
    n_vals = 0
    for mid in dict.fromkeys(sample_ids):
        bf = brute_force(m, mi.loc[mid])
        for k, v in bf.items():
            got = fi.at[mid, k]
            n_vals += 1
            if not ((np.isnan(v) and np.isnan(got)) or np.isclose(v, got, rtol=1e-9, atol=1e-9)):
                mism.append({"match_id": mid, "feature": k, "pipeline": float(got), "brute": float(v)})
    return {"matches_checked": len(dict.fromkeys(sample_ids)), "same_day_cases": len(targeted) - 30,
            "values_checked": n_vals, "mismatches": len(mism), "examples": mism[:10]}


# --------------------------------------------------------------------------- 2. truncation

def check_truncation(m: pd.DataFrame, f: pd.DataFrame, n_dates: int = 5, seed: int = 3) -> dict:
    rng = np.random.default_rng(seed)
    busy = m.date.value_counts()
    days = sorted(rng.choice(busy[busy >= 10].index.to_numpy(), n_dates, replace=False))
    fi = f.set_index("match_id")
    cols = [c for c in f.columns if c not in ("match_id", "home_shots", "away_shots", "total_shots")
            and pd.api.types.is_numeric_dtype(f[c])]
    res = []
    for d in days:
        sub = m[m.date <= d].copy()
        today = sub.date == d
        for c in ("home_shots", "away_shots", "home_sot", "away_sot"):
            sub.loc[today, c] = rng.integers(0, 40, today.sum())
        g = build_features(sub).set_index("match_id")
        ids = sub.loc[today, "match_id"]
        a, b = fi.loc[ids, cols].astype(float), g.loc[ids, cols].astype(float)
        diff = ~(np.isclose(a, b, equal_nan=True))
        res.append({"date": str(pd.Timestamp(d).date()), "matches": int(today.sum()),
                    "changed_values": int(diff.sum())})
    return {"dates": res, "total_changed": sum(r["changed_values"] for r in res)}


# --------------------------------------------------------------------------- 3. correlations

def check_correlations(f: pd.DataFrame) -> dict:
    feats = [c for c in f.columns if c not in ("match_id", "home_shots", "away_shots", "total_shots", "date",
                                               "season", "competition", "home_team", "away_team",
                                               "home_team_id", "away_team_id")]
    corr = f[feats + ["total_shots"]].corr(method="spearman")["total_shots"].drop("total_shots")
    top = corr.abs().sort_values(ascending=False)
    # same-match vs next-match correlation per team (long format)
    parts = []
    for side, opp in (("home", "away"), ("away", "home")):
        parts.append(pd.DataFrame({
            "team": f[f"{side}_team"], "date": f["date"], "match_id": f["match_id"],
            "l5": f[f"{side}_shots_for_l5"], "ewm": f[f"{side}_shots_for_ewm"],
            "std": f[f"{side}_shots_for_std"], "y": f[f"{side}_shots"],
        }))
    long = pd.concat(parts).sort_values(["team", "date", "match_id"])
    long["y_next"] = long.groupby("team")["y"].shift(-1)
    long["y_prev"] = long.groupby("team")["y"].shift(1)
    lag = {}
    for c in ("l5", "ewm", "std"):
        ok = long[[c, "y", "y_next", "y_prev"]].dropna()
        lag[c] = {"corr_same_match": round(ok[c].corr(ok.y), 4),
                  "corr_next_match": round(ok[c].corr(ok.y_next), 4),
                  "corr_prev_match(in window)": round(ok[c].corr(ok.y_prev), 4)}
    return {"top_spearman_with_total": {k: round(float(corr[k]), 3) for k in top.index[:12]},
            "max_abs": round(float(top.iloc[0]), 3), "same_vs_next": lag}


# --------------------------------------------------------------------------- 4. odds

def check_odds(m: pd.DataFrame) -> dict:
    mi = m.set_index("match_id")
    out = {}
    pairs = {"odds_home": ("AvgH", "AvgCH"), "odds_over25": ("Avg>2.5", "AvgC>2.5")}
    tot = {k: {"rows": 0, "eq_pre": 0, "eq_closing": 0, "pre_ne_closing": 0} for k in pairs}
    corr_rows = []
    for sd in sorted(RAW_DIR.iterdir()):
        start = 2000 + int(sd.name[:2])
        for p in sorted(sd.glob("*.csv")):
            raw = read_raw(p)
            can = to_canonical(raw, p.stem, start)
            raw = raw.dropna(subset=["HomeTeam", "AwayTeam", "Date"]).reset_index(drop=True)
            if len(raw) != len(can) or not all(c in raw for c in ("AvgH", "AvgCH")):
                continue
            can = can.set_index("match_id")
            keep = can.index.isin(mi.index)
            for k, (pre, clo) in pairs.items():
                if pre not in raw or clo not in raw:
                    continue
                got = mi.loc[can.index[keep], k].to_numpy(dtype=float)
                pv = pd.to_numeric(raw.loc[keep, pre], errors="coerce").to_numpy(dtype=float)
                cv = pd.to_numeric(raw.loc[keep, clo], errors="coerce").to_numpy(dtype=float)
                t = tot[k]
                t["rows"] += int(keep.sum())
                t["eq_pre"] += int(np.isclose(got, pv, equal_nan=True).sum())
                t["eq_closing"] += int(np.isclose(got, cv).sum())
                t["pre_ne_closing"] += int((~np.isclose(pv, cv)).sum())
            if "AvgC>2.5" in raw and "AvgC<2.5" in raw:
                ids = can.index[keep]
                o = pd.to_numeric(raw.loc[keep, "AvgC>2.5"], errors="coerce").to_numpy(float)
                u = pd.to_numeric(raw.loc[keep, "AvgC<2.5"], errors="coerce").to_numpy(float)
                corr_rows.append(pd.DataFrame({"match_id": ids, "p_close": (1 / o) / (1 / o + 1 / u)}))
    out["parquet_vs_raw_2019plus"] = tot
    if corr_rows:
        c = pd.concat(corr_rows).merge(m[["match_id", "home_shots", "away_shots", "odds_over25", "odds_under25"]],
                                       on="match_id")
        c["p_pre"] = (1 / c.odds_over25) / (1 / c.odds_over25 + 1 / c.odds_under25)
        c["total"] = c.home_shots + c.away_shots
        out["corr_total_shots"] = {"pre_match_p_over25": round(c.p_pre.corr(c.total), 4),
                                   "closing_p_over25": round(c.p_close.corr(c.total), 4)}
    return out


# --------------------------------------------------------------------------- 5. integrity

def check_integrity(m: pd.DataFrame) -> dict:
    out = {"match_id_dupes": int(m.match_id.duplicated().sum())}
    out["same_fixture_same_date_multi_comp"] = int(m.duplicated(["date", "home_team", "away_team"]).sum())
    nonsco = m[m.country != "Scotland"]
    rep = nonsco[nonsco.duplicated(["season", "competition", "home_team", "away_team"], keep=False)]
    out["repeated_fixture_in_season_non_scotland"] = int(len(rep))
    out["repeated_fixture_examples"] = rep.sort_values(["home_team", "date"])[
        ["season", "competition", "date", "home_team", "away_team", "home_shots", "away_shots"]
    ].head(8).astype(str).to_dict("records")
    long = pd.concat([m[["date", "home_team", "country", "competition", "season"]].rename(columns={"home_team": "t"}),
                      m[["date", "away_team", "country", "competition", "season"]].rename(columns={"away_team": "t"})])
    out["team_same_date_multi"] = int(long.duplicated(["t", "date"]).sum())
    multi_country = long.groupby("t")["country"].nunique()
    out["team_names_in_multiple_countries"] = sorted(multi_country[multi_country > 1].index.tolist())
    multi_comp = long.groupby(["t", "season"])["competition"].nunique()
    mc = multi_comp[multi_comp > 1]
    out["team_in_multiple_comps_same_season"] = len(mc)
    out["team_in_multiple_comps_examples"] = [f"{t} {s}" for t, s in mc.index[:10]]
    y0 = m.season.str[:4].astype(int)
    lo = pd.to_datetime(y0.astype(str) + "-07-01")
    hi = pd.to_datetime((y0 + 1).astype(str) + "-08-31")
    bad = m[(m.date < lo) | (m.date > hi)]
    out["dates_outside_season_window"] = int(len(bad))
    out["dates_outside_examples"] = bad[["season", "competition", "date", "home_team", "away_team"]].head(5).astype(str).to_dict("records")
    out["max_date"] = str(m.date.max().date())
    return out


def main() -> None:
    m = pd.read_parquet(MATCHES)
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    for c in ("home_shots", "away_shots", "home_sot", "away_sot"):
        m[c] = m[c].astype("float64")
    m["tier"] = m["tier"].astype("float64")
    f = pd.read_parquet(FEATURES)
    res = {}
    g = build_features(pd.read_parquet(MATCHES)).set_index("match_id").loc[f.match_id]
    num = [c for c in f.columns if pd.api.types.is_numeric_dtype(f[c])]
    res["features_parquet_fresh"] = bool(np.isclose(f.set_index("match_id")[num].astype(float),
                                                    g[num].astype(float), equal_nan=True).all())
    res["integrity"] = check_integrity(m)
    res["odds"] = check_odds(m)
    res["correlations"] = check_correlations(f)
    res["brute_force"] = check_brute_force(m, f)
    res["truncation"] = check_truncation(m, f)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
