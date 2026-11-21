import json

import pytest
import requests

from kalshibot.api_client import KalshiAPIError, KalshiClient, TokenBucket, parse_orderbook


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(script, signer=None):
    sleeps = []
    c = KalshiClient("https://example.test/trade-api/v2", signer=signer, session=FakeSession(script),
                     reads_per_second=1000, writes_per_second=1000, max_retries=3, sleep=sleeps.append)
    return c, sleeps


def test_retries_on_429_then_succeeds():
    c, sleeps = client([FakeResp(429, headers={"Retry-After": "2"}), FakeResp(200, {"markets": [], "cursor": ""})])
    assert c.get_markets("KXBTCD") == []
    assert sleeps[0] == 2.0
    assert len(c.session.calls) == 2


def test_retries_on_5xx_and_connection_errors():
    c, sleeps = client([FakeResp(503), requests.exceptions.ConnectionError("boom"),
                        FakeResp(200, {"market": {"ticker": "X"}})])
    assert c.get_market("X")["ticker"] == "X"
    assert len(sleeps) == 2


def test_gives_up_after_max_retries():
    c, _ = client([FakeResp(500)] * 4)
    with pytest.raises(KalshiAPIError):
        c.get_exchange_status()


def test_4xx_not_retried():
    c, _ = client([FakeResp(400, {"error": "bad"})])
    with pytest.raises(KalshiAPIError) as ei:
        c.get_exchange_status()
    assert ei.value.status == 400 and len(c.session.calls) == 1


def test_write_timeout_is_not_blindly_retried():
    c, _ = client([requests.exceptions.Timeout("slow")])
    with pytest.raises(KalshiAPIError):
        c.request("POST", "/portfolio/events/orders", json_body={}, auth=False, idempotent=False)
    assert len(c.session.calls) == 1


def test_auth_required_without_signer():
    c, _ = client([])
    with pytest.raises(KalshiAPIError):
        c.get_balance()


def test_pagination():
    c, _ = client([FakeResp(200, {"markets": [{"ticker": "A"}], "cursor": "abc"}),
                   FakeResp(200, {"markets": [{"ticker": "B"}], "cursor": ""})])
    assert [m["ticker"] for m in c.get_markets("S")] == ["A", "B"]
    assert c.session.calls[1]["params"]["cursor"] == "abc"


class DummySigner:
    def headers(self, method, url, timestamp_ms=None):
        self.last = (method, url)
        return {"KALSHI-ACCESS-KEY": "k", "KALSHI-ACCESS-TIMESTAMP": "1", "KALSHI-ACCESS-SIGNATURE": "s"}


def test_create_order_body_yes_and_no():
    s = DummySigner()
    c, _ = client([FakeResp(201, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00", "ts_ms": 1}),
                   FakeResp(201, {"order_id": "o2", "fill_count": "0.00", "remaining_count": "2.00", "ts_ms": 1})],
                  signer=s)
    c.create_order("KXBTCD-X", "yes", 0.42, 2, client_order_id="cid")
    body = c.session.calls[0]["json"]
    assert c.session.calls[0]["url"].endswith("/portfolio/events/orders")
    assert body["side"] == "bid" and body["price"] == "0.4200" and body["count"] == "2"
    assert body["time_in_force"] == "immediate_or_cancel" and body["client_order_id"] == "cid"
    assert "KALSHI-ACCESS-SIGNATURE" in c.session.calls[0]["headers"]
    # buying NO at 0.60 == selling YES at 0.40 on the V2 single book
    c.create_order("KXBTCD-X", "no", 0.60, 2, time_in_force="good_till_canceled", post_only=True,
                   expiration_ts=123)
    body = c.session.calls[1]["json"]
    assert body["side"] == "ask" and body["price"] == "0.4000"
    assert body["post_only"] is True and body["expiration_time"] == 123


def test_create_order_validation():
    c, _ = client([], signer=DummySigner())
    with pytest.raises(ValueError):
        c.create_order("X", "maybe", 0.5, 1)
    with pytest.raises(ValueError):
        c.create_order("X", "yes", 1.0, 1)
    with pytest.raises(ValueError):
        c.create_order("X", "yes", 0.5, 0)


def test_parse_orderbook_fixed_point_and_legacy():
    ob = parse_orderbook({"yes_dollars": [["0.3800", "5.00"], ["0.4000", "10.00"]],
                          "no_dollars": [["0.5500", "3.00"]]})
    assert ob["yes_bids"][0] == (0.40, 10.0)  # best first regardless of API ordering
    assert ob["no_bids"] == [(0.55, 3.0)]
    legacy = parse_orderbook({"yes": [[40, 10]], "no": None})
    assert legacy["yes_bids"] == [(0.40, 10.0)] and legacy["no_bids"] == []


def test_token_bucket_waits():
    t = [0.0]
    slept = []

    def sleep(d):
        slept.append(d)
        t[0] += d

    b = TokenBucket(2.0, capacity=1, clock=lambda: t[0], sleep=sleep)
    b.acquire()
    b.acquire()
    assert sum(slept) == pytest.approx(0.5)
