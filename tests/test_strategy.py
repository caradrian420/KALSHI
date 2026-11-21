import pytest

from conftest import NOON
from kalshibot.executor import PaperExecutor
from kalshibot.fees import FeeModel
from kalshibot.strategy import Book, MarketView, Strategy, size_for_budget

FM = FeeModel(fee_type="quadratic", fee_multiplier=1)


def mv(strike=60000.0, close_in=900, bid=0.40, ask=0.42, bsz=100, asz=100):
    return MarketView("KXBTCD-T", "KXBTCD", "KXBTCD-E", "greater", strike, None, NOON + close_in,
                      "active", bid, ask, bsz, asz)


def book(yes_bids, no_bids):
    return Book.from_ladders(yes_bids, no_bids)


def test_book_derives_asks_from_opposite_bids():
    b = book([(0.40, 10)], [(0.58, 7)])
    assert b.yes_ask == pytest.approx(0.42) and b.yes_ask_size == 7
    assert b.no_ask == pytest.approx(0.60) and b.no_ask_size == 10
    assert b.spread() == pytest.approx(0.02)


def test_size_for_budget_includes_fee():
    # $1 budget at 0.42: 2 contracts = 0.84 + fee ceil(0.07*2*.42*.58=0.0341)=0.04 -> 0.88 ok
    assert size_for_budget(0.42, 1.0, FM) == 2
    # at 0.50: 2 contracts = 1.00 + 0.04 > 1 -> 1
    assert size_for_budget(0.50, 1.0, FM) == 1
    assert size_for_budget(0.99, 1.0, FM) == 1  # 0.99 + 0.01 fee = exactly 1.00
    assert size_for_budget(0.99, 0.999, FM) == 0
    assert size_for_budget(0.98, 1.0, FM) == 1


def test_trades_when_edge_exceeds_threshold(cfg):
    st = Strategy(cfg)
    # spot well above strike with little time: fair YES ~0.9+, ask 0.42 -> big edge
    sig = st.evaluate(mv(), book([(0.40, 100)], [(0.58, 100)]), 60300, 0.5, FM, NOON)
    assert sig.is_trade and sig.side == "yes"
    expected_fee = FM.taker(sig.count, 0.42) / sig.count
    assert sig.taker_edge == pytest.approx(sig.side_prob - (0.42 + expected_fee + cfg.strategy.slippage_buffer), abs=1e-6)
    assert sig.count == 2 and sig.limit_price == 0.42


def test_buys_no_when_yes_overpriced(cfg):
    st = Strategy(cfg)
    sig = st.evaluate(mv(), book([(0.40, 100)], [(0.58, 100)]), 59700, 0.5, FM, NOON)
    assert sig.is_trade and sig.side == "no" and sig.limit_price == pytest.approx(0.60)


def test_no_trade_when_fairly_priced(cfg):
    st = Strategy(cfg)
    from kalshibot.pricing import fair_yes_probability
    fair = fair_yes_probability("greater", 60000, None, 60050, 0.5, 900, 60, cfg.strategy.basis_uncertainty_usd)
    bid, ask = round(fair - 0.01, 2), round(fair + 0.01, 2)
    sig = st.evaluate(mv(), book([(bid, 100)], [(round(1 - ask, 2), 100)]), 60050, 0.5, FM, NOON)
    assert not sig.is_trade and sig.reason == "edge_below_min"


def test_edge_threshold_is_respected(cfg):
    cfg.strategy.min_edge = 0.99
    sig = Strategy(cfg).evaluate(mv(), book([(0.40, 100)], [(0.58, 100)]), 60300, 0.5, FM, NOON)
    assert not sig.is_trade and sig.reason == "edge_below_min"


@pytest.mark.parametrize("close_in,reason", [(60, "too_close_to_expiry"), (30, "too_close_to_expiry"),
                                              (99999, "too_far_from_expiry")])
