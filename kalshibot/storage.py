"""SQLite persistence: signals, orders, fills, positions, settlements,
bankroll snapshots, recorded market data (for replay), and bot state.

Money is stored as REAL dollars rounded to 4 dp. One database file per mode
(paper / demo / live) so paper results can never mix with real ones.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, ticker TEXT NOT NULL, series TEXT, strike_type TEXT,
    floor_strike REAL, cap_strike REAL, close_ts REAL, secs_to_close REAL,
    spot REAL, sigma_ann REAL, fair_yes REAL, side TEXT, side_prob REAL,
    best_ask REAL, best_bid REAL, ask_size REAL, spread REAL, count INTEGER,
    taker_fee_pc REAL, taker_edge REAL, maker_price REAL, maker_fee_pc REAL, maker_edge REAL,
    style TEXT, limit_price REAL, edge REAL, decision TEXT, reason TEXT,
    risk_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS ix_signals_ticker ON signals(ticker);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, client_order_id TEXT UNIQUE, exchange_order_id TEXT,
    ticker TEXT NOT NULL, side TEXT NOT NULL, style TEXT, price REAL, count INTEGER,
    status TEXT, filled_count INTEGER DEFAULT 0, fees REAL DEFAULT 0,
    signal_id INTEGER, updated_ts REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, order_id INTEGER, ticker TEXT, side TEXT, price REAL,
    count INTEGER, fee REAL, is_taker INTEGER, exchange_fill_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, series TEXT, side TEXT NOT NULL,
    count INTEGER NOT NULL, avg_price REAL NOT NULL, cost REAL NOT NULL, fees REAL NOT NULL,
    opened_ts REAL NOT NULL, close_ts REAL, predicted_prob REAL, predicted_edge REAL,
    signal_id INTEGER, status TEXT NOT NULL DEFAULT 'open',
    result TEXT, payout REAL, pnl REAL, settled_ts REAL
);
CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, position_id INTEGER, ticker TEXT, side TEXT, count INTEGER,
    result TEXT, payout REAL, cost REAL, fees REAL, pnl REAL
);

CREATE TABLE IF NOT EXISTS bankroll_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, equity REAL, cash REAL, open_risk REAL, realized_pnl REAL,
    day_pnl REAL, exchange_balance REAL, note TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    ts REAL NOT NULL, ticker TEXT NOT NULL, series TEXT, event_ticker TEXT,
    strike_type TEXT, floor_strike REAL, cap_strike REAL, close_ts REAL, status TEXT,
    yes_bid REAL, yes_ask REAL, yes_bid_size REAL, yes_ask_size REAL
);
CREATE INDEX IF NOT EXISTS ix_msnap_ts ON market_snapshots(ts);

CREATE TABLE IF NOT EXISTS spot_ticks (
    ts REAL NOT NULL, spot REAL, sigma_ann REAL, divergence_pct REAL, sources TEXT
);
CREATE INDEX IF NOT EXISTS ix_spot_ts ON spot_ticks(ts);

CREATE TABLE IF NOT EXISTS market_results (
    ticker TEXT PRIMARY KEY, result TEXT, expiration_value TEXT, recorded_ts REAL
);

CREATE TABLE IF NOT EXISTS series_info (
    ticker TEXT PRIMARY KEY, fee_type TEXT, fee_multiplier REAL,
    settlement_sources TEXT, contract_terms_url TEXT, fetched_ts REAL
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_ts REAL);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, kind TEXT, message TEXT
);
"""

SIGNAL_COLS = [
    "ts", "ticker", "series", "strike_type", "floor_strike", "cap_strike", "close_ts",
    "secs_to_close", "spot", "sigma_ann", "fair_yes", "side", "side_prob", "best_ask",
    "best_bid", "ask_size", "spread", "count", "taker_fee_pc", "taker_edge", "maker_price",
    "maker_fee_pc", "maker_edge", "style", "limit_price", "edge", "decision", "reason",
]


def r4(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 4)


