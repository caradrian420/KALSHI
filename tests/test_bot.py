"""Full bot loop in paper mode against FAKE exchange + price feed objects."""

from conftest import NOON
from kalshibot.bot import EXIT_KILL_SWITCH, Bot
from kalshibot.executor import PaperExecutor
from kalshibot.price_feed import SpotQuote
from kalshibot.risk import KILL_KEY


class FakeMarketClient:
    def __init__(self, clock):
        self.clock = clock
        self.result = ""
        self.orderbook_calls = 0
        self.fail_markets = False

    def get_series(self, st):
        return {"ticker": st, "fee_type": "quadratic", "fee_multiplier": 1,
                "settlement_sources": [{"name": "CF Benchmarks", "url": "https://www.cfbenchmarks.com"}]}

    def get_exchange_status(self):
        return {"exchange_active": True, "trading_active": True}

    def _m(self):
        return {"ticker": "KXBTCD-26JAN1513-T60000", "event_ticker": "KXBTCD-26JAN1513",
                "strike_type": "greater", "floor_strike": 60000, "close_time": "2026-01-15T17:15:00Z",
                "status": "active", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4200",
                "yes_bid_size_fp": "100.00", "yes_ask_size_fp": "100.00", "result": self.result}

    def get_markets(self, series_ticker=None, status="open", **kw):
        if self.fail_markets:
            from kalshibot.api_client import KalshiAPIError
            raise KalshiAPIError(503, "down")
        if status == "open" and series_ticker == "KXBTCD" and self.clock() < NOON + 900:
            return [self._m()]
        return []

    def get_market(self, ticker):
        return self._m()

    def get_orderbook(self, ticker, depth=10):
        self.orderbook_calls += 1
        return {"yes_dollars": [["0.4000", "100.00"]], "no_dollars": [["0.5800", "100.00"]]}


class FakeFeed:
    def __init__(self, clock, price=60300.0):
        self.clock, self.price = clock, price

    def fetch_spot(self):
        return SpotQuote(self.price, self.clock(), {"a": self.price, "b": self.price}, 0.0)

    def fetch_vol(self, *a, **k):
        return 0.5, 60, "fake"


def make_bot(cfg, store, clock):
    mc = FakeMarketClient(clock)
    bot = Bot(cfg, store, mc, FakeFeed(clock), PaperExecutor(store, clock=clock), clock=clock,
              sleep=lambda s: None)
    bot.load_series()
    bot.refresh_vol()
    return bot, mc


def test_paper_trade_and_settle(cfg, store, clock):
    bot, mc = make_bot(cfg, store, clock)
    assert bot.step() is None
    pos = store.open_positions()
    assert len(pos) == 1 and pos[0]["side"] == "yes" and pos[0]["avg_price"] == 0.42
    assert mc.orderbook_calls == 1
    # next loop: same market -> no second trade (no averaging down)
    clock.t += 5
    bot.step()
    assert len(store.open_positions()) == 1
    # after close the market resolves YES and the position settles
    clock.t = NOON + 1000
    mc.result = "yes"
    bot._t["settle"] = 0
    bot.step()
    assert not store.open_positions()
    s = store.query("SELECT * FROM settlements")[0]
    assert s["result"] == "yes" and s["pnl"] > 0
    assert store.scalar("SELECT COUNT(*) FROM signals") >= 1


def test_settlement_source_mismatch_disables_series(cfg, store, clock):
    bot, mc = make_bot(cfg, store, clock)
    mc.get_series = lambda st: {"ticker": st, "fee_type": "quadratic", "fee_multiplier": 1,
                                "settlement_sources": [{"name": "Some Other Index"}]}
    bot.load_series()
    assert bot.enabled_series == []
    bot.step()
    assert not store.open_positions()


def test_api_outage_does_not_crash(cfg, store, clock):
    bot, mc = make_bot(cfg, store, clock)
    mc.fail_markets = True
    for _ in range(3):
        assert bot.step() is None
        clock.t += 20
    assert store.scalar("SELECT COUNT(*) FROM events WHERE level='ERROR'") >= 1


def test_stale_or_divergent_spot_blocks_trading(cfg, store, clock):
    bot, mc = make_bot(cfg, store, clock)
    bot.feed.fetch_spot = lambda: SpotQuote(60300, clock(), {"a": 60000, "b": 60600}, 1.0)
    bot.step()
    assert not store.open_positions()


def test_kill_switch_stops_loop(cfg, store, clock):
    bot, _ = make_bot(cfg, store, clock)
    store.set_kv(KILL_KEY, "test")
    assert bot.step() == EXIT_KILL_SWITCH
