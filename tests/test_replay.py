"""Pipeline tests on SYNTHETIC data with a known ground truth.

These verify wiring (recording format -> strategy -> risk -> paper fills ->
settlement -> report), not real-market profitability.
"""

from kalshibot.replay import generate_synthetic, replay
from kalshibot.reporting import build_report
from kalshibot.storage import Storage


def _run(cfg, true_vol, market_vol, hours=60, seed=7):
    src = Storage(":memory:")
    generate_synthetic(src, hours=hours, seed=seed, true_vol=true_vol, market_vol=market_vol)
    out = Storage(":memory:")
    stats = replay(cfg, src, out)
    return stats, build_report(cfg, out, all_time=True), out


def test_efficient_market_produces_no_trades(cfg):
    stats, rep, _ = _run(cfg, 0.5, 0.5)
    assert stats["evaluations"] > 1000
    assert rep["trades_opened"] == 0  # 4c spread + fees + 4c min edge: nothing to do


def test_mispriced_market_is_exploited_and_limits_hold(cfg):
    stats, rep, out = _run(cfg, 0.6, 0.25, hours=120)
    assert rep["trades_settled"] > 50
    assert rep["net_pnl"] > 0
    # every trade respected the $1 max risk
    worst = out.scalar("SELECT MAX(cost + fees) FROM positions")
    assert worst <= cfg.risk.max_risk_per_trade + 1e-9
    # never more than 3 positions open at once
    rows = out.query("SELECT opened_ts, settled_ts FROM positions")
    for r in rows:
        concurrent = sum(1 for q in rows if q["opened_ts"] <= r["opened_ts"] < q["settled_ts"])
        assert concurrent <= cfg.risk.max_open_positions
    # no trade within 60s of close
    assert out.scalar("SELECT MIN(close_ts - opened_ts) FROM positions") > cfg.risk.min_seconds_to_expiry


def test_daily_loss_limit_holds_in_replay(cfg):
    # Market vol HIGHER than truth: the market is right-ish but model uses a vol
    # floor... force losses by inverting: model believes 300% vol, truth 20%.
    cfg.strategy.vol_floor_annual = 2.9
    cfg.strategy.vol_cap_annual = 3.0
    stats, rep, out = _run(cfg, 0.2, 0.2, hours=72)
    from collections import defaultdict
    from datetime import datetime
    from zoneinfo import ZoneInfo
    by_day = defaultdict(float)
    for r in out.query("SELECT settled_ts, pnl FROM positions WHERE status='settled'"):
        d = datetime.fromtimestamp(r["settled_ts"], ZoneInfo(cfg.risk.day_timezone)).date()
        by_day[d] += r["pnl"]
    assert by_day, "expected some (bad) trades in this scenario"
    assert min(by_day.values()) >= -cfg.risk.daily_loss_limit - 1e-9
