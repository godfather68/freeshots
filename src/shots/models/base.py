"""Contract between models (src/shots/models) and the backtest harness (src/shots/evaluation).

A "lower bound at confidence q" is the integer N such that P(total_shots >= N) ≈ q,
i.e. the (1 - q) quantile of the predictive distribution of total match shots.
Coverage of a bound = share of matches where actual total_shots >= N; the target is ≈ q.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

CONFIDENCES = (0.7, 0.8, 0.9)
TARGET = "total_shots"


def bound_column(q: float) -> str:
    """0.8 -> 'lb_80'."""
    return f"lb_{round(q * 100)}"


class ShotModel(ABC):
    """Fit on rows of data/processed/features.parquet; predict per-match bounds.

    The harness only ever passes training rows dated strictly before every prediction row.
    Any calibration/early-stopping split must be carved out of `train` (latest dates), never
    from the rows passed to predict.
    """

    name: str = "base"

    @abstractmethod
    def fit(self, train: pd.DataFrame) -> "ShotModel":
        ...

    @abstractmethod
    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        """Return a frame indexed like `rows` with columns:
        - `median`: point estimate of total shots (float)
        - `lb_70`, `lb_80`, ...: integer lower bounds, one per confidence (see bound_column)
        """
