"""KalshiExecutor against a FAKE client (no network). Verifies how we read
fills back, cancel stale orders and reconcile timed-out creates."""

from conftest import NOON
from kalshibot.api_client import KalshiAPIError
from kalshibot.executor import KalshiExecutor
from kalshibot.fees import FeeModel
from kalshibot.strategy import Book, MarketView, Signal

FM = FeeModel(fee_type="quadratic")


class FakeClient:
    def __init__(self):
        self.created, self.canceled = [], []
        self.fills = {}
        self.orders = {}
        self.fail_create = None

    def create_order(self, **kw):
        if self.fail_create:
            raise self.fail_create
        self.created.append(kw)
        oid = f"ex{len(self.created)}"
        self.orders[oid] = {"order_id": oid, "status": "resting", "client_order_id": kw["client_order_id"]}
        return {"order_id": oid, "fill_count": "0.00", "remaining_count": str(kw["count"]), "ts_ms": 1}

    def get_fills(self, order_id=None, **kw):
        return self.fills.get(order_id, [])

    def get_order(self, oid):
        return self.orders[oid]

    def get_orders(self, ticker=None, **kw):
        return list(self.orders.values())

    def cancel_order(self, oid, ticker=None):
        self.canceled.append(oid)
        self.orders[oid]["status"] = "canceled"
        return {}


def sig(side="no", style="taker", price=0.60, count=1):
    return Signal(ts=NOON, ticker="T", series="KXBTCD", strike_type="greater", floor_strike=1, cap_strike=None,
                  close_ts=NOON + 900, secs_to_close=900, spot=1, sigma_ann=0.5, side=side, side_prob=0.7,
                  style=style, limit_price=price, count=count, edge=0.05, decision="trade")


MV = MarketView("T", "KXBTCD", "E", "greater", 1.0, None, NOON + 900, "active", 0.38, 0.40, 10, 10)
BOOK = Book.from_ladders([(0.38, 10)], [(0.60, 10)])


def test_taker_order_records_fills_from_exchange(store, clock):
    c = FakeClient()
    ex = KalshiExecutor(store, c, "demo", ttl_seconds=30, clock=clock)
    c.fills["ex1"] = [{"fill_id": "f1", "order_id": "ex1", "outcome_side": "no", "count_fp": "1.00",
                       "yes_price_dollars": "0.4000", "no_price_dollars": "0.6000", "fee_cost": "0.0200",
                       "is_taker": True}]
    r = ex.execute(sig(), BOOK, MV, FM, None)
    assert c.created[0]["outcome"] == "no" and c.created[0]["time_in_force"] == "immediate_or_cancel"
    assert r["filled"] == 1
    p = store.open_positions()[0]
    assert p["side"] == "no" and p["avg_price"] == 0.60 and p["fees"] == 0.02
    # polling again must not double-count the same fill
    ex._sync_fills(r["order_id"], "ex1", "T")
    assert store.open_positions()[0]["count"] == 1


def test_maker_order_is_post_only_and_stale_cancelled(store, clock):
    c = FakeClient()
    ex = KalshiExecutor(store, c, "demo", ttl_seconds=30, clock=clock)
    ex.execute(sig(style="maker", price=0.39), BOOK, MV, FM, None)
    kw = c.created[0]
    assert kw["post_only"] is True and kw["time_in_force"] == "good_till_canceled"
    assert kw["expiration_ts"] == int(NOON + 30)
    assert len(store.open_orders()) == 1
    clock.t += 10
    ex.poll(30)
    assert c.canceled == []
    clock.t += 30
    ex.poll(30)
    assert c.canceled == ["ex1"] and store.open_orders() == []


def test_timeout_on_create_is_reconciled(store, clock):
    c = FakeClient()
    ex = KalshiExecutor(store, c, "live", ttl_seconds=30, clock=clock)
    c.fail_create = KalshiAPIError(0, "timeout")
    ex.execute(sig(), BOOK, MV, FM, None)
    o = store.open_orders()[0]
    assert o["status"] == "unknown"
    # the order actually reached the exchange:
    c.orders["exZ"] = {"order_id": "exZ", "status": "executed", "client_order_id": o["client_order_id"]}
    ex.poll(30)
    row = store.query("SELECT * FROM orders WHERE id=?", (o["id"],))[0]
    assert row["exchange_order_id"] == "exZ"


def test_rejected_order_leaves_no_position(store, clock):
    c = FakeClient()
    c.fail_create = KalshiAPIError(400, "invalid price")
    ex = KalshiExecutor(store, c, "demo", ttl_seconds=30, clock=clock)
    r = ex.execute(sig(), BOOK, MV, FM, None)
    assert r["status"] == "rejected" and not store.open_positions() and not store.open_orders()
