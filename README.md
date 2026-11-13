# Crypto Trading Bot

A multi-strategy Binance trading bot with a built-in backtester, walk-forward strategy selection, paper trading,
testnet support, Telegram control and a Streamlit dashboard.

* **Spot bot:** long-only, and **chooses its own strategy** from recent after-cost backtests.
* **Futures bot:** long + short on USDⓈ-M perpetuals, always at 1x leverage. Runs in paper mode by default.
* Starts in paper mode. Live trading is gated behind several explicit settings and capped at 100 USDT.

> ⚠️ **Disclaimer:** this is an educational/research project, not financial advice. The research below shows the
> spot strategies have **no reliable edge after fees**. Trading crypto can lose all of your money. Use it at your
> own risk, and only with funds you can afford to lose.

> History: v1 was a 1-minute "scalper". A 2-year backtest showed it lost about 0.24% per trade after fees, and its
> signal had no edge even before fees, so it was replaced by the version described here.

## How it works

```
every 20 s ─► manage open positions (stop / target / max-hold)   ← runs even when PAUSED or HALTED
           ├► selector refresh (every 6 h): backtest every strategy × symbol on recent data after costs
           ├► on each newly CLOSED candle: signal exits, then entry candidates
           └► if RUNNING and risk allows: open the highest-scoring ELIGIBLE candidate
```

**Strategies** (`app/strategies/library.py`) are all ATR-scaled and evaluated on closed candles only:

| name | idea |
|---|---|
| `trend_follow_4h` | EMA20 crosses above EMA50 while above EMA200; exit on cross down |
| `turtle_breakout_4h` | 55-bar high breakout; exit on 20-bar low |
| `donchian_breakout_1h` | 20-bar high breakout + volume + ADX |
| `squeeze_breakout_1h` | Bollinger squeeze resolving upward |
| `trend_pullback_{15m,1h,4h}` | pullback to EMA20 in an uptrend |
| `mean_reversion_{15m,1h}` | lower Bollinger band + RSI<30 in a ranging (low ADX) market |
| `rsi2_dip_1h` | RSI(2)<10 above EMA200 |
| `ema_momentum_5m` | the v1 logic, kept as a baseline |

**Selector** (`app/selector.py`): a (strategy, symbol) pair is *eligible* only if its recent backtest, using the
same fee and slippage model as paper and live, has at least the minimum trades, profit factor ≥ 1.2, expectancy
≥ 0.05% per trade and drawdown ≤ 15%. Slow strategies are judged on 240 days, fast ones on 60.
Eligible pairs are ranked by a t-statistic score. **If nothing is eligible, the bot stays flat. That is intended.**

`ENABLED_STRATEGIES` limits which strategies the selector may pick. The default is the two 4h strategies.
`SYMBOLS` defaults to 14 liquid coins: BTC, ETH, SOL, XRP, BNB, NEAR, UNI, DOGE, SUI, TRX, LINK, AVAX, ADA and LTC.
The extra coins were picked by an objective rule (at least ~$15M daily volume, at least 2 years of history, no
micro-priced meme coins), not by backtest results. Up to `MAX_OPEN_POSITIONS=2` positions are held at once.

## Backtest results (be realistic)

Run `docker compose run --rm backtest --days 730`. Data: Binance, Nov 2024 – Nov 2026, with fee 0.10% and
slippage 0.02% per side. The walk-forward test picks strategies using only past data, trades the next week, and
repeats. Starting equity is 100, with 0.5% risk per trade.

* Every intraday strategy (5m/15m/1h pullback, mean reversion, RSI-2, v1 momentum) **lost money after fees**.
* The 1h breakout strategies also lost money across 14 coins.
* The 4h strategies were positive on the original 3 coins. On the 11 new coins they averaged **+0.04% per trade**,
  essentially zero, and only 7 of 22 coin/strategy pairs were positive.
  **This is the key result: the edge does not carry over to new coins.**

| setup | out-of-sample result | max drawdown | trades |
|---|---|---|---|
| 3 coins, 4 slow strategies, 1 position (v2 initial) | +2.9% | 7.0% | 123 |
| same, but 2 positions | −2.6% | 10.9% | 159 |
| 14 coins, 4 slow strategies, 1 position | −3.2% | 11.2% | 300 |
| **14 coins, two 4h strategies, 2 positions (current default)** | **+2.6%** | **5.8%** | 52 |
| all strategies, no selector | −95% | 95% | — |

Small configuration changes flip the result between +3% and −3%, so this is **noise around breakeven, not an
edge**. The selector mainly **protects capital** in a falling market: over this period BTC moved roughly −30% from
its peak, and alts fell further. Treat the testnet run as a check that the execution works, not as proof of profit.

## Research: profiting when prices fall (futures, not implemented in the bot)

