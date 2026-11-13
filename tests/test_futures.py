import time

import ccxt
import pandas as pd
import pytest
from conftest import make_ohlcv

from app.config import Settings, settings
from app.execution import FatalError
from app.futures_execution import FuturesLiveBroker, FuturesPaperBroker
from app.models import Position
from app.strategies import Strategy

SYM = "SOL/USDT:USDT"
H = 3_600_000


@pytest.fixture()
def futures(monkeypatch):
    monkeypatch.setattr(settings, "market", "futures")
    monkeypatch.setattr(settings, "fee_rate", 0.0005)
    yield


@pytest.fixture()
def live_futures(futures, monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "testnet")
    yield


class ShortAlways(Strategy):
    def __init__(self):
        super().__init__(name="short_always_1h", timeframe="1h", warmup=10, max_hold_bars=5, side="short")

    def compute(self, df):
        return pd.DataFrame({"entry": True, "stop_dist": df.close * 0.02, "target_dist": df.close * 0.04,
                             "exit": False}, index=df.index)


SIG = {"stop_dist": 2.0, "target_dist": 4.0}


class FakeFutures:
    """ccxt binanceusdm-like double with a one-way position book."""

    def __init__(self, bid=100.0, ask=100.02, leverage=1):
        self.bid, self.ask, self.leverage = bid, ask, leverage
        self.calls, self.trades, self.orders = [], [], {}
        self.position = 0.0                 # signed contracts
        self.protect_fail = False
        self.funding_rates = [0.0001, 0.0001]
        self.funding_income = 0.0
        self.ohlcv = {}

    # data
    def market(self, symbol):
        return {"id": symbol, "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.01}}}

    def fetch_ticker(self, symbol):
        return {"bid": self.bid, "ask": self.ask, "last": (self.bid + self.ask) / 2}

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=100):
        return self.ohlcv[(symbol, timeframe)].tail(limit).values.tolist()

    def amount_to_precision(self, symbol, x):
        return f"{float(x):.3f}"

    def price_to_precision(self, symbol, x):
        return f"{float(x):.4f}"

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        start = since or 0
        return [{"timestamp": start + 1, "fundingRate": r} for r in self.funding_rates]

    # account
    def set_position_mode(self, hedged, symbol=None):
        self.calls.append(("position_mode", hedged))

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("margin", mode))
        raise ccxt.ExchangeError("binanceusdm {\"code\":-4046,\"msg\":\"No need to change margin type.\"}")

    def set_leverage(self, lev, symbol):
        self.calls.append(("leverage", lev))
        return {"leverage": self.leverage}

    def fetch_balance(self):
        return {"total": {"USDT": 1000.0}, "free": {"USDT": 1000.0}}

    def fetch_positions(self, symbols):
        return [{"symbol": SYM, "contracts": abs(self.position)}]

    def fetch_funding_history(self, symbol, since=None):
        return [{"amount": self.funding_income}]

    # orders
    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        amount = float(amount)
        self.calls.append(("order", type_, side, amount, dict(params)))
        oid = f"o{len(self.calls)}"
        if type_ in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            if self.protect_fail:
                raise ccxt.ExchangeError("Order type not supported")
            return {"id": oid}
        if params.get("reduceOnly"):
            amount = min(amount, abs(self.position))
        px = self.ask if side == "buy" else self.bid
        self.position += amount if side == "buy" else -amount
        self.trades.append({"order": oid, "side": side, "amount": amount, "price": px,
                            "fee": {"cost": amount * px * 0.0005}, "timestamp": int(time.time() * 1000)})
        return {"id": oid}

    def fill_protective(self, side, price):
        """Simulate the exchange triggering a protective order."""
        amount = abs(self.position)
        self.position = 0.0
        self.trades.append({"order": "algo", "side": side, "amount": amount, "price": price,
                            "fee": {"cost": amount * price * 0.0005}, "timestamp": int(time.time() * 1000)})

    def fetch_my_trades(self, symbol, since=None):
        return [t for t in self.trades if since is None or t["timestamp"] >= since]

    def cancel_all_orders(self, symbol, params=None):
        self.calls.append(("cancel_all", dict(params or {})))

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        return [{"id": "sl"}, {"id": "tp"}]


# ---------------------------------------------------------------- config / registry
def test_futures_config_switches_costs_symbols_and_selection():
    s = Settings(_env_file=None, market="futures", symbols="BTC/USDT,SOL/USDT")
    assert s.fee_rate == 0.0005
    assert s.trade_symbols == ["BTC/USDT:USDT", "SOL/USDT:USDT"]
    assert s.effective_selection_mode == "always_on"
    assert Settings(_env_file=None).effective_selection_mode == "selector"
    with pytest.raises(Exception):
        Settings(_env_file=None, market="futures", trading_mode="testnet",
                 binance_testnet_api_key="spot-k", binance_testnet_api_secret="spot-s")   # spot keys don't count


