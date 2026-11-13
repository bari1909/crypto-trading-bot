"""Brokers for Binance USDⓈ-M perpetual futures (MARKET=futures): long AND short.

Safety rules (enforced in code, not configurable):
  * leverage is ALWAYS 1x, margin is ISOLATED, one-way position mode
  * one position per symbol; protection = exchange-hosted reduce-only STOP_MARKET + TAKE_PROFIT_MARKET
  * the position row is committed BEFORE protection; if protection fails -> market close -> FatalError (halt)

PnL (both brokers, and identical to the backtester):
  pnl = sign * (exit - entry) * qty - entry_fee - exit_fee + funding
  where sign = +1 long / -1 short, and funding > 0 means received.
  `entry_cost` = entry notional + entry fee, so entry_fee = entry_cost - entry_price * qty.
"""
import logging
import time
from datetime import datetime, timezone

import ccxt

from app.config import settings
from app.execution import FatalError, _hold_until, event
from app.models import Position, aware

log = logging.getLogger(__name__)


def sign(p_or_side) -> int:
    side = p_or_side if isinstance(p_or_side, str) else p_or_side.side
    return -1 if side == "short" else 1


def exit_side(p: Position) -> str:
    return "buy" if p.side == "short" else "sell"


def _ms(dt: datetime) -> int:
    return int(aware(dt).timestamp() * 1000)


def finish_futures(p: Position, exit_price: float, exit_fee: float, funding: float, reason: str):
    entry_fee = max(0.0, p.entry_cost - p.entry_price * p.quantity)
    p.status, p.exit_price, p.exit_reason, p.funding = "CLOSED", exit_price, reason, funding
    p.closed_at = datetime.now(timezone.utc)
    p.pnl = sign(p) * (exit_price - p.entry_price) * p.quantity - entry_fee - exit_fee + funding
    notional = p.entry_price * p.quantity
    p.pnl_pct = p.pnl / notional * 100 if notional else 0.0


def min_notional(ex, symbol: str, price: float) -> float:
    m = ex.market(symbol)
    lim = m.get("limits") or {}
    cost = float((lim.get("cost") or {}).get("min") or 5.0)
    amt = float((lim.get("amount") or {}).get("min") or 0.0)
    return max(cost, amt * price)


def _levels(side: str, price: float, sig) -> tuple[float, float]:
    sd, td = float(sig["stop_dist"]), float(sig["target_dist"])
    return (price - sd, price + td) if side == "long" else (price + sd, price - td)


