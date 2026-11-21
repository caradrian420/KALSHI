"""Backtest / replay of the strategy on RECORDED data.

The live/paper bot records, into its SQLite DB:
  * market_snapshots: top-of-book (bid/ask + sizes) for every open market,
    every market refresh;
  * spot_ticks: composite BTC spot + current vol estimate;
  * market_results: the official yes/no result of each market.

replay() walks those snapshots in time order and runs the exact same
Strategy -> RiskManager -> PaperExecutor code path with a simulated clock,
writing into a separate output DB, then prints the standard report.

Limitations (be honest about them):
  * only top-of-book is recorded, so fills are capped at the displayed top
    size -- same rule as paper mode;
  * snapshots are ~15s apart, so the bot's real reaction latency is not
    modelled; quotes may have moved in between;
  * vol used is the vol the live bot had computed at that time (no lookahead).

generate_synthetic() creates FAKE data with a known ground truth, used only to
test that the pipeline is wired correctly. Synthetic results say nothing
about real-market profitability.
"""

from __future__ import annotations

import bisect
import copy
import math
import random
from typing import Any, Dict, List, Optional

from .config import Config
from .executor import PaperExecutor
from .fees import FeeModel
from .price_feed import per_second_from_annual
from .pricing import fair_yes_probability
from .risk import RiskManager
from .storage import Storage
from .strategy import Book, MarketView, Strategy


class SimClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def replay(cfg: Config, src: Storage, out: Storage, fee_models: Optional[Dict[str, FeeModel]] = None,
           start_ts: Optional[float] = None, end_ts: Optional[float] = None, verbose: bool = False) -> Dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    cfg.mode = "paper"
    clock = SimClock()
    strategy = Strategy(cfg)
    risk = RiskManager(cfg, out, clock=clock)
    ex = PaperExecutor(out, clock=clock)
    if fee_models is None:
        fee_models = {}
        for row in src.query("SELECT * FROM series_info"):
            fee_models[row["ticker"]] = FeeModel(cfg.fees.taker_rate, cfg.fees.maker_rate, row["fee_type"],
                                                 row["fee_multiplier"], cfg.fees.apply_series_multiplier)
    default_fm = FeeModel(cfg.fees.taker_rate, cfg.fees.maker_rate)

    spots = src.query("SELECT ts, spot, sigma_ann, divergence_pct FROM spot_ticks ORDER BY ts")
    spot_ts = [r["ts"] for r in spots]
    results = {r["ticker"]: r["result"] for r in src.query("SELECT ticker, result FROM market_results")}
    q = "SELECT DISTINCT ts FROM market_snapshots WHERE 1=1"
    params: List[Any] = []
    if start_ts:
        q += " AND ts>=?"
        params.append(start_ts)
    if end_ts:
        q += " AND ts<?"
        params.append(end_ts)
    batches = [r["ts"] for r in src.query(q + " ORDER BY ts", params)]
    n_eval = n_trade = 0
    s_cfg = cfg.strategy
    seen: set = set()
    last_logged: Dict[str, float] = {}

    def settle_due(now: float):
        for p in out.open_positions():
            if p["close_ts"] <= now:
                res = results.get(p["ticker"])
                if res in ("yes", "no"):
                    out.settle_position(p["id"], res, p["close_ts"])
                    out.record_result(p["ticker"], res)

    for ts in batches:
        clock.t = ts
        settle_due(ts)
        risk.update_state(ts)
        if risk.kill_switch_active():
            break
        i = bisect.bisect_right(spot_ts, ts) - 1
        spot = sigma = None
        if i >= 0 and ts - spots[i]["ts"] <= s_cfg.max_spot_age_seconds and \
                (spots[i]["divergence_pct"] or 0) <= s_cfg.max_feed_divergence_pct:
            spot, sigma = spots[i]["spot"], spots[i]["sigma_ann"]
        for row in src.query("SELECT * FROM market_snapshots WHERE ts=?", (ts,)):
            mv = MarketView(ticker=row["ticker"], series=row["series"], event_ticker=row["event_ticker"] or "",
                            strike_type=row["strike_type"], floor_strike=row["floor_strike"],
                            cap_strike=row["cap_strike"], close_ts=row["close_ts"], status=row["status"],
                            yes_bid=row["yes_bid"], yes_ask=row["yes_ask"], yes_bid_size=row["yes_bid_size"],
                            yes_ask_size=row["yes_ask_size"])
            fm = fee_models.get(mv.series, default_fm)
            book = Book.from_market_view(mv)
            sig = strategy.evaluate(mv, book, spot, sigma, fm, ts)
            n_eval += 1
            if mv.ticker not in seen and results.get(mv.ticker) in ("yes", "no"):
                out.record_result(mv.ticker, results[mv.ticker])
                seen.add(mv.ticker)
            if not sig.is_trade:
                # sparse logging of skipped signals (for model calibration in the report)
                if sig.fair_yes is not None and ts - last_logged.get(mv.ticker, -1e18) >= 300:
                    out.insert_signal(sig.as_dict())
                    last_logged[mv.ticker] = ts
                continue
            cost = sig.count * sig.limit_price + fm.taker(sig.count, sig.limit_price)
            ok, why = risk.check_order(mv.ticker, cost, mv.close_ts - ts, book.spread(), ts)
            sid = out.insert_signal(sig.as_dict(), None if ok else why)
            if ok:
                r = ex.execute(sig, book, mv, fm, sid)
                n_trade += 1 if r["filled"] else 0
                if verbose:
                    print(f"{ts:.0f} TRADE {mv.ticker} {sig.side} x{r['filled']} @ {sig.limit_price:.2f} "
                          f"p={sig.side_prob:.3f} edge={sig.edge:.3f}")
    if batches:
        settle_due(float("inf"))
    return {"batches": len(batches), "evaluations": n_eval, "fills": n_trade}


