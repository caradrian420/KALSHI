"""Daily report, calibration and status output (plain text)."""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import Config
from .risk import RiskManager
from .storage import Storage, day_bounds

BUCKETS = [(i / 10, (i + 1) / 10) for i in range(10)]


def max_drawdown(pnls: List[float], start_equity: float) -> Tuple[float, float]:
    """(max drawdown in $, as fraction of peak) over a sequence of P&Ls."""
    eq = peak = start_equity
    mdd = mdd_pct = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = peak - eq
        if dd > mdd:
            mdd = dd
            mdd_pct = dd / peak if peak > 0 else 0.0
    return mdd, mdd_pct


def calibration_table(pairs: List[Tuple[float, int]]) -> List[Dict[str, Any]]:
    """pairs = [(predicted_prob, outcome 0/1)] -> per-bucket stats."""
    rows = []
    for lo, hi in BUCKETS:
        sel = [(p, o) for p, o in pairs if (lo <= p < hi) or (hi == 1.0 and p == 1.0)]
        if not sel:
            continue
        n = len(sel)
        avg_p = sum(p for p, _ in sel) / n
        freq = sum(o for _, o in sel) / n
        se = math.sqrt(max(avg_p * (1 - avg_p), 1e-9) / n)
        rows.append({"bucket": f"{lo:.1f}-{hi:.1f}", "n": n, "avg_pred": avg_p, "actual": freq,
                     "z": (freq - avg_p) / se if se > 0 else 0.0})
    return rows


def brier(pairs: List[Tuple[float, int]]) -> Optional[float]:
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def build_report(cfg: Config, store: Storage, day: Optional[str] = None, all_time: bool = False) -> Dict[str, Any]:
    tz = cfg.risk.day_timezone
    if all_time:
        start, end, label = 0.0, time.time() + 1, "ALL TIME"
    else:
        day = day or datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
        start, end = day_bounds(day, tz)
        label = f"{day} ({tz})"

    opened = store.query("SELECT * FROM positions WHERE opened_ts>=? AND opened_ts<?", (start, end))
    settled = store.query("SELECT * FROM positions WHERE status='settled' AND result IN ('yes','no') "
                          "AND settled_ts>=? AND settled_ts<? ORDER BY settled_ts", (start, end))
    fees = float(store.scalar("SELECT SUM(fee) FROM fills WHERE ts>=? AND ts<?", (start, end), 0.0))
    wins = sum(1 for p in settled if p["result"] == p["side"])
    pnls = [p["pnl"] for p in settled]
    net = sum(pnls)
    pred_edges = [p["predicted_edge"] for p in settled if p["predicted_edge"] is not None]
    realized_edges = [((1.0 if p["result"] == p["side"] else 0.0) - p["avg_price"] - p["fees"] / p["count"])
                      for p in settled if p["count"]]
    equity_before = cfg.risk.starting_bankroll + float(store.scalar(
        "SELECT SUM(pnl) FROM positions WHERE status='settled' AND settled_ts<?", (start,), 0.0))
    mdd, mdd_pct = max_drawdown(pnls, equity_before)

    trade_pairs = [(p["predicted_prob"], 1 if p["result"] == p["side"] else 0)
                   for p in settled if p["predicted_prob"] is not None]

    # Model calibration over ALL evaluated markets with known outcomes (one
    # signal per ticker: the latest logged before close), plus the market mid
    # as a benchmark. If the model's Brier score is not below the market's,
    # the model has no informational edge.
    sig_rows = store.query(
        "SELECT s.ticker, s.fair_yes, s.best_bid, s.best_ask, s.side, r.result FROM signals s "
        "JOIN market_results r ON r.ticker = s.ticker "
        "WHERE s.fair_yes IS NOT NULL AND s.ts>=? AND s.ts<? AND r.result IN ('yes','no') "
        "AND s.id IN (SELECT MAX(id) FROM signals WHERE fair_yes IS NOT NULL GROUP BY ticker)",
        (start, end))
    model_pairs, both_model, both_market = [], [], []
    for s in sig_rows:
        o = 1 if s["result"] == "yes" else 0
        model_pairs.append((s["fair_yes"], o))
        if s["best_bid"] is not None and s["best_ask"] is not None:
            mid = (s["best_bid"] + s["best_ask"]) / 2
            mid_yes = mid if s["side"] == "yes" else 1 - mid
            both_market.append((mid_yes, o))
            both_model.append((s["fair_yes"], o))  # same subset -> fair comparison

    signal_counts = store.query("SELECT decision, COALESCE(risk_reason, reason) AS why, COUNT(*) AS n "
                                "FROM signals WHERE ts>=? AND ts<? GROUP BY decision, why ORDER BY n DESC",
                                (start, end))
    all_settled = store.query("SELECT pnl FROM positions WHERE status='settled' AND result IN ('yes','no')")
    all_pnls = [r["pnl"] for r in all_settled]
    n_all = len(all_pnls)
    mean = sum(all_pnls) / n_all if n_all else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in all_pnls) / (n_all - 1)) if n_all > 1 else 0.0
    t_stat = mean / (sd / math.sqrt(n_all)) if n_all > 1 and sd > 0 else 0.0
    return {
        "label": label, "mode": cfg.mode,
        "trades_opened": len(opened), "trades_settled": len(settled), "wins": wins,
        "win_rate": wins / len(settled) if settled else None,
        "fees": fees, "net_pnl": net,
        "avg_pred_edge": sum(pred_edges) / len(pred_edges) if pred_edges else None,
        "avg_realized_edge": sum(realized_edges) / len(realized_edges) if realized_edges else None,
        "max_drawdown": mdd, "max_drawdown_pct": mdd_pct,
        "trade_calibration": calibration_table(trade_pairs),
        "model_calibration": calibration_table(model_pairs),
        "brier_model": brier(both_model), "brier_market": brier(both_market), "n_brier": len(both_model),
        "signal_counts": [dict(r) for r in signal_counts],
        "lifetime": {"settled": n_all, "net_pnl": sum(all_pnls), "mean_pnl": mean, "t_stat": t_stat},
    }


