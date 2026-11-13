import os
import sys

# Must be set before any app import: isolated in-memory DB, deterministic settings, paper mode.
os.environ.update({
    "DATABASE_URL": "sqlite+pysqlite:///:memory:", "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false",
    "LIVE_ARMED": "false", "LIVE_TRADING_ACK": "", "FEE_RATE": "0.001", "SLIPPAGE_RATE": "0.0002",
    "STARTING_EQUITY": "1000", "RISK_PER_TRADE": "0.005", "MAX_OPEN_POSITIONS": "1",
    "MAX_CONSECUTIVE_LOSSES": "3", "COOLDOWN_MINUTES": "240", "DAILY_LOSS_LIMIT": "0.02",
    "WEEKLY_LOSS_LIMIT": "0.05", "MAX_TRADES_PER_DAY": "8", "AI_VETO_ENABLED": "false",
    "TELEGRAM_BOT_TOKEN": "", "OPENAI_API_KEY": "", "API_TOKEN": "test-token", "MAX_SPREAD_PCT": "0.001",
    "ENABLED_STRATEGIES": "", "SYMBOLS": "BTC/USDT", "BINANCE_TESTNET": "false",
    "BINANCE_API_KEY": "", "BINANCE_API_SECRET": "", "BINANCE_TESTNET_API_KEY": "", "BINANCE_TESTNET_API_SECRET": "",
    "BINANCE_FUTURES_TESTNET_API_KEY": "", "BINANCE_FUTURES_TESTNET_API_SECRET": "", "MARKET": "spot",
    "SELECTION_MODE": "auto",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture()
def session():
    init_db()
    with SessionLocal() as s:
        yield s
    Base.metadata.drop_all(bind=engine)


def make_ohlcv(n=600, seed=1, drift=0.0, start_ts=1_700_000_000_000, step_ms=3_600_000, price=100.0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.01, n)
    close = price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[price], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.005, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.005, n))
    vol = rng.uniform(50, 150, n)
    return pd.DataFrame({"ts": start_ts + np.arange(n) * step_ms, "open": open_, "high": high,
                         "low": low, "close": close, "volume": vol})


class FakeExchange:
    """Minimal ccxt-like double. Records every order call."""

    def __init__(self, bid=100.0, ask=100.02, base="BTC", quote="USDT"):
        self.bid, self.ask = bid, ask
        self.base, self.quote = base, quote
        self.balances = {quote: 1000.0, base: 0.0}
        self.calls = []
        self.orders = {}
        self.oco_status = "EXECUTING"
        self.oco_fail = False
        self.oco_filled_leg = None
        self.ohlcv = {}

    # market data
    def fetch_ticker(self, symbol):
        return {"bid": self.bid, "ask": self.ask, "last": self.bid}

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=100):
        df = self.ohlcv[(symbol, timeframe)]
        if since is not None:
            df = df[df.ts >= since]
        return df.head(limit).values.tolist() if since is not None else df.tail(limit).values.tolist()

    def market(self, symbol):
        return {"id": symbol.replace("/", ""), "base": self.base, "quote": self.quote,
                "limits": {"cost": {"min": 5.0}}}

    def amount_to_precision(self, symbol, x):
        return f"{float(x):.5f}"

    def price_to_precision(self, symbol, x):
        return f"{float(x):.2f}"

    def cost_to_precision(self, symbol, x):
        return f"{float(x):.2f}"

    def fetch_balance(self):
        return {"free": dict(self.balances), "total": dict(self.balances)}

    # trading
    def create_market_buy_order_with_cost(self, symbol, cost):
        qty = cost / self.ask
        fee = qty * 0.001
        self.balances[self.quote] -= cost
        self.balances[self.base] += qty - fee
        o = {"id": f"b{len(self.orders)}", "status": "closed", "filled": qty, "average": self.ask, "cost": cost,
             "fees": [{"currency": self.base, "cost": fee}], "type": "market"}
        self.orders[o["id"]] = o
        self.calls.append(("buy", cost))
        return o

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        amount = float(amount)
        self.balances[self.base] -= amount
        proceeds = amount * self.bid
        self.balances[self.quote] += proceeds * 0.999
        o = {"id": f"s{len(self.orders)}", "status": "closed", "filled": amount, "average": self.bid,
             "cost": proceeds, "fees": [{"currency": self.quote, "cost": proceeds * 0.001}], "type": type_}
        self.orders[o["id"]] = o
        self.calls.append(("sell", type_, amount))
        return o

    def fetch_order(self, oid, symbol):
        return self.orders[oid]

    def private_post_orderlist_oco(self, params):
        self.calls.append(("oco", params))
        if self.oco_fail:
            raise RuntimeError("OCO rejected")
        return {"orderListId": 77}

    def private_get_orderlist(self, params):
        orders = []
        if self.oco_filled_leg:
            orders = [{"orderId": self.oco_filled_leg}]
        return {"listOrderStatus": self.oco_status, "orders": orders}

    def private_delete_orderlist(self, params):
        self.calls.append(("cancel_oco", params))
        return {}
