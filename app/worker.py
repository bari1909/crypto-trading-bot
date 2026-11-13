"""Trading worker.

Each cycle (SCAN_INTERVAL_SECONDS):
  1. heartbeat
  2. manage every open position (stop/target, max-hold) — runs even when PAUSED or HALTED
  3. refresh the strategy selector when due
  4. for each (symbol, timeframe) with a newly closed candle: signal exits, then entry candidates
  5. if RUNNING and no risk block: open the best-scored eligible candidate(s)

Errors: network/data problems are transient (retry with backoff, alert after N in a row).
FatalError / auth / insufficient funds -> HALTED (entries blocked, exits still managed).
"""
import logging
import time
from datetime import datetime, timezone

import ccxt

from app import ai
from app.config import settings
from app.db import SessionLocal, acquire_worker_lock, init_db
from app.exchange import (TF_MS, TRANSIENT_ERRORS, StaleDataError, closed_candles, is_sandbox, make_exchange,
                          make_order_exchange, quote)
from app.execution import FatalError, event, hold_expired, make_broker
from app.models import BotState
from app.notify import telegram_send
from app.reports import mark_sent, report_due, weekly_report
from app.risk import RiskManager
from app.selector import Selector
from app.strategies import active_strategies


logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")


FATAL_ERRORS = (FatalError, ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.AccountSuspended,
                ccxt.InsufficientFunds)


MAX_ENTRY_DELAY_MS = 15 * 60_000


def state_row(s) -> BotState:
    row = s.get(BotState, 1)
    if not row:
        row = BotState(id=1, state="PAUSED" if settings.trading_mode == "live" else "RUNNING",
                       mode=settings.trading_mode, reason="startup default")
        s.add(row)
        s.commit()
    if row.mode != settings.trading_mode:
        row.mode, row.state, row.reason = settings.trading_mode, "PAUSED", "mode changed; review then /resume"
        s.commit()
    return row


def set_state(s, state: str, reason: str):
    row = state_row(s)
    if row.state != state or row.reason != reason:
        row.state, row.reason, row.updated_at = state, reason[:1000], datetime.now(timezone.utc)
        s.commit()


