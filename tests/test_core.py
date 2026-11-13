import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from conftest import FakeExchange, make_ohlcv

from app.backtest import SimTrade, eligible, metrics, run_backtest, simulate_portfolio, walk_forward
from app.config import Settings, settings
from app.models import Position
from app.risk import RiskManager
from app.strategies import Strategy, all_strategies


# ---------------------------------------------------------------- config
def test_defaults_are_paper():
    s = Settings(_env_file=None)
    assert s.trading_mode == "paper" and not s.enable_live_trading and not s.live_armed


@pytest.mark.parametrize("kw", [
    {},  # nothing
    {"enable_live_trading": True, "live_armed": True},  # no ack
    {"enable_live_trading": True, "live_armed": True, "live_trading_ack": "I_ACCEPT_LIVE_TRADING_RISK"},  # no keys
    {"enable_live_trading": True, "live_armed": True, "live_trading_ack": "I_ACCEPT_LIVE_TRADING_RISK",
     "binance_api_key": "k", "binance_api_secret": "s", "live_max_quote_allocation": 500},  # over cap
])
def test_live_gates(kw):
    with pytest.raises(Exception):
        Settings(_env_file=None, trading_mode="live", **kw)


def test_risk_bounds_and_ai_requires_model():
    with pytest.raises(Exception):
        Settings(_env_file=None, risk_per_trade=0.1)
    with pytest.raises(Exception):
        Settings(_env_file=None, ai_veto_enabled=True)


# ---------------------------------------------------------------- strategies
@pytest.mark.parametrize("st", all_strategies(), ids=lambda s: s.name)
def test_strategies_have_no_lookahead(st):
    df = make_ohlcv(700, seed=3, drift=0.0005)
    full = st.signals(df)
    for k in (400, 523, 650, 700):
        part = st.signals(df.iloc[:k].reset_index(drop=True))
        a, b = part.iloc[-1], full.iloc[k - 1]
        assert bool(a.entry) == bool(b.entry) and bool(a.exit) == bool(b.exit)
        if a.entry:
            assert a.stop_dist == pytest.approx(b.stop_dist) and a.target_dist == pytest.approx(b.target_dist)


def test_strategy_names_unique():
    names = [s.name for s in all_strategies()]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------- backtester
class OneShot(Strategy):
    def __init__(self, at, stop=2.0, target=3.0, hold=5, exit_at=None):
        super().__init__(name="oneshot", timeframe="1h", warmup=0, max_hold_bars=hold)
        self.at, self.stop, self.target, self.exit_at = at, stop, target, exit_at

    def compute(self, df):
        e = pd.Series(False, index=df.index)
        e.iloc[self.at] = True
        x = pd.Series(False, index=df.index)
        if self.exit_at is not None:
            x.iloc[self.exit_at] = True
        return pd.DataFrame({"entry": e, "stop_dist": self.stop, "target_dist": self.target, "exit": x})


def flat(n=20, price=100.0):
    return pd.DataFrame({"ts": np.arange(n) * 3_600_000, "open": price, "high": price + 0.5,
                         "low": price - 0.5, "close": price, "volume": 1.0})


def test_target_fill_and_costs():
    df = flat()
    df.loc[3, "high"] = 104
    t = run_backtest(OneShot(1), df, "X", fee=0.001, slip=0.0002)[0]
    e = 100 * 1.0002
    assert t.reason == "target" and t.entry == pytest.approx(e) and t.exit == pytest.approx(e + 3)
    assert t.ret == pytest.approx((e + 3) / e - 1 - 0.001 * (1 + (e + 3) / e))


def test_stop_checked_before_target_and_gap_fill():
    df = flat()
    df.loc[3, ["high", "low"]] = [104, 97]           # both touched -> stop wins
    assert run_backtest(OneShot(1), df, "X", 0, 0)[0].reason == "stop"
    df = flat()
    df.loc[3, ["open", "low", "close"]] = [90, 89, 90]  # gap below stop -> fill at open
    t = run_backtest(OneShot(1), df, "X", 0, 0)[0]
    assert t.reason == "stop" and t.exit == pytest.approx(90)


def test_time_and_signal_exit():
    t = run_backtest(OneShot(1, hold=4), flat(), "X", 0, 0)[0]
    assert t.reason == "time" and t.bars == 4
    t = run_backtest(OneShot(1, exit_at=3), flat(), "X", 0, 0)[0]
    assert t.reason == "signal" and t.bars == 2


def test_open_trade_at_end_is_not_counted():
    assert run_backtest(OneShot(15, hold=50), flat(20), "X", 0, 0) == []


def _t(ret, entry_day, exit_day, strat="s", sym="X"):
    d = 86_400_000
    return SimTrade(strat, sym, entry_day * d, exit_day * d, 100, 100 * (1 + ret), ret, 0.02, "x", 1)


