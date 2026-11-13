"""Brokers. PaperBroker mirrors the backtester's fill/cost model; LiveBroker trades Binance Spot.

LiveBroker flow
  open   : market BUY by quote cost (quoteOrderQty) -> record position immediately -> fee-adjusted
           qty -> exchange-hosted OCO (LIMIT_MAKER target + STOP_LOSS_LIMIT stop). If protection
           cannot be placed after retries -> emergency market SELL -> FatalError (bot halts).
  manage : poll the OCO; when ALL_DONE, record the filled leg as the exit.
  close  : (signal/time exit) cancel OCO -> market SELL free balance.
  restart: reconcile() re-checks every OPEN position and re-protects if needed.
"""
import logging
import time
from datetime import datetime, timezone

import ccxt

from app.config import settings
from app.exchange import TF_MS
from app.models import Event, Position, aware

log = logging.getLogger(__name__)

STOP_LIMIT_BUFFER = 0.005   # stop-limit price this far below the trigger so it actually fills


class FatalError(RuntimeError):
    """Unrecoverable condition: the worker must HALT new entries."""


def event(session, level: str, message: str):
    session.add(Event(level=level, message=message[:4000]))
    session.commit()
    log.log(getattr(logging, level, logging.INFO), message)


def _hold_until(bar_ts: int, timeframe: str, max_hold_bars: int) -> datetime:
    close_ms = bar_ts + TF_MS[timeframe]
    return datetime.fromtimestamp((close_ms + max_hold_bars * TF_MS[timeframe]) / 1000, tz=timezone.utc)


def _finish(p: Position, exit_price: float, proceeds: float, reason: str):
    p.status, p.exit_price, p.exit_reason = "CLOSED", exit_price, reason
    p.closed_at = datetime.now(timezone.utc)
    p.pnl = proceeds - p.entry_cost
    p.pnl_pct = p.pnl / p.entry_cost * 100 if p.entry_cost else 0.0


class PaperBroker:
    mode = "paper"

    def __init__(self, exchange, session):
        self.ex, self.s = exchange, session

    def reconcile(self):
        return None

    def equity(self, risk) -> tuple[float, float]:
        eq = risk.paper_equity()
        in_use = sum(p.entry_cost for p in risk.open_positions())
        return eq, max(0.0, eq - in_use)

    def open(self, symbol, strategy, sig, bar_ts: int, notional: float, q: dict) -> Position:
        price = q["ask"] * (1 + settings.slippage_rate)
        p = Position(symbol=symbol, strategy=strategy.name, timeframe=strategy.timeframe, mode="paper",
                     entry_price=price, quantity=notional / price, entry_cost=notional * (1 + settings.fee_rate),
                     stop_price=price - float(sig["stop_dist"]), target_price=price + float(sig["target_dist"]),
                     stop_frac=float(sig["stop_dist"]) / price, signal_bar_ts=int(bar_ts),
                     max_hold_until=_hold_until(bar_ts, strategy.timeframe, strategy.max_hold_bars),
                     notes="paper fill at ask+slippage")
        self.s.add(p)
        self.s.commit()
        event(self.s, "INFO", f"PAPER BUY {symbol} [{strategy.name}] qty={p.quantity:.8f} @ {price:.4f} "
                              f"stop={p.stop_price:.4f} target={p.target_price:.4f}")
        return p

    def check_exits(self, p: Position, q: dict) -> bool:
        if q["bid"] <= p.stop_price:
            self.close(p, "stop", q, price=min(q["bid"], p.stop_price) * (1 - settings.slippage_rate))
        elif q["bid"] >= p.target_price:
            self.close(p, "target", q, price=p.target_price)
        return p.status == "CLOSED"

    def close(self, p: Position, reason: str, q: dict, price: float | None = None):
        price = price if price is not None else q["bid"] * (1 - settings.slippage_rate)
        _finish(p, price, p.quantity * price * (1 - settings.fee_rate), reason)
        self.s.commit()
        event(self.s, "INFO", f"PAPER SELL {p.symbol} [{p.strategy}] @ {price:.4f} reason={reason} pnl={p.pnl:+.4f}")


