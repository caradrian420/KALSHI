# kalshibot: paper-first Kalshi BTC short-duration trading bot

This bot trades Kalshi's hourly (`KXBTCD`) and 15-minute (`KXBTC15M`) Bitcoin markets only when
a volatility-based fair probability beats the executable price **after fees, a slippage buffer
and a minimum edge**. Most of the time it should do nothing.

- **It starts in PAPER mode.** Paper mode reads live production market data and simulates fills
  conservatively.
- **DEMO mode** runs the real order code against Kalshi's demo exchange.
- **LIVE mode** uses real money. It needs `mode: live`, a typed confirmation, and by default a paper
  record of at least 300 settled trades with positive net P&L.

> Nothing here is a claim that this strategy makes money. The edge is unproven until your own
> paper record shows it. See [What is untested](#what-is-untested--what-could-break).

---

## 1. Research notes (what was confirmed, and how)

The build sandbox could not reach `docs.kalshi.com`, `kalshi.com` or `api.elections.kalshi.com`
because outbound network was blocked. I confirmed facts from these sources instead:
- **Kalshi's official Python SDK** `kalshi_python_sync` 3.36.0 from PyPI. It is generated from their
  OpenAPI spec, and I read its endpoint paths, request and response models, and auth code.
- **Kalshi's BTC contract-terms PDF** at `kalshi-public-docs.s3.amazonaws.com/contract_terms/BTC.pdf`.
- **Web search snippets** of docs.kalshi.com pages.

| Item | Status | Source |
|---|---|---|
| Prod REST `https://api.elections.kalshi.com/trade-api/v2` (also `external-api.kalshi.com`) | confirmed | SDK configuration + docs snippet |
| Demo REST `https://demo-api.kalshi.co/trade-api/v2` (also `external-api.demo.kalshi.co`) | confirmed | SDK configuration |
| Auth headers `KALSHI-ACCESS-KEY / -TIMESTAMP (ms) / -SIGNATURE`; message = `timestamp + METHOD + path` (path includes `/trade-api/v2`, query stripped); RSA-PSS SHA-256 with salt = digest length, or Ed25519 | confirmed | SDK `auth.py` + docs snippet |
| Markets `GET /markets` (status `unopened/open/closed/settled`; **close-time filters can't combine with `status=open`**), `GET /markets/{t}`, `GET /series/{s}` | confirmed | SDK |
| Orderbook `GET /markets/{t}/orderbook` → `orderbook_fp.yes_dollars / no_dollars`: **bids only**, `[price_str, count_str]` | confirmed | SDK + docs snippet |
| Orders: **V2** `POST /portfolio/events/orders` with `side: bid/ask` on the YES book, `price` as a dollar string, `count` string, `time_in_force ∈ {fill_or_kill, good_till_canceled, immediate_or_cancel}`, `post_only`, `expiration_time` (s), `self_trade_prevention_type`; cancel `DELETE /portfolio/events/orders/{id}`. Buying NO at q = selling YES at 1−q. | confirmed | SDK models |
| `GET /portfolio/balance` (cents), `/positions`, `/orders`, `/orders/{id}`, `/fills`, `/settlements` | confirmed | SDK |
| Prices and counts migrated to fixed-point strings (`*_dollars`, `*_fp`) | confirmed | SDK models + docs snippet |
| Rate limits: token buckets; Basic tier is 200 read and 100 write tokens/sec, with per-endpoint costs | partly confirmed (snippet only) | docs snippet. The bot defaults to 10 reads and 5 writes/sec, well under that. |
| Taker fee `roundup(0.07·C·P·(1−P))`; maker `0.0175·…` only on maker-fee series | confirmed (secondary sources) | multiple fee guides. The official PDF was unreachable. |
| Series `fee_type ∈ {quadratic, quadratic_with_maker_fees, quadratic_with_combo_maker_fees, flat}` plus `fee_multiplier` | confirmed | SDK `Series` model |
| Fee rounding: since the "fee rounding" change, the fee is per fill, in centicents, with a rounding fee and later rebates | confirmed (snippet) | docs.kalshi.com/getting_started/fee_rounding snippet |
| **Whether KXBTCD / KXBTC15M charge maker fees** | **NOT confirmed** | The bot reads `fee_type` from `GET /series/{s}` at startup, logs it, and adapts. |
| Settlement: "simple average of the CF Bitcoin Real-Time Index (BRTI) for the minute (60 seconds) prior to <time>"; no data → resolves No | confirmed | BTC contract-terms PDF |
| KXBTCD = hourly "BTC above $X" ladder | confirmed (market pages) | search results |
| KXBTC15M = YES if the 60s BRTI average at the window's end ≥ the 60s average before the window opened ("target") | secondary sources only | bitcoin.com / blog snippets |
| BRTI constituent exchanges | **not confirmed** | The bot uses a Coinbase/Kraken/Bitstamp median as a proxy and models the basis as noise. |

**Fee rounding and small orders.** The bot charges itself `ceil_to_cent(rate·C·P·(1−P))` per order.
That is never lower than what Kalshi's per-fill rounding scheme ends up charging, so it is
conservative. On a $1 budget this matters a lot. One contract at 50¢ pays 2¢ of fee: 0.0175
rounded up is 0.02, a 14% markup on the theoretical fee. That 2¢ is half of the default 4¢
minimum edge. Sizing and edge always use the fee for the actual contract count.

**Start-up safety checks.** On start the bot fetches each series. If the `settlement_sources` of a
series do not mention "CF Benchmarks", the bot **disables that series**. It does the same if the
fee type is `flat` or unknown.

## 2. Strategy (math only)

1. **Spot**: the median of last-trade BTC/USD on Coinbase, Kraken and Bitstamp, as a BRTI proxy.
   The bot does not trade if the sources disagree by more than 0.25% or the quote is over 15s old.
2. **Vol**: the stdev (or EWMA) of the last 60 one-minute log returns from Coinbase candles.
   The bot falls back to its own sampled history. Vol is clamped to [20%, 300%] annualised and
   multiplied by `vol_multiplier ≥ 1`.
3. **Fair probability** of the 60-second settlement average finishing above the strike K, with
   t seconds left and w = 60:
   - variance `σ²·(t − 2w/3)`. This is the exact variance of a Brownian window-average; when
     t < w the bot uses `σ²·t³/(3w²)`.
   - plus `(basis_usd/S)²` for the gap between our proxy and BRTI.
   - `P = N((ln(S/K) − var/2)/√var)`. "below" and "between" are built from the same formula.
   - A unit test checks this against a brute-force Monte Carlo of the 60-second average. They
     agree within ±2pp.
4. **Edge** for each side: `P(side) − (best_ask + fee_per_contract + slippage_buffer)`. The bot
   trades only if edge ≥ `min_edge` (default $0.04). The maker alternative is also computed:
   join at best bid + 1¢ and pay the maker fee. Both are logged.
5. **Size**: the largest count where `count·ask + fee(count) ≤ $1`, capped at the size displayed
   at the best ask. There is no martingale, no averaging down, and at most one entry per market.
   Positions are held to settlement.

## 3. Risk limits (all in `config.yaml`)

| Limit | Default | Enforcement |
|---|---|---|
| Bankroll | $20 | Bot equity = $20 + realized P&L, tracked in its own DB, even if the account holds more |
| Max risk per trade | $1 | Cost plus fees. A binary contract can't lose more than that. |
| Max open positions | 3 | Counts open positions plus markets with resting orders |
| Daily loss limit | $4 | Halts until local midnight once hit. A trade is also refused if today's loss + all open risk + the new trade *could* exceed $4. |
| Kill switch | equity ≤ $10 | Persistent DB flag. The process exits with code 3, which systemd will not restart. Clear it with `reset-kill-switch`. Pre-emptive mode refuses any trade whose worst case would cross $10. |
| Expiry | no trades in the last 60s | Checked in the strategy and again in the risk manager |
| Spread | > $0.10 → skip | Both places |
| Price band | only 5¢–95¢ | Model error dominates at the extremes |
| Stale orders | resting orders cancelled after 30s | Also sent with `expiration_time` |

## 4. Paper / demo / live

**Paper** (default). Uses production data with no key. Fills are simulated **only at the best ask,
only up to the size shown there, with the full taker fee**. Maker fills are never simulated.
Paper trades settle on the real market `result`.

**Demo**. Set `mode: demo` and put demo keys in `.env`:
```bash
python -m kalshibot check        # auth + balance on demo
python -m kalshibot demo-smoke   # place a 1-contract post-only 1¢ bid, read it back, cancel it
python -m kalshibot run          # full loop on demo (demo prices are not real-market prices)
```

**Live**. Real money, locked by default:
1. Set `mode: live` in `config.yaml` and put production keys in `.env`.
2. By default the paper DB must show at least 300 settled trades and net P&L > 0. Otherwise the bot
   refuses to start. `live.override_paper_gate: true` bypasses this. Don't.
3. Confirm with a typed phrase:
   - From a terminal, `python -m kalshibot run` asks you to type
     `I UNDERSTAND THIS TRADES REAL MONEY`.
   - Under systemd there is no terminal. Run `python -m kalshibot arm-live` once and type the
     phrase. It writes `data/LIVE_ARMED`, which is tied to a hash of your exact config and valid
     for 24h (`armed_valid_hours`). If anything in config changes, you must re-arm.

   After 24h, a restart in live mode refuses to start until you re-arm. That is deliberate and
   fails safe. Set `armed_valid_hours: 0` if you want arming not to expire.

To go back to paper, set `mode: paper` and restart. Each mode has its own database
(`data/kalshibot_{mode}.db`), so paper and live results never mix.

## 5. Commands

```bash
python -m kalshibot run                  # main loop
python -m kalshibot status               # bankroll, open positions, today's P&L, halts
python -m kalshibot report               # today's report
python -m kalshibot report --day 2026-11-20
python -m kalshibot report --all         # lifetime report + live-readiness gate
python -m kalshibot check                # connectivity, series fee/settlement info, spot, vol
python -m kalshibot replay               # backtest on data recorded by paper mode
python -m kalshibot replay --set strategy.min_edge=0.06 --set strategy.vol_multiplier=1.2
python -m kalshibot replay --synthetic --true-vol 0.6 --market-vol 0.25   # pipeline self-test (FAKE data)
python -m kalshibot reset-kill-switch
python -m kalshibot arm-live
```

The **report** includes:
- trades, win rate, fees, net P&L
- average predicted vs realized edge per contract
- max drawdown
- calibration tables for traded sides and for every evaluated market
- Brier score of the model vs the market mid, on the same markets. If the model doesn't beat the
  market mid, it has no informational edge.
- counts of signal decisions, with the reason for each skip
- lifetime mean P&L per trade with a t-statistic

**Replay caveats.**
- Only top-of-book is recorded, about every 15s, so latency is not modelled.
- Parameter sweeps on the same data overfit. Hold out recent days.

## 6. Setup on a cheap Linux VPS (Ubuntu 22.04/24.04, 1 vCPU / 1 GB is plenty)

```bash
# as root
apt update && apt install -y python3 python3-venv git
timedatectl set-ntp true                       # clock drift breaks request signing
adduser --disabled-password --gecos "" kalshibot
su - kalshibot

# as kalshibot
git clone <your repo url> KALSHI && cd KALSHI
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                  # should be all green
cp .env.example .env && chmod 600 .env         # paper mode needs no keys
mkdir -p ~/keys && chmod 700 ~/keys            # put *.pem here (chmod 600) for demo/live
.venv/bin/python -m kalshibot check            # verify data feeds from the VPS
exit

# as root: install the service
cp /home/kalshibot/KALSHI/deploy/kalshibot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshibot
journalctl -u kalshibot -f                     # live logs (also data/kalshibot_paper.log)
```

The service restarts automatically on crashes (`Restart=always`). It does **not** restart after
exit code 3 (kill switch) or exit code 2 (refused to start, for example live not confirmed).
`systemctl stop kalshibot` sends SIGTERM. The bot then cancels its resting orders and exits.

**Daily report by cron** (as kalshibot, `crontab -e`):
```
55 23 * * * cd ~/KALSHI && .venv/bin/python -m kalshibot report >> data/daily_reports.txt 2>&1
```

## 7. Code layout

```
config.yaml            behaviour (no secrets)        .env           API keys only (gitignored)
kalshibot/config.py    typed config + validation     auth.py        request signing (RSA-PSS / Ed25519)
kalshibot/api_client.py REST client, rate limit, backoff, clock drift
kalshibot/price_feed.py spot median + realized vol   pricing.py     fair probability model
kalshibot/fees.py      fee math (Decimal, ceil-to-cent)
kalshibot/strategy.py  edge, sizing, filters         risk.py        hard limits, kill switch
kalshibot/executor.py  paper + real (demo/live)      storage.py     SQLite schema
kalshibot/bot.py       main loop                     reporting.py   report / status
kalshibot/replay.py    backtest + synthetic generator  cli.py       commands
deploy/kalshibot.service
tests/                 94 unit/integration tests (no network needed)
```

## What is tested
`python -m pytest -q` runs 94 tests, all offline:
- **Fees.** Published examples (1.75 / 0.44), cent rounding on 1–10 contract orders, the float-fuzz
  trap, and series fee types and multipliers.
- **Signing.** RSA-PSS and Ed25519 signatures are checked with the public key. Also covered: query
  stripping, the `/trade-api/v2` prefix, the millisecond timestamp, and that the key never appears
  in `repr`.
- **Pricing.** Monotonicity, limits, window-average variance and all strike types. The model is
  checked against a Monte Carlo of the 60-second average and agrees within 2pp. The vol estimator
  recovers a known vol.
- **Every risk limit.** Per-trade max, open positions (including resting orders), one entry per
  market, expiry, spread, daily limit with reset at the next day, worst-case daily, kill switch
  surviving a restart, pre-emptive kill, cash, and bankroll accounting.
- **Strategy.** Edge formula, sizing with fees and depth, YES/NO choice, every skip reason.
- **Paper executor.** Fills only up to displayed size, no fill if the book moved, settlement P&L.
- **API client.** Retry on 429 (honours Retry-After), retry on 5xx and connection errors, give-up,
  no retry on 4xx, no blind resend of a timed-out write, pagination, V2 order body for YES and NO.
- **Demo/live executor**, against a fake exchange. Fills read from `/fills` with no double-counting,
  post-only plus stale cancel, reconciliation of a timed-out create, rejected orders.
- **Live gate.** Mode check, paper-record gate, exact phrase, arming file tied to the config hash,
  expiry.
- **Full bot loop**, with a fake exchange and feed. Trade → no re-entry → settle. Series with the
  wrong settlement source get disabled. The loop survives an API outage. Divergent spot blocks
  trading. The kill switch exits.
- **Replay on synthetic data.** An efficient market gives 0 trades. A mispriced market gets
  exploited with predicted edge ≈ realized edge, and the limits hold across hundreds of trades.

Run by hand in the sandbox: `check` and `run` against a fully blocked network. Both kept running,
logged the errors and shut down cleanly on SIGTERM.

## What is untested / what could break
1. **Nothing has touched the real Kalshi API or any real exchange feed.** The sandbox had no route
   to them. Every endpoint path and field name comes from Kalshi's generated SDK, not from a live
   call. Run `python -m kalshibot check` on your VPS first, then `demo-smoke`.
2. **Demo order placement has not been run.** The V2 order body (`side` bid/ask, string `price`
   and `count`, `self_trade_prevention_type`) matches the SDK schema but has never been accepted by
   a real server. Possible failures:
   - the price string format
   - `cancel_order_on_pause`
   - whether duplicate `client_order_id`s are rejected (retries on 5xx rely on this)
3. **KXBTC15M strike semantics.** The bot assumes the API gives the "target" in `floor_strike`
   with a `greater`/`greater_or_equal` `strike_type`. If it uses `functional_strike` or
   `custom_strike` instead, those markets are skipped with `unsupported_strike` and never mispriced.
   Check `check` output and the `rules_primary` text it prints.
4. **Reference time.** The bot treats `close_time` as the end of the 60-second averaging window.
   If Kalshi closes trading before the reference time, time-to-expiry is slightly wrong. Compare
   `close_time` with `rules_primary` for a few markets.
5. **Fees.**
   - Maker-fee status for the BTC series is read from the API and was not confirmed in the docs.
   - `fee_multiplier` semantics are assumed: a multiplier on the 0.07 base, never allowed to lower
     the estimate.
   - Real fills report `fee_cost`. Compare it with the model after your first demo/live fills.
6. **Spot proxy vs BRTI.** The median of three exchanges is not BRTI. The $15 basis noise is a
   guess. During fast moves, venue lag can make "edges" that are really just data lag. That is the
   most likely way this strategy looks good in paper and loses live.
7. **Model risk.** BTC returns have fat tails and vol clustering. A one-hour realized vol is noisy.
   Short-dated BTC markets are traded by professionals with faster BRTI-grade feeds. Expect few
   real edges. The Brier comparison in the report tells you whether the model beats the market mid.
8. **Paper realism.**
   - Fills assume your order would have taken the displayed size at decision time, with zero
     latency and no queue competition.
   - Real taker fills will be worse.
   - Maker orders are never simulated, so "maker vs taker" is only a logged comparison.
9. **Rate limits.**
   - KXBTCD has many open strikes. The loop lists every open market per series every 15s, up to 5
     pages, and fetches an orderbook only when the listing quote already shows an edge.
   - Per-endpoint token costs were not verified.
   - `GET /account/limits` exists if you need to check your tier.
10. **Clock drift** is estimated from the HTTP `Date` header, which has 1-second resolution. Keep
    NTP on.
11. **Live-account reconciliation** is partial. The bot trusts its own DB for bankroll and risk,
    and checks exchange cash before each order. It does not import positions it didn't open, so
    don't trade the same account by hand.
12. **Settlement edge cases.**
    - Markets resolved other than yes/no are not handled specially. After 8 days unresolved, a
      position is voided at cost.
    - Kalshi "Market Outcome Review" delays just leave the position open longer.