The scripts are `app/research_futures.py` and `app/research_longshort.py`. They cover 3 years (Nov 2023 – Nov 2026) of
Binance USDⓈ-M perpetual futures on the same 14 coins, with fee 0.05% + slippage 0.02% per side and funding applied.

| finding | numbers |
|---|---|
| `trend_follow_short_4h` (mirror of the long trend strategy) | +0.41%/trade, 10/14 coins positive, but +1.03% in the 1st half and −0.12% in the 2nd |
| `turtle_breakdown_4h` and `rally_fade_4h` | lose money (crypto breakdowns tend to snap back) |
| Weekly selector applied to futures | **hurts**: −2.3% versus +32% for the same trades always on |
| **Always-on: 2 longs + trend short, no selector** | **+22% to +37% over 3 years, max drawdown 6–17%, both halves positive** for every setting of 1–4 positions |
| Same longs only, spot fees | +3% to +27%, very sensitive to the number of positions |
| Funding carry (long spot + short perp) | about 3%/yr on capital, falling to 0–2%/yr in 2025–26; not worth the complexity |

The long/short mix works mostly as a **hedge**: longs lost in 2025 (−0.68% per trade) while shorts made +1.5% per
trade, and in 2026 it reversed. Caveats:
* Three short strategies were tested and only one worked, so luck is possible.
* Per-strategy t-statistics are only about 1.
* Shorting needs a futures account, which is not available in every country, plus a new futures broker
  (1x isolated margin, reduce-only stops).
* It should go through paper trading and the Binance Futures testnet before anything else.

## Futures bot (long + short), running alongside the spot bot

