"""Configuration: config.yaml (behaviour) + .env (secrets only).

Every default below is the hard-coded safe default from the spec; config.yaml
can override any of them. Secrets are read only from the environment / .env
and are never stored on the Config object's repr or written to logs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

VALID_MODES = ("paper", "demo", "live")

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


@dataclass
class RiskConfig:
    starting_bankroll: float = 20.0
    max_risk_per_trade: float = 1.0
    max_open_positions: int = 3
    daily_loss_limit: float = 4.0
    kill_switch_bankroll: float = 10.0
    # Also refuse a trade whose worst-case loss (plus all open risk) could push
    # equity below the kill-switch level.
    kill_switch_preemptive: bool = True
    min_seconds_to_expiry: int = 60
    max_spread: float = 0.10
    min_price: float = 0.05
    max_price: float = 0.95
    order_ttl_seconds: int = 30
    day_timezone: str = "America/New_York"
    max_trades_per_market: int = 1


@dataclass
class StrategyConfig:
    min_edge: float = 0.04
    slippage_buffer: float = 0.01
    vol_window_minutes: int = 60
    vol_method: str = "stdev"  # stdev | ewma
    ewma_lambda: float = 0.94
    vol_floor_annual: float = 0.20
    vol_cap_annual: float = 3.00
    vol_multiplier: float = 1.0
    settlement_avg_seconds: int = 60
    basis_uncertainty_usd: float = 15.0
    max_minutes_to_expiry: float = 75.0
    order_style: str = "taker"  # taker | maker | auto (paper simulates taker only)
    max_feed_divergence_pct: float = 0.25
    max_spot_age_seconds: float = 15.0
    min_vol_samples: int = 30


@dataclass
class FeeConfig:
    taker_rate: float = 0.07
    maker_rate: float = 0.0175
    apply_series_multiplier: bool = True
    ignore_fee_waivers: bool = True


@dataclass
class FeedConfig:
    sources: List[str] = field(default_factory=lambda: ["coinbase", "kraken", "bitstamp"])
    vol_source: str = "coinbase"
    timeout_seconds: float = 5.0


@dataclass
class ApiConfig:
    prod_base_url: str = PROD_BASE_URL
    demo_base_url: str = DEMO_BASE_URL
    reads_per_second: float = 10.0
    writes_per_second: float = 5.0
    timeout_seconds: float = 10.0
    max_retries: int = 5
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0
    max_clock_drift_seconds: float = 2.0
    orderbook_depth: int = 10


@dataclass
class LoopConfig:
    poll_seconds: float = 5.0
    market_refresh_seconds: float = 15.0
    vol_refresh_seconds: float = 60.0
    snapshot_minutes: float = 15.0
    signal_log_seconds: float = 60.0
    record_market_data: bool = True
    settle_check_seconds: float = 60.0


@dataclass
class LiveConfig:
    min_paper_trades: int = 300
    require_positive_paper_pnl: bool = True
    override_paper_gate: bool = False
    confirmation_phrase: str = "I UNDERSTAND THIS TRADES REAL MONEY"
    armed_file: str = "data/LIVE_ARMED"
    armed_valid_hours: float = 24.0


@dataclass
class Config:
    mode: str = "paper"
    series: List[str] = field(default_factory=lambda: ["KXBTCD", "KXBTC15M"])
    require_settlement_source: str = "CF Benchmarks"
    db_path: str = "data/kalshibot_{mode}.db"
    log_file: str = "data/kalshibot_{mode}.log"
    log_level: str = "INFO"
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    feeds: FeedConfig = field(default_factory=FeedConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    live: LiveConfig = field(default_factory=LiveConfig)

    # ---- derived -------------------------------------------------------
    @property
    def base_url(self) -> str:
        """REST base for orders/portfolio. Market data in paper mode uses prod."""
        return self.api.demo_base_url if self.mode == "demo" else self.api.prod_base_url

    @property
    def market_data_base_url(self) -> str:
        return self.api.demo_base_url if self.mode == "demo" else self.api.prod_base_url

    def resolved_db_path(self, mode: Optional[str] = None) -> str:
        return self.db_path.format(mode=mode or self.mode)

    def resolved_log_file(self) -> str:
        return self.log_file.format(mode=self.mode)

    def fingerprint(self) -> str:
        """Stable hash of all behavioural settings (used by the live arming file)."""
        blob = json.dumps(dataclasses.asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        r, s = self.risk, self.strategy
        checks = [
            (r.starting_bankroll > 0, "risk.starting_bankroll must be > 0"),
            (r.max_risk_per_trade > 0, "risk.max_risk_per_trade must be > 0"),
            (r.max_open_positions >= 1, "risk.max_open_positions must be >= 1"),
            (r.daily_loss_limit > 0, "risk.daily_loss_limit must be > 0"),
            (0 <= r.kill_switch_bankroll < r.starting_bankroll,
             "risk.kill_switch_bankroll must be in [0, starting_bankroll)"),
            (r.min_seconds_to_expiry >= 0, "risk.min_seconds_to_expiry must be >= 0"),
            (0 < r.max_spread <= 1, "risk.max_spread must be in (0, 1]"),
            (0 < r.min_price < r.max_price < 1, "need 0 < risk.min_price < risk.max_price < 1"),
            (s.min_edge >= 0, "strategy.min_edge must be >= 0"),
            (s.slippage_buffer >= 0, "strategy.slippage_buffer must be >= 0"),
            (s.vol_window_minutes >= 5, "strategy.vol_window_minutes must be >= 5"),
            (s.vol_method in ("stdev", "ewma"), "strategy.vol_method must be stdev|ewma"),
            (s.order_style in ("taker", "maker", "auto"), "strategy.order_style must be taker|maker|auto"),
            (s.vol_multiplier >= 1.0, "strategy.vol_multiplier must be >= 1.0 (never shrink vol)"),
            (0 < s.vol_floor_annual < s.vol_cap_annual, "need 0 < vol_floor_annual < vol_cap_annual"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)
        if not self.series:
            raise ValueError("series list is empty")


def _merge(dc_obj, data: Dict[str, Any], path: str = ""):
    if not isinstance(data, dict):
        raise ValueError(f"config section {path or 'root'} must be a mapping")
    known = {f.name: f for f in dataclasses.fields(dc_obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(dc_obj, key)
        if dataclasses.is_dataclass(current):
            _merge(current, value or {}, f"{path}{key}.")
        else:
            setattr(dc_obj, key, value)
    return dc_obj


def load_config(path: Optional[str] = "config.yaml") -> Config:
    cfg = Config()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _merge(cfg, data)
    cfg.mode = str(cfg.mode).lower().strip()
    cfg.validate()
    return cfg


@dataclass
class Secrets:
    key_id: Optional[str]
    private_key_path: Optional[str]

    def __repr__(self) -> str:  # never print secrets
        return f"Secrets(key_id={'set' if self.key_id else 'unset'}, key_path={'set' if self.private_key_path else 'unset'})"

    @property
    def present(self) -> bool:
        return bool(self.key_id and self.private_key_path)


def load_secrets(mode: str, env_file: str = ".env") -> Secrets:
    """Load API credentials for the given mode from .env / environment.

    demo uses KALSHI_DEMO_API_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_PATH; paper and
    live use KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH (paper needs no key).
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except ImportError:  # pragma: no cover
        pass
    prefix = "KALSHI_DEMO_" if mode == "demo" else "KALSHI_"
    return Secrets(
        key_id=os.environ.get(prefix + "API_KEY_ID") or None,
        private_key_path=os.environ.get(prefix + "PRIVATE_KEY_PATH") or None,
    )
