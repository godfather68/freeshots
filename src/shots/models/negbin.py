"""Negative binomial GLMs for home and away shots -> total-shots distribution (TASKS 3.2).

Two NB2 regressions (log link, Var = mu + alpha * mu^2), one for home shots and one for away
shots, fitted by maximum likelihood with ``statsmodels.discrete.NegativeBinomial`` so the
dispersion ``alpha`` is estimated jointly with the coefficients (not fixed, not moment-guessed).

Design matrix for side s against opponent o (built by ``design``):
  * log rate features (log link => multiplicative effects): s shots-for and o shots-against,
    each as shrunk / EWM / venue-last-5 / season-to-date, s SOT-for EWM and o SOT-against EWM,
    and the league prior. Missing rate features (cold starts) are filled with the league prior
    (SOT: league prior x training SOT/shots ratio) and flagged with missing indicators
    (one per feature family per side).
  * rest days: log1p(clip(rest, 0, 30)); missing -> training median, flagged by ``no_history``.
  * tier_change (missing -> 0), early-season weight min(n_prior_season, 10) / 10.
  * market: P(win s), P(win o), P(over 2.5) (missing -> training mean, flagged).

Total distribution. ``dependence="auto"`` (default) measures the correlation of the home/away
Pearson residuals on the training data. If |rho| < ``rho_tol`` the two NB pmfs are convolved
(independence). Otherwise the total is a single NB moment-matched to
  mean = mu_h + mu_a,  var = V_h + V_a + 2 rho sqrt(V_h V_a),
which carries the measured dependence into the bounds. ``dependence`` can be forced to
"independent" or "moment". The fitted rho is kept in ``self.rho_``.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
from scipy import stats

from shots.models.base import CONFIDENCES, ShotModel
from shots.models.common import clean_train, finalize, lb_from_cdf

log = logging.getLogger(__name__)

MAX_SIDE = 80     # support for each side's pmf
MAX_TOTAL = 160

# (feature column on side s or o, family name for the missing flag)
RATE_COLS = [
    ("s", "shots_for_shrunk"), ("o", "shots_against_shrunk"),
    ("s", "shots_for_ewm"), ("o", "shots_against_ewm"),
    ("s", "shots_for_venue_l5"), ("o", "shots_against_venue_l5"),
    ("s", "shots_for_std"), ("o", "shots_against_std"),
]
SOT_COLS = [("s", "sot_for_ewm"), ("o", "sot_against_ewm")]


def _col(df: pd.DataFrame, who: str, side: str, name: str) -> pd.Series:
    other = "away" if side == "home" else "home"
    return df[f"{side if who == 's' else other}_{name}"].astype(float)


class _Design:
    """Learns fill values on training rows and builds the design matrix for one side."""

    def __init__(self, side: str):
        self.side = side

    def fit(self, df: pd.DataFrame) -> "_Design":
        self.sot_ratio_ = float(
            (df["home_sot_for_ewm"].sum() + df["away_sot_for_ewm"].sum())
            / (df["home_shots_for_ewm"].sum() + df["away_shots_for_ewm"].sum())
        )
        self.prior_mean_ = float(df["league_prior_shots"].mean())
        self.rest_med_ = float(pd.concat([df["home_rest_days"], df["away_rest_days"]]).median())
        # A training set without any odds (the "no odds" variant) gives a NaN mean; fill with 0 so
        # the market columns are constant and get dropped as such.
        self.mkt_mean_ = {c: float(df[c].mean()) if df[c].notna().any() else 0.0 for c in
                          ("mkt_p_home", "mkt_p_away", "mkt_p_over25")}
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        side, other = self.side, ("away" if self.side == "home" else "home")
        prior = df["league_prior_shots"].astype(float).fillna(self.prior_mean_).clip(lower=1.0)
        X = pd.DataFrame(index=df.index)
        X["const"] = 1.0
        X["log_prior"] = np.log(prior)
        for who, name in RATE_COLS:
            v = _col(df, who, side, name)
            X[f"log_{who}_{name}"] = np.log(v.fillna(prior).clip(lower=0.5))
            fam = name.rsplit("_", 1)[-1] if not name.endswith("venue_l5") else "venue"
            flag = f"miss_{who}_{fam}"
            if flag not in X:
                X[flag] = v.isna().astype(float)
        for who, name in SOT_COLS:
            v = _col(df, who, side, name)
            X[f"log_{who}_{name}"] = np.log(v.fillna(prior * self.sot_ratio_).clip(lower=0.2))
        for who in ("s", "o"):
            rest = _col(df, who, side, "rest_days")
            X[f"{who}_no_history"] = rest.isna().astype(float)
            X[f"{who}_log_rest"] = np.log1p(rest.fillna(self.rest_med_).clip(0, 30))
            X[f"{who}_tier_change"] = _col(df, who, side, "tier_change").fillna(0.0).clip(-3, 3)
            X[f"{who}_early"] = (_col(df, who, side, "n_prior_season").fillna(0).clip(0, 10) / 10)
        p_s, p_o = df[f"mkt_p_{side}"].astype(float), df[f"mkt_p_{other}"].astype(float)
        X["mkt_missing"] = (p_s.isna() | p_o.isna()).astype(float)
        X["mkt_p_s"] = p_s.fillna(self.mkt_mean_[f"mkt_p_{side}"])
        X["mkt_p_o"] = p_o.fillna(self.mkt_mean_[f"mkt_p_{other}"])
        o25 = df["mkt_p_over25"].astype(float)
        X["mkt_o25_missing"] = o25.isna().astype(float)
        X["mkt_p_over25"] = o25.fillna(self.mkt_mean_["mkt_p_over25"])
        return X


def _fit_nb(y: np.ndarray, X: pd.DataFrame):
    import statsmodels.api as sm

    # drop constant columns (e.g. a missing flag that is all zero in this window)
    # and exact duplicate columns (e.g. ``miss_s_ewm`` == ``s_no_history``)
    keep: list[str] = []
    for c in X.columns:
        v = X[c].to_numpy()
        if c != "const" and v.std() == 0:
            continue
        if any(np.array_equal(v, X[k].to_numpy()) for k in keep):
            continue
        keep.append(c)
    Xk = X[keep]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # collinear rate features are expected; MLE still fine
        pois = sm.GLM(y, Xk, family=sm.families.Poisson()).fit()
        start = np.append(pois.params.to_numpy(), 0.05)
        res = sm.NegativeBinomial(y, Xk, loglike_method="nb2").fit(
            start_params=start, method="bfgs", maxiter=500, disp=0)
        if not res.mle_retvals.get("converged", True):
            res = sm.NegativeBinomial(y, Xk, loglike_method="nb2").fit(
                start_params=res.params.to_numpy(), method="newton", maxiter=100, disp=0)
    params = res.params
    return keep, params.iloc[:-1].to_numpy(), float(params.iloc[-1]), res


def nb_pmf(mu: np.ndarray, alpha: float | np.ndarray, kmax: int) -> np.ndarray:
    """NB2 pmf on 0..kmax, rows = matches. alpha -> 0 gives Poisson."""
    mu = np.asarray(mu, float)[:, None]
    k = np.arange(kmax + 1)[None, :]
    alpha = np.broadcast_to(np.asarray(alpha, float), (mu.shape[0],))[:, None]
    pois = alpha < 1e-8
    a = np.where(pois, 1.0, alpha)
    n = 1.0 / a
    p = n / (n + mu)
    out = stats.nbinom.pmf(k, n, p)
    if pois.any():
        out = np.where(pois, stats.poisson.pmf(k, mu), out)
    return out


def convolve_rows(ph: np.ndarray, pa: np.ndarray) -> np.ndarray:
    n, K = ph.shape
    total = np.zeros((n, 2 * K - 1))
    for i in range(K):
        total[:, i:i + K] += ph[:, [i]] * pa
    return total


class NegBinModel(ShotModel):
    name = "negbin"

    def __init__(self, dependence: str = "auto", rho_tol: float = 0.03):
        assert dependence in ("auto", "independent", "moment")
        self.dependence = dependence
        self.rho_tol = rho_tol

    def _design(self, side: str) -> _Design:
        """Design-matrix builder for one side (subclasses add features, e.g. ``drift.py``)."""
        return _Design(side)

    def fit(self, train: pd.DataFrame) -> "NegBinModel":
        tr = clean_train(train)
        self.sides_ = {}
        pearson = {}
        for side in ("home", "away"):
            d = self._design(side).fit(tr)
            X = d.transform(tr)
            y = tr[f"{side}_shots"].to_numpy(float)
            cols, beta, alpha, res = _fit_nb(y, X)
            mu = np.exp(X[cols].to_numpy() @ beta)
            pearson[side] = (y - mu) / np.sqrt(mu + alpha * mu ** 2)
            self.sides_[side] = dict(design=d, cols=cols, beta=beta, alpha=alpha,
                                     coef=pd.Series(beta, index=cols))
            log.info("negbin %s: alpha=%.4f converged=%s", side, alpha,
                     res.mle_retvals.get("converged"))
        self.rho_ = float(np.corrcoef(pearson["home"], pearson["away"])[0, 1])
        if self.dependence == "auto":
            self.mode_ = "moment" if abs(self.rho_) >= self.rho_tol else "independent"
        else:
            self.mode_ = self.dependence
        return self

    def side_mu(self, rows: pd.DataFrame, side: str) -> np.ndarray:
        s = self.sides_[side]
        X = s["design"].transform(rows)
        return np.exp(X[s["cols"]].to_numpy() @ s["beta"])

    def total_pmf(self, rows: pd.DataFrame) -> np.ndarray:
        return self.pmf_from_mu(self.side_mu(rows, "home"), self.side_mu(rows, "away"))

    def pmf_from_mu(self, mh: np.ndarray, ma: np.ndarray) -> np.ndarray:
        """Total-shots pmf for given home / away means (the fitted dispersions and dependence)."""
        ah, aa = self.sides_["home"]["alpha"], self.sides_["away"]["alpha"]
        if self.mode_ == "independent":
            return convolve_rows(nb_pmf(mh, ah, MAX_SIDE), nb_pmf(ma, aa, MAX_SIDE))
        mu, alpha_t = self.moment_params(mh, ma)
        return nb_pmf(mu, alpha_t, MAX_TOTAL)

    def moment_params(self, mh: np.ndarray, ma: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(mean, dispersion) of the single NB moment-matched to the total (``mode_ == "moment"``)."""
        ah, aa = self.sides_["home"]["alpha"], self.sides_["away"]["alpha"]
        vh, va = mh + ah * mh ** 2, ma + aa * ma ** 2
        mu = mh + ma
        var = vh + va + 2 * self.rho_ * np.sqrt(vh * va)
        alpha_t = np.clip((var - mu) / mu ** 2, 0.0, None)
        return mu, alpha_t

    def predict(self, rows: pd.DataFrame, confidences=CONFIDENCES) -> pd.DataFrame:
        pmf = self.total_pmf(rows)
        cdf = np.cumsum(pmf, axis=1)
        # median of the predictive distribution (integer); also used as point estimate
        median = lb_from_cdf(cdf, 0.5).astype(float)
        bounds = {q: lb_from_cdf(cdf, q) for q in confidences}
        return finalize(rows.index, median, bounds)