def test_shorts_only_reach_the_bot_in_futures_mode(monkeypatch):
    from app.strategies import active_strategies
    monkeypatch.setattr(settings, "enabled_strategies", "")
    assert all(s.side == "long" for s in active_strategies())
    monkeypatch.setattr(settings, "market", "futures")
    monkeypatch.setattr(settings, "futures_strategies", "trend_follow_4h,turtle_breakout_4h,trend_follow_short_4h")
    names = {s.name: s.side for s in active_strategies()}
    assert names == {"trend_follow_4h": "long", "turtle_breakout_4h": "long", "trend_follow_short_4h": "short"}


def test_order_client_futures_test_venues(monkeypatch):
    from app.exchange import is_sandbox, make_exchange, make_order_exchange
    monkeypatch.setattr(settings, "market", "futures")
    monkeypatch.setattr(settings, "trading_mode", "testnet")
    monkeypatch.setattr(settings, "binance_futures_testnet_api_key", "fk")
    monkeypatch.setattr(settings, "binance_futures_testnet_api_secret", "fs")
    for venue, host in (("testnet", "testnet.binancefuture.com"), ("demo", "demo-fapi.binance.com")):
        monkeypatch.setattr(settings, "futures_testnet_venue", venue)
        ex = make_order_exchange()
        assert isinstance(ex, ccxt.binanceusdm) and ex.apiKey == "fk" and is_sandbox(ex)
        assert host in ex.urls["api"]["fapiPrivate"]
    data = make_exchange()
    assert isinstance(data, ccxt.binanceusdm) and not is_sandbox(data)


# ---------------------------------------------------------------- paper
def test_paper_short_stop_loss_with_funding(session, futures):
    ex = FakeFutures(bid=100.0, ask=100.0)
    b = FuturesPaperBroker(ex, session)
    p = b.open(SYM, ShortAlways(), SIG, int(time.time() * 1000) - H, 500.0, {"bid": 100.0, "ask": 100.0})
    e = 100 * (1 - settings.slippage_rate)
    assert p.side == "short" and p.market == "futures" and p.entry_price == pytest.approx(e)
    assert p.stop_price == pytest.approx(e + 2) and p.target_price == pytest.approx(e - 4)
    assert not b.check_exits(p, {"bid": 100.5, "ask": 100.5})
    assert b.check_exits(p, {"bid": 103.0, "ask": 103.0}) and p.exit_reason == "stop"
    x = 103.0 * (1 + settings.slippage_rate)
    qty = 500.0 / e
    funding = 0.0002 * e * qty                          # short RECEIVES positive funding
    expect = -(x - e) * qty - 0.0005 * e * qty - 0.0005 * x * qty + funding
    assert p.funding == pytest.approx(funding) and p.pnl == pytest.approx(expect)


def test_paper_matches_backtest_return(session, futures):
    from app.backtest import run_backtest
    df = pd.DataFrame({"ts": [i * H for i in range(4)], "open": 100.0, "high": 100.5, "low": 100.0, "close": 100.0,
                       "volume": 1.0})
    df.loc[2, "low"] = 95.0
    st = ShortAlways()
    st.warmup = 0
    t = run_backtest(st, df.iloc[:3], SYM, fee=0.0005, slip=0.0002)[0]
    ex = FakeFutures(bid=100.0, ask=100.0)
    ex.funding_rates = []
    b = FuturesPaperBroker(ex, session)
    p = b.open(SYM, st, {"stop_dist": 2.0, "target_dist": 4.0}, 0, 100.0, {"bid": 100.0, "ask": 100.0})
    b.check_exits(p, {"bid": 95.0, "ask": 95.0})
    assert t.reason == p.exit_reason == "target"
    assert p.pnl / 100.0 == pytest.approx(t.ret, rel=1e-6)


def test_paper_skips_below_exchange_minimum(session, futures):
    p = FuturesPaperBroker(FakeFutures(), session).open(SYM, ShortAlways(), SIG, 0, 3.0, {"bid": 100.0, "ask": 100.02})
    assert p is None and session.query(Position).count() == 0


# ---------------------------------------------------------------- live / testnet
def test_live_short_configures_1x_and_places_reduce_only_protection(session, live_futures):
    ex = FakeFutures(bid=100.0, ask=100.02)
    p = FuturesLiveBroker(ex, session).open(SYM, ShortAlways(), SIG, 0, 50.0, {"bid": 100.0, "ask": 100.02})
    assert ("leverage", 1) in ex.calls and ("margin", "isolated") in ex.calls
    entry = [c for c in ex.calls if c[0] == "order" and c[1] == "market"][0]
    assert entry[2] == "sell" and entry[3] == pytest.approx(0.5)
    sl = [c for c in ex.calls if c[1:2] == ("STOP_MARKET",)][0]
    tp = [c for c in ex.calls if c[1:2] == ("TAKE_PROFIT_MARKET",)][0]
    assert sl[2] == tp[2] == "buy" and sl[4]["reduceOnly"] and tp[4]["reduceOnly"]
    assert float(sl[4]["stopPrice"]) == pytest.approx(102.0) and float(tp[4]["stopPrice"]) == pytest.approx(96.0)
    assert p.status == "OPEN" and p.side == "short" and p.protection_id
    assert p.entry_cost == pytest.approx(0.5 * 100.0 * 1.0005)