def test_metrics_and_eligibility():
    m = metrics([_t(0.02, i, i) for i in range(10)] + [_t(-0.01, i, i) for i in range(5)])
    assert m["n"] == 15 and m["profit_factor"] == pytest.approx(4.0) and m["win_rate"] == pytest.approx(66.7)
    assert eligible(m, min_trades=12)
    assert not eligible(m, min_trades=20)
    assert not eligible(metrics([_t(-0.01, i, i) for i in range(20)]))


def test_walk_forward_only_uses_closed_past_trades():
    # Winning in-sample trades that CLOSE after the decision point must not be used for selection.
    trades = [_t(0.03, d, 200) for d in range(1, 59)] + [_t(-0.01, 61, 62)]
    wf = walk_forward(trades, 0, 70 * 86_400_000, step_days=7)
    assert wf["trades"] == 0


def test_portfolio_respects_max_open_and_min_notional():
    trades = [_t(0.01, 1, 5, sym="A"), _t(0.01, 2, 3, sym="B"), _t(0.01, 6, 7, sym="B")]
    p = simulate_portfolio(trades, equity0=1000, max_open=1)
    assert p["trades"] == 2 and p["final_equity"] > 1000
    assert simulate_portfolio(trades, equity0=10, max_open=1)["trades"] == 0   # below $5 min notional


# ---------------------------------------------------------------- risk
def _closed(s, pnl, minutes_ago, mode="paper"):
    now = datetime.now(timezone.utc)
    s.add(Position(symbol="BTC/USDT", strategy="s", timeframe="1h", mode=mode, status="CLOSED", entry_price=1,
                   quantity=1, entry_cost=100, stop_price=0.9, target_price=1.1, pnl=pnl, pnl_pct=pnl,
                   opened_at=now - timedelta(minutes=minutes_ago + 60), closed_at=now - timedelta(minutes=minutes_ago)))
    s.commit()


def test_consecutive_losses_cooldown_expires(session):
    r = RiskManager(session)
    for m in (30, 20, 10):
        _closed(session, -1, m)
    assert "consecutive" in r.entry_block_reason(1000)
    later = datetime.now(timezone.utc) + timedelta(minutes=settings.cooldown_minutes)
    # the losses happened "yesterday" relative to `later` only if past midnight; daily loss is 3 < 20 anyway
    assert r.entry_block_reason(1000, later) is None


def test_daily_loss_limit(session):
    _closed(session, -25, 5)
    assert "daily" in RiskManager(session).entry_block_reason(1000)


def test_position_sizing(session):
    r = RiskManager(session)
    n = r.position_notional(stop_frac=0.02, equity=1000, free_quote=1000)
    assert n == pytest.approx(1000 * 0.005 / (0.02 + 2 * 0.0012))
    assert r.position_notional(0.0001, 1000, 1000) == pytest.approx(1000 * settings.max_position_fraction)
    assert r.position_notional(0.02, 1000, 50) == pytest.approx(50 * 0.995)


# ---------------------------------------------------------------- paper broker parity with backtester
def test_paper_broker_matches_backtest_math(session):
    from app.execution import PaperBroker
    ex = FakeExchange(bid=100.0, ask=100.0)
    st = OneShot(1)
    st.timeframe = "1h"
    b = PaperBroker(ex, session)
    p = b.open("BTC/USDT", st, {"stop_dist": 2.0, "target_dist": 3.0}, int(time.time() * 1000), 500.0,
               {"ask": 100.0, "bid": 100.0})
    e = 100 * (1 + settings.slippage_rate)
    assert p.entry_price == pytest.approx(e)
    assert not b.check_exits(p, {"bid": 101.0})
    assert b.check_exits(p, {"bid": 104.0}) and p.exit_reason == "target"
    x = e + 3
    assert p.pnl == pytest.approx(500 * (x / e - 1 - settings.fee_rate * (1 + x / e)))


# ---------------------------------------------------------------- testnet mode
def test_testnet_requires_testnet_keys_not_mainnet_keys():
    with pytest.raises(Exception):
        Settings(_env_file=None, trading_mode="testnet", binance_api_key="k", binance_api_secret="s")
    s = Settings(_env_file=None, trading_mode="testnet", binance_testnet_api_key="tk", binance_testnet_api_secret="ts")
    assert s.uses_exchange_orders and s.order_api_credentials == ("tk", "ts")


def test_live_rejects_testnet_flag():
    with pytest.raises(Exception):
        Settings(_env_file=None, trading_mode="live", enable_live_trading=True, live_armed=True,
                 live_trading_ack="I_ACCEPT_LIVE_TRADING_RISK", binance_api_key="k", binance_api_secret="s",
                 binance_testnet=True)


def test_order_client_points_at_testnet_and_data_client_at_mainnet(monkeypatch):
    from app.exchange import is_sandbox, make_exchange, make_order_exchange
    monkeypatch.setattr(settings, "trading_mode", "testnet")
    monkeypatch.setattr(settings, "binance_testnet_api_key", "tk")
    monkeypatch.setattr(settings, "binance_testnet_api_secret", "ts")
    ex = make_order_exchange()
    assert is_sandbox(ex) and ex.apiKey == "tk"
    assert not is_sandbox(make_exchange())
