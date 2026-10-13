"""Indirect validation of the experimental cross-border (Champions League) model.

    python -m shots.evaluation.cross_border [--cache DIR] [--jobs 6] [--out reports/..json]

There are no Champions League shot counts to test on, so calibration on that competition cannot be
measured. These proxies use domestic matches only and answer narrower questions:

  lolo     leave-one-league-out: a top-tier league is removed from fitting AND calibration, then
           predicted (global PIT fallback) -> what an unseen competition costs.
  moved    clubs that changed division: in their first matches of the season their form comes from
           another league. Scored in "cross-border mode" (tier_change = 0, league level = mean of
           the two leagues, shrunk rates toward the old league, global PIT) -> what mixing leagues
           costs, for totals and for the home / away split. Control: other early-season matches.
  masked   one or both teams' features hidden and replaced by the neutral profile mixture -> are
           "partial" / "none" predictions calibrated when the hidden club is an ordinary club of
           the same league?

Periods are by date. The spread parameters (level_sd, split_sd) are estimated on the VALIDATION
seasons only; the TEST season (2025/2026) is scored once with them. All models are the no-odds
variant (market features blanked), fitted on matches before the scored season.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from shots.evaluation.metrics import pinball
from shots.features import cross_border as cbf
from shots.features import team_form as tf
from shots.models import MODELS
from shots.models.cross_border import CrossBorderModel

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
FEATURES_PATH = ROOT / "data" / "processed" / "features.parquet"
MATCHES_PATH = ROOT / "data" / "processed" / "matches.parquet"
BASE = "negbin_pit_comp"
TEST_SEASON = "2025/2026"
LOLO_VAL = ("2024/2025",)
MOVED_VAL = ("2021/2022", "2022/2023", "2023/2024", "2024/2025")
QS = (0.7, 0.8, 0.9)
PROXY_COMP = "cross-border proxy"  # not a fitted competition -> global PIT fallback
EARLY = 2  # "first matches": n_prior_season <= EARLY


def no_odds(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out[[c for c in out.columns if c.startswith("mkt_")]] = float("nan")
    return out


def season_start(feats: pd.DataFrame, season: str) -> pd.Timestamp:
    return feats.loc[feats["season"] == season, "date"].min()


def fit_model(feats: pd.DataFrame, season: str, without: str | None, cache: Path | None):
    """No-odds BASE fitted on rows before `season` (optionally without one competition)."""
    import joblib

    key = f"{BASE}_{season.replace('/', '-')}_{(without or 'all').replace(' ', '_')}.joblib"
    if cache is not None and (cache / key).exists():
        return joblib.load(cache / key)
    train = feats[feats["date"] < season_start(feats, season)]
    if without:
        train = train[train["competition"] != without]
    model = MODELS[BASE]().fit(no_odds(train))
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, cache / key, compress=3)
    return model


def total_scores(y, pred: pd.DataFrame, mean=None, p_bounds: dict | None = None) -> dict:
    """Coverage (>= and mid = avg of >= and >), pinball, MAE of the median; optional mean ratio."""
    y = np.asarray(y, float)
    out = {"n": int(len(y))}
    pins = []
    for q in QS:
        k = round(q * 100)
        lb = pred[f"lb_{k}"].to_numpy(float)
        out[f"cov_{k}"] = float(np.mean(y >= lb))
        out[f"mid_{k}"] = float(0.5 * (np.mean(y >= lb) + np.mean(y > lb)))
        out[f"lb_{k}"] = float(lb.mean())
        pins.append(pinball(y, lb, q))
        out[f"pinball_{k}"] = pins[-1]
    out["pinball_mean"] = float(np.mean(pins))
    out["mae"] = float(np.mean(np.abs(pred["median"].to_numpy(float) - y)))
    out["actual_mean"] = float(y.mean())
    if mean is not None:
        out["pred_mean"] = float(np.mean(mean))
        out["ratio"] = float(y.mean() / np.mean(mean))
    return out


def base_mean(model, rows: pd.DataFrame) -> np.ndarray:
    return model.nb_.side_mu(rows, "home") + model.nb_.side_mu(rows, "away")


def wrap(model, train: pd.DataFrame, level_sd: float, split_sd: float) -> CrossBorderModel:
    return CrossBorderModel.from_base(model, train, level_sd=level_sd, split_sd=split_sd)


# ----------------------------------------------------------------------------- lolo

def lolo(feats: pd.DataFrame, season: str, leagues, cache, level_sd: float | None = None) -> pd.DataFrame:
    """Per held-out league: in-league reference vs unseen (raw) vs unseen with the wrapper."""
    ref = fit_model(feats, season, None, cache)
    train = feats[feats["date"] < season_start(feats, season)]
    rows = []
    for league in leagues:
        test = no_odds(feats[(feats["season"] == season) & (feats["competition"] == league)])
        if test.empty:
            continue
        y = test["total_shots"]
        held = fit_model(feats, season, league, cache)
        rec = {"league": league, "season": season}
        rec.update({f"ref_{k}": v for k, v in total_scores(y, ref.predict(test), base_mean(ref, test)).items()})
        rec.update({f"out_{k}": v for k, v in total_scores(y, held.predict(test), base_mean(held, test)).items()})
        if level_sd is not None:
            w = wrap(held, no_odds(train[train["competition"] != league]), level_sd, 0.0)
            known = test.assign(home_known=True, away_known=True)  # domestic cold starts stay the base model's
            rec.update({f"xb_{k}": v for k, v in total_scores(y, w.predict(known), w.mean(known)).items()})
        rec["se_log_ratio"] = float(y.std() / y.mean() / np.sqrt(len(y)))
        rows.append(rec)
    return pd.DataFrame(rows)


def level_sd_from(lolo_val: pd.DataFrame) -> dict:
    """Between-league SD of log(actual / predicted mean) for unseen leagues, sampling noise removed."""
    lr = np.log(lolo_val["out_ratio"])
    noise = float((lolo_val["se_log_ratio"] ** 2).mean())
    raw = float(lr.var(ddof=1)) if len(lr) > 1 else 0.0
    return {"mean_log_ratio": float(lr.mean()), "sd_raw": float(np.sqrt(raw)),
            "sd_between": float(np.sqrt(max(raw - noise, 0.0))), "rms": float(np.sqrt((lr ** 2).mean())),
            "max_abs": float(lr.abs().max())}


# ----------------------------------------------------------------------------- moved clubs

def moved_teams(feats: pd.DataFrame) -> dict[tuple[str, str], float]:
    """{(team_id, season): tier_change at its first match of the season} for clubs that changed tier."""
    out = {}
    for side in ("home", "away"):
        first = feats[(feats[f"{side}_n_prior_season"] == 0) & feats[f"{side}_tier_change"].notna()
                      & (feats[f"{side}_tier_change"] != 0)]
        for team, season, ch in zip(first[f"{side}_team_id"], first["season"], first[f"{side}_tier_change"]):
            out[(team, season)] = float(ch)
    return out


def previous_competition(matches: pd.DataFrame) -> dict[tuple[str, str], str]:
    """{(team_id, season): competition of the club's last match before that season}."""
    long = pd.concat([matches[["date", "season", "competition", f"{s}_team_id"]].set_axis(
        ["date", "season", "competition", "team"], axis=1) for s in ("home", "away")])
    long = long.sort_values(["team", "date"])
    last = long.groupby(["team", "season"], sort=False).tail(1)
    prev = last.groupby("team", sort=False)["competition"].shift(1)
    return {(t, s): c for t, s, c in zip(last["team"], last["season"], prev) if isinstance(c, str)}


def cross_border_mode(rows: pd.DataFrame, moved_side: pd.Series, old_prior: pd.Series) -> pd.DataFrame:
    """Rows as the cross-border feature builder would give them: no tier information, league level =
    mean of the two leagues, the moved club's shrunk rates shrunk toward its old league, and a
    competition the calibration has never seen."""
    out = rows.copy()
    out["competition"] = PROXY_COMP
    out["home_known"], out["away_known"] = True, True  # cold starts are the base model's, not the mixture's
    for side in ("home", "away"):
        out[f"{side}_tier_change"] = out[f"{side}_tier_change"].where(out[f"{side}_tier_change"].isna(), 0.0)
        m = (moved_side == side) & old_prior.notna()
        for d in ("for", "against"):
            l10 = out[f"{side}_shots_{d}_l10"]
            n = np.minimum(out[f"{side}_n_prior_matches"], tf.SHRINK_WINDOW)
            re = (n * l10.fillna(0.0) + tf.SHRINK_K * old_prior) / (n + tf.SHRINK_K)
            out.loc[m, f"{side}_shots_{d}_shrunk"] = re[m]
    both = old_prior.notna()
    out.loc[both, "league_prior_shots"] = 0.5 * (out.loc[both, "league_prior_shots"] + old_prior[both])
    return out


def team_scores(model, rows: pd.DataFrame, side: pd.Series, which: str) -> dict:
    """Per-team shots of the moved club (which='moved') or its opponent: actual / predicted mean and
    coverage of the calibrated team bounds (global PIT)."""
    out = {}
    act, mu, cov = [], [], {q: [] for q in QS}
    for s in ("home", "away"):
        pick = (side == s) if which == "moved" else (side == ("away" if s == "home" else "home"))
        sub = rows[pick]
        if sub.empty:
            continue
        y = sub[f"{s}_shots"].to_numpy(float)
        tp = model.team_predict(sub, s)
        act.append(y)
        mu.append(tp["mean"].to_numpy(float))
        for q in QS:
            cov[q].append(y >= tp[f"lb_{round(q * 100)}"].to_numpy(float))
    if not act:
        return out
    y, m = np.concatenate(act), np.concatenate(mu)
    out.update(n=int(len(y)), actual=float(y.mean()), pred=float(m.mean()), ratio=float(y.mean() / m.mean()))
    for q in QS:
        out[f"cov_{round(q * 100)}"] = float(np.concatenate(cov[q]).mean())
    return out


def moved(feats: pd.DataFrame, matches: pd.DataFrame, seasons, cache, level_sd: float | None = None,
          split_sd: float = 0.0) -> dict:
    """Pooled over `seasons`. Groups: up (promoted club), down (relegated club), control."""
    mv, prev = moved_teams(feats), previous_competition(matches)
    levels = cbf.LeagueLevels(cbf._prepare(matches))
    acc: dict[str, list] = {}
    for season in seasons:
        model = fit_model(feats, season, None, cache)
        train = no_odds(feats[feats["date"] < season_start(feats, season)])
        test = no_odds(feats[feats["season"] == season])
        hm = [mv.get((t, season)) for t in test["home_team_id"]]
        am = [mv.get((t, season)) for t in test["away_team_id"]]
        test = test.assign(_hm=hm, _am=am)
        early_h, early_a = test["home_n_prior_season"] <= EARLY, test["away_n_prior_season"] <= EARLY
        one = (test["_hm"].notna() & early_h & test["_am"].isna()) | (test["_am"].notna() & early_a & test["_hm"].isna())
        treated = test[one].copy()
        treated["_side"] = np.where(treated["_hm"].notna(), "home", "away")
        treated["_change"] = treated["_hm"].fillna(treated["_am"])
        team = np.where(treated["_side"] == "home", treated["home_team_id"], treated["away_team_id"])
        treated["_old"] = [levels.prior(prev.get((t, season)), season, d)[0] if prev.get((t, season)) else np.nan
                           for t, d in zip(team, treated["date"])]
        control = test[test["_hm"].isna() & test["_am"].isna() & early_h & early_a].copy()
        control["_side"], control["_change"], control["_old"] = "none", 0.0, np.nan
        for name, part in (("up", treated[treated["_change"] < 0]), ("down", treated[treated["_change"] > 0]),
                           ("control", control)):
            if part.empty:
                continue
            xb_rows = cross_border_mode(part, part["_side"], part["_old"])
            acc.setdefault(name, []).append(dict(
                y=part["total_shots"].to_numpy(float), rows=xb_rows, side=part["_side"],
                prod=model.predict(part), prod_mean=base_mean(model, part),
                raw=model.predict(xb_rows), raw_mean=base_mean(model, xb_rows), model=model,
                xb=(wrap(model, train, level_sd, split_sd) if level_sd is not None else None)))
    out = {}
    for name, parts in acc.items():
        y = np.concatenate([p["y"] for p in parts])
        rec = {"production": total_scores(y, pd.concat([p["prod"] for p in parts]), np.concatenate([p["prod_mean"] for p in parts])),
               "cross_border_mode": total_scores(y, pd.concat([p["raw"] for p in parts]), np.concatenate([p["raw_mean"] for p in parts]))}
        if parts[0]["xb"] is not None:
            preds = [p["xb"].predict(p["rows"]) for p in parts]
            means = [p["xb"].mean(p["rows"]) for p in parts]
            rec["experimental"] = total_scores(y, pd.concat(preds), np.concatenate(means))
        if name != "control":
            for which in ("moved", "opponent"):
                for label, key in (("team_raw", "model"), ("team_experimental", "xb")):
                    if parts[0][key] is None:
                        continue
                    rs = [team_scores(p[key], p["rows"], p["side"], which) for p in parts]
                    rs = [r for r in rs if r]
                    n = sum(r["n"] for r in rs)
                    agg = {k: float(sum(r[k] * r["n"] for r in rs) / n) for k in rs[0] if k != "n"}
                    agg["n"] = n
                    agg["ratio"] = agg["actual"] / agg["pred"]
                    rec[f"{label}_{which}"] = agg
        out[name] = rec
    return out


# ----------------------------------------------------------------------------- masked clubs

def masked(feats: pd.DataFrame, season: str, cache, level_sd: float = 0.0, max_rows: int = 800) -> dict:
    """Top-tier matches of `season` with the away side / the home side / both hidden."""
    model = fit_model(feats, season, None, cache)
    train = no_odds(feats[feats["date"] < season_start(feats, season)])
    test = no_odds(feats[(feats["season"] == season) & (feats["tier"] == 1)]).dropna(
        subset=["home_shots_for_ewm", "away_shots_for_ewm"]).sort_values("match_id")
    if len(test) > max_rows:
        test = test.iloc[np.sort(np.random.default_rng(0).choice(len(test), max_rows, replace=False))]
    w = wrap(model, train, level_sd, 0.0)
    y = test["total_shots"]
    out = {"full": total_scores(y, model.predict(test), base_mean(model, test))}
    for name, kh, ka in (("partial_away_hidden", True, False), ("partial_home_hidden", False, True), ("none", False, False)):
        rows = test.assign(home_known=kh, away_known=ka, competition=PROXY_COMP)
        out[name] = total_scores(y, w.predict(rows), w.mean(rows))
    return out


# ----------------------------------------------------------------------------- Champions League finals

# StatsBomb open data: Champions League finals only. (season_id, {StatsBomb name: (our name, country)}).
FINALS = {
    24: {"Bayern Munich": ("Bayern Munich", "Germany"), "Borussia Dortmund": ("Dortmund", "Germany")},
    25: {"Real Madrid": ("Real Madrid", "Spain"), "Atlético Madrid": ("Ath Madrid", "Spain")},
    26: {"Juventus": ("Juventus", "Italy"), "Barcelona": ("Barcelona", "Spain")},
    27: {"Real Madrid": ("Real Madrid", "Spain"), "Atlético Madrid": ("Ath Madrid", "Spain")},
    2: {"Juventus": ("Juventus", "Italy"), "Real Madrid": ("Real Madrid", "Spain")},
    1: {"Real Madrid": ("Real Madrid", "Spain"), "Liverpool": ("Liverpool", "England")},
    4: {"Tottenham Hotspur": ("Tottenham", "England"), "Liverpool": ("Liverpool", "England")},
}
CL_COMPETITION_ID = 16


def finals(feats: pd.DataFrame, matches: pd.DataFrame, level_sd: float, split_sd: float) -> list[dict]:
    """The real pipeline on the few Champions League matches with public shot counts (StatsBomb open
    data: finals 2013-2019). Shots = StatsBomb Shot events of the first 90 minutes (periods 1-2; three
    of these finals went to extra time), a different provider and definition from football-data, at a
    neutral venue: an anecdote (n = 7), not a calibration test. Model: fitted on matches before each final."""
    import requests

    from shots.ingest import statsbomb as sb
    from shots.ingest.schema import make_team_id

    session = requests.Session()
    session.headers["User-Agent"] = sb.USER_AGENT
    out = []
    for season_id, names in FINALS.items():
        mpath = sb._get(session, f"matches/{CL_COMPETITION_ID}/{season_id}.json",
                        sb.RAW_DIR / "matches" / f"{CL_COMPETITION_ID}_{season_id}.json")
        match = sb._load(mpath)[0]
        events = sb._load(sb._get(session, f"events/{match['match_id']}.json",
                                  sb.RAW_DIR / "events" / f"{match['match_id']}.json"))
        shots: dict[str, int] = {}
        for ev in events:
            if ev.get("type", {}).get("name") == "Shot" and ev.get("period", 1) <= 2:
                team = sb.fix_mojibake(ev["team"]["name"])
                shots[team] = shots.get(team, 0) + 1
        home = sb.fix_mojibake(match["home_team"]["home_team_name"])
        away = sb.fix_mojibake(match["away_team"]["away_team_name"])
        day = pd.Timestamp(match["match_date"])
        (hn, hc), (an, ac) = names[home], names[away]
        season = f"{day.year - 1}/{day.year}"
        fx = pd.DataFrame([dict(match_id=f"sb{match['match_id']}", competition="UEFA Champions League", tier=1,
                                season=season, date=day, home_team=hn, away_team=an,
                                home_team_id=make_team_id(hc, hn), away_team_id=make_team_id(ac, an))])
        before = feats[feats["date"] < day]
        rows = cbf.build_cross_border_features(matches[pd.to_datetime(matches["date"]) < day], fx, columns=before.columns)
        model = MODELS[BASE]().fit(no_odds(before))
        w = wrap(model, no_odds(before), level_sd, split_sd)
        pred = w.predict(rows).iloc[0]
        actual = shots.get(home, 0) + shots.get(away, 0)
        out.append(dict(date=str(day.date()), home=hn, away=an, data_quality=rows["data_quality"].iloc[0],
                        actual_90=int(actual), home_90=int(shots.get(home, 0)), away_90=int(shots.get(away, 0)),
                        median=float(pred["median"]), lb_70=int(pred["lb_70"]), lb_80=int(pred["lb_80"]),
                        lb_90=int(pred["lb_90"]), p_24=float(w.prob_at_least(rows, 24)[0]),
                        league_level=float(2 * rows["league_prior_shots"].iloc[0])))
    return out


# ----------------------------------------------------------------------------- run

def run(cache: Path | None = None, jobs: int = 1) -> dict:
    feats = pd.read_parquet(FEATURES_PATH)
    feats = feats[feats["total_shots"].notna()].copy()
    feats["date"] = pd.to_datetime(feats["date"])
    feats["season"] = feats["season"].astype(str)
    matches = pd.read_parquet(MATCHES_PATH)
    matches["season"] = matches["season"].astype(str)
    leagues = sorted(feats.loc[feats["tier"] == 1, "competition"].unique())

    todo = [(s, None) for s in {*MOVED_VAL, *LOLO_VAL, TEST_SEASON}]
    todo += [(s, lg) for s in (*LOLO_VAL, TEST_SEASON) for lg in leagues]
    if jobs > 1 and cache is not None:
        from joblib import Parallel, delayed

        Parallel(n_jobs=jobs)(delayed(fit_model)(feats, s, lg, cache) for s, lg in todo)

    res: dict = {"base_model": f"{BASE} (no odds)", "test_season": TEST_SEASON, "leagues": leagues}
    val = pd.concat([lolo(feats, s, leagues, cache) for s in LOLO_VAL], ignore_index=True)
    res["level"] = level_sd_from(val)
    mv_val = moved(feats, matches, MOVED_VAL, cache)
    # Level uncertainty of the total = unseen competition (LOLO: rms log error of the league mean) combined
    # with mixing two leagues (moved clubs: rms log error of the total's mean in cross-border mode).
    mix = [np.log(mv_val[g]["cross_border_mode"]["ratio"]) for g in ("up", "down") if g in mv_val]
    res["level"]["rms_mixed_leagues"] = float(np.sqrt(np.mean(np.square(mix)))) if mix else 0.0
    level_sd = round(float(np.hypot(res["level"]["rms"], res["level"]["rms_mixed_leagues"])), 3)
    res["level_sd"] = level_sd
    # split uncertainty: how far a club's rates carried across a division step are off, for it and
    # for its opponent (log actual / predicted), as a root-mean-square over both directions
    logs = [np.log(mv_val[g][f"team_raw_{w}"]["ratio"]) for g in ("up", "down") if g in mv_val
            for w in ("moved", "opponent") if f"team_raw_{w}" in mv_val[g]]
    split_sd = round(float(np.sqrt(np.mean(np.square(logs)))), 3) if logs else 0.0
    res["split_sd"] = split_sd
    res["validation"] = {
        "lolo": lolo_summary(pd.concat([lolo(feats, s, leagues, cache, level_sd) for s in LOLO_VAL], ignore_index=True)),
        "moved": moved(feats, matches, MOVED_VAL, cache, level_sd, split_sd),
        "masked": masked(feats, LOLO_VAL[-1], cache, level_sd),
    }
    res["test"] = {
        "lolo": lolo_summary(lolo(feats, TEST_SEASON, leagues, cache, level_sd)),
        "moved": moved(feats, matches, (TEST_SEASON,), cache, level_sd, split_sd),
        "masked": masked(feats, TEST_SEASON, cache, level_sd),
    }
    return res


def lolo_summary(df: pd.DataFrame) -> dict:
    out = {"per_league": df.round(4).to_dict("records")}
    for kind in ("ref", "out", "xb"):
        if f"{kind}_n" not in df:
            continue
        w = df[f"{kind}_n"]
        rec = {c[len(kind) + 1:]: float((df[c] * w).sum() / w.sum()) for c in df if c.startswith(kind + "_") and c != f"{kind}_n"}
        rec["n"] = int(w.sum())
        for q in QS:
            k = round(q * 100)
            rec[f"min_cov_{k}"] = float(df[f"{kind}_cov_{k}"].min())
            rec[f"leagues_below_{k}"] = int((df[f"{kind}_cov_{k}"] < q).sum())
            rec[f"max_abs_mid_err_{k}"] = float((df[f"{kind}_mid_{k}"] - q).abs().max())
        out[kind] = rec
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", type=Path, default=None, help="directory for the fitted models (re-runs are fast)")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--finals", action="store_true", help="only the StatsBomb Champions League finals check "
                    "(downloads 7 open-data matches; uses the production level_sd / split_sd)")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "2026-10-13_champions_league_validation.json")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if args.finals:
        from shots.models.cross_border import LEVEL_SD, SPLIT_SD

        feats = pd.read_parquet(FEATURES_PATH)
        feats = feats[feats["total_shots"].notna()].copy()
        feats["date"] = pd.to_datetime(feats["date"])
        res = finals(feats, pd.read_parquet(MATCHES_PATH), LEVEL_SD, SPLIT_SD)
        out = args.out.with_name(args.out.stem + "_finals.json")
        out.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(pd.DataFrame(res).to_string(index=False))
        return
    res = run(args.cache, args.jobs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("level", "level_sd", "split_sd")}, indent=1))


if __name__ == "__main__":
    main()
