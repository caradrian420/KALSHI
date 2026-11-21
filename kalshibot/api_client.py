"""Kalshi Trade API v2 REST client.

Endpoint paths and field names were taken from Kalshi's official generated
Python SDK (kalshi_python_sync 3.36.0, built from their OpenAPI spec):

  GET    /exchange/status
  GET    /series/{series_ticker}                -> {"series": Series}
  GET    /markets?series_ticker=&status=open    -> {"markets": [...], "cursor": ""}
  GET    /markets/{ticker}                      -> {"market": Market}
  GET    /markets/{ticker}/orderbook?depth=N    -> {"orderbook_fp": {"yes_dollars": [[p, n]], "no_dollars": [[p, n]]}}
  POST   /portfolio/events/orders               (V2 create order; side = bid|ask on the YES book)
  DELETE /portfolio/events/orders/{order_id}?market_ticker=
  GET    /portfolio/orders/{order_id}           -> {"order": Order}
  GET    /portfolio/orders?status=resting
  GET    /portfolio/positions, /portfolio/balance, /portfolio/fills, /portfolio/settlements

Robustness: token-bucket rate limiting (separate read/write buckets),
exponential backoff with jitter on 429 / 5xx / network errors, Retry-After
support, and clock-drift measurement from the HTTP Date header. Writes
(POST/DELETE) are retried only on errors where the request provably did not
reach the exchange OR when a client_order_id makes the retry idempotent.
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

import requests

from .auth import KalshiSigner

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiAPIError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: Optional[float] = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.rate = max(rate_per_sec, 0.01)
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return waited
                need = (tokens - self.tokens) / self.rate
            self._sleep(need)
            waited += need


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        signer: Optional[KalshiSigner] = None,
        reads_per_second: float = 10.0,
        writes_per_second: float = 5.0,
        timeout: float = 10.0,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        max_clock_drift: float = 2.0,
        session: Optional[requests.Session] = None,
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_clock_drift = max_clock_drift
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "kalshibot/0.1"})
        self.read_bucket = TokenBucket(reads_per_second, sleep=sleep)
        self.write_bucket = TokenBucket(writes_per_second, sleep=sleep)
        self._sleep = sleep
        self.clock_offset = 0.0  # server_time - local_time (seconds, ~1s resolution)
        self._drift_warned = False

    # ------------------------------------------------------------------ core
    def _backoff(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return min(self.backoff_max, retry_after)
        delay = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        return delay * (0.5 + random.random() / 2)

    def _note_server_time(self, resp: requests.Response) -> None:
        date = resp.headers.get("Date")
        if not date:
            return
        try:
            server = email.utils.parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            return
        offset = server - time.time()
        # HTTP Date has 1-second resolution; smooth it.
        self.clock_offset = 0.8 * self.clock_offset + 0.2 * offset
        if abs(self.clock_offset) > self.max_clock_drift and not self._drift_warned:
            log.warning("Local clock differs from Kalshi server by ~%.1fs. Enable NTP "
                        "(timedatectl set-ntp true). Signing timestamps will be corrected.",
                        self.clock_offset)
            self._drift_warned = True

    def _signed_headers(self, method: str, path: str) -> Dict[str, str]:
        if not self.signer:
            return {}
        corr = self.clock_offset if abs(self.clock_offset) > self.max_clock_drift else 0.0
        ts_ms = int((time.time() + corr) * 1000)
        return self.signer.headers(method, self.base_url + path, timestamp_ms=ts_ms)

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
        idempotent: Optional[bool] = None,
    ) -> Dict[str, Any]:
        method = method.upper()
        is_write = method in ("POST", "PUT", "DELETE")
        if idempotent is None:
            idempotent = not is_write
        if auth and not self.signer:
            raise KalshiAPIError(0, "this call needs API credentials but none are configured")
        bucket = self.write_bucket if is_write else self.read_bucket
        url = self.base_url + path
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            bucket.acquire()
            headers = self._signed_headers(method, path) if auth else {}
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            try:
                resp = self.session.request(method, url, params=clean_params or None,
                                            json=json_body, headers=headers, timeout=self.timeout)
            except requests.exceptions.ConnectionError as exc:
                # Connection refused / DNS: the request did not reach the server.
                last_exc = exc
                wait = self._backoff(attempt)
                log.warning("%s %s connection error (%s); retry in %.1fs", method, path, exc.__class__.__name__, wait)
                self._sleep(wait)
                continue
            except requests.exceptions.Timeout as exc:
                last_exc = exc
                if not idempotent:
                    # A timed-out write may have been executed: do not blindly resend.
                    raise KalshiAPIError(0, f"timeout on non-idempotent {method} {path}") from exc
                wait = self._backoff(attempt)
                log.warning("%s %s timeout; retry in %.1fs", method, path, wait)
                self._sleep(wait)
                continue
            except requests.exceptions.RequestException as exc:
                raise KalshiAPIError(0, f"request failed: {exc.__class__.__name__}") from exc

            self._note_server_time(resp)
            if resp.status_code in RETRYABLE_STATUS and (idempotent or resp.status_code == 429):
                wait = self._backoff(attempt, _parse_retry_after(resp.headers.get("Retry-After")))
                log.warning("%s %s -> HTTP %s; retry %d/%d in %.1fs", method, path,
                            resp.status_code, attempt + 1, self.max_retries, wait)
                last_exc = KalshiAPIError(resp.status_code, resp.text[:300])
                self._sleep(wait)
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text[:500]
                raise KalshiAPIError(resp.status_code, str(body)[:300], body)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise KalshiAPIError(resp.status_code, "invalid JSON in response") from exc
        raise KalshiAPIError(getattr(last_exc, "status", 0), f"giving up after {self.max_retries + 1} attempts: {last_exc}")

    # ------------------------------------------------------------ market data
    def get_exchange_status(self) -> Dict[str, Any]:
        return self.request("GET", "/exchange/status")

    def get_series(self, series_ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/series/{series_ticker}").get("series", {})

    def get_markets(self, series_ticker: Optional[str] = None, status: Optional[str] = "open",
                    event_ticker: Optional[str] = None, max_close_ts: Optional[int] = None,
                    min_close_ts: Optional[int] = None, min_settled_ts: Optional[int] = None,
                    limit: int = 200, max_pages: int = 10) -> List[Dict[str, Any]]:
        """Note (API spec): close_ts filters only combine with status 'closed' or
        empty; settled_ts filters only with 'settled' or empty."""
        out: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            data = self.request("GET", "/markets", params={
                "series_ticker": series_ticker, "status": status, "event_ticker": event_ticker,
                "max_close_ts": max_close_ts, "min_close_ts": min_close_ts,
                "min_settled_ts": min_settled_ts, "limit": limit, "cursor": cursor,
            })
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_market(self, ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        data = self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return data.get("orderbook_fp") or data.get("orderbook") or {}

    # -------------------------------------------------------------- portfolio
    def get_balance(self) -> Dict[str, Any]:
        return self.request("GET", "/portfolio/balance", auth=True)

    def get_positions(self, ticker: Optional[str] = None) -> Dict[str, Any]:
        return self.request("GET", "/portfolio/positions", params={"ticker": ticker}, auth=True)

    def get_orders(self, status: Optional[str] = None, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        data = self.request("GET", "/portfolio/orders", params={"status": status, "ticker": ticker}, auth=True)
        return data.get("orders") or []

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/portfolio/orders/{order_id}", auth=True).get("order", {})

    def get_fills(self, ticker: Optional[str] = None, order_id: Optional[str] = None,
                  min_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        data = self.request("GET", "/portfolio/fills",
                            params={"ticker": ticker, "order_id": order_id, "min_ts": min_ts}, auth=True)
        return data.get("fills") or []

    def get_settlements(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        data = self.request("GET", "/portfolio/settlements", params={"ticker": ticker}, auth=True)
        return data.get("settlements") or []

    def create_order(
        self,
        ticker: str,
        outcome: str,
        outcome_price: float,
        count: int,
        time_in_force: str = "immediate_or_cancel",
        post_only: bool = False,
        expiration_ts: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Buy `count` contracts of `outcome` ('yes'|'no') at limit `outcome_price`.

        The V2 endpoint quotes everything on the YES book: buying YES = bid at p;
        buying NO at q = ask (sell YES) at 1 - q. We never send reduce_only or
        sells of existing positions: this bot only opens positions and holds them
        to settlement.
        """
        outcome = outcome.lower()
        if outcome not in ("yes", "no"):
            raise ValueError("outcome must be 'yes' or 'no'")
        if count < 1:
            raise ValueError("count must be >= 1")
        if not 0 < outcome_price < 1:
            raise ValueError("price must be strictly between 0 and 1")
        book_side, yes_price = ("bid", outcome_price) if outcome == "yes" else ("ask", 1.0 - outcome_price)
        body: Dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": str(int(count)),
            "price": f"{yes_price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": True,
        }
        if post_only:
            body["post_only"] = True
        if expiration_ts is not None and time_in_force == "good_till_canceled":
            body["expiration_time"] = int(expiration_ts)
        # client_order_id makes a resend safe (exchange rejects duplicates), so
        # we allow retry on 5xx.
        return self.request("POST", "/portfolio/events/orders", json_body=body, auth=True, idempotent=True)

    def cancel_order(self, order_id: str, ticker: Optional[str] = None) -> Dict[str, Any]:
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}",
                            params={"market_ticker": ticker}, auth=True, idempotent=True)


