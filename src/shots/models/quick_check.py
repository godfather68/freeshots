"""Sanity check of every registered model (not the formal backtest; see shots.evaluation).

    python -m shots.models.quick_check

Trains each model on seasons <= 2023/2024 (calibration, if any, carved out of that window by the
model itself) and reports on 2024/2025 and 2025/2026: coverage per confidence, pinball loss of
each bound at tau = 1 - q, MAE of the median, mean bound. Writes
``reports/<today>_quick_check_models.md``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time
from pathlib import Path

import pandas as pd

from shots.models import MODELS
from shots.models.base import CONFIDENCES, TARGET, bound_column
from shots.models.common import pinball

ROOT = Path(__file__).resolve().parents[3]
FEATURES_PATH = ROOT / "data" / "processed" / "features.parquet"
TRAIN_LAST = "2023/2024"
TEST_SEASONS = ("2024/2025", "2025/2026")


def evaluate(y: pd.Series, pred: pd.DataFrame) -> dict:
    out = {"n": len(y)}
    for q in CONFIDENCES:
        c = bound_column(q)
        out[f"cov{c[2:]}"] = float((y >= pred[c]).mean())
    for q in CONFIDENCES:
        c = bound_column(q)
        out[f"pin{c[2:]}"] = pinball(y, pred[c], 1 - q)
    out["mae_median"] = float((y - pred["median"]).abs().mean())
    for q in CONFIDENCES:
        c = bound_column(q)
        out[f"mean_{c}"] = float(pred[c].mean())
    return out


def main(argv=None) -> pd.DataFrame:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=list(MODELS))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    df = pd.read_parquet(FEATURES_PATH)
    df = df[df[TARGET].notna()]
    train = df[df["season"] <= TRAIN_LAST]
    test = df[df["season"].isin(TEST_SEASONS)]

    rows, notes = [], []
    for name in args.models:
        t0 = time.time()
        model = MODELS[name]().fit(train)
        fit_s = time.time() - t0
        pred = model.predict(test)
        assert pred.index.equals(test.index)
        for label, mask in [*((s, test["season"] == s) for s in TEST_SEASONS),
                            ("both", pd.Series(True, index=test.index))]:
            rows.append({"model": name, "test": label, "fit_s": round(fit_s, 1),
                         **evaluate(test.loc[mask, TARGET], pred[mask])})
        if hasattr(model, "rho_"):
            notes.append(f"- negbin: alpha_home={model.sides_['home']['alpha']:.4f}, "
                         f"alpha_away={model.sides_['away']['alpha']:.4f}, home/away Pearson "
                         f"residual corr rho={model.rho_:.3f} -> total mode `{model.mode_}`.")
        if hasattr(model, "cal_report_"):
            cal = ", ".join(f"q={q}: raw {r['raw_coverage']:.3f} -> calibrated "
                            f"{r['cal_coverage']:.3f} (shift {r['shift']:+.2f}, n={r['n']})"
                            for q, r in model.cal_report_.items())
            notes.append(f"- qgbm: calibration seasons {model.cal_seasons_}; "
                         f"best iterations { {t: b.best_iteration for t, b in model.boosters_.items()} }; "
                         f"conformal half: {cal}.")

    table = pd.DataFrame(rows)
    fmt = table.copy()
    for c in fmt.columns:
        if c.startswith("cov"):
            fmt[c] = fmt[c].map("{:.3f}".format)
        elif c.startswith(("pin", "mae", "mean_")):
            fmt[c] = fmt[c].map("{:.3f}".format)
    md = fmt.to_markdown(index=False)
    print(md)
    print("\n".join(notes))

    today = dt.date.today().isoformat()
    out = ROOT / "reports" / f"{today}_quick_check_models.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        f"# Quick check of shot models ({today})\n\n"
        f"Sanity check only (formal walk-forward backtest lives in `shots.evaluation`).\n"
        f"Train: seasons <= {TRAIN_LAST} ({len(train):,} matches); test: {', '.join(TEST_SEASONS)} "
        f"({len(test):,} matches). Bounds `lb_q`: P(total >= lb) target q. `covXX` = share of "
        f"matches with total >= lb_XX (target 0.XX); `pinXX` = pinball loss of lb_XX at "
        f"tau = 1 - q; `mae_median` = MAE of the `median` column.\n\n"
        f"{md}\n\nNotes:\n" + "\n".join(notes) + "\n",
        encoding="utf-8",
    )
    print(f"\nwritten {out}")
    return table


if __name__ == "__main__":
    main()
