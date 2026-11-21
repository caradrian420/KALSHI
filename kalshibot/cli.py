"""Command line entry point:  python -m kalshibot <command>

  run                 run the bot (mode from config.yaml)
  status              bankroll, open positions, today's P&L
  report [--day D | --all]
  check               connectivity / data sanity check (no trading)
  replay              backtest on recorded data (or --synthetic)
  demo-smoke          place + cancel one tiny order on Kalshi DEMO
  arm-live            typed confirmation for live mode under systemd
  reset-kill-switch   clear a tripped kill switch (manual restart)
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from typing import Optional

from .api_client import KalshiAPIError, KalshiClient, parse_orderbook
from .auth import KalshiSigner
from .config import Config, load_config, load_secrets
from .storage import Storage

log = logging.getLogger("kalshibot")


def setup_logging(cfg: Config, to_file: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    root.addHandler(h)
    if to_file:
        path = cfg.resolved_log_file()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def make_client(cfg: Config, base_url: str, signer: Optional[KalshiSigner]) -> KalshiClient:
    a = cfg.api
    return KalshiClient(base_url, signer=signer, reads_per_second=a.reads_per_second,
                        writes_per_second=a.writes_per_second, timeout=a.timeout_seconds,
                        max_retries=a.max_retries, backoff_base=a.backoff_base_seconds,
                        backoff_max=a.backoff_max_seconds, max_clock_drift=a.max_clock_drift_seconds)


def make_signer(cfg: Config) -> Optional[KalshiSigner]:
    sec = load_secrets(cfg.mode)
    if not sec.present:
        return None
    return KalshiSigner.from_file(sec.key_id, sec.private_key_path)


# ------------------------------------------------------------ live gating
def paper_gate(cfg: Config) -> tuple:
    path = cfg.resolved_db_path("paper")
    if not os.path.exists(path):
        return False, f"no paper database at {path}"
    st = Storage(path)
    try:
        n = int(st.scalar("SELECT COUNT(*) FROM positions WHERE status='settled' AND result IN ('yes','no')", (), 0))
        pnl = float(st.scalar("SELECT SUM(pnl) FROM positions WHERE status='settled' AND result IN ('yes','no')", (), 0.0))
    finally:
        st.close()
    ok = n >= cfg.live.min_paper_trades and (pnl > 0 or not cfg.live.require_positive_paper_pnl)
    return ok, f"paper record: {n} settled trades, net P&L ${pnl:+.2f} (need >= {cfg.live.min_paper_trades} and > $0)"


def confirm_live(cfg: Config, input_fn=input, isatty: Optional[bool] = None) -> tuple:
    """Return (ok, message). Requires mode=live AND a typed confirmation."""
    if cfg.mode != "live":
        return False, "mode is not live"
    ok, msg = paper_gate(cfg)
    if not ok and not cfg.live.override_paper_gate:
        return False, f"REFUSING LIVE: {msg}. (Set live.override_paper_gate: true to bypass -- not recommended.)"
    if not ok:
        print(f"WARNING: paper gate not met ({msg}) but override_paper_gate is set.")
    phrase = cfg.live.confirmation_phrase
    tty = sys.stdin.isatty() if isatty is None else isatty
    if tty:
        print("\n*** LIVE MODE: this bot will place REAL orders with REAL money. ***")
        print(f"Bankroll cap ${cfg.risk.starting_bankroll:.2f}, max ${cfg.risk.max_risk_per_trade:.2f}/trade, "
              f"daily loss limit ${cfg.risk.daily_loss_limit:.2f}, kill at ${cfg.risk.kill_switch_bankroll:.2f}.")
        typed = input_fn(f'Type exactly "{phrase}" to continue: ')
        if typed.strip() != phrase:
            return False, "confirmation phrase did not match"
        return True, "confirmed interactively"
    # Non-interactive (systemd): require a fresh arming file for THIS config.
    path = cfg.live.armed_file
    if not os.path.exists(path):
        return False, f"non-interactive start and no arming file ({path}); run `python -m kalshibot arm-live`"
    try:
        with open(path) as fh:
            armed = json.load(fh)
    except (OSError, ValueError):
        return False, "arming file unreadable"
    if armed.get("fingerprint") != cfg.fingerprint():
        return False, "config changed since arm-live; re-run arm-live"
    age_h = (time.time() - float(armed.get("armed_at", 0))) / 3600
    if cfg.live.armed_valid_hours > 0 and age_h > cfg.live.armed_valid_hours:
        return False, f"arming expired ({age_h:.1f}h old); re-run arm-live"
    return True, "armed via arm-live file"


# ------------------------------------------------------------ commands
def cmd_run(cfg: Config, args) -> int:
    from .bot import Bot
    from .executor import KalshiExecutor, PaperExecutor
    from .price_feed import PriceFeed

    setup_logging(cfg)
    if cfg.mode == "live":
        ok, msg = confirm_live(cfg)
        if not ok:
            log.error(msg)
            return 2
        log.warning("LIVE MODE ENABLED (%s)", msg)
    store = Storage(cfg.resolved_db_path())
    feed = PriceFeed(cfg.feeds.sources, cfg.feeds.vol_source, cfg.feeds.timeout_seconds)
    trading_client = None
    if cfg.mode == "paper":
        market_client = make_client(cfg, cfg.market_data_base_url, None)
        executor = PaperExecutor(store)
        log.info("PAPER mode: live production market data (read-only), simulated taker fills")
    else:
        signer = make_signer(cfg)
        if signer is None:
            log.error("mode=%s needs API credentials in .env (see .env.example)", cfg.mode)
            return 2
        trading_client = make_client(cfg, cfg.base_url, signer)
        market_client = trading_client
        executor = KalshiExecutor(store, trading_client, cfg.mode, cfg.risk.order_ttl_seconds)
    store.log_event("INFO", "start", f"mode={cfg.mode} fingerprint={cfg.fingerprint()}")
    bot = Bot(cfg, store, market_client, feed, executor, trading_client=trading_client)
    return bot.run()


def cmd_status(cfg: Config, args) -> int:
    from .reporting import format_status

    path = cfg.resolved_db_path()
    if not os.path.exists(path):
        print(f"No database yet at {path} (bot has not run in {cfg.mode} mode).")
        return 0
    print(format_status(cfg, Storage(path)))
    return 0


def cmd_report(cfg: Config, args) -> int:
    from .reporting import build_report, format_report

    path = args.db or cfg.resolved_db_path()
    if not os.path.exists(path):
        print(f"No database at {path}")
        return 1
    r = build_report(cfg, Storage(path), day=args.day, all_time=args.all)
    print(format_report(r, cfg.live.min_paper_trades))
    return 0


def cmd_reset_kill(cfg: Config, args) -> int:
    from .risk import RiskManager

    store = Storage(cfg.resolved_db_path())
    rm = RiskManager(cfg, store)
    k = rm.kill_switch_active()
    if not k:
        print("Kill switch is not active.")
        return 0
    print(f"Kill switch: {k}")
    b = rm.bankroll()
    print(f"Current bot equity: ${b.equity:.2f}. Resetting does NOT add money; if equity is still at/below "
          f"${cfg.risk.kill_switch_bankroll:.2f} it will trip again immediately unless you change config.")
    if input('Type "RESET" to clear: ').strip() != "RESET":
        print("aborted")
        return 1
    rm.reset_kill_switch()
    print("Kill switch cleared.")
    return 0


def cmd_arm_live(cfg: Config, args) -> int:
    if cfg.mode != "live":
        print("config.yaml mode is not 'live'; nothing to arm.")
        return 1
    if not sys.stdin.isatty():
        print("arm-live must be run from an interactive terminal.")
        return 1
    ok, msg = confirm_live(cfg, isatty=True)
    if not ok:
        print(msg)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(cfg.live.armed_file)), exist_ok=True)
    with open(cfg.live.armed_file, "w") as fh:
        json.dump({"fingerprint": cfg.fingerprint(), "armed_at": time.time()}, fh)
    print(f"Armed for {cfg.live.armed_valid_hours or 'unlimited'}h for this exact config. "
          f"Now: sudo systemctl restart kalshibot")
    return 0


def cmd_check(cfg: Config, args) -> int:
    from .fees import FeeModel
    from .price_feed import PriceFeed
    from .strategy import MarketView

    setup_logging(cfg, to_file=False)
    ok = True
    mc = make_client(cfg, cfg.market_data_base_url, None)
    print(f"Market data: {cfg.market_data_base_url}")
    try:
        print("  exchange status:", mc.get_exchange_status())
    except KalshiAPIError as exc:
        ok = False
        print("  exchange status FAILED:", exc)
    for st in cfg.series:
        try:
            info = mc.get_series(st)
            fm = FeeModel(cfg.fees.taker_rate, cfg.fees.maker_rate, info.get("fee_type"), info.get("fee_multiplier"))
            print(f"  series {st}: fee_type={info.get('fee_type')} fee_multiplier={info.get('fee_multiplier')} "
                  f"sources={info.get('settlement_sources')} -> {fm!r}")
            print(f"    contract terms: {info.get('contract_terms_url')}")
            ms = mc.get_markets(series_ticker=st, status="open", max_pages=1, limit=200)
            views = sorted([v for v in (MarketView.from_api(m, st) for m in ms) if v], key=lambda v: v.close_ts)
            print(f"    {len(ms)} open markets (first page)")
            for v in views[:3]:
                print(f"    e.g. {v.ticker} strike_type={v.strike_type} floor={v.floor_strike} cap={v.cap_strike} "
                      f"closes in {(v.close_ts - time.time()) / 60:.1f} min bid={v.yes_bid} ask={v.yes_ask}")
            if views:
                ob = parse_orderbook(mc.get_orderbook(views[0].ticker, 5))
                print(f"    orderbook {views[0].ticker}: yes_bids={ob['yes_bids'][:3]} no_bids={ob['no_bids'][:3]}")
                raw = next(m for m in ms if m.get("ticker") == views[0].ticker)
                print(f"    rules_primary: {str(raw.get('rules_primary'))[:400]}")
        except (KalshiAPIError, StopIteration) as exc:
            ok = False
            print(f"  series {st} FAILED: {exc}")
    feed = PriceFeed(cfg.feeds.sources, cfg.feeds.vol_source, cfg.feeds.timeout_seconds)
    q = feed.fetch_spot()
    if q:
        print(f"Spot: median ${q.price:,.2f} from {q.sources} (divergence {q.divergence_pct:.3f}%)")
    else:
        ok = False
        print("Spot: FAILED (no source reachable)")
    sigma, n, src = feed.fetch_vol(cfg.strategy.vol_window_minutes, cfg.strategy.vol_method,
                                   cfg.strategy.ewma_lambda, cfg.strategy.min_vol_samples)
    print(f"Vol: {('%.1f%%' % (sigma * 100)) if sigma else 'n/a'} annualised from {n} returns ({src})")
    if cfg.mode in ("demo", "live"):
        signer = make_signer(cfg)
        if signer is None:
            ok = False
            print(f"Auth: no credentials for mode={cfg.mode}")
        else:
            tc = make_client(cfg, cfg.base_url, signer)
            try:
                print(f"Auth OK ({signer.key_type}); balance:", tc.get_balance().get("balance_dollars"))
                print(f"Clock offset vs server: {tc.clock_offset:+.2f}s")
            except KalshiAPIError as exc:
                ok = False
                print("Auth/balance FAILED:", exc)
    return 0 if ok else 1


def cmd_demo_smoke(cfg: Config, args) -> int:
    """End-to-end order test against Kalshi DEMO: create a far-from-market
    post-only order for 1 contract, read it back, cancel it."""
    setup_logging(cfg, to_file=False)
    if cfg.mode != "demo":
        print("Set mode: demo in config.yaml for demo-smoke (refusing to send orders elsewhere).")
        return 2
    signer = make_signer(cfg)
    if signer is None:
        print("Missing KALSHI_DEMO_API_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_PATH in .env")
        return 2
    c = make_client(cfg, cfg.base_url, signer)
    try:
        print("balance:", c.get_balance())
        ticker = args.ticker
        if not ticker:
            ms = []
            for st in cfg.series:
                ms = c.get_markets(series_ticker=st, status="open", max_pages=1)
                if ms:
                    break
            if not ms:
                ms = c.get_markets(status="open", max_pages=1, limit=50)
            ticker = ms[0]["ticker"]
        print("market:", ticker)
        ob = parse_orderbook(c.get_orderbook(ticker, 5))
        print("book:", ob)
        price = 0.01
        best_yes_ask = (1 - ob["no_bids"][0][0]) if ob["no_bids"] else None
        if best_yes_ask is not None and best_yes_ask <= price:
            print("best ask is at 0.01; a post-only bid there would cross. Pick another --ticker.")
            return 1
        resp = c.create_order(ticker, "yes", price, 1, time_in_force="good_till_canceled", post_only=True,
                              expiration_ts=int(time.time() + 120))
        print("create:", resp)
        oid = resp["order_id"]
        print("get:", c.get_order(oid))
        print("cancel:", c.cancel_order(oid, ticker=ticker))
        st = c.get_order(oid).get("status")
        print("status after cancel:", st)
        return 0 if st == "canceled" else 1
    except KalshiAPIError as exc:
        print("DEMO SMOKE FAILED:", exc, getattr(exc, "body", ""))
        return 1


def cmd_replay(cfg: Config, args) -> int:
    from .replay import generate_synthetic, replay
    from .reporting import build_report, format_report

    if args.synthetic:
        src = Storage(":memory:")
        generate_synthetic(src, hours=args.hours, true_vol=args.true_vol, market_vol=args.market_vol)
        print(f"SYNTHETIC data: true vol {args.true_vol:.0%}, market quotes priced at {args.market_vol:.0%} vol "
              f"(fake data -- tests the pipeline, not profitability)")
    else:
        path = args.src or cfg.resolved_db_path("paper")
        if not os.path.exists(path):
            print(f"No recorded data at {path}. Run the bot in paper mode first to record data.")
            return 1
        src = Storage(path)
    for kv in args.set or []:
        sect, _, rest = kv.partition(".")
        key, _, val = rest.partition("=")
        obj = getattr(cfg, sect)
        cur = getattr(obj, key)
        if isinstance(cur, bool):
            setattr(obj, key, val.strip().lower() in ("1", "true", "yes"))
        else:
            setattr(obj, key, type(cur)(val))
    out_path = args.out or ":memory:"
    if out_path != ":memory:" and os.path.exists(out_path):
        os.remove(out_path)
    out = Storage(out_path)
    stats = replay(cfg, src, out, verbose=args.verbose)
    print(f"replayed {stats['batches']} snapshots, {stats['evaluations']} evaluations, {stats['fills']} fills")
    print(format_report(build_report(cfg, out, all_time=True), cfg.live.min_paper_trades))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kalshibot")
    ap.add_argument("--config", default=os.environ.get("KALSHIBOT_CONFIG", "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    rp = sub.add_parser("report")
    rp.add_argument("--day", help="YYYY-MM-DD (local day in risk.day_timezone)")
    rp.add_argument("--all", action="store_true")
    rp.add_argument("--db")
    sub.add_parser("check")
    sub.add_parser("reset-kill-switch")
    sub.add_parser("arm-live")
    ds = sub.add_parser("demo-smoke")
    ds.add_argument("--ticker")
    rl = sub.add_parser("replay")
    rl.add_argument("--src", help="recorded DB (default: the paper DB)")
    rl.add_argument("--out", help="output DB (default: in-memory)")
    rl.add_argument("--synthetic", action="store_true")
    rl.add_argument("--hours", type=int, default=48)
    rl.add_argument("--true-vol", type=float, default=0.50)
    rl.add_argument("--market-vol", type=float, default=0.50)
    rl.add_argument("--set", action="append", help="override e.g. strategy.min_edge=0.06")
    rl.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    handlers = {"run": cmd_run, "status": cmd_status, "report": cmd_report, "check": cmd_check,
                "reset-kill-switch": cmd_reset_kill, "arm-live": cmd_arm_live,
                "demo-smoke": cmd_demo_smoke, "replay": cmd_replay}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