class Storage:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            import os

            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- helpers
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def scalar(self, sql: str, params: Iterable = (), default=None):
        rows = self.query(sql, params)
        if not rows or rows[0][0] is None:
            return default
        return rows[0][0]

    def close(self):
        with self.lock:
            self.conn.close()

    # ---------------------------------------------------------------- kv
    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        v = self.scalar("SELECT value FROM kv WHERE key=?", (key,))
        return default if v is None else v

    def set_kv(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.execute("INSERT INTO kv(key,value,updated_ts) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                         (key, value, time.time()))

    def log_event(self, level: str, kind: str, message: str) -> None:
        self.execute("INSERT INTO events(ts,level,kind,message) VALUES(?,?,?,?)",
                     (time.time(), level, kind, message[:2000]))

    # ----------------------------------------------------------- signals
    def insert_signal(self, sig: Dict[str, Any], risk_reason: Optional[str] = None) -> int:
        cols = SIGNAL_COLS + ["risk_reason"]
        vals = [sig.get(c) for c in SIGNAL_COLS] + [risk_reason]
        cur = self.execute(f"INSERT INTO signals({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        return cur.lastrowid

    # ------------------------------------------------------------ orders
    def insert_order(self, ts: float, client_order_id: str, ticker: str, side: str, style: str,
                     price: float, count: int, status: str, signal_id: Optional[int],
                     exchange_order_id: Optional[str] = None, note: str = "") -> int:
        cur = self.execute(
            "INSERT INTO orders(ts,client_order_id,exchange_order_id,ticker,side,style,price,count,status,"
            "signal_id,updated_ts,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, client_order_id, exchange_order_id, ticker, side, style, r4(price), count, status,
             signal_id, ts, note))
        return cur.lastrowid

    def update_order(self, order_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_ts"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE orders SET {sets} WHERE id=?", list(fields.values()) + [order_id])

    def open_orders(self) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM orders WHERE status IN ('pending','resting','unknown') ORDER BY ts")

    def insert_fill(self, ts: float, order_id: int, ticker: str, side: str, price: float, count: int,
                    fee: float, is_taker: bool, exchange_fill_id: Optional[str] = None) -> Optional[int]:
        try:
            cur = self.execute(
                "INSERT INTO fills(ts,order_id,ticker,side,price,count,fee,is_taker,exchange_fill_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ts, order_id, ticker, side, r4(price), count, r4(fee), int(is_taker), exchange_fill_id))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # duplicate exchange fill id

    # --------------------------------------------------------- positions
    def add_to_position(self, ticker: str, series: str, side: str, count: int, price: float, fee: float,
                        ts: float, close_ts: Optional[float], predicted_prob: Optional[float],
                        predicted_edge: Optional[float], signal_id: Optional[int]) -> int:
        """Create the open position for ticker/side, or add fills of the SAME order to it."""
        row = self.query("SELECT * FROM positions WHERE ticker=? AND side=? AND status='open'", (ticker, side))
        if row:
            p = row[0]
            new_count = p["count"] + count
            new_cost = p["cost"] + count * price
            self.execute("UPDATE positions SET count=?, cost=?, avg_price=?, fees=? WHERE id=?",
                         (new_count, r4(new_cost), r4(new_cost / new_count), r4(p["fees"] + fee), p["id"]))
            return p["id"]
        cur = self.execute(
            "INSERT INTO positions(ticker,series,side,count,avg_price,cost,fees,opened_ts,close_ts,"
            "predicted_prob,predicted_edge,signal_id,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
            (ticker, series, side, count, r4(price), r4(count * price), r4(fee), ts, close_ts,
             predicted_prob, predicted_edge, signal_id))
        return cur.lastrowid

    def open_positions(self) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM positions WHERE status='open' ORDER BY opened_ts")

    def trades_in_market(self, ticker: str) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM positions WHERE ticker=?", (ticker,), 0))

    def settle_position(self, pos_id: int, result: str, ts: float) -> Dict[str, Any]:
        p = self.query("SELECT * FROM positions WHERE id=?", (pos_id,))[0]
        won = (result == p["side"])
        payout = float(p["count"]) if won else 0.0
        pnl = payout - p["cost"] - p["fees"]
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("UPDATE positions SET status='settled', result=?, payout=?, pnl=?, settled_ts=? "
                                  "WHERE id=? AND status='open'", (result, r4(payout), r4(pnl), ts, pos_id))
                self.conn.execute("INSERT INTO settlements(ts,position_id,ticker,side,count,result,payout,cost,fees,pnl) "
                                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                  (ts, pos_id, p["ticker"], p["side"], p["count"], result, r4(payout),
                                   p["cost"], p["fees"], r4(pnl)))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {"ticker": p["ticker"], "side": p["side"], "count": p["count"], "result": result,
                "payout": payout, "pnl": pnl}

    def void_position(self, pos_id: int, ts: float, note: str = "void") -> None:
        """Refund at cost (e.g. a market that was voided / settled 'scalar' at cost)."""
        p = self.query("SELECT * FROM positions WHERE id=?", (pos_id,))[0]
        self.execute("UPDATE positions SET status='settled', result=?, payout=?, pnl=?, settled_ts=? WHERE id=?",
                     (note, p["cost"] + p["fees"], 0.0, ts, pos_id))

    # ---------------------------------------------------------- bankroll
    def realized_pnl(self, since_ts: Optional[float] = None) -> float:
        if since_ts is None:
            return float(self.scalar("SELECT SUM(pnl) FROM positions WHERE status='settled'", (), 0.0))
        return float(self.scalar("SELECT SUM(pnl) FROM positions WHERE status='settled' AND settled_ts>=?",
                                 (since_ts,), 0.0))

    def open_risk(self) -> float:
        """Max loss of open positions + reserved cost of resting orders."""
        pos = float(self.scalar("SELECT SUM(cost+fees) FROM positions WHERE status='open'", (), 0.0))
        resting = float(self.scalar(
            "SELECT SUM((count-filled_count)*price) FROM orders WHERE status IN ('pending','resting','unknown')", (), 0.0))
        return pos + resting

    def snapshot(self, equity: float, cash: float, open_risk: float, realized: float, day_pnl: float,
                 exchange_balance: Optional[float] = None, note: str = "") -> None:
        self.execute("INSERT INTO bankroll_snapshots(ts,equity,cash,open_risk,realized_pnl,day_pnl,exchange_balance,note) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (time.time(), r4(equity), r4(cash), r4(open_risk), r4(realized), r4(day_pnl),
                      r4(exchange_balance), note))

    # ------------------------------------------------- recorded market data
    def record_markets(self, ts: float, views: List[Any]) -> None:
        rows = [(ts, v.ticker, v.series, v.event_ticker, v.strike_type, v.floor_strike, v.cap_strike,
                 v.close_ts, v.status, v.yes_bid, v.yes_ask, v.yes_bid_size, v.yes_ask_size) for v in views]
        with self.lock:
            self.conn.executemany("INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def record_spot(self, ts: float, spot: float, sigma: Optional[float], div: float, sources: Dict) -> None:
        self.execute("INSERT INTO spot_ticks VALUES(?,?,?,?,?)", (ts, spot, sigma, div, json.dumps(sources)))

    def record_result(self, ticker: str, result: str, expiration_value: Optional[str] = None) -> None:
        self.execute("INSERT OR REPLACE INTO market_results VALUES(?,?,?,?)",
                     (ticker, result, expiration_value, time.time()))

    def known_result(self, ticker: str) -> Optional[str]:
        return self.scalar("SELECT result FROM market_results WHERE ticker=?", (ticker,))

    def save_series(self, info: Dict[str, Any]) -> None:
        self.execute("INSERT OR REPLACE INTO series_info VALUES(?,?,?,?,?,?)",
                     (info.get("ticker"), info.get("fee_type"), info.get("fee_multiplier"),
                      json.dumps(info.get("settlement_sources")), info.get("contract_terms_url"), time.time()))


def day_start_ts(now: float, tz_name: str) -> float:
    tz = ZoneInfo(tz_name)
    local = datetime.fromtimestamp(now, tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def day_bounds(day: str, tz_name: str):
    """(start_ts, end_ts) for a YYYY-MM-DD local date."""
    tz = ZoneInfo(tz_name)
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()
