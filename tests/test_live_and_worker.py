import time

import pandas as pd
import pytest
from conftest import FakeExchange, make_ohlcv

from app.config import settings
from app.execution import FatalError, LiveBroker
from app.models import BotState, Position
from app.strategies import Strategy

H = 3_600_000


@pytest.fixture(params=["live", "testnet"])
def live(request, monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", request.param)
    yield
    monkeypatch.setattr(settings, "trading_mode", "paper")


class Always(Strategy):
    def __init__(self):
        super().__init__(name="always_1h", timeframe="1h", warmup=10, max_hold_bars=5)

    def compute(self, df):
        return pd.DataFrame({"entry": True, "stop_dist": df.close * 0.02, "target_dist": df.close * 0.04,
                             "exit": False}, index=df.index)


SIG = {"stop_dist": 2.0, "target_dist": 4.0}


# ---------------------------------------------------------------- live broker
def test_live_open_places_fee_adjusted_oco(session, live):
    ex = FakeExchange(bid=100.0, ask=100.0)
    p = LiveBroker(ex, session).open("BTC/USDT", Always(), SIG, int(time.time() * 1000), 50.0, {"ask": 100, "bid": 100})
    assert p.quantity == pytest.approx(0.5 * 0.999, rel=1e-4)        # base fee deducted
    oco = [c for c in ex.calls if c[0] == "oco"][0][1]
    assert oco["side"] == "SELL" and oco["aboveType"] == "LIMIT_MAKER" and oco["belowType"] == "STOP_LOSS_LIMIT"
    assert float(oco["quantity"]) <= ex.balances["BTC"]
    assert float(oco["belowStopPrice"]) == pytest.approx(98.0) and float(oco["abovePrice"]) == pytest.approx(104.0)
    assert float(oco["belowPrice"]) < float(oco["belowStopPrice"])
    assert p.protection_id == "77" and p.status == "OPEN"


def test_live_protection_failure_flattens_and_halts(session, live, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = FakeExchange()
    ex.oco_fail = True
    with pytest.raises(FatalError):
        LiveBroker(ex, session).open("BTC/USDT", Always(), SIG, int(time.time() * 1000), 50.0, {"ask": 100, "bid": 100})
    p = session.query(Position).one()
    assert p.status == "CLOSED" and p.exit_reason == "protection_failed"
    assert any(c[0] == "sell" for c in ex.calls)


def test_live_oco_fill_is_recorded(session, live):
    ex = FakeExchange(bid=100.0, ask=100.0)
    b = LiveBroker(ex, session)
    p = b.open("BTC/USDT", Always(), SIG, int(time.time() * 1000), 50.0, {"ask": 100, "bid": 100})
    assert not b.check_exits(p, {"bid": 101})
    ex.orders["tp"] = {"id": "tp", "status": "closed", "filled": p.quantity, "average": 104.0,
                       "cost": p.quantity * 104.0, "fees": [{"currency": "USDT", "cost": 0.02}], "type": "limit",
                       "info": {"type": "LIMIT_MAKER"}}
    ex.oco_status, ex.oco_filled_leg = "ALL_DONE", "tp"
    assert b.check_exits(p, {"bid": 104})
    assert p.exit_reason == "target" and p.pnl == pytest.approx(p.quantity * 104 - 0.02 - 50.0)


def test_live_time_exit_cancels_oco_then_sells(session, live):
    ex = FakeExchange()
    b = LiveBroker(ex, session)
    p = b.open("BTC/USDT", Always(), SIG, int(time.time() * 1000), 50.0, {"ask": 100.02, "bid": 100})
    b.close(p, "time", {"bid": 100})
    kinds = [c[0] for c in ex.calls]
    assert kinds.index("cancel_oco") < kinds.index("sell")
    assert p.status == "CLOSED" and p.exit_reason == "time"


# ---------------------------------------------------------------- worker
class StubSelector:
    symbols = ["BTC/USDT"]

    def __init__(self, eligible=True):
        self.ok = eligible

    def due(self):
        return False

    def eligible_pairs(self):
        return [("always_1h", "BTC/USDT", 2.0)] if self.ok else []

    def summary(self):
        return ""


def _bot(ex, eligible=True):
    from app.worker import Bot
    bot = Bot(ex, selector=StubSelector(eligible), notify=lambda m: None)
    bot.strategies, bot.timeframes = [Always()], ["1h"]
    return bot


def _feed(ex):
    now = int(time.time() * 1000)
    last_open = now // H * H - H
    df = make_ohlcv(300, start_ts=last_open - 299 * H)
    ex.ohlcv[("BTC/USDT", "1h")] = df
    ex.bid, ex.ask = float(df.close.iloc[-1]), float(df.close.iloc[-1]) * 1.0001
    return last_open + H + 1000   # "now" = 1 s after the last bar closed


def test_worker_opens_then_stops_out(session):
    ex = FakeExchange()
    now_ms = _feed(ex)
    bot = _bot(ex)
    bot.cycle(now_ms)
    p = session.query(Position).one()
    assert p.status == "OPEN" and p.strategy == "always_1h"
    ex.bid = p.stop_price * 0.999
    bot.cycle(now_ms + 20_000)
    session.refresh(p)
    assert p.status == "CLOSED" and p.exit_reason == "stop" and p.pnl < 0


def test_worker_skips_ineligible_and_stale_and_paused(session):
    ex = FakeExchange()
    now_ms = _feed(ex)
    _bot(ex, eligible=False).cycle(now_ms)
    assert session.query(Position).count() == 0
    _bot(ex).cycle(now_ms + 20 * 60_000)              # bar closed 20 min ago -> too late to enter
    assert session.query(Position).count() == 0
    session.get(BotState, 1).state = "PAUSED"
    session.commit()
    _bot(ex).cycle(now_ms)
    assert session.query(Position).count() == 0


def test_worker_manages_exits_while_halted(session):
    ex = FakeExchange()
    now_ms = _feed(ex)
    bot = _bot(ex)
    bot.cycle(now_ms)
    p = session.query(Position).one()
    session.get(BotState, 1).state = "HALTED"
    session.commit()
    ex.bid = p.target_price * 1.001
    ex.ask = ex.bid * 1.0001
    bot.cycle(now_ms + 20_000)
    session.refresh(p)
    assert p.status == "CLOSED" and p.exit_reason == "target"


def test_transient_errors_do_not_halt(session):
    import ccxt
    bot = _bot(FakeExchange())
    bot.handle_error(ccxt.NetworkError("blip"))
    from app.worker import state_row
    assert state_row(session).state == "RUNNING"
    bot.handle_error(FatalError("boom"))
    session.expire_all()
    assert state_row(session).state == "HALTED"


# ---------------------------------------------------------------- API
def test_api_control_requires_token_and_reset_flow(session):
    from fastapi.testclient import TestClient

    from app.api import app
    c = TestClient(app)
    assert c.post("/control/stop").status_code == 401
    h = {"X-API-Token": "test-token"}
    assert c.post("/control/stop", headers=h).json()["state"] == "HALTED"
    assert c.post("/control/resume", headers=h).status_code == 409
    assert c.post("/control/reset", headers=h).json()["state"] == "PAUSED"
    assert c.post("/control/resume", headers=h).json()["state"] == "RUNNING"
    assert c.get("/status").json()["state"] == "RUNNING"


def test_oco_leg_reason_matches_real_ccxt_shapes():
    from app.execution import oco_leg_reason
    # shapes observed on the Binance Spot Testnet: both legs come back as ccxt type "limit"
    assert oco_leg_reason({"type": "limit", "stopPrice": 67672.63, "info": {"type": "STOP_LOSS_LIMIT"}}) == "stop"
    assert oco_leg_reason({"type": "limit", "stopPrice": None, "info": {"type": "LIMIT_MAKER"}}) == "target"
    assert oco_leg_reason({"type": "limit", "triggerPrice": 1.0}) == "stop"
    assert oco_leg_reason({"type": "limit"}) == "target"