# ---------------------------------------------------------------- synthetic
def generate_synthetic(store: Storage, hours: int = 48, seed: int = 7, true_vol: float = 0.50,
                       market_vol: float = 0.50, spread: float = 0.04, step_seconds: int = 15,
                       start_ts: float = 1_700_000_000.0, s0: float = 60000.0,
                       strikes_each_side: int = 4, strike_step: float = 250.0, size: float = 50.0) -> None:
    """Fake hourly above/below markets on a GBM path.

    Market makers quote fair value computed with `market_vol`; the true path
    uses `true_vol`. If market_vol == true_vol the market is efficient and a
    correct strategy should rarely trade and should NOT show a reliable profit.
    """
    rng = random.Random(seed)
    sig_s = per_second_from_annual(true_vol)
    n = hours * 3600 + 3600
    # 1-second path
    path = [s0]
    for _ in range(n):
        path.append(path[-1] * math.exp(-0.5 * sig_s * sig_s + sig_s * rng.gauss(0, 1)))
    store.execute("INSERT OR REPLACE INTO series_info VALUES('SYN','quadratic',1.0,'[]','',0)")
    t_first_close = start_ts + 3600
    closes = [t_first_close + 3600 * h for h in range(hours)]
    markets = []  # (ticker, strike, close_ts, open_ts)
    for c in closes:
        open_ts = c - 3600
        ref = path[int(open_ts - start_ts)]
        base = round(ref / strike_step) * strike_step
        for k in range(-strikes_each_side, strikes_each_side + 1):
            strike = base + k * strike_step
            markets.append((f"SYN-{int(c)}-{int(strike)}", strike, c, open_ts))
    for tk, strike, c, _ in markets:
        i_end = int(c - start_ts)
        avg = sum(path[i_end - 60:i_end]) / 60.0
        store.record_result(tk, "yes" if avg > strike else "no")
    t = start_ts
    end = start_ts + hours * 3600
    while t < end:
        spot = path[int(t - start_ts)]
        store.record_spot(t, spot, true_vol, 0.0, {"syn": spot})
        views = []
        for tk, strike, c, o in markets:
            if not (o <= t < c):
                continue
            fair = fair_yes_probability("greater", strike, None, spot, market_vol, c - t, 60, 0.0)
            mid = min(0.99, max(0.01, fair + rng.gauss(0, 0.005)))
            bid = max(0.01, round(mid - spread / 2, 2))
            ask = min(0.99, round(mid + spread / 2, 2))
            if ask <= bid:
                ask = round(bid + 0.01, 2)
            views.append(MarketView(tk, "SYN", tk.rsplit("-", 1)[0], "greater", strike, None, c, "active",
                                    bid, ask, size, size))
        store.record_markets(t, views)
        t += step_seconds
