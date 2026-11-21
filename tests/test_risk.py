import pytest

from conftest import NOON, open_pos
from kalshibot.risk import RiskManager


@pytest.fixture
def rm(cfg, store, clock):
    return RiskManager(cfg, store, clock=clock)


def ok(rm, ticker="T1", cost=0.95, secs=600, spread=0.02):
    return rm.check_order(ticker, cost, secs, spread)


def test_clean_state_allows_trade(rm):
    assert ok(rm) == (True, "ok")


def test_max_risk_per_trade(rm):
    assert ok(rm, cost=1.00)[0] is True
    assert ok(rm, cost=1.01) == (False, "max_risk_per_trade")


def test_max_open_positions(rm, store):
    for t in ("A", "B", "C"):
        open_pos(store, t)
    assert ok(rm, "D") == (False, "max_open_positions")


def test_resting_orders_count_towards_open_positions(rm, store):
    open_pos(store, "A")
    open_pos(store, "B")
    store.insert_order(NOON, "c1", "C", "yes", "maker", 0.4, 2, "resting", None)
    assert ok(rm, "D") == (False, "max_open_positions")


def test_no_second_trade_in_same_market(rm, store):
    open_pos(store, "T1")
    assert ok(rm, "T1") == (False, "already_in_market")


def test_no_reentry_after_settlement(rm, store, cfg):
    pid = open_pos(store, "T1")
    store.settle_position(pid, "no", NOON)
    assert ok(rm, "T1") == (False, "max_trades_per_market")


def test_min_time_to_expiry(rm):
    assert ok(rm, secs=60) == (False, "too_close_to_expiry")
    assert ok(rm, secs=59) == (False, "too_close_to_expiry")
    assert ok(rm, secs=61)[0] is True


def test_spread_threshold(rm):
    assert ok(rm, spread=0.11) == (False, "spread_too_wide")
    assert ok(rm, spread=None) == (False, "spread_too_wide")
    assert ok(rm, spread=0.10)[0] is True


def _lose(store, ticker, amount, ts=NOON):
    # a 'yes' position of `amount` dollars that settles 'no'
    pid = store.add_to_position(ticker, "S", "yes", 1, amount, 0.0, ts - 10, ts - 5, 0.5, 0.05, None)
    store.settle_position(pid, "no", ts)


def test_daily_loss_limit_halts_until_next_day(rm, store, clock):
    for i in range(4):
        _lose(store, f"L{i}", 1.0)
    rm.update_state()
    assert rm.daily_halted()
    assert ok(rm) == (False, "daily_loss_limit")
    clock.t = NOON + 86400  # next local day
    rm.update_state()
    assert not rm.daily_halted()
    assert ok(rm, "NEW")[0] is True


def test_daily_loss_worst_case_blocks_before_limit(rm, store):
    _lose(store, "L1", 1.0)
    _lose(store, "L2", 1.0)
    open_pos(store, "A", cost_price=0.5, count=2, fee=0.02)   # 1.02 at risk
    # realized -2.00, open risk 1.02, new 0.99 -> worst case 4.01 > 4.00
    assert ok(rm, "B", cost=0.99) == (False, "daily_loss_worst_case")
    assert ok(rm, "B", cost=0.98)[0] is True


def test_yesterdays_loss_does_not_count_today(rm, store, clock):
    for i in range(4):
        _lose(store, f"L{i}", 1.0, ts=NOON - 86400)
    rm.update_state()
    assert not rm.daily_halted()


def test_kill_switch_trips_and_persists(cfg, store, clock):
    rm = RiskManager(cfg, store, clock=clock)
    # lose $10 over several days so the daily limit isn't the binding constraint
    for i in range(10):
        _lose(store, f"K{i}", 1.0, ts=NOON - 86400 * (i + 1))
    rm.update_state()
    assert rm.kill_switch_active()
    assert ok(rm) == (False, "kill_switch")
    # survives a "restart" (new RiskManager on the same DB)
    rm2 = RiskManager(cfg, store, clock=clock)
    assert rm2.kill_switch_active()
    rm2.reset_kill_switch()
    assert not rm2.kill_switch_active()


def test_kill_switch_preemptive(rm, store, clock):
    for i in range(8):  # equity 12.00
        _lose(store, f"K{i}", 1.0, ts=NOON - 86400 * (i + 1))
    open_pos(store, "A", cost_price=0.5, count=2, fee=0.02)   # 1.02 open
    # 12 - 1.02 - 0.99 = 9.99 < 10
    assert ok(rm, "B", cost=0.99) == (False, "kill_switch_worst_case")
    assert ok(rm, "B", cost=0.97)[0] is True


def test_insufficient_cash(cfg, store, clock):
    cfg.risk.starting_bankroll = 2.0
    cfg.risk.kill_switch_bankroll = 0.0
    cfg.risk.daily_loss_limit = 100
    rm = RiskManager(cfg, store, clock=clock)
    open_pos(store, "A", cost_price=0.5, count=2, fee=0.02)
    open_pos(store, "B", cost_price=0.4, count=2, fee=0.02)
    assert ok(rm, "C", cost=0.95) == (False, "insufficient_cash")


def test_bankroll_accounting(rm, store):
    pid = open_pos(store, "A", cost_price=0.4, count=2, fee=0.02)  # cost .80 + .02
    b = rm.bankroll()
    assert b.equity == 20.0 and b.open_risk == pytest.approx(0.82) and b.cash == pytest.approx(19.18)
    store.settle_position(pid, "yes", NOON)
    b = rm.bankroll()
    assert b.realized == pytest.approx(2 - 0.82) and b.open_risk == 0
