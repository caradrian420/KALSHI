"""BTC/USD spot + 1-minute volatility from free public exchange APIs.

Kalshi BTC markets settle on the CF Benchmarks Bitcoin Real Time Index (BRTI),
via a simple average of BRTI over the 60 seconds before the reference time
(confirmed from Kalshi's BTC contract terms PDF). BRTI itself is not free, so
we build a proxy: the MEDIAN of last-trade prices from several large USD
spot venues (Coinbase, Kraken, Bitstamp by default). BRTI is computed from
USD order books on constituent exchanges; the residual basis between this
proxy and BRTI is modelled as extra uncertainty in the pricing model
(strategy.basis_uncertainty_usd), not ignored.

All endpoints here are public and need no key. Their exact response formats
were NOT verifiable from the build sandbox (network blocked) -- see README.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def _coinbase_spot(s: requests.Session, timeout: float) -> float:
    r = s.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=timeout)
    r.raise_for_status()
    return float(r.json()["price"])


def _kraken_spot(s: requests.Session, timeout: float) -> float:
    r = s.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError(f"kraken error {data['error']}")
    result = data["result"]
    first = next(iter(result.values()))
    return float(first["c"][0])


def _bitstamp_spot(s: requests.Session, timeout: float) -> float:
    r = s.get("https://www.bitstamp.net/api/v2/ticker/btcusd/", timeout=timeout)
    r.raise_for_status()
    return float(r.json()["last"])


def _gemini_spot(s: requests.Session, timeout: float) -> float:
    r = s.get("https://api.gemini.com/v1/pubticker/btcusd", timeout=timeout)
    r.raise_for_status()
    return float(r.json()["last"])


SPOT_SOURCES: Dict[str, Callable[[requests.Session, float], float]] = {
    "coinbase": _coinbase_spot,
    "kraken": _kraken_spot,
    "bitstamp": _bitstamp_spot,
    "gemini": _gemini_spot,
}


def coinbase_1m_closes(s: requests.Session, timeout: float, minutes: int) -> List[Tuple[int, float]]:
    """[(unix_ts, close)] oldest-first for the last `minutes` 1-minute candles.

    Coinbase Exchange candles: [time, low, high, open, close, volume], newest first,
    max 300 per request.
    """
    minutes = min(max(minutes, 2), 300)
    end = int(time.time())
    start = end - minutes * 60
    r = s.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        params={"granularity": 60, "start": start, "end": end},
        timeout=timeout,
    )
    r.raise_for_status()
    rows = r.json()
    out = [(int(row[0]), float(row[4])) for row in rows if len(row) >= 5]
    out.sort(key=lambda x: x[0])
    return out


# ------------------------------------------------------------------ vol math
def log_returns(closes: List[float]) -> List[float]:
    out = []
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def realized_vol_per_minute(returns: List[float], method: str = "stdev", ewma_lambda: float = 0.94) -> Optional[float]:
    """Std-dev of 1-minute log returns (zero-mean assumption for ewma)."""
    if len(returns) < 2:
        return None
    if method == "ewma":
        var = returns[0] ** 2
        for r in returns[1:]:
            var = ewma_lambda * var + (1 - ewma_lambda) * r * r
        return math.sqrt(var)
    return statistics.stdev(returns)


def annualize_per_minute(sigma_min: float) -> float:
    return sigma_min * math.sqrt(SECONDS_PER_YEAR / 60.0)


def per_second_from_annual(sigma_ann: float) -> float:
    return sigma_ann / math.sqrt(SECONDS_PER_YEAR)


@dataclass
class SpotQuote:
    price: float
    ts: float
    sources: Dict[str, float]
    divergence_pct: float  # (max-min)/median*100 across sources


class PriceFeed:
    def __init__(self, sources: List[str], vol_source: str = "coinbase", timeout: float = 5.0,
                 session: Optional[requests.Session] = None):
        unknown = [s for s in sources if s not in SPOT_SOURCES]
        if unknown:
            raise ValueError(f"unknown price sources: {unknown}")
        self.sources = sources
        self.vol_source = vol_source
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "kalshibot/0.1"})
        self.last: Optional[SpotQuote] = None
        self._fail_counts: Dict[str, int] = {s: 0 for s in sources}
        # our own sampled spot history (ts, price) for the minute-close fallback
        self.history: Deque[Tuple[float, float]] = deque(maxlen=20000)

    def fetch_spot(self) -> Optional[SpotQuote]:
        prices: Dict[str, float] = {}
        for name in self.sources:
            try:
                p = SPOT_SOURCES[name](self.session, self.timeout)
                if p > 0 and math.isfinite(p):
                    prices[name] = p
                    self._fail_counts[name] = 0
            except Exception as exc:  # noqa: BLE001 - any feed failure is non-fatal
                self._fail_counts[name] += 1
                if self._fail_counts[name] in (1, 10) or self._fail_counts[name] % 100 == 0:
                    log.warning("spot source %s failed (%dx): %s", name, self._fail_counts[name], exc)
        if not prices:
            return None
        vals = sorted(prices.values())
        med = statistics.median(vals)
        div = (vals[-1] - vals[0]) / med * 100.0 if len(vals) > 1 else 0.0
        q = SpotQuote(price=med, ts=time.time(), sources=prices, divergence_pct=div)
        self.last = q
        self.history.append((q.ts, q.price))
        return q

    def minute_closes_from_history(self, minutes: int) -> List[float]:
        """Fallback 1-minute closes built from our own spot samples."""
        if not self.history:
            return []
        buckets: Dict[int, float] = {}
        for ts, p in self.history:
            buckets[int(ts // 60)] = p  # last sample in each minute
        keys = sorted(buckets)[-(minutes + 1):]
        # require consecutive minutes; gaps would inflate returns
        closes = []
        prev = None
        for k in keys:
            if prev is not None and k != prev + 1:
                closes = []
            closes.append(buckets[k])
            prev = k
        return closes

    def fetch_vol(self, window_minutes: int, method: str = "stdev", ewma_lambda: float = 0.94,
                  min_samples: int = 30) -> Tuple[Optional[float], int, str]:
        """Return (annualized vol, n_returns, source) or (None, n, source)."""
        closes: List[float] = []
        src = "none"
        if self.vol_source == "coinbase":
            try:
                closes = [c for _, c in coinbase_1m_closes(self.session, self.timeout, window_minutes + 1)]
                src = "coinbase_candles"
            except Exception as exc:  # noqa: BLE001
                log.warning("coinbase candles failed: %s; falling back to sampled history", exc)
        if len(closes) < min_samples + 1:
            hist = self.minute_closes_from_history(window_minutes)
            if len(hist) > len(closes):
                closes, src = hist, "sampled_history"
        rets = log_returns(closes)
        sig = realized_vol_per_minute(rets, method, ewma_lambda)
        if sig is None or len(rets) < min_samples:
            return None, len(rets), src
        return annualize_per_minute(sig), len(rets), src
