"""Main trading loop. Designed to run forever under systemd.

Every step is wrapped so that a failure (network, API error, bad data) is
logged and the loop continues. The only intentional exits are:
  * SIGTERM / SIGINT          -> cancel resting orders, exit 0
  * kill switch tripped       -> exit code 3 (systemd RestartPreventExitStatus=3)
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Dict, List, Optional, Tuple

from .api_client import KalshiAPIError, KalshiClient, parse_orderbook
from .config import Config
from .fees import FeeModel
from .price_feed import PriceFeed, SpotQuote
from .risk import RiskManager
from .storage import Storage
from .strategy import Book, MarketView, Signal, Strategy

log = logging.getLogger(__name__)

EXIT_KILL_SWITCH = 3


class Bot:
    def __init__(self, cfg: Config, store: Storage, market_client: KalshiClient, feed: PriceFeed,
                 executor, trading_client: Optional[KalshiClient] = None, clock=time.time,
                 sleep=time.sleep):
        self.cfg = cfg
        self.store = store
        self.mc = market_client
        self.tc = trading_client
        self.feed = feed
        self.executor = executor
        self.clock = clock
        self.sleep = sleep
        self.risk = RiskManager(cfg, store, clock=clock)
        self.strategy = Strategy(cfg)
        self.fee_models: Dict[str, FeeModel] = {}
        self.enabled_series: List[str] = []
        self.markets: List[MarketView] = []
        self.spot: Optional[SpotQuote] = None
        self.sigma: Optional[float] = None
        self.exchange_open = True
        self._t: Dict[str, float] = {}
        self._sig_log: Dict[str, Tuple[float, str]] = {}
        self._stop = False
        self._consecutive_errors = 0

    # ------------------------------------------------------------ helpers
    def _due(self, key: str, every: float) -> bool:
        now = self.clock()
        if now - self._t.get(key, 0.0) >= every:
            self._t[key] = now
            return True
        return False

    def stop(self, *_args):
        log.info("stop requested")
        self._stop = True

    def _guard(self, name: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 -- keep running no matter what
            self._consecutive_errors += 1
            log.exception("step %s failed: %s", name, exc)
            try:
                self.store.log_event("ERROR", name, repr(exc)[:500])
            except Exception:  # noqa: BLE001
                pass
            return None

    # ------------------------------------------------------------ startup
    def load_series(self) -> None:
        self.enabled_series = []
        self.fee_models = {}  # rebuilt from scratch so a disabled series can't keep trading
        need =(self.cfg.require_settlement_source or "").lower()
        for st in self.cfg.series:
            try:
                info = self.mc.get_series(st)
            except KalshiAPIError as exc:
                log.error("cannot load series %s: %s (series disabled)", st, exc)
                continue
            self.store.save_series(info)
            fm = FeeModel(self.cfg.fees.taker_rate, self.cfg.fees.maker_rate, info.get("fee_type"),
                          info.get("fee_multiplier"), self.cfg.fees.apply_series_multiplier)
            sources = info.get("settlement_sources") or []
            src_names = " ".join(str(s.get("name", "")) + " " + str(s.get("url", "")) for s in sources).lower()
            log.info("series %s: fee_type=%s fee_multiplier=%s settlement_sources=%s terms=%s -> %r",
                     st, info.get("fee_type"), info.get("fee_multiplier"), sources,
                     info.get("contract_terms_url"), fm)
            if need and need not in src_names:
                log.error("series %s settlement source %s does not mention %r -- DISABLED (our model "
                          "prices against BRTI)", st, sources, self.cfg.require_settlement_source)
                continue
            if not fm.supported:
                log.error("series %s has unsupported fee_type %r -- DISABLED", st, info.get("fee_type"))
                continue
            self.fee_models[st] = fm
            self.enabled_series.append(st)
        if not self.enabled_series:
            log.error("no tradable series enabled; bot will only record data")

    # ------------------------------------------------------------ steps
    def refresh_spot(self) -> None:
        q = self.feed.fetch_spot()
        if q is None:
            log.warning("no spot price from any source")
            return
        self.spot = q
        if self.cfg.loop.record_market_data:
            self.store.record_spot(q.ts, q.price, self.sigma, q.divergence_pct, q.sources)

    def refresh_vol(self) -> None:
        s = self.cfg.strategy
        sigma, n, src = self.feed.fetch_vol(s.vol_window_minutes, s.vol_method, s.ewma_lambda, s.min_vol_samples)
        if sigma is None:
            log.warning("vol unavailable (%d returns from %s)", n, src)
            self.sigma = None
            return
        self.sigma = sigma
        log.debug("vol %.1f%% annualised from %d 1-min returns (%s)", sigma * 100, n, src)

    def refresh_exchange_status(self) -> None:
        st = self.mc.get_exchange_status()
        active = st.get("trading_active", True) and st.get("exchange_active", True)
        if active != self.exchange_open:
            log.warning("exchange trading_active=%s", active)
        self.exchange_open = bool(active)

    def refresh_markets(self) -> None:
        now = self.clock()
        horizon = int(now + self.cfg.strategy.max_minutes_to_expiry * 60 + 120)
        views: List[MarketView] = []
        for st in self.cfg.series:
            # Per the API spec, close-time filters cannot be combined with
            # status=open, so filter the horizon client-side.
            raw = self.mc.get_markets(series_ticker=st, status="open", max_pages=5)
            for m in raw:
                mv = MarketView.from_api(m, st)
                if mv and now < mv.close_ts <= horizon:
                    views.append(mv)
        self.markets = views
        if self.cfg.loop.record_market_data and views:
            self.store.record_markets(now, views)

    def usable_spot(self) -> Optional[float]:
        s = self.cfg.strategy
        if not self.spot:
            return None
        if self.clock() - self.spot.ts > s.max_spot_age_seconds:
            return None
        if self.spot.divergence_pct > s.max_feed_divergence_pct:
            return None
        return self.spot.price

    def _log_signal(self, sig: Signal, risk_reason: Optional[str] = None, force: bool = False) -> Optional[int]:
        key = sig.ticker
        reason = f"{sig.decision}:{sig.reason}:{risk_reason}"
        last = self._sig_log.get(key)
        now = self.clock()
        if not force and last and last[1] == reason and now - last[0] < self.cfg.loop.signal_log_seconds:
            return None
        self._sig_log[key] = (now, reason)
        return self.store.insert_signal(sig.as_dict(), risk_reason)

    def evaluate_and_trade(self) -> None:
        now = self.clock()
        spot = self.usable_spot()
        ok_global, why_global = self.risk.can_trade_now(now)
        for mv in self.markets:
            if self._stop:
                return
            fm = self.fee_models.get(mv.series)
            if fm is None:
                continue
            if self.cfg.fees.ignore_fee_waivers is False and mv.fee_waiver_until and mv.fee_waiver_until > now:
                fm = FeeModel(0.0, 0.0, fm.fee_type, None, False)
            quick = self.strategy.evaluate(mv, Book.from_market_view(mv), spot, self.sigma, fm, now)
            if not quick.is_trade:
                # only spend an orderbook call when the listing quote already shows an edge
                self._log_signal(quick)
                continue
            if not ok_global or not self.exchange_open:
                self._log_signal(quick, risk_reason=why_global if not ok_global else "exchange_closed")
                continue
            try:
                ob = parse_orderbook(self.mc.get_orderbook(mv.ticker, self.cfg.api.orderbook_depth))
            except KalshiAPIError as exc:
                log.warning("orderbook %s failed: %s", mv.ticker, exc)
                continue
            book = Book.from_ladders(ob["yes_bids"], ob["no_bids"])
            now = self.clock()
            sig = self.strategy.evaluate(mv, book, spot, self.sigma, fm, now)
            if not sig.is_trade:
                self._log_signal(sig, force=True)
                continue
            cost = sig.count * sig.limit_price + (fm.maker(sig.count, sig.limit_price) if sig.style == "maker"
                                                  else fm.taker(sig.count, sig.limit_price))
            allowed, why = self.risk.check_order(mv.ticker, cost, mv.close_ts - now, book.spread(), now)
            if allowed and self.tc is not None:
                allowed, why = self._exchange_cash_ok(cost)
            sid = self._log_signal(sig, risk_reason=None if allowed else why, force=True)
            if not allowed:
                log.info("signal %s %s edge %.3f blocked by risk: %s", mv.ticker, sig.side, sig.edge or 0, why)
                continue
            log.info("TRADE %s %s x%d @ %.2f fair=%.3f edge=%.3f (%s, %.0fs to close, spot %.0f, vol %.0f%%)",
                     mv.ticker, sig.side, sig.count, sig.limit_price, sig.side_prob, sig.edge, sig.style,
                     sig.secs_to_close, spot or 0, (self.sigma or 0) * 100)
            self.executor.execute(sig, book, mv, fm, sid)

    def _exchange_cash_ok(self, cost: float) -> Tuple[bool, str]:
        try:
            bal = self.tc.get_balance()
        except KalshiAPIError as exc:
            return False, f"balance_unavailable:{exc.status}"
        cash = (bal.get("balance") or 0) / 100.0
        if cash + 1e-9 < cost:
            return False, "exchange_insufficient_balance"
        return True, "ok"

    def settle(self) -> None:
        now = self.clock()
        for p in self.store.open_positions():
            if p["close_ts"] and now < p["close_ts"]:
                continue
            result = self.store.known_result(p["ticker"])
            if result not in ("yes", "no"):
                try:
                    m = self.mc.get_market(p["ticker"])
                except KalshiAPIError as exc:
                    log.warning("settle lookup %s failed: %s", p["ticker"], exc)
                    continue
                result = (m.get("result") or "").lower()
                if result in ("yes", "no"):
                    self.store.record_result(p["ticker"], result, m.get("expiration_value"))
            if result in ("yes", "no"):
                out = self.store.settle_position(p["id"], result, now)
                log.info("SETTLED %s %s x%d result=%s pnl=%+.2f", out["ticker"], out["side"],
                         out["count"], result, out["pnl"])
            elif p["close_ts"] and now - p["close_ts"] > 8 * 86400:
                log.error("position %s unresolved 8 days after close; marking void at cost", p["ticker"])
                self.store.void_position(p["id"], now, "void_unresolved")

    def record_results(self) -> None:
        """Record outcomes of recently settled markets (for calibration + replay)."""
        now = self.clock()
        for st in self.cfg.series:
            for m in self.mc.get_markets(series_ticker=st, status="settled",
                                         min_settled_ts=int(now - 6 * 3600), max_pages=3):
                res = (m.get("result") or "").lower()
                if res in ("yes", "no") and not self.store.known_result(m["ticker"]):
                    self.store.record_result(m["ticker"], res, m.get("expiration_value"))

    def snapshot(self, note: str = "") -> None:
        b = self.risk.bankroll()
        ex_bal = None
        if self.tc is not None:
            try:
                ex_bal = (self.tc.get_balance().get("balance") or 0) / 100.0
            except KalshiAPIError as exc:
                log.warning("balance fetch failed: %s", exc)
        self.store.snapshot(b.equity, b.cash, b.open_risk, b.realized, b.day_realized, ex_bal, note)
        log.info("BANKROLL equity=%.2f cash=%.2f open_risk=%.2f realized=%+.2f today=%+.2f%s",
                 b.equity, b.cash, b.open_risk, b.realized, b.day_realized,
                 f" exchange_cash={ex_bal:.2f}" if ex_bal is not None else "")

    # ------------------------------------------------------------ loop
    def step(self) -> Optional[int]:
        L = self.cfg.loop
        self._consecutive_errors_before = self._consecutive_errors
        if self._due("settle", L.settle_check_seconds):
            self._guard("settle", self.settle)
        self._guard("risk", self.risk.update_state)
        if self.risk.kill_switch_active():
            self._guard("cancel_all", self.executor.cancel_all)
            self._guard("snapshot", self.snapshot, "kill switch")
            return EXIT_KILL_SWITCH
        self._guard("spot", self.refresh_spot)
        if self._due("vol", L.vol_refresh_seconds):
            self._guard("vol", self.refresh_vol)
        if self._due("exchange", 60):
            self._guard("exchange", self.refresh_exchange_status)
        if self._due("markets", L.market_refresh_seconds):
            self._guard("markets", self.refresh_markets)
        self._guard("trade", self.evaluate_and_trade)
        self._guard("orders", self.executor.poll, self.cfg.risk.order_ttl_seconds)
        if self._due("results", 300):
            self._guard("results", self.record_results)
        if self._due("snapshot", L.snapshot_minutes * 60):
            self._guard("snapshot", self.snapshot)
        if self._consecutive_errors == self._consecutive_errors_before:
            self._consecutive_errors = 0
        return None

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("starting bot: mode=%s series=%s", self.cfg.mode, self.cfg.series)
        self._guard("series", self.load_series)
        self._guard("vol", self.refresh_vol)
        self._t["vol"] = self.clock()
        self._guard("snapshot", self.snapshot, "startup")
        code = 0
        while not self._stop:
            t0 = self.clock()
            rc = self.step()
            if rc is not None:
                code = rc
                break
            if not self.enabled_series and self._due("series_retry", 600):
                self._guard("series", self.load_series)
            # back off when everything is failing (e.g. network down)
            backoff = min(60.0, 2 ** min(self._consecutive_errors, 6)) if self._consecutive_errors > 3 else 0.0
            elapsed = self.clock() - t0
            self.sleep(max(0.5, self.cfg.loop.poll_seconds - elapsed) + backoff)
        if code == 0:
            self._guard("cancel_all", self.executor.cancel_all)
            self._guard("snapshot", self.snapshot, "shutdown")
        log.info("bot stopped (exit code %d)", code)
        return code