# =====================================================================================================
class FuturesPaperBroker:
    """Simulated perps: same fill/cost model as the backtester, funding from real funding-rate history."""

    def __init__(self, exchange, session):
        self.ex, self.s = exchange, session
        self.mode = "paper"

    def reconcile(self):
        return None

    def equity(self, risk) -> tuple[float, float]:
        eq = risk.paper_equity()
        in_use = sum(p.entry_price * p.quantity for p in risk.open_positions())   # 1x: margin = notional
        return eq, max(0.0, eq - in_use)

    def open(self, symbol, strategy, sig, bar_ts: int, notional: float, q: dict) -> Position | None:
        side = strategy.side
        price = q["ask"] * (1 + settings.slippage_rate) if side == "long" else q["bid"] * (1 - settings.slippage_rate)
        need = min_notional(self.ex, symbol, price)
        if notional < need * 1.05:
            event(self.s, "INFO", f"Skip {symbol} [{strategy.name}]: size {notional:.2f} < exchange minimum {need:.2f}")
            return None
        stop, target = _levels(side, price, sig)
        p = Position(symbol=symbol, strategy=strategy.name, timeframe=strategy.timeframe, mode="paper",
                     market="futures", side=side, entry_price=price, quantity=notional / price,
                     entry_cost=notional * (1 + settings.fee_rate), stop_price=stop, target_price=target,
                     stop_frac=float(sig["stop_dist"]) / price, signal_bar_ts=int(bar_ts),
                     max_hold_until=_hold_until(bar_ts, strategy.timeframe, strategy.max_hold_bars),
                     notes="paper perp fill, 1x isolated")
        self.s.add(p)
        self.s.commit()
        event(self.s, "INFO", f"PAPER {side.upper()} {symbol} [{strategy.name}] qty={p.quantity:.6g} @ {price:.6g} "
                              f"stop={stop:.6g} target={target:.6g}")
        return p

    def check_exits(self, p: Position, q: dict) -> bool:
        if p.side == "long":
            if q["bid"] <= p.stop_price:
                self.close(p, "stop", q, price=min(q["bid"], p.stop_price) * (1 - settings.slippage_rate))
            elif q["bid"] >= p.target_price:
                self.close(p, "target", q, price=p.target_price)
        else:
            if q["ask"] >= p.stop_price:
                self.close(p, "stop", q, price=max(q["ask"], p.stop_price) * (1 + settings.slippage_rate))
            elif q["ask"] <= p.target_price:
                self.close(p, "target", q, price=p.target_price)
        return p.status == "CLOSED"

    def _funding(self, p: Position) -> float:
        start, now = _ms(p.opened_at), int(time.time() * 1000)
        rows = self.ex.fetch_funding_rate_history(p.symbol, since=start, limit=1000) or []
        rate_sum = sum(float(r["fundingRate"]) for r in rows if start < r["timestamp"] <= now)
        return -sign(p) * rate_sum * p.entry_price * p.quantity     # longs pay positive funding

    def close(self, p: Position, reason: str, q: dict, price: float | None = None):
        if price is None:
            price = q["bid"] * (1 - settings.slippage_rate) if p.side == "long" \
                else q["ask"] * (1 + settings.slippage_rate)
        funding = self._funding(p)           # may raise (network) -> retried next cycle, nothing mutated yet
        finish_futures(p, price, price * p.quantity * settings.fee_rate, funding, reason)
        self.s.commit()
        event(self.s, "INFO", f"PAPER CLOSE {p.side.upper()} {p.symbol} [{p.strategy}] @ {price:.6g} reason={reason} "
                              f"funding={funding:+.4f} pnl={p.pnl:+.4f}")


