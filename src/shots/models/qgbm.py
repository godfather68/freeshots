"""Quantile LightGBM on total shots + split-conformal (CQR) calibration (TASKS 3.3).

Fit procedure (all inside ``train``; prediction rows are never seen):
  1. ``carve_calibration``: the latest training season (>= 3000 rows, earlier seasons added if
     the latest is partial) is held out; the quantile models are trained on the rest.
  2. The held-out slice is split 50/50 by a deterministic hash of ``match_id``:
       * ``es`` half: early stopping only (pinball loss of the respective quantile);
       * ``conf`` half: conformal calibration only.
     Both halves cover the whole held-out season, so calibration sees early- and late-season
     matches alike.
  3. For each confidence q a LightGBM regressor with ``objective="quantile", alpha=1-q`` is
     trained (plus an alpha=0.5 model for the median). Hyperparameters are fixed and modest.
  4. CQR (one-sided): conformity score E = qhat(x) - y on the ``conf`` half; Q_q = the
     ceil((n+1)q)-th smallest score. Bound = ceil(qhat(x) - Q_q). Since y is an integer,
     y >= ceil(qhat - Q) <=> E <= Q, so coverage on the calibration half is >= q (finite-sample
     valid under exchangeability), and the ceil gives the tightest integer bound with that
     guarantee.

Features: ``FEATURES`` (all numeric pre-match features; identifiers/targets excluded), two
derived sums (team-average estimate from shrunk and from EWM form) and ``competition`` as a
categorical (unseen competitions -> missing category). LightGBM handles NaN natively.
"""

from __future__ import annotations

import logging

import lightgbm as lgb
import numpy as np
import pandas as pd

from shots.models.base import CONFIDENCES, TARGET, ShotModel
from shots.models.common import (FEATURES, carve_calibration, ceil_int, clean_train, finalize,
                                 hash_half, threshold_for_coverage)

log = logging.getLogger(__name__)

PARAMS = dict(
    objective="quantile", learning_rate=0.05, num_leaves=31, min_data_in_leaf=200,
    feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
    max_bin=255, verbose=-1, seed=7, deterministic=True, force_row_wise=True,
)
MAX_ROUNDS = 2000
EARLY_STOP = 100


def gbm_matrix(df: pd.DataFrame, categories) -> pd.DataFrame:
    X = df.reindex(columns=FEATURES).astype(float)
    X["pt_shrunk"] = (df["home_shots_for_shrunk"] + df["away_shots_against_shrunk"]
                      + df["away_shots_for_shrunk"] + df["home_shots_against_shrunk"]) / 2
    X["pt_ewm"] = (df["home_shots_for_ewm"] + df["away_shots_against_ewm"]
                   + df["away_shots_for_ewm"] + df["home_shots_against_ewm"]) / 2
    X["competition"] = pd.Categorical(df["competition"], categories=categories)
    return X


class QuantileGBM(ShotModel):
    name = "qgbm"

    def __init__(self, confidences=CONFIDENCES, min_cal_rows: int = 3000):
        self.confidences = tuple(confidences)
        self.min_cal_rows = min_cal_rows

    def fit(self, train: pd.DataFrame) -> "QuantileGBM":
        tr = clean_train(train, [TARGET])
        fit_df, cal_df = carve_calibration(tr, self.min_cal_rows)
        self.categories_ = sorted(fit_df["competition"].unique())
        self.cal_seasons_ = sorted(cal_df["season"].unique())
        es_mask = hash_half(cal_df["match_id"])
        es_df, conf_df = cal_df[es_mask], cal_df[~es_mask]

        self.fit_boosters(fit_df, es_df)
        X_conf, y_conf = gbm_matrix(conf_df, self.categories_), conf_df[TARGET].to_numpy(float)
        self.Q_: dict[float, float] = {}
        self.cal_report_: dict[float, dict] = {}
        for q in self.confidences:
            tau = round(1 - q, 6)
            qhat = self._raw(X_conf, tau)
            Q = threshold_for_coverage(qhat - y_conf, q, finite_sample=True)
            self.Q_[q] = Q
            lb = ceil_int(qhat - Q)
            self.cal_report_[q] = dict(
                raw_coverage=float(np.mean(y_conf >= ceil_int(qhat))),
                cal_coverage=float(np.mean(y_conf >= lb)), shift=Q, n=len(y_conf),
            )
        return self

    def fit_boosters(self, fit_df: pd.DataFrame, es_df: pd.DataFrame) -> "QuantileGBM":
        """Train the uncalibrated quantile boosters (tau = 0.5 and 1 - q) with early stopping on
        ``es_df``. Sets ``categories_`` from ``fit_df`` if not already set."""
        if not hasattr(self, "categories_"):
            self.categories_ = sorted(fit_df["competition"].unique())
        X_fit, y_fit = gbm_matrix(fit_df, self.categories_), fit_df[TARGET].to_numpy(float)
        X_es, y_es = gbm_matrix(es_df, self.categories_), es_df[TARGET].to_numpy(float)
        self.boosters_: dict[float, lgb.Booster] = {}
        for tau in [0.5, *[round(1 - q, 6) for q in self.confidences]]:
            if tau in self.boosters_:
                continue
            params = {**PARAMS, "alpha": tau}
            dtr = lgb.Dataset(X_fit, y_fit, free_raw_data=False)
            des = lgb.Dataset(X_es, y_es, reference=dtr)
            self.boosters_[tau] = lgb.train(
                params, dtr, num_boost_round=MAX_ROUNDS, valid_sets=[des],
                callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)],
            )
            log.info("qgbm tau=%.2f best_iter=%d", tau, self.boosters_[tau].best_iteration)
        return self

    def raw_quantiles(self, rows: pd.DataFrame) -> dict[float, np.ndarray]:
        """Uncalibrated predictions {tau: array} for every fitted tau (0.5 and 1 - q)."""
        X = gbm_matrix(rows, self.categories_)
        return {tau: self._raw(X, tau) for tau in self.boosters_}

    def _raw(self, X: pd.DataFrame, tau: float) -> np.ndarray:
        b = self.boosters_[tau]
        return b.predict(X, num_iteration=b.best_iteration)

    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        missing = [q for q in confidences if q not in self.Q_]
        if missing:
            raise ValueError(f"qgbm was not fitted for confidences {missing}")
        X = gbm_matrix(rows, self.categories_)
        median = self._raw(X, 0.5)
        bounds = {q: ceil_int(self._raw(X, round(1 - q, 6)) - self.Q_[q]) for q in confidences}
        return finalize(rows.index, median, bounds)
