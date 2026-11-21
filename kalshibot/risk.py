"""Hard risk limits. Every check returns (allowed, reason).

Bankroll accounting (identical in every mode, derived from the bot's own DB):
    equity    = starting_bankroll + realized P&L (settled positions, net of fees)
    open_risk = cost + fees of open positions + reserved cost of resting orders
                (= the most we can still lose on them; binary payoffs can't go below 0)
    cash      = equity - open_risk

Limits:
  * max_risk_per_trade: new order's cost + fee <= limit (also enforced in sizing)
  * max_open_positions: open positions + markets with resting orders
  * one position per market, no adding (no averaging down / martingale)
  * daily loss: once today's realized loss >= limit, halt until local midnight.
    A new trade is also refused if today's realized loss + all open risk +
    this trade's cost could exceed the limit -- so the limit holds in the
    worst case, not just after the fact.
  * kill switch: equity <= kill level -> persistent halt (DB flag), survives
    restarts, cleared only by `kalshibot reset-kill-switch`. With
    kill_switch_preemptive, a trade that could push worst-case equity below
    the kill level is also refused.
  * time-to-expiry and spread limits are enforced in strategy AND re-checked here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from .config import Config
from .storage import Storage, day_start_ts

log = logging.getLogger(__name__)

KILL_KEY = "kill_switch"
DAILY_HALT_KEY = "daily_halt_day_start"


@dataclass
class Bankroll:
    equity: float
    cash: float
    open_risk: float
    realized: float
    day_realized: float


class RiskManager:
    def __init__(self, cfg: Config, store: Storage, clock=time.time):
        self.cfg = cfg
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------ state
    def bankroll(self, now: Optional[float] = None) -> Bankroll:
        now = self.clock() if now is None else now
        realized = self.store.realized_pnl()
        day_realized = self.store.realized_pnl(since_ts=day_start_ts(now, self.cfg.risk.day_timezone))
        open_risk = self.store.open_risk()
        equity = self.cfg.risk.starting_bankroll + realized
        return Bankroll(equity=round(equity, 4), cash=round(equity - open_risk, 4),
                        open_risk=round(open_risk, 4), realized=round(realized, 4),
                        day_realized=round(day_realized, 4))

    def kill_switch_active(self) -> Optional[str]:
        return self.store.get_kv(KILL_KEY)

    def trip_kill_switch(self, reason: str) -> None:
        if not self.kill_switch_active():
            msg = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}"
            self.store.set_kv(KILL_KEY, msg)
            self.store.log_event("CRITICAL", "kill_switch", msg)
            log.critical("KILL SWITCH TRIPPED: %s -- manual restart required "
                         "(kalshibot reset-kill-switch)", reason)

    def reset_kill_switch(self) -> None:
        self.store.set_kv(KILL_KEY, None)
        self.store.log_event("WARNING", "kill_switch", "reset manually")

    def daily_halted(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        today = day_start_ts(now, self.cfg.risk.day_timezone)
        val = self.store.get_kv(DAILY_HALT_KEY)
        return val is not None and float(val) == today

    def update_state(self, now: Optional[float] = None) -> Bankroll:
        """Re-evaluate halts after settlements. Call every loop."""
        now = self.clock() if now is None else now
        b = self.bankroll(now)
        r = self.cfg.risk
        if b.equity <= r.kill_switch_bankroll + 1e-9:
            self.trip_kill_switch(f"equity {b.equity:.2f} <= kill level {r.kill_switch_bankroll:.2f}")
        if -b.day_realized >= r.daily_loss_limit - 1e-9 and not self.daily_halted(now):
            self.store.set_kv(DAILY_HALT_KEY, str(day_start_ts(now, r.day_timezone)))
            self.store.log_event("WARNING", "daily_halt", f"day loss {-b.day_realized:.2f} >= {r.daily_loss_limit:.2f}")
            log.warning("Daily loss limit hit (%.2f). No new trades until next local day.", -b.day_realized)
        return b

    # ------------------------------------------------------------ checks
    def can_trade_now(self, now: Optional[float] = None) -> Tuple[bool, str]:
        now = self.clock() if now is None else now
        if self.kill_switch_active():
            return False, "kill_switch"
        if self.daily_halted(now):
            return False, "daily_loss_limit"
        return True, "ok"

    def check_order(self, ticker: str, cost_with_fees: float, secs_to_close: float,
                    spread: Optional[float], now: Optional[float] = None) -> Tuple[bool, str]:
        now = self.clock() if now is None else now
        r = self.cfg.risk
        ok, why = self.can_trade_now(now)
        if not ok:
            return False, why
        if cost_with_fees <= 0:
            return False, "zero_cost"
        if cost_with_fees > r.max_risk_per_trade + 1e-9:
            return False, "max_risk_per_trade"
        if secs_to_close <= r.min_seconds_to_expiry:
            return False, "too_close_to_expiry"
        if spread is None or spread > r.max_spread + 1e-9:
            return False, "spread_too_wide"

        open_pos = self.store.open_positions()
        open_tickers = {p["ticker"] for p in open_pos}
        resting_tickers = {o["ticker"] for o in self.store.open_orders()}
        if ticker in open_tickers or ticker in resting_tickers:
            return False, "already_in_market"
        if self.store.trades_in_market(ticker) >= r.max_trades_per_market:
            return False, "max_trades_per_market"
        if len(open_tickers | resting_tickers) >= r.max_open_positions:
            return False, "max_open_positions"

        b = self.bankroll(now)
        if cost_with_fees > b.cash + 1e-9:
            return False, "insufficient_cash"
        worst_day_loss = -b.day_realized + b.open_risk + cost_with_fees
        if worst_day_loss > r.daily_loss_limit + 1e-9:
            return False, "daily_loss_worst_case"
        if r.kill_switch_preemptive:
            worst_equity = b.equity - b.open_risk - cost_with_fees
            if worst_equity < r.kill_switch_bankroll - 1e-9:
                return False, "kill_switch_worst_case"
        return True, "ok"