# ---------------------------------------------------------------- parsing
def fnum(value, default: Optional[float] = None) -> Optional[float]:
    """Parse a Kalshi fixed-point string ("0.5600", "10.00") or number."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_orderbook(ob: Dict[str, Any]) -> Dict[str, List[tuple]]:
    """Normalise an orderbook_fp payload to bid ladders sorted best-first.

    Kalshi's book holds BIDS only. yes_dollars = bids for YES, no_dollars =
    bids for NO. Best YES ask = 1 - best NO bid (size = that NO bid's size).
    Returns {"yes_bids": [(price, size)], "no_bids": [(price, size)]}.
    """
    def ladder(levels: Optional[Iterable]) -> List[tuple]:
        out = []
        for lvl in levels or []:
            try:
                p, n = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError, IndexError):
                continue
            # Legacy integer-cent books (pre fixed-point) used 1..99.
            if p > 1.0:
                p = p / 100.0
            if 0 < p < 1 and n > 0:
                out.append((round(p, 6), n))
        return sorted(out, key=lambda x: -x[0])

    yes = ob.get("yes_dollars") if "yes_dollars" in ob else ob.get("yes")
    no = ob.get("no_dollars") if "no_dollars" in ob else ob.get("no")
    return {"yes_bids": ladder(yes), "no_bids": ladder(no)}