# =====================================================================================================
class FuturesLiveBroker:
    """Real perps orders (futures testnet / demo / live)."""

    def __init__(self, exchange, session):
        if not settings.uses_exchange_orders or settings.market != "futures":
            raise FatalError("FuturesLiveBroker requires MARKET=futures and TRADING_MODE=testnet|live")
        self.ex, self.s = exchange, session
        self.mode = settings.trading_mode
        self._prepared: set[str] = set()

    # ---- account setup ---------------------------------------------------------------------------
    def _prepare(self, symbol: str):
        if symbol in self._prepared:
            return
        for call in (lambda: self.ex.set_position_mode(False, symbol),          # one-way mode
                     lambda: self.ex.set_margin_mode("isolated", symbol)):
            try:
                call()
            except ccxt.ExchangeError as exc:
                if "No need" not in str(exc) and "-4059" not in str(exc) and "-4046" not in str(exc):
                    raise FatalError(f"Could not configure {symbol}: {exc}") from exc
        resp = self.ex.set_leverage(1, symbol)
        lev = int(float((resp or {}).get("leverage", 1)))
        if lev != 1:
            raise FatalError(f"Leverage on {symbol} is {lev}x, refusing to trade (must be 1x)")
        self._prepared.add(symbol)

    def equity(self, risk) -> tuple[float, float]:
        bal = self.ex.fetch_balance()
        total = float((bal.get("total") or {}).get(settings.quote_currency) or 0.0)
        free = float((bal.get("free") or {}).get(settings.quote_currency) or 0.0)
        exposure = sum(p.entry_price * p.quantity for p in risk.open_positions())
        cap = settings.live_max_quote_allocation + risk.realized()
        return max(0.0, min(total, cap)), max(0.0, min(free, cap - exposure))

    # ---- helpers ------------------------------------------------------------------------------------
    def _fill(self, order_id: str, symbol: str, since_ms: int, fallback_price: float) -> tuple[float, float, float]:
        """(filled qty, average price, fee in quote) from our trades for `order_id`."""
        deadline = time.time() + 10
        while True:
            trades = [t for t in self.ex.fetch_my_trades(symbol, since=since_ms - 60_000)
                      if str(t.get("order")) == str(order_id)]
            if trades or time.time() > deadline:
                break
            time.sleep(0.5)
        if not trades:
            o = self.ex.fetch_order(order_id, symbol)
            qty, avg = float(o.get("filled") or 0), float(o.get("average") or fallback_price)
            return qty, avg, qty * avg * settings.fee_rate
        qty = sum(float(t["amount"]) for t in trades)
        avg = sum(float(t["amount"]) * float(t["price"]) for t in trades) / qty
        fee = sum(float((t.get("fee") or {}).get("cost") or 0) for t in trades)
        return qty, avg, fee

    def _position_size(self, symbol: str) -> float:
        for pos in self.ex.fetch_positions([symbol]) or []:
            if pos.get("symbol") == symbol:
                return abs(float(pos.get("contracts") or 0))
        return 0.0

    def _cancel_protection(self, symbol: str):
        for params in ({"trigger": True}, {}):
            try:
                self.ex.cancel_all_orders(symbol, params)
            except Exception as exc:  # noqa: BLE001 - "no orders" style errors are fine
                log.debug("cancel_all_orders(%s, %s): %s", symbol, params, exc)

    def _funding(self, p: Position) -> float:
        try:
            rows = self.ex.fetch_funding_history(p.symbol, since=_ms(p.opened_at)) or []
            return sum(float(r.get("amount") or 0) for r in rows)
        except Exception as exc:  # noqa: BLE001
            log.warning("Funding history unavailable for %s: %s", p.symbol, exc)
            return 0.0

    # ---- entry ----------------------------------------------------------------------------------------
    def open(self, symbol, strategy, sig, bar_ts: int, notional: float, q: dict) -> Position | None:
        side = strategy.side
        self._prepare(symbol)
        ref = q["ask"] if side == "long" else q["bid"]
        amount = float(self.ex.amount_to_precision(symbol, notional / ref))
        need = min_notional(self.ex, symbol, ref)
        if amount * ref < need * 1.05:
            event(self.s, "INFO", f"Skip {symbol} [{strategy.name}]: size {amount * ref:.2f} < exchange minimum {need:.2f}")
            return None
        t0 = int(time.time() * 1000)
        order = self.ex.create_order(symbol, "market", "buy" if side == "long" else "sell", amount)
        qty, avg, fee = self._fill(str(order["id"]), symbol, t0, ref)
        if qty <= 0:
            raise FatalError(f"Entry order {order.get('id')} on {symbol} has no confirmed fill; reconcile manually")
        stop, target = _levels(side, avg, sig)
        p = Position(symbol=symbol, strategy=strategy.name, timeframe=strategy.timeframe, mode=self.mode,
                     market="futures", side=side, entry_price=avg, quantity=qty, entry_cost=avg * qty + fee,
                     stop_price=stop, target_price=target, stop_frac=float(sig["stop_dist"]) / avg,
                     signal_bar_ts=int(bar_ts), entry_order_id=str(order["id"]),
                     max_hold_until=_hold_until(bar_ts, strategy.timeframe, strategy.max_hold_bars))
        self.s.add(p)
        self.s.commit()    # persist BEFORE protection
        event(self.s, "INFO", f"{self.mode.upper()} {side.upper()} {symbol} [{strategy.name}] qty={qty} avg={avg} fee={fee:.4f}")
        try:
            self.protect(p)
        except Exception as exc:
            event(self.s, "CRITICAL", f"PROTECTION FAILED {symbol}: {exc}. Emergency close.")
            self.emergency_close(p, "protection_failed")
            raise FatalError(f"Protective orders failed for {symbol}; position closed, bot halted") from exc
        return p

    def protect(self, p: Position, attempts: int = 3):
        side, qty = exit_side(p), self.ex.amount_to_precision(p.symbol, p.quantity)
        last = None
        for i in range(attempts):
            try:
                self._cancel_protection(p.symbol)
                sl = self.ex.create_order(p.symbol, "STOP_MARKET", side, qty, None, {
                    "stopPrice": self.ex.price_to_precision(p.symbol, p.stop_price),
                    "reduceOnly": True, "workingType": "MARK_PRICE"})
                tp = self.ex.create_order(p.symbol, "TAKE_PROFIT_MARKET", side, qty, None, {
                    "stopPrice": self.ex.price_to_precision(p.symbol, p.target_price),
                    "reduceOnly": True, "workingType": "MARK_PRICE"})
                p.protection_id = f"{sl['id']},{tp['id']}"
                self.s.commit()
                event(self.s, "INFO", f"Protection placed {p.symbol}: stop {p.stop_price:.6g} / target "
                                      f"{p.target_price:.6g} (reduce-only, ids {p.protection_id})")
                return
            except Exception as exc:  # noqa: BLE001 - retried then escalated
                last = exc
                time.sleep(1 + i)
        raise last

    # ---- management -------------------------------------------------------------------------------------
    def _record_exit(self, p: Position, since_ms: int, reason: str | None = None):
        side = exit_side(p)
        trades = [t for t in self.ex.fetch_my_trades(p.symbol, since=since_ms) if t.get("side") == side]
        if trades:
            qty = sum(float(t["amount"]) for t in trades)
            avg = sum(float(t["amount"]) * float(t["price"]) for t in trades) / qty
            fee = sum(float((t.get("fee") or {}).get("cost") or 0) for t in trades)
        else:   # should not happen; estimate from the current price
            avg = float(self.ex.fetch_ticker(p.symbol)["last"])
            fee = avg * p.quantity * settings.fee_rate
        if reason is None:
            loss = (avg < p.entry_price) if p.side == "long" else (avg > p.entry_price)
            reason = "stop" if loss else "target"
        self._cancel_protection(p.symbol)
        finish_futures(p, avg, fee, self._funding(p), reason)
        self.s.commit()
        event(self.s, "INFO", f"{self.mode.upper()} CLOSE {p.side.upper()} {p.symbol} [{p.strategy}] @ {avg:.6g} "
                              f"reason={reason} funding={p.funding:+.4f} pnl={p.pnl:+.4f}")

    def check_exits(self, p: Position, q: dict) -> bool:
        if self._position_size(p.symbol) > 0:
            if not p.protection_id:
                self.protect(p)
            return False
        self._record_exit(p, _ms(p.opened_at) + 1)     # flat: a protective order filled
        return True

    def close(self, p: Position, reason: str, q: dict, price: float | None = None):
        self._cancel_protection(p.symbol)
        p.protection_id = ""
        self.s.commit()
        if self._position_size(p.symbol) <= 0:          # already closed by a protective order
            self._record_exit(p, _ms(p.opened_at) + 1)
            return
        self.emergency_close(p, reason)

    def emergency_close(self, p: Position, reason: str):
        t0 = int(time.time() * 1000)
        size = self._position_size(p.symbol)
        if size <= 0:
            p.status, p.exit_reason = "ERROR", f"{reason}: no position on exchange"
            self.s.commit()
            raise FatalError(f"Cannot close {p.symbol}: exchange shows no position")
        self.ex.create_order(p.symbol, "market", exit_side(p), self.ex.amount_to_precision(p.symbol, size),
                             None, {"reduceOnly": True})
        self._record_exit(p, t0 - 1000, reason)

    def reconcile(self):
        from app.risk import RiskManager
        for p in RiskManager(self.s, self.mode).open_positions():
            try:
                self._prepared.discard(p.symbol)
                if self._position_size(p.symbol) <= 0:
                    self._record_exit(p, _ms(p.opened_at) + 1)
                    continue
                open_orders = []
                try:
                    open_orders = self.ex.fetch_open_orders(p.symbol, None, None, {"trigger": True})
                except Exception:  # noqa: BLE001
                    open_orders = self.ex.fetch_open_orders(p.symbol)
                if len(open_orders) < 2:
                    event(self.s, "WARNING", f"{p.symbol}: protection incomplete after restart; re-placing")
                    self.protect(p)
            except FatalError:
                raise
            except Exception as exc:
                event(self.s, "CRITICAL", f"Reconcile failed for {p.symbol}: {exc}")
                raise FatalError(f"Startup reconciliation failed for {p.symbol}") from exc
