import math
import random

import pytest

from kalshibot.price_feed import annualize_per_minute, log_returns, per_second_from_annual, realized_vol_per_minute
from kalshibot.pricing import effective_variance, fair_yes_probability, norm_cdf, prob_above


def test_norm_cdf():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-6)


def test_at_the_money_is_about_half():
    p = prob_above(60000, 60000, 0.5, 1800)
    assert 0.49 < p < 0.5  # tiny negative lognormal drift correction


def test_monotonic_in_strike_and_spot():
    ps = [prob_above(60000, k, 0.5, 900) for k in (59000, 59500, 60000, 60500, 61000)]
    assert all(a > b for a, b in zip(ps, ps[1:]))
    assert prob_above(61000, 60000, 0.5, 900) > prob_above(60500, 60000, 0.5, 900)


def test_more_time_or_vol_pulls_towards_half():
    itm_short = prob_above(60300, 60000, 0.5, 120)
    itm_long = prob_above(60300, 60000, 0.5, 3600)
    assert itm_short > itm_long > 0.5
    assert prob_above(60300, 60000, 0.2, 900) > prob_above(60300, 60000, 0.8, 900)


def test_expired_is_deterministic():
    assert prob_above(60001, 60000, 0.5, 0) == 1.0
    assert prob_above(59999, 60000, 0.5, 0) == 0.0


def test_effective_variance_averaging_window():
    s = per_second_from_annual(0.5)
    assert effective_variance(0.5, 600, 60) == pytest.approx(s * s * (600 - 40))
    assert effective_variance(0.5, 600, 0) == pytest.approx(s * s * 600)
    # inside the window: only the unknown remainder contributes
    assert effective_variance(0.5, 30, 60) == pytest.approx(s * s * 30 ** 3 / (3 * 60 * 60))
    assert effective_variance(0.5, 0, 60) == 0.0


def test_basis_uncertainty_widens_distribution():
    tight = prob_above(60100, 60000, 0.3, 120, basis_usd=0)
    wide = prob_above(60100, 60000, 0.3, 120, basis_usd=100)
    assert tight > wide > 0.5


def test_strike_types():
    kw = dict(spot=60000, sigma_annual=0.5, seconds_to_close=900)
    above = fair_yes_probability("greater", 60200, None, **kw)
    assert fair_yes_probability("greater_or_equal", 60200, None, **kw) == above
    below = fair_yes_probability("less", None, 60200, **kw)
    assert above + below == pytest.approx(1.0)
    between = fair_yes_probability("between", 59800, 60200, **kw)
    assert between == pytest.approx(fair_yes_probability("greater", 59800, None, **kw) - above)
    assert fair_yes_probability("custom", 1, 2, **kw) is None
    assert fair_yes_probability("greater", None, None, **kw) is None
    assert fair_yes_probability("between", 60200, 59800, **kw) is None


def test_matches_monte_carlo_with_60s_average():
    """Model vs. brute-force simulation of the 60-second settlement average."""
    rng = random.Random(42)
    vol, s0, T = 0.6, 60000.0, 300
    s = per_second_from_annual(vol)
    for K in (60050.0, 59920.0):
        n, hits = 6000, 0
        for _ in range(n):
            x = acc = 0.0
            for i in range(T):
                x += -0.5 * s * s + s * rng.gauss(0, 1)
                if i >= T - 60:
                    acc += s0 * math.exp(x)
            hits += acc / 60 > K
        mc = hits / n
        model = prob_above(s0, K, vol, T, 60)
        assert abs(mc - model) < 0.02, (K, mc, model)


def test_vol_estimator_recovers_known_vol():
    rng = random.Random(3)
    sig_min = 0.001
    closes = [60000.0]
    for _ in range(5000):
        closes.append(closes[-1] * math.exp(sig_min * rng.gauss(0, 1)))
    est = realized_vol_per_minute(log_returns(closes))
    assert est == pytest.approx(sig_min, rel=0.05)
    ew = realized_vol_per_minute(log_returns(closes), "ewma", 0.97)
    assert ew == pytest.approx(sig_min, rel=0.35)
    assert annualize_per_minute(sig_min) == pytest.approx(sig_min * math.sqrt(525600))
    assert realized_vol_per_minute([0.01]) is None
