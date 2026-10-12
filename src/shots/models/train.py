"""Train a registered model on all data up to a date and save it (TASKS 3b.5).

    python -m shots.models.train --model negbin_pit_comp [--until YYYY-MM-DD]

Trains on every row of ``data/processed/features.parquet`` with a known target and
``date <= until`` (default: latest date with a target), then saves to
``models_store/<model>_<until>/`` (``model.joblib`` + ``metadata.json``, see ``persist.py``).
Use the saved model only for matches dated after ``until``.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd

from shots.models import MODELS
from shots.models.base import TARGET
from shots.models.persist import save

ROOT = Path(__file__).resolve().parents[3]
FEATURES_PATH = ROOT / "data" / "processed" / "features.parquet"
STORE = ROOT / "models_store"

log = logging.getLogger(__name__)


def training_rows(features: pd.DataFrame, until: str | None) -> tuple[pd.DataFrame, pd.Timestamp]:
    f = features.copy()
    f["date"] = pd.to_datetime(f["date"])
    f = f[f[TARGET].notna()]
    cutoff = pd.Timestamp(until) if until else f["date"].max()
    return f[f["date"] <= cutoff], cutoff


def train_and_save(name: str, until: str | None = None, features_path: Path = FEATURES_PATH,
                   store: Path = STORE) -> Path:
    if name not in MODELS:
        raise SystemExit(f"unknown model {name!r}; registry has {sorted(MODELS)}")
    train, cutoff = training_rows(pd.read_parquet(features_path), until)
    if train.empty:
        raise SystemExit(f"no training rows dated <= {cutoff.date()}")
    t0 = time.time()
    model = MODELS[name]().fit(train)
    out = store / f"{name}_{cutoff:%Y-%m-%d}"
    save(model, out, train, until=str(cutoff.date()), fit_seconds=round(time.time() - t0, 1),
         features_path=str(features_path))
    log.info("trained %s on %d rows (<= %s) in %.0fs -> %s", name, len(train), cutoff.date(),
             time.time() - t0, out)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", required=True, choices=sorted(MODELS))
    ap.add_argument("--until", default=None, help="last training date, YYYY-MM-DD (inclusive)")
    ap.add_argument("--features", type=Path, default=FEATURES_PATH)
    ap.add_argument("--store", type=Path, default=STORE)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    train_and_save(args.model, args.until, args.features, args.store)


if __name__ == "__main__":
    main()