class LiveBroker:
    """Real exchange orders. Used for both TRADING_MODE=testnet (fake funds) and live."""

    def __init__(self, exchange, session):
        if not settings.uses_exchange_orders:
            raise FatalError("LiveBroker requires TRADING_MODE=testnet or live")
        self.mode = settings.trading_mode
        self.ex, self.s = exchange, session

    # ---- helpers -------------------------------------------------------------
    def _market(self, symbol):
        return self.ex.market(symbol)

    def _free(self, asset: str) -> float:
        return float((self.ex.fetch_balance().get("free") or {}).get(asset) or 0.0)

    def _wait_closed(self, order_id: str, symbol: str, timeout_s: float = 10.0) -> dict:
        deadline = time.time() + timeout_s
        while True:
            o = self.ex.fetch_order(order_id, symbol)
            if o.get("status") in ("closed", "canceled", "expired", "rejected") or time.time() > deadline:
                return o
            time.sleep(0.5)

    def _fees(self, order: dict, symbol: str) -> tuple[float, float]:
        """(fee paid in base asset, fee paid in quote or quote-equivalent)."""
        m = self._market(symbol)
        base_fee = quote_fee = 0.0
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for f in fees:
            if not f or f.get("cost") is None:
                continue
            if f.get("currency") == m["base"]:
                base_fee += float(f["cost"])
            elif f.get("currency") == m["quote"]:
                quote_fee += float(f["cost"])
            else:   # e.g. BNB: estimate its quote value from the configured rate
                quote_fee += float(order.get("cost") or 0) * settings.fee_rate
        return base_fee, quote_fee

    # ---- account --------------------------------------------------------------
    def equity(self, risk) -> tuple[float, float]:
        bal = self.ex.fetch_balance()
        quote_total = float((bal.get("total") or {}).get(settings.quote_currency) or 0.0)
        quote_free = float((bal.get("free") or {}).get(settings.quote_currency) or 0.0)
        opens = risk.open_positions()
        exposure = sum(p.entry_cost for p in opens)
        value = quote_total + sum(p.quantity * float(self.ex.fetch_ticker(p.symbol)["bid"]) for p in opens)
        cap_equity = settings.live_max_quote_allocation + risk.realized()
        equity = max(0.0, min(value, cap_equity))
        return equity, max(0.0, min(quote_free, cap_equity - exposure))

    # ---- entry -----------------------------------------------------------------
    def open(self, symbol, strategy, sig, bar_ts: int, notional: float, q: dict) -> Position | None:
        m = self._market(symbol)
        min_cost = float(((m.get("limits") or {}).get("cost") or {}).get("min") or 5.0)
        if notional < min_cost * 1.1:
            event(self.s, "INFO", f"Skip {symbol}: notional {notional:.2f} below exchange minimum {min_cost}")
            return None
        order = self.ex.create_market_buy_order_with_cost(symbol, float(self.ex.cost_to_precision(symbol, notional)))
        order = self._wait_closed(str(order["id"]), symbol)
        filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            raise FatalError(f"Entry order {order.get('id')} on {symbol} has no confirmed fill; reconcile manually")
        avg = float(order.get("average") or q["ask"])
        base_fee, quote_fee = self._fees(order, symbol)
        p = Position(symbol=symbol, strategy=strategy.name, timeframe=strategy.timeframe, mode=self.mode,
                     entry_price=avg, quantity=filled - base_fee,
                     entry_cost=float(order.get("cost") or filled * avg) + quote_fee,
                     stop_price=avg - float(sig["stop_dist"]), target_price=avg + float(sig["target_dist"]),
                     stop_frac=float(sig["stop_dist"]) / avg, signal_bar_ts=int(bar_ts),
                     max_hold_until=_hold_until(bar_ts, strategy.timeframe, strategy.max_hold_bars),
                     entry_order_id=str(order["id"]))
        self.s.add(p)
        self.s.commit()   # persist BEFORE protection so a crash can never lose track of the fill
        event(self.s, "INFO", f"{self.mode.upper()} BUY {symbol} [{strategy.name}] qty={p.quantity} avg={avg} cost={p.entry_cost:.4f}")
        try:
            self.protect(p, q)
        except Exception as exc:
            event(self.s, "CRITICAL", f"PROTECTION FAILED {symbol}: {exc}. Emergency exit.")
            self.emergency_exit(p, "protection_failed")
            raise FatalError(f"Protective OCO failed for {symbol}; position flattened, bot halted") from exc
        return p

    def protect(self, p: Position, q: dict | None = None, attempts: int = 3):
        m = self._market(p.symbol)
        qty = float(self.ex.amount_to_precision(p.symbol, min(p.quantity, self._free(m["base"]))))
        if qty <= 0:
            raise FatalError(f"No free {m['base']} balance to protect position {p.id}")
        p.quantity = qty
        bid = (q or {}).get("bid") or float(self.ex.fetch_ticker(p.symbol)["bid"])
        if bid <= p.stop_price or bid >= p.target_price:   # already through a level: just exit now
            self.close(p, "stop" if bid <= p.stop_price else "target", {"bid": bid})
            return
        params = {
            "symbol": m["id"], "side": "SELL", "quantity": self.ex.amount_to_precision(p.symbol, qty),
            "aboveType": "LIMIT_MAKER", "abovePrice": self.ex.price_to_precision(p.symbol, p.target_price),
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": self.ex.price_to_precision(p.symbol, p.stop_price),
            "belowPrice": self.ex.price_to_precision(p.symbol, p.stop_price * (1 - STOP_LIMIT_BUFFER)),
            "belowTimeInForce": "GTC",
        }
        last = None
        for i in range(attempts):
            try:
                resp = self.ex.private_post_orderlist_oco(params)
                p.protection_id = str(resp["orderListId"])
                self.s.commit()
                event(self.s, "INFO", f"OCO placed {p.symbol} list={p.protection_id} stop={params['belowStopPrice']} "
                                      f"target={params['abovePrice']}")
                return
            except Exception as exc:  # noqa: BLE001 - retried then escalated
                last = exc
                time.sleep(1 + i)
        raise last

    # ---- management --------------------------------------------------------------
    def check_exits(self, p: Position, q: dict) -> bool:
        if not p.protection_id:
            self.protect(p, q)
            return p.status == "CLOSED"
        st = self.ex.private_get_orderlist({"orderListId": p.protection_id})
        if st.get("listOrderStatus") != "ALL_DONE":
            return False
        fill = None
        for o in st.get("orders", []):
            od = self.ex.fetch_order(str(o["orderId"]), p.symbol)
            if float(od.get("filled") or 0) > 0:
                fill = od
        if fill is None:   # OCO cancelled outside the bot
            if self._free(self._market(p.symbol)["base"]) >= p.quantity * 0.5:
                event(self.s, "WARNING", f"OCO {p.protection_id} cancelled externally; re-protecting {p.symbol}")
                p.protection_id = ""
                self.protect(p, q)
            else:
                _finish(p, q["bid"], p.quantity * q["bid"] * (1 - settings.fee_rate), "external")
                self.s.commit()
                event(self.s, "WARNING", f"{p.symbol} position closed outside the bot; PnL estimated at bid")
            return p.status == "CLOSED"
        self._record_exit(p, fill, oco_leg_reason(fill))
        return True

    def _record_exit(self, p: Position, order: dict, reason: str):
        _, quote_fee = self._fees(order, p.symbol)
        avg = float(order.get("average") or order.get("price") or 0)
        proceeds = float(order.get("cost") or float(order.get("filled") or 0) * avg) - quote_fee
        _finish(p, avg, proceeds, reason)
        self.s.commit()
        event(self.s, "INFO", f"{self.mode.upper()} SELL {p.symbol} [{p.strategy}] @ {avg} reason={reason} pnl={p.pnl:+.4f}")

    def close(self, p: Position, reason: str, q: dict, price: float | None = None):
        if p.protection_id:
            try:
                self.ex.private_delete_orderlist({"symbol": self._market(p.symbol)["id"],
                                                  "orderListId": p.protection_id})
            except (ccxt.OrderNotFound, ccxt.InvalidOrder, ccxt.ExchangeError):
                if self.check_exits(p, q):   # it already filled
                    return
            p.protection_id = ""
            self.s.commit()
        self.emergency_exit(p, reason)

    def emergency_exit(self, p: Position, reason: str):
        base = self._market(p.symbol)["base"]
        qty = float(self.ex.amount_to_precision(p.symbol, min(p.quantity, self._free(base))))
        if qty <= 0:
            p.status, p.exit_reason = "ERROR", f"{reason}: no balance to sell"
            self.s.commit()
            raise FatalError(f"Cannot exit {p.symbol}: no free {base}")
        order = self._wait_closed(str(self.ex.create_order(p.symbol, "market", "sell", qty)["id"]), p.symbol)
        self._record_exit(p, order, reason)

    def reconcile(self):
        from app.risk import RiskManager
        for p in RiskManager(self.s, self.mode).open_positions():
            try:
                q = {"bid": float(self.ex.fetch_ticker(p.symbol)["bid"])}
                self.check_exits(p, q)
            except Exception as exc:
                event(self.s, "CRITICAL", f"Reconcile failed for position {p.id} {p.symbol}: {exc}")
                raise FatalError(f"Startup reconciliation failed for {p.symbol}") from exc


def oco_leg_reason(order: dict) -> str:
    """ccxt reports BOTH Binance OCO legs as type 'limit', so use the raw Binance type / trigger price."""
    raw = str((order.get("info") or {}).get("type", "")).upper()
    if raw:
        return "stop" if raw.startswith("STOP") else "target"
    return "stop" if (order.get("stopPrice") or order.get("triggerPrice")) else "target"


def make_broker(exchange, session):
    if settings.market == "futures":
        from app.futures_execution import FuturesLiveBroker, FuturesPaperBroker
        return FuturesLiveBroker(exchange, session) if settings.uses_exchange_orders             else FuturesPaperBroker(exchange, session)
    return LiveBroker(exchange, session) if settings.uses_exchange_orders else PaperBroker(exchange, session)


def hold_expired(p: Position, now: datetime | None = None) -> bool:

    now = now or datetime.now(timezone.utc)
    return p.max_hold_until is not None and now >= aware(p.max_hold_until)