`docker compose up -d` also starts `futures-worker`, `futures-api` (http://localhost:8001) and `futures-dashboard`
(http://localhost:8502). They use their own database, `scalper_futures`, so the futures results are a separate
ledger from the spot bot's.

* **Market:** Binance USDⓈ-M perpetuals (`MARKET=futures`). Fee 0.05% per side. Funding is paid or received
  and included in profit/loss.
* **Strategies:** `trend_follow_4h` and `turtle_breakout_4h` (long) plus `trend_follow_short_4h` (short), set in
  `FUTURES_STRATEGIES`.
* **Always-on:** every strategy/coin pair may trade (`SELECTION_MODE=auto` resolves to `always_on` for futures). The
  recent backtest score is only used to rank signals that fire at the same time. This follows the research
  result that weekly re-selection hurt these strategies.
* **Safety, enforced in code:**
  * 1x leverage and isolated margin in one-way mode, set per coin before its first trade. The bot refuses to trade
    if the exchange reports anything other than 1x.
  * Stop-loss and take-profit are exchange-hosted, reduce-only `STOP_MARKET` and `TAKE_PROFIT_MARKET` orders.
  * The position is saved before protection is placed. If protection fails, the bot closes the position with a
    reduce-only market order and halts.
  * On restart it reconciles open positions and re-places missing protection.
* **Exchange minimums:** BTC needs about $69, ETH, LINK and LTC need $20, and the other coins need $5. At $100 equity
  and 0.5% risk, positions are about $10–15, so BTC, ETH, LINK and LTC are **skipped**, and each skip is logged.
* Telegram messages are tagged, e.g. `[paper futures]` or `[testnet spot]`. Telegram commands control the spot bot;
  use the futures dashboard or the API (port 8001, same `API_TOKEN`) to control the futures bot.

**To move the futures bot to a test venue:**
1. Create keys in **Binance Demo Trading** (https://demo.binance.com; the old testnet.binancefuture.com now
   redirects there). `FUTURES_TESTNET_VENUE=demo` is the default.
2. Set `BINANCE_FUTURES_TESTNET_API_KEY` and `BINANCE_FUTURES_TESTNET_API_SECRET` in `.env`.
3. Run the order-path check:
   ```bash
   docker compose run --rm --no-deps -e MARKET=futures -e TRADING_MODE=testnet futures-worker python -m app.futures_testnet_check
   ```
4. Set `FUTURES_TRADING_MODE=testnet` and run `docker compose up -d`.

Live futures are deliberately not wired into compose. Binance Futures is also not available in every country.

## Running on a cloud server (24/7)

These scripts work on any fresh **Ubuntu 24.04** VPS with about 4 GB of RAM. Hetzner CX22 in Germany or Finland
costs around €4–5 a month. Use an **EU or Asian region**, because Binance blocks US server IPs.

1. Create the server and add the public key `~/.ssh/scalper_server.pub`.
2. Harden it and install Docker, once:
   ```bash
   ssh -i ~/.ssh/scalper_server root@SERVER_IP 'bash -s' < deploy/server_setup.sh
   ```
   This creates user `trader` (key-only login), disables root and password login, sets the firewall to SSH only,
   enables automatic security updates, adds 2 GB swap, and schedules daily database backups to `~/backups`.
3. Deploy, and repeat after every code change:
   ```bash
   SERVER=SERVER_IP bash deploy/deploy.sh
   ```
4. **Stop the bots on your PC** with `docker compose down`. Two copies would trade the same testnet account
   twice and fight over the same Telegram bot.
5. Dashboards stay private on the server. Open them through an SSH tunnel:
   ```bash
   ssh -i ~/.ssh/scalper_server -N -L 8501:localhost:8501 -L 8502:localhost:8502 trader@SERVER_IP
   ```
   Then browse to http://localhost:8501 (spot) and http://localhost:8502 (futures).

## Weekly report

Every Monday at 08:00 UTC (`WEEKLY_REPORT_WEEKDAY`, `WEEKLY_REPORT_HOUR_UTC`), Telegram receives a report with:
* trades closed and opened, wins and losses
* profit/loss, and on testnet also profit/loss **after estimated real fees** (the testnet charges none)
* average return per trade compared with the backtest expectation for those pairs
* exit reasons, results per strategy, open positions with their current move, and all-time totals
* which pairs the selector allows, and any warnings or halts

Get it any time with `/report` in Telegram or `GET /report?days=30`.

## Quick start (paper)

```bash
cp .env.example .env      # set API_TOKEN to a long random string
docker compose up -d --build
```

* Dashboard: http://localhost:8501 (strategy scoreboard, positions, equity curve, events, controls)
* API: http://localhost:8000/docs. `POST /control/{pause|resume|stop|reset}` needs the `X-API-Token` header.
* Backtest: `docker compose run --rm backtest --days 730 --strategies trend_follow_4h,turtle_breakout_4h`

## Binance Spot Testnet (fake funds, real order path)

In `TRADING_MODE=testnet`, **orders go to testnet.binance.vision**, while candles and strategy selection still use
**mainnet** public data, because testnet candles are thin and would make selection meaningless. Testnet trades are
stored with `mode=testnet`, so their profit/loss is kept separate from paper results.

1. Go to https://testnet.binance.vision, log in with GitHub, choose **Generate HMAC_SHA256 Key**, and copy the key
   and secret.
2. In `.env`, set `BINANCE_TESTNET_API_KEY=…`, `BINANCE_TESTNET_API_SECRET=…` and `TRADING_MODE=testnet`.
   Your mainnet `BINANCE_API_KEY` is never used in this mode.
3. Smoke-test the full order path. It buys about $15, places the OCO, cancels it and sells, using a throwaway database:
   ```bash
   docker compose run --rm worker python -m app.testnet_check
   ```
4. Start the bot with `docker compose up -d --build`. In testnet mode it starts RUNNING.

Testnet balances and orders are wiped roughly once a month. If a reset happens while a position is open, the bot
halts on reconciliation. Run `/reset` and then `/resume`.

## Controls & states

* `RUNNING`: entries allowed. `PAUSED`: no new entries. `HALTED`: set by a fatal error or `/stop`.
* Exits are **always** managed, even when PAUSED or HALTED.
* From HALTED: check balances and open orders on Binance, then `/reset` (→ PAUSED), then `/resume`.
* Risk blocks clear themselves: the daily loss limit resets at 00:00 UTC, the weekly limit is a rolling 7 days,
  and consecutive losses trigger a cooldown (`COOLDOWN_MINUTES`).
* Network and data errors are retried with backoff (an alert is sent after 10 in a row). They no longer halt the bot.

Telegram: `/status /report /positions /pnl /strategies /pause /resume /stop /reset /help`, only from
`TELEGRAM_ALLOWED_CHAT_IDS`.

## Live mode — read first

Live mode requires `TRADING_MODE=live`, `ENABLE_LIVE_TRADING=true`, `LIVE_ARMED=true`,
`LIVE_TRADING_ACK=I_ACCEPT_LIVE_TRADING_RISK` and API keys. Allocation is capped at 100 USDT, and the bot
starts PAUSED. Use a key with Spot trading only, no withdrawals, and IP restrictions.

The live broker (`app/execution.py`) works like this:
* Market buy by quote amount.
* Records the position **before** placing protection.
* Subtracts the fee taken in the base asset from the quantity.
* Places an exchange-hosted OCO: a LIMIT_MAKER take-profit plus a STOP_LOSS_LIMIT stop.
* If the OCO fails, it does an emergency market sell and halts.
* For signal or time exits, it cancels the OCO and then sells.
* On restart, it reconciles every open position.

All of this is unit-tested against a fake exchange only. **Before using real money, run it on the Binance
Spot Testnet** (`BINANCE_TESTNET=true`). Watch it place, fill and cancel real OCOs.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
DATABASE_URL=sqlite:///:memory: pytest -q      # 53 tests
```

Layout: `config.py` · `exchange.py` · `indicators.py` · `strategies/` · `backtest.py` · `selector.py` ·
`risk.py` · `execution.py` · `worker.py` · `api.py` · `telegram_bot.py` · `dashboard/main.py`.

This is not financial advice. Trading crypto can lose money.
