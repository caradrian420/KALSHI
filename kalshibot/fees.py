"""Kalshi trading-fee math.

Fee schedule (as documented by Kalshi; see README "Research notes"):

    taker fee = roundup(0.07   * C * P * (1 - P))   to the next cent
    maker fee = roundup(0.0175 * C * P * (1 - P))   only on series whose fee_type
                                                    includes maker fees

C = number of contracts, P = price in dollars. The fee is computed per order
here and rounded UP to the next whole cent. Kalshi's newer per-fill
"fee rounding" mechanism (centicent trade fee + rounding fee - rebate) can
only produce a total that is <= this per-order cent-ceiling for a single
order, so this module is deliberately conservative (it never under-estimates).

All arithmetic is done in Decimal so the ceiling is exact (no float fuzz
like 0.07*100*0.5*0.5 = 1.7500000000000002 -> 1.76).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Optional

CENT = Decimal("0.01")

DEFAULT_TAKER_RATE = 0.07
DEFAULT_MAKER_RATE = 0.0175

# Series fee_type values from the Kalshi OpenAPI spec (Series.fee_type).
FEE_TYPE_QUADRATIC = "quadratic"
FEE_TYPE_QUADRATIC_MAKER = "quadratic_with_maker_fees"
FEE_TYPE_QUADRATIC_COMBO_MAKER = "quadratic_with_combo_maker_fees"
FEE_TYPE_FLAT = "flat"
SUPPORTED_FEE_TYPES = {
    FEE_TYPE_QUADRATIC,
    FEE_TYPE_QUADRATIC_MAKER,
    FEE_TYPE_QUADRATIC_COMBO_MAKER,
}


def _d(x) -> Decimal:
    # str() first so 0.07 -> Decimal("0.07") rather than its binary expansion.
    return x if isinstance(x, Decimal) else Decimal(str(x))


def ceil_to_cent(amount) -> Decimal:
    """Round a non-negative dollar amount UP to the next whole cent."""
    a = _d(amount)
    if a <= 0:
        return Decimal("0.00")
    return a.quantize(CENT, rounding=ROUND_CEILING)


def quadratic_fee(count: int, price: float, rate: float) -> float:
    """Generic Kalshi quadratic fee for one order, rounded up to the cent.

    count: contracts (>= 0). price: dollars in [0, 1]. rate: 0.07 / 0.0175 / ...
    """
    if count <= 0:
        return 0.0
    p = _d(price)
    if p <= 0 or p >= 1:
        return 0.0
    raw = _d(rate) * _d(count) * p * (Decimal(1) - p)
    return float(ceil_to_cent(raw))


def taker_fee(count: int, price: float, rate: float = DEFAULT_TAKER_RATE) -> float:
    return quadratic_fee(count, price, rate)


def maker_fee(count: int, price: float, rate: float = DEFAULT_MAKER_RATE) -> float:
    return quadratic_fee(count, price, rate)


def fee_per_contract(count: int, price: float, rate: float) -> float:
    """Effective per-contract fee for an order of `count` contracts.

    Because of the cent ceiling, small orders pay much more per contract:
    1 contract at 0.50 with rate 0.07 -> raw 0.0175 -> charged 0.02.
    """
    if count <= 0:
        return 0.0
    return quadratic_fee(count, price, rate) / count


class FeeModel:
    """Fee rates for one series, derived from config + the series metadata.

    Conservative rules:
      * effective rate = base_rate * max(1.0, fee_multiplier) when
        apply_series_multiplier is set, so a discounted multiplier is ignored
        but a surcharge is honoured. (Kalshi's docs describe fee_multiplier only
        as "a floating point multiplier applied to the fee calculations"; we
        never let it LOWER our fee estimate.)
      * maker fees are charged unless the series fee_type is plain 'quadratic'
        (no maker fees) -- an unknown fee_type is treated as charging them.
      * 'flat' or unknown fee types are flagged unsupported; the bot refuses to
        trade those series because the formula differs.
    """

    def __init__(
        self,
        taker_rate: float = DEFAULT_TAKER_RATE,
        maker_rate: float = DEFAULT_MAKER_RATE,
        fee_type: Optional[str] = None,
        fee_multiplier: Optional[float] = None,
        apply_series_multiplier: bool = True,
    ):
        mult = 1.0
        if apply_series_multiplier and fee_multiplier is not None:
            try:
                mult = max(1.0, float(fee_multiplier))
            except (TypeError, ValueError):
                mult = 1.0
        self.fee_type = fee_type
        self.fee_multiplier = fee_multiplier
        self.taker_rate = taker_rate * mult
        if fee_type == FEE_TYPE_QUADRATIC:
            self.maker_rate = 0.0
        elif fee_type == FEE_TYPE_QUADRATIC_COMBO_MAKER:
            # Spec: "same maker-fee structure with a 0.5 maker multiplier instead
            # of 0.25" -> twice the standard maker rate.
            self.maker_rate = maker_rate * 2.0 * mult
        else:
            self.maker_rate = maker_rate * mult
        self.supported = fee_type is None or fee_type in SUPPORTED_FEE_TYPES

    def taker(self, count: int, price: float) -> float:
        return quadratic_fee(count, price, self.taker_rate)

    def maker(self, count: int, price: float) -> float:
        return quadratic_fee(count, price, self.maker_rate)

    def __repr__(self) -> str:  # pragma: no cover - logging only
        return (
            f"FeeModel(fee_type={self.fee_type!r}, mult={self.fee_multiplier!r}, "
            f"taker_rate={self.taker_rate:.4f}, maker_rate={self.maker_rate:.4f}, "
            f"supported={self.supported})"
        )