def _fmt(x, pct=False, money=False):
    if x is None:
        return "n/a"
    if pct:
        return f"{x * 100:.1f}%"
    if money:
        return f"${x:+.2f}"
    return f"{x:.4f}"


def format_report(r: Dict[str, Any], min_trades: int = 300) -> str:
    L = [f"=== Kalshi bot report: {r['label']}  [mode={r['mode']}] ==="]
    L.append(f"Trades opened: {r['trades_opened']}   settled: {r['trades_settled']}   wins: {r['wins']}   "
             f"win rate: {_fmt(r['win_rate'], pct=True)}")
    L.append(f"Fees paid: ${r['fees']:.2f}   Net P&L (after fees): {_fmt(r['net_pnl'], money=True)}")
    L.append(f"Avg predicted edge/contract: {_fmt(r['avg_pred_edge'])}   "
             f"avg realized edge/contract: {_fmt(r['avg_realized_edge'])}")
    L.append(f"Max drawdown: ${r['max_drawdown']:.2f} ({_fmt(r['max_drawdown_pct'], pct=True)})")
    for title, rows in (("Calibration of TRADED side (pred prob vs win freq)", r["trade_calibration"]),
                        ("Model calibration, all evaluated markets (P(yes) vs outcome)", r["model_calibration"])):
        L.append(f"\n{title}:")
        if not rows:
            L.append("  (no settled data yet)")
        for c in rows:
            flag = "  <-- off by >2 s.e." if abs(c["z"]) > 2 else ""
            L.append(f"  {c['bucket']}: n={c['n']:4d} predicted={c['avg_pred']:.3f} actual={c['actual']:.3f}{flag}")
    L.append("  NOTE: strikes in the same event share one BTC path, so outcomes are correlated and the")
    L.append("  '>2 s.e.' flags overstate significance. Judge calibration over many distinct events.")
    L.append(f"\nBrier score (lower=better) over {r['n_brier']} markets with a two-sided quote: "
             f"model={_fmt(r['brier_model'])} market-mid={_fmt(r['brier_market'])}"
             "  (model should beat market-mid if it has real edge)")
    L.append("\nSignal decisions:")
    for s in r["signal_counts"][:15]:
        L.append(f"  {s['decision']:5s} {s['why']}: {s['n']}")
    lt = r["lifetime"]
    L.append(f"\nLifetime: {lt['settled']} settled trades, net {_fmt(lt['net_pnl'], money=True)}, "
             f"mean {_fmt(lt['mean_pnl'], money=True)}/trade, t-stat {lt['t_stat']:.2f}")
    gate = lt["settled"] >= min_trades and lt["net_pnl"] > 0
    L.append(f"Live-readiness gate ({min_trades}+ trades AND net P&L > 0): {'MET' if gate else 'NOT MET'}"
             + ("" if not gate else "  (note: also want t-stat > 2 before trusting it)"))
    return "\n".join(L)


def format_status(cfg: Config, store: Storage) -> str:
    risk = RiskManager(cfg, store)
    b = risk.bankroll()
    L = [f"Mode: {cfg.mode}   DB: {store.path}"]
    kill = risk.kill_switch_active()
    L.append(f"Kill switch: {'TRIPPED -> ' + kill if kill else 'off'}   "
             f"Daily halt: {'YES' if risk.daily_halted() else 'no'}")
    L.append(f"Bankroll: equity ${b.equity:.2f}  cash ${b.cash:.2f}  open risk ${b.open_risk:.2f}  "
             f"(start ${cfg.risk.starting_bankroll:.2f}, kill at ${cfg.risk.kill_switch_bankroll:.2f})")
    L.append(f"Today's realized P&L: ${b.day_realized:+.2f}   (daily loss limit ${cfg.risk.daily_loss_limit:.2f})")
    L.append(f"Lifetime realized P&L: ${b.realized:+.2f}")
    pos = store.open_positions()
    L.append(f"Open positions ({len(pos)}/{cfg.risk.max_open_positions}):")
    now = time.time()
    for p in pos:
        mins = (p["close_ts"] - now) / 60 if p["close_ts"] else float("nan")
        L.append(f"  {p['ticker']:32s} {p['side'].upper():3s} x{p['count']:<3d} @ {p['avg_price']:.2f} "
                 f"fees {p['fees']:.2f}  p={p['predicted_prob'] or 0:.3f}  closes in {mins:.1f} min")
    orders = store.open_orders()
    if orders:
        L.append(f"Resting orders ({len(orders)}):")
        for o in orders:
            L.append(f"  {o['ticker']} {o['side']} x{o['count']} @ {o['price']:.2f} [{o['status']}]")
    last = store.query("SELECT ts FROM signals ORDER BY id DESC LIMIT 1")
    if last:
        L.append(f"Last signal: {int(now - last[0]['ts'])}s ago")
    return "\n".join(L)