def test_live_refuses_if_leverage_not_1x(session, live_futures):
    ex = FakeFutures(leverage=5)
    with pytest.raises(FatalError):
        FuturesLiveBroker(ex, session).open(SYM, ShortAlways(), SIG, 0, 50.0, {"bid": 100.0, "ask": 100.02})
    assert not [c for c in ex.calls if c[0] == "order"]


def test_live_target_fill_recorded_with_fees_and_funding(session, live_futures):
    ex = FakeFutures(bid=100.0, ask=100.0)
    b = FuturesLiveBroker(ex, session)
    p = b.open(SYM, ShortAlways(), SIG, 0, 50.0, {"bid": 100.0, "ask": 100.0})
    assert not b.check_exits(p, {"bid": 99.0, "ask": 99.0})
    ex.funding_income = 0.01
    ex.fill_protective("buy", 96.0)
    assert b.check_exits(p, {"bid": 96.0, "ask": 96.0})
    assert p.exit_reason == "target" and p.funding == pytest.approx(0.01)
    assert p.pnl == pytest.approx(4.0 * 0.5 - 0.5 * 100 * 0.0005 - 0.5 * 96 * 0.0005 + 0.01)
    assert ("cancel_all", {"trigger": True}) in ex.calls


def test_live_protection_failure_closes_and_halts(session, live_futures, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = FakeFutures()
    ex.protect_fail = True
    with pytest.raises(FatalError):
        FuturesLiveBroker(ex, session).open(SYM, ShortAlways(), SIG, 0, 50.0, {"bid": 100.0, "ask": 100.02})
    p = session.query(Position).one()
    assert p.status == "CLOSED" and p.exit_reason == "protection_failed" and ex.position == 0
    close = [c for c in ex.calls if c[0] == "order" and c[1] == "market"][-1]
    assert close[2] == "buy" and close[4] == {"reduceOnly": True}


def test_live_time_exit_cancels_then_closes_reduce_only(session, live_futures):
    ex = FakeFutures()
    b = FuturesLiveBroker(ex, session)
    p = b.open(SYM, ShortAlways(), SIG, 0, 50.0, {"bid": 100.0, "ask": 100.02})
    b.close(p, "time", {"bid": 100.0, "ask": 100.02})
    tp_idx = max(i for i, c in enumerate(ex.calls) if c[1:2] == ("TAKE_PROFIT_MARKET",))
    close_idx = max(i for i, c in enumerate(ex.calls) if c[0] == "order" and c[1] == "market")
    assert any(c[0] == "cancel_all" for c in ex.calls[tp_idx:close_idx])     # protection cancelled first
    assert ex.calls[close_idx][4] == {"reduceOnly": True} and ex.position == 0
    assert p.status == "CLOSED" and p.exit_reason == "time"


# ---------------------------------------------------------------- worker end to end (paper futures)
def test_worker_opens_short_in_futures_paper_mode(session, futures):
    from app.worker import Bot

    class Sel:
        symbols = [SYM]

        def due(self):
            return False

        def eligible_pairs(self):
            return [("short_always_1h", SYM, 1.0)]

        def summary(self):
            return ""

    ex = FakeFutures()
    now = int(time.time() * 1000)
    last_open = now // H * H - H
    ex.ohlcv[(SYM, "1h")] = make_ohlcv(300, start_ts=last_open - 299 * H)
    ex.bid = float(ex.ohlcv[(SYM, "1h")].close.iloc[-1])
    ex.ask = ex.bid * 1.0001
    bot = Bot(ex, selector=Sel(), notify=lambda m: None)
    bot.strategies, bot.timeframes = [ShortAlways()], ["1h"]
    bot.cycle(last_open + H + 1000)
    p = session.query(Position).one()
    assert p.side == "short" and p.market == "futures" and p.stop_price > p.entry_price > p.target_price


def test_quote_falls_back_to_order_book_when_ticker_has_no_bid_ask():
    # Observed on Binance USDⓈ-M mainnet AND demo: fetch_ticker() returns bid=None, ask=None.
    from app.exchange import quote

    class NoBidAsk:
        def fetch_ticker(self, symbol):
            return {"bid": None, "ask": None, "last": 79.9}

        def fetch_order_book(self, symbol, limit=None):
            return {"bids": [[79.85, 3]], "asks": [[79.86, 2]]}

    q = quote(NoBidAsk(), SYM)
    assert q["bid"] == 79.85 and q["ask"] == 79.86 and q["last"] == 79.9


def test_real_futures_quote_works_against_binance():
    """Network test against the public mainnet futures API (skipped offline)."""
    from app.exchange import quote
    try:
        q = quote(ccxt.binanceusdm({"timeout": 10000}), SYM)
    except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
        pytest.skip(f"offline: {exc}")
    assert 0 < q["bid"] < q["ask"] and q["spread_pct"] < 0.01
