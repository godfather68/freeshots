"""Weighted NB2 maximum likelihood with a dispersion submodel (structural candidates, 2026-10-15).

    y_i ~ NB2(mu_i, alpha_i),  log mu_i = o_i + x_i' beta,  log alpha_i = z_i' gamma
    Var(y_i) = mu_i + alpha_i mu_i^2

* ``w``: per-row sample weights (recency weighting); normalised to mean 1 so the ridge penalty and
  the convergence tolerance mean the same thing whatever the weights.
* ``o``: offset (e.g. log of a fixed mean when only the dispersion is fitted, or the GLM log mean a
  boosted model starts from).
* ridge: ``0.5 * lam * ||beta[pen_x]||^2 + 0.5 * lam_z * ||gamma[pen_z]||^2`` on the selected
  columns only (competition dummies), on the *standardised* scale of the design (columns are
  centred and scaled internally; the returned coefficients are on the original scale).

With ``z = const``, no weights, no penalty this is the model ``statsmodels.NegativeBinomial(nb2)``
fits (``tests/test_models_candidates.py`` checks the two agree), solved by L-BFGS with the analytic
gradient: d ll / d eta = (y - mu) / (1 + alpha mu); d ll / d log alpha = -r (psi(y + r) - psi(r)
- log(1 + alpha mu) + (mu - y) / (r + mu)), r = 1 / alpha.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import optimize
from scipy.special import digamma, gammaln

LOG_ALPHA_CLIP = (-12.0, 3.0)


@dataclass
class NBFit:
    beta: np.ndarray            # coefficients of x (original scale), length p
    gamma: np.ndarray           # coefficients of z (original scale), length q
    loglik: float               # weighted log-likelihood at the optimum (no penalty)
    converged: bool
    n_iter: int
    info: dict = field(default_factory=dict)

    def mu(self, X: np.ndarray, offset=None) -> np.ndarray:
        eta = (X @ self.beta if X.shape[1] else 0.0) + (0.0 if offset is None else offset)
        return np.exp(np.clip(eta, -20, 20))

    def alpha(self, Z: np.ndarray) -> np.ndarray:
        return np.exp(np.clip(Z @ self.gamma, *LOG_ALPHA_CLIP))


def nb_loglik_terms(y, mu, alpha) -> np.ndarray:
    """Per-row NB2 log-likelihood (alpha > 0)."""
    r = 1.0 / alpha
    am = alpha * mu
    return (gammaln(y + r) - gammaln(r) - gammaln(y + 1.0) - r * np.log1p(am)
            + y * (np.log(am) - np.log1p(am)))


def _standardise(A: np.ndarray, const_idx: int | None):
    """(A_std, centre, scale): constant column(s) kept as they are."""
    centre = A.mean(axis=0)
    scale = A.std(axis=0)
    const = scale < 1e-12
    centre = np.where(const, 0.0, centre)
    scale = np.where(const, 1.0, scale)
    if const_idx is None:
        centre = np.zeros_like(centre)  # no intercept to absorb the centring: scale only
    else:
        centre[const_idx], scale[const_idx] = 0.0, 1.0
    return (A - centre) / scale, centre, scale


def _unstandardise(b_std: np.ndarray, centre, scale, const_idx: int | None) -> np.ndarray:
    b = b_std / scale
    if const_idx is not None:
        b[const_idx] -= float((centre * b).sum() - centre[const_idx] * b[const_idx])
    return b


def fit_nb(y, X, Z=None, w=None, offset=None, pen_x=None, pen_z=None, lam: float = 0.0,
           lam_z: float = 0.0, x_const: int | None = 0, z_const: int | None = 0,
           beta0=None, gamma0=None, maxiter: int = 2000, tol: float = 1e-9) -> NBFit:
    """Maximise the (weighted, penalised) NB2 likelihood. ``X`` may have zero columns (only the
    dispersion is fitted, the mean is ``exp(offset)``). ``x_const`` / ``z_const``: index of the
    intercept column in X / Z (None if there is none); intercepts absorb the centring."""
    y = np.asarray(y, float)
    n = len(y)
    X = np.asarray(X, float).reshape(n, -1)
    Z = np.ones((n, 1)) if Z is None else np.asarray(Z, float).reshape(n, -1)
    w = np.ones(n) if w is None else np.asarray(w, float)
    w = w * (n / w.sum())
    o = np.zeros(n) if offset is None else np.asarray(offset, float)
    p, q = X.shape[1], Z.shape[1]
    if p and x_const is not None and np.ptp(X[:, x_const]) > 0:
        x_const = None
    if z_const is not None and np.ptp(Z[:, z_const]) > 0:
        z_const = None
    Xs, xc, xs = _standardise(X, x_const) if p else (X, np.zeros(0), np.ones(0))
    Zs, zc, zs = _standardise(Z, z_const)
    px = np.zeros(p, bool) if pen_x is None else np.asarray(pen_x, bool)
    pz = np.zeros(q, bool) if pen_z is None else np.asarray(pen_z, bool)

    # starting values on the standardised scale
    b = np.zeros(p)
    if beta0 is not None and p:
        b = np.asarray(beta0, float) * xs
        if x_const is not None:
            b[x_const] = float(beta0[x_const] + (np.asarray(beta0) * xc).sum() - beta0[x_const] * xc[x_const])
    elif p and x_const is not None:
        b[x_const] = np.log(max(np.average(y, weights=w), 1e-3)) - float(np.average(o, weights=w))
    g = np.zeros(q)
    if gamma0 is not None:
        g = np.asarray(gamma0, float) * zs
        if z_const is not None:
            g[z_const] = float(gamma0[z_const] + (np.asarray(gamma0) * zc).sum() - gamma0[z_const] * zc[z_const])
    elif z_const is not None:
        g[z_const] = np.log(0.05)

    lgy1 = gammaln(y + 1.0)

    def f(theta):
        bb, gg = theta[:p], theta[p:]
        eta = np.clip((Xs @ bb if p else 0.0) + o, -20, 20)
        mu = np.exp(eta)
        la = np.clip(Zs @ gg, *LOG_ALPHA_CLIP)
        a = np.exp(la)
        r = 1.0 / a
        am = a * mu
        l1 = np.log1p(am)
        ll = gammaln(y + r) - gammaln(r) - lgy1 - r * l1 + y * (la + eta - l1)
        val = -np.dot(w, ll) / n
        d_eta = (y - mu) / (1.0 + am)
        d_r = digamma(y + r) - digamma(r) - l1 + (mu - y) / (r + mu)
        d_la = -r * d_r
        grad_b = -(Xs.T @ (w * d_eta)) / n if p else np.zeros(0)
        grad_g = -(Zs.T @ (w * d_la)) / n
        if lam and px.any():
            val += 0.5 * lam * np.sum(bb[px] ** 2) / n
            grad_b = grad_b + lam * np.where(px, bb, 0.0) / n
        if lam_z and pz.any():
            val += 0.5 * lam_z * np.sum(gg[pz] ** 2) / n
            grad_g = grad_g + lam_z * np.where(pz, gg, 0.0) / n
        return val, np.concatenate([grad_b, grad_g])

    res = optimize.minimize(f, np.concatenate([b, g]), jac=True, method="L-BFGS-B",
                            options=dict(maxiter=maxiter, ftol=tol, gtol=1e-7, maxcor=30))
    bb, gg = res.x[:p], res.x[p:]
    beta = _unstandardise(bb.copy(), xc, xs, x_const) if p else np.zeros(0)
    gamma = _unstandardise(gg.copy(), zc, zs, z_const)
    eta = np.clip((X @ beta if p else 0.0) + o, -20, 20)
    alpha = np.exp(np.clip(Z @ gamma, *LOG_ALPHA_CLIP))
    ll = float(np.dot(w, nb_loglik_terms(y, np.exp(eta), alpha)))
    return NBFit(beta, gamma, ll, bool(res.success), int(res.nit), {"message": str(res.message)})


def recency_weights(dates, half_life_days: float | None) -> np.ndarray | None:
    """0.5 ** (age / half_life), age in days from the latest date; None = unweighted."""
    if not half_life_days:
        return None
    d = np.asarray(dates, dtype="datetime64[D]")
    age = (d.max() - d).astype(float)
    return 0.5 ** (age / float(half_life_days))


__all__ = ["NBFit", "fit_nb", "nb_loglik_terms", "recency_weights"]
