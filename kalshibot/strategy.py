"""Signal generation: fair probability vs. executable price after fees.

    edge(side) = P_model(side) - (best_ask(side) + fee_per_contract + slippage_buffer)

Trade only if edge >= min_edge. Both sides (YES / NO) are evaluated and the
better one is reported. Taker and maker variants are both computed and
logged; which one is used depends on strategy.order_style.

Contract count is the largest integer such that
    count * ask + taker_fee(count, ask) <= max_risk_per_trade
and count <= displayed size at the best ask. Because the fee is rounded up
per order, fee_per_contract depends on count -- it is recomputed for the
actual count, so tiny orders correctly show their worse per-contract fee.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .api_client import fnum
from .config import Config
from .fees import FeeModel
from .pricing import fair_yes_probability

TICK = 0.01


def parse_ts(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class MarketView:
    """The subset of a Kalshi market we need, normalised to floats/seconds."""
    ticker: str
    series: str
    event_ticker: str
    strike_type: Optional[str]
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    close_ts: float
    status: str
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    yes_bid_size: Optional[float]
    yes_ask_size: Optional[float]
    fee_waiver_until: Optional[float] = None

    @classmethod
    def from_api(cls, m: Dict[str, Any], series: str) -> Optional["MarketView"]:
        close_ts = parse_ts(m.get("close_time"))
        if not m.get("ticker") or close_ts is None:
            return None
        yb = fnum(m.get("yes_bid_dollars"))
        ya = fnum(m.get("yes_ask_dollars"))
        # Kalshi reports 0 bid / 1 ask (or 0) when a side is empty.
        if yb is not None and yb <= 0:
            yb = None
        if ya is not None and (ya <= 0 or ya >= 1):
            ya = None
        return cls(
            ticker=m["ticker"], series=series, event_ticker=m.get("event_ticker", ""),
            strike_type=m.get("strike_type"), floor_strike=fnum(m.get("floor_strike")),
            cap_strike=fnum(m.get("cap_strike")), close_ts=close_ts, status=m.get("status", ""),
            yes_bid=yb, yes_ask=ya,
            yes_bid_size=fnum(m.get("yes_bid_size_fp")), yes_ask_size=fnum(m.get("yes_ask_size_fp")),
            fee_waiver_until=parse_ts(m.get("fee_waiver_expiration_time")),
        )


@dataclass
class Book:
    """Executable top-of-book for both outcomes, derived from a bids-only book."""
    yes_ask: Optional[float]
    yes_ask_size: float
    no_ask: Optional[float]
    no_ask_size: float
    yes_bid: Optional[float]
    no_bid: Optional[float]
    yes_asks: List[Tuple[float, float]] = field(default_factory=list)  # best first
    no_asks: List[Tuple[float, float]] = field(default_factory=list)

    @classmethod
    def from_ladders(cls, yes_bids: List[Tuple[float, float]], no_bids: List[Tuple[float, float]]) -> "Book":
        # A NO bid at q is a YES offer at 1-q (and vice versa).
        yes_asks = [(round(1 - p, 6), n) for p, n in no_bids]
        no_asks = [(round(1 - p, 6), n) for p, n in yes_bids]
        return cls(
            yes_ask=yes_asks[0][0] if yes_asks else None,
            yes_ask_size=yes_asks[0][1] if yes_asks else 0.0,
            no_ask=no_asks[0][0] if no_asks else None,
            no_ask_size=no_asks[0][1] if no_asks else 0.0,
            yes_bid=yes_bids[0][0] if yes_bids else None,
            no_bid=no_bids[0][0] if no_bids else None,
            yes_asks=yes_asks, no_asks=no_asks,
        )

    @classmethod
    def from_market_view(cls, mv: MarketView) -> "Book":
        """Top-of-book from the /markets listing (used for pre-filter and replay)."""
        yb = [(mv.yes_bid, mv.yes_bid_size or 0.0)] if mv.yes_bid else []
        nb = [(round(1 - mv.yes_ask, 6), mv.yes_ask_size or 0.0)] if mv.yes_ask else []
        return cls.from_ladders(yb, nb)

    def ask(self, side: str) -> Tuple[Optional[float], float]:
        return (self.yes_ask, self.yes_ask_size) if side == "yes" else (self.no_ask, self.no_ask_size)

    def bid(self, side: str) -> Optional[float]:
        return self.yes_bid if side == "yes" else self.no_bid

    def spread(self) -> Optional[float]:
        if self.yes_ask is None or self.yes_bid is None:
            return None
        return round(self.yes_ask - self.yes_bid, 6)


@dataclass
class Signal:
    ts: float
    ticker: str
    series: str
    strike_type: Optional[str]
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    close_ts: float
    secs_to_close: float
    spot: Optional[float]
    sigma_ann: Optional[float]
    fair_yes: Optional[float] = None
    side: Optional[str] = None
    side_prob: Optional[float] = None
    best_ask: Optional[float] = None
    best_bid: Optional[float] = None
    ask_size: float = 0.0
    spread: Optional[float] = None
    count: int = 0
    taker_fee_pc: Optional[float] = None
    taker_edge: Optional[float] = None
    maker_price: Optional[float] = None
    maker_fee_pc: Optional[float] = None
    maker_edge: Optional[float] = None
    style: Optional[str] = None
    limit_price: Optional[float] = None
    edge: Optional[float] = None
    decision: str = "skip"
    reason: str = ""

    @property
    def is_trade(self) -> bool:
        return self.decision == "trade"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def size_for_budget(price: float, budget: float, fee_model: FeeModel, maker: bool = False) -> int:
    """Largest count with count*price + fee(count) <= budget."""
    if price <= 0 or budget <= 0:
        return 0
    n = int(math.floor(budget / price + 1e-9))
    fee_fn = fee_model.maker if maker else fee_model.taker
    while n > 0 and n * price + fee_fn(n, price) > budget + 1e-9:
        n -= 1
    return n


class Strategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def evaluate(self, mv: MarketView, book: Book, spot: Optional[float], sigma_ann: Optional[float],
                 fee_model: FeeModel, now: float, budget: Optional[float] = None) -> Signal:
        s, r = self.cfg.strategy, self.cfg.risk
        budget = r.max_risk_per_trade if budget is None else min(budget, r.max_risk_per_trade)
        secs = mv.close_ts - now
        sig = Signal(ts=now, ticker=mv.ticker, series=mv.series, strike_type=mv.strike_type,
                     floor_strike=mv.floor_strike, cap_strike=mv.cap_strike, close_ts=mv.close_ts,
                     secs_to_close=round(secs, 1), spot=spot, sigma_ann=sigma_ann, spread=book.spread())

        def skip(reason: str) -> Signal:
            sig.decision, sig.reason = "skip", reason
            return sig

        if spot is None or sigma_ann is None:
            return skip("no_price_or_vol")
        if not fee_model.supported:
            return skip(f"unsupported_fee_type:{fee_model.fee_type}")
        if secs <= r.min_seconds_to_expiry:
            return skip("too_close_to_expiry")
        if secs > s.max_minutes_to_expiry * 60:
            return skip("too_far_from_expiry")

        sigma_used = min(max(sigma_ann, s.vol_floor_annual), s.vol_cap_annual) * s.vol_multiplier
        fair = fair_yes_probability(mv.strike_type, mv.floor_strike, mv.cap_strike, spot, sigma_used,
                                    secs, s.settlement_avg_seconds, s.basis_uncertainty_usd)
        if fair is None:
            return skip(f"unsupported_strike:{mv.strike_type}")
        sig.fair_yes = round(fair, 6)

        candidates = []
        for side in ("yes", "no"):
            p_side = fair if side == "yes" else 1.0 - fair
            ask, size = book.ask(side)
            bid = book.bid(side)
            cand = {"side": side, "p": p_side, "ask": ask, "size": size, "bid": bid}
            if ask is not None:
                n = size_for_budget(ask, budget, fee_model)
                n = min(n, int(size))
                cand["count"] = n
                # fee per contract at the size we'd actually trade (>=1 for display)
                n_fee = max(n, 1)
                cand["taker_fee_pc"] = fee_model.taker(n_fee, ask) / n_fee
                cand["taker_edge"] = p_side - (ask + cand["taker_fee_pc"] + s.slippage_buffer)
            # maker alternative: improve best bid by one tick, never cross the ask.
            if bid is not None:
                mp = round(bid + TICK, 4)
                if ask is not None and mp >= ask:
                    mp = None
            else:
                mp = None
            if mp is not None and 0 < mp < 1:
                mn = max(size_for_budget(mp, budget, fee_model, maker=True), 1)
                cand["maker_price"] = mp
                cand["maker_fee_pc"] = fee_model.maker(mn, mp) / mn
                cand["maker_count"] = size_for_budget(mp, budget, fee_model, maker=True)
                # no slippage on a resting order, but adverse selection is real:
                # keep the same buffer for comparability.
                cand["maker_edge"] = p_side - (mp + cand["maker_fee_pc"] + s.slippage_buffer)
            candidates.append(cand)

        def best_edge(c):
            return max(c.get("taker_edge", -9), c.get("maker_edge", -9))

        best = max(candidates, key=best_edge)
        sig.side = best["side"]
        sig.side_prob = round(best["p"], 6)
        sig.best_ask = best["ask"]
        sig.best_bid = best["bid"]
        sig.ask_size = best["size"]
        sig.taker_fee_pc = _r(best.get("taker_fee_pc"))
        sig.taker_edge = _r(best.get("taker_edge"))
        sig.maker_price = best.get("maker_price")
        sig.maker_fee_pc = _r(best.get("maker_fee_pc"))
        sig.maker_edge = _r(best.get("maker_edge"))

        style = s.order_style
        if style == "auto":
            style = "maker" if (best.get("maker_edge", -9) > best.get("taker_edge", -9)) else "taker"
        if self.cfg.mode == "paper":
            style = "taker"  # paper fills are simulated conservatively as taker only
        sig.style = style

        if style == "taker":
            if best["ask"] is None:
                return skip("no_ask")
            sig.limit_price, sig.edge, sig.count = best["ask"], sig.taker_edge, best.get("count", 0)
        else:
            if best.get("maker_price") is None:
                return skip("no_maker_price")
            sig.limit_price, sig.edge, sig.count = best["maker_price"], sig.maker_edge, best.get("maker_count", 0)

        spread = book.spread()
        if spread is None:
            return skip("one_sided_book")
        if spread > r.max_spread:
            return skip("spread_too_wide")
        if not (r.min_price <= sig.limit_price <= r.max_price):
            return skip("price_out_of_band")
        if sig.edge is None or sig.edge < s.min_edge:
            return skip("edge_below_min")
        if sig.count < 1:
            return skip("size_zero_after_fees_or_depth")
        sig.decision, sig.reason = "trade", "edge_ok"
        return sig


def _r(x, nd=6):
    return None if x is None else round(x, nd)
