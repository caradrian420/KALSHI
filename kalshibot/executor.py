"""Order execution: paper simulation and real (demo/live) Kalshi orders.

PaperExecutor (conservative by design):
  * always crosses the spread (buys at the best ask), pays full taker fees;
  * fills only up to the size actually displayed at that price in the REAL
    production book at decision time (partial fills allowed, never more);
  * no maker fills are ever simulated.

KalshiExecutor (demo or live):
  * taker  -> immediate_or_cancel limit order at the best ask (never sweeps
    deeper than the price we priced the edge at);
  * maker  -> post_only good_till_canceled order with expiration_time =
    now + order_ttl_seconds, plus active cancellation of stale orders;
  * fills are read back from GET /portfolio/fills?order_id=..., using
    outcome_side + yes/no_price_dollars + fee_cost (unambiguous per fill).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from .api_client import KalshiAPIError, KalshiClient, fnum
from .fees import FeeModel
from .storage import Storage
from .strategy import Book, MarketView, Signal

log = logging.getLogger(__name__)


class PaperExecutor:
    name = "paper"

    def __init__(self, store: Storage, clock=time.time):
        self.store = store
        self.clock = clock

    def execute(self, sig: Signal, book: Book, mv: MarketView, fee_model: FeeModel,
                signal_id: Optional[int]) -> Dict[str, Any]:
        now = self.clock()
        side = sig.side
        levels = book.yes_asks if side == "yes" else book.no_asks
        limit = sig.limit_price
        available = sum(n for p, n in levels if p <= limit + 1e-9) if levels else 0.0
        # Only the top level is guaranteed to be at our priced level; we priced
        # the edge at the best ask, so restrict to size at exactly that price.
        top_price, top_size = book.ask(side)
        if top_price is None or abs(top_price - limit) > 1e-9:
            available = 0.0
        else:
            available = min(available, top_size)
        qty = int(min(sig.count, available))
        coid = f"paper-{uuid.uuid4()}"
        if qty < 1:
            oid = self.store.insert_order(now, coid, mv.ticker, side, "taker", limit, sig.count,
                                          "canceled", signal_id, note="paper: no displayed size at limit")
            return {"order_id": oid, "filled": 0, "status": "canceled"}
        fee = fee_model.taker(qty, limit)
        oid = self.store.insert_order(now, coid, mv.ticker, side, "taker", limit, sig.count,
                                      "filled" if qty == sig.count else "partial", signal_id)
        self.store.update_order(oid, filled_count=qty, fees=fee)
        self.store.insert_fill(now, oid, mv.ticker, side, limit, qty, fee, True)
        self.store.add_to_position(mv.ticker, mv.series, side, qty, limit, fee, now, mv.close_ts,
                                   sig.side_prob, sig.edge, signal_id)
        log.info("PAPER FILL %s %s x%d @ %.2f fee %.2f (edge %.3f, p=%.3f)",
                 mv.ticker, side.upper(), qty, limit, fee, sig.edge or 0, sig.side_prob or 0)
        return {"order_id": oid, "filled": qty, "status": "filled", "fee": fee}

    def poll(self, ttl_seconds: float) -> None:
        return None

    def cancel_all(self) -> None:
        return None


class KalshiExecutor:
    def __init__(self, store: Storage, client: KalshiClient, mode: str, ttl_seconds: int, clock=time.time):
        if mode not in ("demo", "live"):
            raise ValueError("KalshiExecutor only for demo/live")
        self.name = mode
        self.store = store
        self.client = client
        self.ttl = ttl_seconds
        self.clock = clock
        self._meta: Dict[int, Dict[str, Any]] = {}  # local order id -> signal info for positions

    def execute(self, sig: Signal, book: Book, mv: MarketView, fee_model: FeeModel,
                signal_id: Optional[int]) -> Dict[str, Any]:
        now = self.clock()
        coid = str(uuid.uuid4())
        maker = sig.style == "maker"
        oid = self.store.insert_order(now, coid, mv.ticker, sig.side, sig.style, sig.limit_price,
                                      sig.count, "pending", signal_id)
        self._meta[oid] = {"series": mv.series, "close_ts": mv.close_ts, "prob": sig.side_prob,
                           "edge": sig.edge, "signal_id": signal_id}
        try:
            resp = self.client.create_order(
                ticker=mv.ticker, outcome=sig.side, outcome_price=sig.limit_price, count=sig.count,
                time_in_force="good_till_canceled" if maker else "immediate_or_cancel",
                post_only=maker,
                expiration_ts=int(now + self.ttl) if maker else None,
                client_order_id=coid,
            )
        except KalshiAPIError as exc:
            # status 0 timeout on a write: the order MAY exist. Mark unknown and
            # reconcile via client_order_id on the next poll.
            status = "unknown" if exc.status == 0 else "rejected"
            self.store.update_order(oid, status=status, note=str(exc)[:300])
            self.store.log_event("ERROR", "order", f"{mv.ticker}: {exc}")
            log.error("order %s %s failed: %s", mv.ticker, sig.side, exc)
            return {"order_id": oid, "filled": 0, "status": status}
        ex_id = resp.get("order_id")
        remaining = fnum(resp.get("remaining_count"), 0.0) or 0.0
        self.store.update_order(oid, exchange_order_id=ex_id,
                                status="resting" if (maker and remaining > 0) else "done")
        filled = self._sync_fills(oid, ex_id, mv.ticker)
        log.info("%s ORDER %s %s x%d @ %.2f -> filled %d (%s)", self.name.upper(), mv.ticker,
                 sig.side.upper(), sig.count, sig.limit_price, filled, "resting" if maker else "ioc")
        return {"order_id": oid, "filled": filled, "status": "resting" if maker else "done"}

    def _sync_fills(self, oid: int, ex_id: Optional[str], ticker: str) -> int:
        """Pull fills for an exchange order; record new ones; return total filled."""
        if not ex_id:
            return 0
        try:
            fills = self.client.get_fills(order_id=ex_id)
        except KalshiAPIError as exc:
            log.warning("get_fills(%s) failed: %s", ex_id, exc)
            return 0
        meta = self._meta.get(oid) or self._meta_from_db(oid)
        total = 0
        fees = 0.0
        for f in fills:
            if f.get("order_id") and f.get("order_id") != ex_id:
                continue
            side = (f.get("outcome_side") or f.get("side") or "").lower()
            cnt = int(round(fnum(f.get("count_fp"), 0.0) or 0.0))
            price = fnum(f.get("no_price_dollars") if side == "no" else f.get("yes_price_dollars"), 0.0)
            fee = fnum(f.get("fee_cost"), 0.0) or 0.0
            total += cnt
            fees += fee
            fid = self.store.insert_fill(self.clock(), oid, ticker, side, price, cnt, fee,
                                         bool(f.get("is_taker")), f.get("fill_id") or f.get("trade_id"))
            if fid is not None and cnt > 0:
                self.store.add_to_position(ticker, meta.get("series", ""), side, cnt, price, fee,
                                           self.clock(), meta.get("close_ts"), meta.get("prob"),
                                           meta.get("edge"), meta.get("signal_id"))
        self.store.update_order(oid, filled_count=total, fees=round(fees, 4))
        return total

    def _meta_from_db(self, oid: int) -> Dict[str, Any]:
        rows = self.store.query(
            "SELECT s.series, s.close_ts, s.side_prob, s.edge, s.id AS sid FROM orders o "
            "LEFT JOIN signals s ON s.id = o.signal_id WHERE o.id=?", (oid,))
        if not rows:
            return {}
        r = rows[0]
        return {"series": r["series"] or "", "close_ts": r["close_ts"], "prob": r["side_prob"],
                "edge": r["edge"], "signal_id": r["sid"]}

    def _reconcile_unknown(self, o) -> Optional[str]:
        """Find an order whose create call timed out, by client_order_id."""
        try:
            for eo in self.client.get_orders(ticker=o["ticker"]):
                if eo.get("client_order_id") == o["client_order_id"]:
                    self.store.update_order(o["id"], exchange_order_id=eo.get("order_id"),
                                            status="resting" if eo.get("status") == "resting" else "done")
                    return eo.get("order_id")
        except KalshiAPIError as exc:
            log.warning("reconcile %s failed: %s", o["client_order_id"], exc)
            return None
        if self.clock() - o["ts"] > 120:
            self.store.update_order(o["id"], status="rejected", note="not found on exchange after timeout")
        return None

    def poll(self, ttl_seconds: float) -> None:
        """Sync resting orders, cancel stale ones, reconcile timed-out creates."""
        now = self.clock()
        for o in self.store.open_orders():
            ex_id = o["exchange_order_id"]
            if not ex_id and o["status"] == "unknown":
                ex_id = self._reconcile_unknown(o)
            if not ex_id:
                continue
            self._sync_fills(o["id"], ex_id, o["ticker"])
            try:
                order = self.client.get_order(ex_id)
            except KalshiAPIError as exc:
                log.warning("get_order(%s) failed: %s", ex_id, exc)
                continue
            status = order.get("status")
            if status in ("canceled", "executed"):
                self.store.update_order(o["id"], status=status)
                continue
            if now - o["ts"] > ttl_seconds:
                try:
                    self.client.cancel_order(ex_id, ticker=o["ticker"])
                    self.store.update_order(o["id"], status="canceled", note="stale: ttl exceeded")
                    log.info("canceled stale order %s (%s)", ex_id, o["ticker"])
                except KalshiAPIError as exc:
                    if exc.status == 404:
                        self.store.update_order(o["id"], status="canceled", note="cancel 404")
                    else:
                        log.warning("cancel %s failed: %s", ex_id, exc)
                self._sync_fills(o["id"], ex_id, o["ticker"])

    def cancel_all(self) -> None:
        for o in self.store.open_orders():
            if o["exchange_order_id"]:
                try:
                    self.client.cancel_order(o["exchange_order_id"], ticker=o["ticker"])
                    self.store.update_order(o["id"], status="canceled", note="shutdown")
                except KalshiAPIError as exc:
                    log.warning("cancel on shutdown failed for %s: %s", o["exchange_order_id"], exc)
