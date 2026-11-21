"""Fair-probability model for BTC above/below/between markets.

Settlement value A = simple average of BRTI over the `w` seconds (60) ending
at the reference time T. Model log-price as driftless Brownian motion with
per-second vol sigma. Over the remaining time t:

  * if t >= w: A averages a window entirely in the future. For BM, the
    variance of the window average is sigma^2 * (t - w) + sigma^2 * w / 3
    = sigma^2 * (t - 2w/3).
  * if t < w: part of the window has already happened. We approximate the
    observed part by the current spot (we do not have BRTI history), and the
    unknown part contributes sigma^2 * t^3 / (3 w^2).

Basis risk between our free-feed spot proxy and BRTI is added as an
independent normal error with sd = basis_usd (converted to log terms).

P(A > K) ~= N(d),  d = (ln(S/K) - var/2) / sqrt(var)    (lognormal approximation)

The average of a lognormal is not lognormal, but at horizons of minutes to
an hour and vols < 300% the approximation error is far below 1 cent.
"""

from __future__ import annotations

import math
from typing import Optional

from .price_feed import per_second_from_annual


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def effective_variance(sigma_annual: float, seconds_to_close: float, avg_window: float = 60.0) -> float:
    """Log-variance of the settlement average from now until the reference time."""
    if seconds_to_close <= 0:
        return 0.0
    s = per_second_from_annual(sigma_annual)
    t, w = float(seconds_to_close), float(max(avg_window, 0.0))
    if w <= 0:
        tau = t
    elif t >= w:
        tau = t - 2.0 * w / 3.0
    else:
        tau = t ** 3 / (3.0 * w * w)
    return s * s * tau


def prob_above(spot: float, strike: float, sigma_annual: float, seconds_to_close: float,
               avg_window: float = 60.0, basis_usd: float = 0.0) -> float:
    """P(settlement average > strike)."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    var = effective_variance(sigma_annual, seconds_to_close, avg_window)
    if basis_usd > 0:
        var += (basis_usd / spot) ** 2
    if var <= 0:
        return 1.0 if spot > strike else 0.0
    sd = math.sqrt(var)
    d = (math.log(spot / strike) - 0.5 * var) / sd
    return norm_cdf(d)


def fair_yes_probability(strike_type: Optional[str], floor_strike: Optional[float],
                         cap_strike: Optional[float], spot: float, sigma_annual: float,
                         seconds_to_close: float, avg_window: float = 60.0,
                         basis_usd: float = 0.0) -> Optional[float]:
    """Fair P(YES) for a Kalshi market, or None if the strike structure is unsupported.

    strike_type semantics (Kalshi Market.strike_type):
      greater / greater_or_equal : YES if value >(=) floor_strike
      less / less_or_equal       : YES if value <(=) cap_strike
      between                    : YES if floor_strike <= value <= cap_strike
    Strict vs non-strict inequality has zero probability difference under a
    continuous distribution.
    """
    st = (strike_type or "").lower()
    kw = dict(spot=spot, sigma_annual=sigma_annual, seconds_to_close=seconds_to_close,
              avg_window=avg_window, basis_usd=basis_usd)
    if st in ("greater", "greater_or_equal"):
        if not floor_strike:
            return None
        return prob_above(strike=float(floor_strike), **kw)
    if st in ("less", "less_or_equal"):
        if not cap_strike:
            return None
        return 1.0 - prob_above(strike=float(cap_strike), **kw)
    if st == "between":
        if not floor_strike or not cap_strike or float(cap_strike) < float(floor_strike):
            return None
        p = prob_above(strike=float(floor_strike), **kw) - prob_above(strike=float(cap_strike), **kw)
        return max(0.0, min(1.0, p))
    return None