def test_time_filters(cfg, close_in, reason):
    sig = Strategy(cfg).evaluate(mv(close_in=close_in), book([(0.40, 100)], [(0.58, 100)]), 60300, 0.5, FM, NOON)
    assert sig.reason == reason


def test_spread_filter(cfg):
    sig = Strategy(cfg).evaluate(mv(), book([(0.30, 100)], [(0.58, 100)]), 60300, 0.5, FM, NOON)
    assert sig.reason == "spread_too_wide"


def test_one_sided_book_skipped(cfg):
    sig = Strategy(cfg).evaluate(mv(), book([], [(0.58, 100)]), 60300, 0.5, FM, NOON)
    assert not sig.is_trade


def test_price_band(cfg):
    # deep ITM: yes ask 0.97 > max_price 0.95
    sig = Strategy(cfg).evaluate(mv(), book([(0.96, 100)], [(0.03, 100)]), 61000, 0.5, FM, NOON)
    assert not sig.is_trade


def test_depth_limits_count(cfg):
    sig = Strategy(cfg).evaluate(mv(), book([(0.40, 100)], [(0.58, 1)]), 60300, 0.5, FM, NOON)
    assert sig.is_trade and sig.count == 1


def test_missing_inputs_skip(cfg):
    assert Strategy(cfg).evaluate(mv(), book([(0.4, 1)], [(0.58, 1)]), None, 0.5, FM, NOON).reason == "no_price_or_vol"
    assert Strategy(cfg).evaluate(mv(), book([(0.4, 1)], [(0.58, 1)]), 60000, None, FM, NOON).reason == "no_price_or_vol"
    bad = FeeModel(fee_type="flat")
    assert Strategy(cfg).evaluate(mv(), book([(0.4, 1)], [(0.58, 1)]), 60000, 0.5, bad, NOON).reason.startswith("unsupported_fee")


def test_maker_option_is_logged(cfg):
    sig = Strategy(cfg).evaluate(mv(), book([(0.30, 100)], [(0.62, 100)]), 60300, 0.5, FM, NOON)
    # yes bid .30, ask .38 -> maker price .31 computed and logged even in paper mode
    assert sig.maker_price == pytest.approx(0.31) and sig.maker_edge is not None
    assert sig.style == "taker"  # paper always simulates taker


def test_paper_fill_needs_displayed_size(cfg, store):
    st = Strategy(cfg)
    b = book([(0.40, 100)], [(0.58, 1)])
    m = mv()
    sig = st.evaluate(m, b, 60300, 0.5, FM, NOON)
    sig.count = 2  # pretend we wanted 2; only 1 displayed
    ex = PaperExecutor(store, clock=lambda: NOON)
    r = ex.execute(sig, b, m, FM, None)
    assert r["filled"] == 1
    pos = store.open_positions()[0]
    assert pos["count"] == 1 and pos["avg_price"] == pytest.approx(0.42)
    assert pos["fees"] == pytest.approx(FM.taker(1, 0.42))


def test_paper_no_fill_if_book_moved(cfg, store):
    st = Strategy(cfg)
    m = mv()
    sig = st.evaluate(m, book([(0.40, 100)], [(0.58, 100)]), 60300, 0.5, FM, NOON)
    moved = book([(0.40, 100)], [(0.55, 100)])  # ask now 0.45, above our limit
    r = PaperExecutor(store, clock=lambda: NOON).execute(sig, moved, m, FM, None)
    assert r["filled"] == 0 and not store.open_positions()


def test_paper_settlement_pnl(cfg, store):
    m = mv()
    b = book([(0.40, 100)], [(0.58, 100)])
    sig = Strategy(cfg).evaluate(m, b, 60300, 0.5, FM, NOON)
    PaperExecutor(store, clock=lambda: NOON).execute(sig, b, m, FM, None)
    p = store.open_positions()[0]
    out = store.settle_position(p["id"], "yes", NOON + 900)
    assert out["pnl"] == pytest.approx(2 * 1.0 - 2 * 0.42 - FM.taker(2, 0.42))
