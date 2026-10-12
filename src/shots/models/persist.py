"""Save / load fitted models with their metadata (TASKS 3b.5).

Layout of a saved model directory::

    <path>/model.joblib     the fitted ShotModel (joblib pickle)
    <path>/metadata.json    name, class, params, features, training window, calibration info,
                            package versions, creation time

Only load model files you produced yourself: joblib/pickle executes code on load.
"""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path

import joblib
import pandas as pd

from shots.models.base import ShotModel
from shots.models.common import FEATURES

MODEL_FILE = "model.joblib"
META_FILE = "metadata.json"
PACKAGES = ("numpy", "pandas", "scipy", "statsmodels", "lightgbm", "scikit-learn", "joblib")


def package_versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for p in PACKAGES:
        try:
            out[p] = importlib_metadata.version(p)
        except importlib_metadata.PackageNotFoundError:
            out[p] = "missing"
    return out


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if hasattr(x, "item"):  # numpy scalar
        return x.item()
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)


def model_features(model: ShotModel) -> list[str]:
    """Input columns the fitted model reads (beyond identifiers such as ``competition``)."""
    members = [model]
    for attr in ("nb_", "qg_"):
        if hasattr(model, attr):
            members.append(getattr(model, attr))
    cols: list[str] = []
    for m in members:
        if hasattr(m, "sides_"):  # negbin: its own design built from these feature columns
            cols += [c for c in FEATURES if c in _negbin_inputs()]
            if hasattr(m, "extra_inputs"):  # e.g. the league-drift NB (drift.py)
                cols += list(m.extra_inputs())
        if hasattr(m, "boosters_"):
            cols += list(FEATURES) + ["competition"]
    if getattr(model, "group", None):
        cols.append(model.group)
    if not cols:  # baselines
        cols = ["competition"] if model.name == "league_avg" else [
            "home_shots_for_shrunk", "away_shots_for_shrunk", "home_shots_against_shrunk",
            "away_shots_against_shrunk", "league_prior_shots"]
    return list(dict.fromkeys(cols))


def _negbin_inputs() -> set[str]:
    from shots.models.negbin import RATE_COLS, SOT_COLS

    names = {n for _, n in RATE_COLS + SOT_COLS} | {"rest_days", "tier_change", "n_prior_season"}
    cols = {f"{s}_{n}" for s in ("home", "away") for n in names}
    return cols | {"league_prior_shots", "mkt_p_home", "mkt_p_away", "mkt_p_over25"}


def build_metadata(model: ShotModel, train: pd.DataFrame | None = None, **extra) -> dict:
    meta = {
        "model": model.name,
        "class": f"{type(model).__module__}.{type(model).__name__}",
        "params": {k: v for k, v in vars(model).items() if not k.endswith("_")},
        "features": model_features(model),
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packages": package_versions(),
    }
    if train is not None:
        d = pd.to_datetime(train["date"])
        meta["training"] = {"date_min": str(d.min().date()), "date_max": str(d.max().date()),
                            "n_rows": int(len(train)),
                            "seasons": sorted(train["season"].astype(str).unique().tolist())}
    for attr in ("cal_seasons_", "cal_report_"):
        if hasattr(model, attr):
            meta[attr.rstrip("_")] = getattr(model, attr)
    cal = getattr(model, "calibrator_", None)
    if cal is not None:
        meta["calibration"] = {"type": type(cal).__name__, "group": cal.group_col,
                               "min_group": cal.min_group, "thresholds": cal.summary()}
    team = getattr(model, "team_calibrator_", None)
    if team:
        meta["team_calibration"] = {
            side: {"type": type(c).__name__, "thresholds": c.summary(),
                   "cal_report": getattr(model, "team_cal_report_", {}).get(side)}
            for side, c in team.items()}
    if hasattr(model, "Q_"):
        meta["conformal_shift"] = model.Q_
    meta.update(extra)
    return _jsonable(meta)


def save(model: ShotModel, path: str | Path, train: pd.DataFrame | None = None, **extra) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path / MODEL_FILE, compress=3)
    meta = build_metadata(model, train, **extra)
    (path / META_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> ShotModel:
    path = Path(path)
    model = joblib.load(path / MODEL_FILE)
    if not isinstance(model, ShotModel):
        raise TypeError(f"{path / MODEL_FILE} does not contain a ShotModel")
    return model


def load_metadata(path: str | Path) -> dict:
    return json.loads((Path(path) / META_FILE).read_text(encoding="utf-8"))


__all__ = ["save", "load", "load_metadata", "build_metadata", "package_versions"]