class Bot:
    def __init__(self, exchange, selector: Selector | None = None, session_factory=SessionLocal, notify=telegram_send,
                 data_exchange=None):
        self.ex = exchange                          # where quotes come from and orders go
        self.data_ex = data_exchange or exchange    # where candles / selector history come from
        self.Session = session_factory
        self.strategies = active_strategies()
        self.selector = selector or Selector(self.data_ex, self.strategies)
        self.notify = notify
        self.timeframes = sorted({st.timeframe for st in self.strategies}, key=lambda tf: TF_MS[tf])
        self.last_bar: dict[tuple[str, str], int] = {}
        self.last_block: str | None = None
        self.errors = 0

    # ------------------------------------------------------------------------
    def startup(self):
        with self.Session() as s:
            state_row(s)
            make_broker(self.ex, s).reconcile()
            if not self.selector.load(s) or self.selector.due():
                self.selector.refresh(s)
            event(s, "INFO", f"Worker started in {settings.trading_mode.upper()} mode, {settings.market.upper()} "
                             f"market, selection={settings.effective_selection_mode}. "
                             f"Strategies: {', '.join(st.name for st in self.strategies)}")
        self.notify(f"Bot started ({settings.trading_mode}, {settings.market}).\n{self.selector.summary()}")

    def cycle(self, now_ms: int | None = None):
        now_ms = now_ms or int(time.time() * 1000)
        now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        with self.Session() as s:
            row = state_row(s)
            row.heartbeat_at = now
            s.commit()
            broker, risk = make_broker(self.ex, s), RiskManager(s)

            # 2. manage open positions
            for p in risk.open_positions():
                q = quote(self.ex, p.symbol)
                if not broker.check_exits(p, q) and hold_expired(p, now):
                    broker.close(p, "time", q)
                if p.status == "CLOSED":
                    self.notify(f"Closed {p.symbol} [{p.strategy}] {p.exit_reason}: PnL {p.pnl:+.4f} "
                                f"{settings.quote_currency} ({p.pnl_pct:+.2f}%)")

            # 2b. weekly report (never allowed to break trading)
            self.maybe_send_report(s, now)

            # 3. selector
            if self.selector.due():
                self.selector.refresh(s)
                self.notify(f"Strategy selection refreshed:\n{self.selector.summary()}")
            eligible = {(st, sym): sc for st, sym, sc in self.selector.eligible_pairs()}

            # 4. new closed bars -> exits + candidates
            candidates = []
            for sym in self.selector.symbols:
                for tf in self.timeframes:
                    key = (sym, tf)
                    if key in self.last_bar and now_ms < self.last_bar[key] + 2 * TF_MS[tf]:
                        continue   # next bar not closed yet
                    group = [st for st in self.strategies if st.timeframe == tf]
                    df = closed_candles(self.data_ex, sym, tf, limit=max(st.warmup for st in group) + 60)
                    bar_ts = int(df.ts.iloc[-1])
                    if self.last_bar.get(key) == bar_ts:
                        continue
                    self.last_bar[key] = bar_ts
                    fresh = now_ms - (bar_ts + TF_MS[tf]) <= min(TF_MS[tf] // 4, MAX_ENTRY_DELAY_MS)
                    for st in group:
                        sig = st.signals(df).iloc[-1]
                        for p in risk.open_positions():
                            if p.symbol == sym and p.strategy == st.name and bool(sig["exit"]):
                                broker.close(p, "signal", quote(self.ex, sym))
                                self.notify(f"Closed {p.symbol} [{p.strategy}] signal exit: PnL {p.pnl:+.4f}")
                        if fresh and bool(sig["entry"]) and (st.name, sym) in eligible:
                            candidates.append((eligible[(st.name, sym)], st, sym, sig, bar_ts, df))

            # 5. entries
            if row.state != "RUNNING" or not candidates:
                return
            for score, st, sym, sig, bar_ts, df in sorted(candidates, key=lambda c: -c[0]):
                equity, free = broker.equity(risk)
                block = risk.entry_block_reason(equity, now)
                if block:
                    if block != self.last_block:
                        event(s, "INFO", f"Entry blocked: {block}")
                        self.last_block = block
                    return
                self.last_block = None
                if risk.symbol_busy(sym):
                    continue
                q = quote(self.ex, sym)
                if q["spread_pct"] > settings.max_spread_pct:
                    event(s, "INFO", f"Skip {sym} [{st.name}]: spread {q['spread_pct']:.4%}")
                    continue
                vetoed, note = ai.veto({"symbol": sym, "strategy": st.name, "quote": q,
                                        "last_bars": df.tail(5).to_dict("records"),
                                        "selector_score": score})
                if vetoed:
                    event(s, "INFO", f"AI veto {sym} [{st.name}]: {note}")
                    continue
                notional = risk.position_notional(float(sig["stop_dist"]) / q["ask"], equity, free)
                if notional < 5.5:
                    event(s, "INFO", f"Skip {sym} [{st.name}]: size {notional:.2f} below exchange minimum")
                    continue
                p = broker.open(sym, st, sig, bar_ts, notional, q)
                if p:
                    verb = "SHORT" if p.side == "short" else ("LONG" if settings.market == "futures" else "BUY")
                    self.notify(f"{verb} {sym} [{st.name}] {settings.trading_mode}/{settings.market}: "
                                f"{notional:.2f} {settings.quote_currency} @ {p.entry_price:.6g}, "
                                f"stop {p.stop_price:.6g}, target {p.target_price:.6g}")

    def maybe_send_report(self, s, now):
        try:
            if report_due(s, now):
                self.notify(weekly_report(s, now, exchange=self.ex))
                mark_sent(s, now)
        except Exception:  # noqa: BLE001
            log.exception("Weekly report failed")

    # ------------------------------------------------------------------------
    def handle_error(self, exc: Exception):
        transient = isinstance(exc, TRANSIENT_ERRORS + (StaleDataError,))
        fatal = isinstance(exc, FATAL_ERRORS) or (settings.uses_exchange_orders and not transient)
        if fatal:
            log.exception("Fatal error, halting entries")
            try:
                with self.Session() as s:
                    set_state(s, "HALTED", f"{type(exc).__name__}: {exc}")
                    event(s, "CRITICAL", f"HALTED: {type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                log.exception("Could not persist HALTED state")
            self.notify(f"CRITICAL: bot HALTED (exits still managed): {exc}")
            return
        self.errors += 1
        log.warning("Transient error #%d: %s", self.errors, exc)
        if self.errors == settings.max_consecutive_errors_before_alert:
            self.notify(f"WARNING: {self.errors} consecutive errors, still retrying. Last: {exc}")

    def run_forever(self):
        while True:
            try:
                self.startup()
                break
            except Exception as exc:  # noqa: BLE001
                self.handle_error(exc)
                if isinstance(exc, FATAL_ERRORS):
                    break      # HALTED; keep cycling so open positions are still managed
                time.sleep(30)
        while True:
            try:
                self.cycle()
                self.errors = 0
            except Exception as exc:  # noqa: BLE001
                self.handle_error(exc)
            time.sleep(min(settings.scan_interval_seconds * (1 + min(self.errors, 5)), 300))


def _load_markets(ex):
    for attempt in range(10):
        try:
            return ex.load_markets()
        except TRANSIENT_ERRORS as exc:
            log.warning("load_markets failed (%s), retrying", exc)
            time.sleep(10 * (attempt + 1))
    return ex.load_markets()


def run():
    init_db()
    lock = acquire_worker_lock()  # noqa: F841 - held for process lifetime
    data_ex = make_exchange()
    _load_markets(data_ex)
    ex = data_ex
    if settings.uses_exchange_orders:
        ex = make_order_exchange()
        _load_markets(ex)
        if settings.trading_mode == "testnet" and not is_sandbox(ex):
            raise RuntimeError("Testnet mode but order client is not pointed at the testnet; refusing to start")
        if settings.trading_mode == "live" and is_sandbox(ex):
            raise RuntimeError("Live mode but order client points at the testnet; refusing to start")
        orders_url = next(v for k, v in ex.urls["api"].items() if "rivate" in k and isinstance(v, str))
        log.warning("%s %s MODE: orders go to %s; signals use mainnet data.", settings.trading_mode.upper(),
                    settings.market.upper(), orders_url)

    if settings.trading_mode == "live":
        log.critical("LIVE MODE: all activation gates passed. Starting PAUSED until /resume.")
    Bot(ex, data_exchange=data_ex).run_forever()


if __name__ == "__main__":
    run()
