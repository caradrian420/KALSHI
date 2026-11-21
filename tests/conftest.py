import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from kalshibot.config import Config  # noqa: E402
from kalshibot.storage import Storage  # noqa: E402

# 2026-01-15 17:00:00 UTC = 12:00 America/New_York (mid-day, away from midnight)
NOON = 1768496400.0


class Clock:
    def __init__(self, t=NOON):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def cfg():
    c = Config()
    c.validate()
    return c


@pytest.fixture
def store():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def clock():
    return Clock()


def open_pos(store, ticker, cost_price=0.5, count=2, fee=0.02, ts=NOON, close_ts=None, side="yes"):
    return store.add_to_position(ticker, "KXBTCD", side, count, cost_price, fee, ts,
                                 close_ts or ts + 1800, 0.6, 0.05, None)
