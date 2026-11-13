from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models import Event, Position, StrategyScore
from app.reports import est_fees, mark_sent, net_pnl, report_due, weekly_report

MON_9 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)   # a Monday


def _pos(s, mode, pnl, days_ago, reason="target", status="CLOSED", sym="BTC/USDT"):
    t = MON_9 - timedelta(days=days_ago)
    p = Position(symbol=sym, strategy="trend_follow_4h", timeframe="4h", mode=mode, status=status,
                 entry_price=100, quantity=1, entry_cost=100, stop_price=95, target_price=110,
                 exit_price=100 + pnl if status == "CLOSED" else None, exit_reason=reason if status == "CLOSED" else "",
                 pnl=pnl if status == "CLOSED" else None, opened_at=t - timedelta(hours=8),
                 closed_at=t if status == "CLOSED" else None)
    s.add(p)
    s.commit()
    return p


def test_testnet_pnl_gets_estimated_real_fees(session, monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "testnet")
    p = _pos(session, "testnet", 1.0, 1)
    assert est_fees(p) == pytest.approx((100 + 101) * settings.fee_rate)
    assert net_pnl(p) == pytest.approx(1.0 - 201 * settings.fee_rate)
    paper = _pos(session, "paper", 1.0, 1)
    assert est_fees(paper) == 0 and net_pnl(paper) == 1.0     # paper/live PnL already include fees


def test_report_content(session, monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "testnet")
    _pos(session, "testnet", 2.0, 2, "target")
    _pos(session, "testnet", -1.0, 3, "stop", sym="SOL/USDT")
    _pos(session, "testnet", 5.0, 20, "target")                   # older than this week
    _pos(session, "testnet", 0, 1, status="OPEN", sym="ETH/USDT")
    _pos(session, "paper", 50.0, 1)                               # other mode: ignored
    session.add(StrategyScore(strategy="trend_follow_4h", symbol="BTC/USDT", timeframe="4h", lookback_days=240,
                              n=9, win_rate=44, avg_ret_pct=1.14, profit_factor=1.9, total_ret_pct=10,
                              max_dd_pct=7, score=0.65, eligible=True))
    session.add(Event(level="CRITICAL", message="HALTED: boom", created_at=MON_9 - timedelta(days=1)))
    session.commit()
    r = weekly_report(session, MON_9)
    assert "2 closed (1 won, 1 lost)" in r
    assert "after est. fees" in r and "Backtest expected for these pairs: +1.14%/trade" in r
    assert "Exits: target 1, stop 1" in r or "Exits: stop 1, target 1" in r
    assert "ETH/USDT" in r                                     # open position listed
    assert "3 trades" in r and "too few to judge" in r         # since-start summary
    assert "1/1 pairs eligible" in r and "1 halts" in r


def test_empty_report_is_friendly(session):
    assert "no closed trades" in weekly_report(session, MON_9)


def test_report_schedule_once_per_week(session, monkeypatch):
    monkeypatch.setattr(settings, "weekly_report_weekday", 0)
    monkeypatch.setattr(settings, "weekly_report_hour_utc", 8)
    assert not report_due(session, MON_9.replace(hour=7))           # too early
    assert not report_due(session, MON_9 + timedelta(days=1))       # wrong day
    assert report_due(session, MON_9)
    mark_sent(session, MON_9)
    assert not report_due(session, MON_9 + timedelta(hours=5))      # already sent today (survives restarts: DB)
    assert report_due(session, MON_9 + timedelta(days=7))           # next Monday
    monkeypatch.setattr(settings, "weekly_report_enabled", False)
    assert not report_due(session, MON_9 + timedelta(days=7))


def test_worker_sends_report_and_never_crashes(session, monkeypatch):
    from conftest import FakeExchange

    from app.worker import Bot
    sent = []
    bot = Bot(FakeExchange(), selector=type("S", (), {"symbols": [], "due": lambda self: False,
                                                       "eligible_pairs": lambda self: [], "summary": lambda self: ""})(),
              notify=sent.append)
    bot.maybe_send_report(session, MON_9)
    bot.maybe_send_report(session, MON_9 + timedelta(minutes=1))
    assert len(sent) == 1 and sent[0].startswith("📊 Weekly report")
    monkeypatch.setattr("app.worker.weekly_report", lambda *a, **k: 1 / 0)
    bot.maybe_send_report(session, MON_9 + timedelta(days=7))      # error is logged, not raised


def test_api_report(session):
    from fastapi.testclient import TestClient

    from app.api import app
    assert "Weekly report" in TestClient(app).get("/report").json()["text"]


def test_selector_cache_invalidated_when_universe_changes(session):
    from app.selector import Selector
    from app.strategies import by_name
    session.add(StrategyScore(strategy="trend_follow_4h", symbol="BTC/USDT", timeframe="4h", lookback_days=240,
                              n=9, win_rate=44, avg_ret_pct=1.1, profit_factor=1.9, total_ret_pct=10,
                              max_dd_pct=7, score=0.6, eligible=True))
    session.commit()
    st = [by_name("trend_follow_4h")]
    assert Selector(None, st, ["BTC/USDT"]).load(session)
    assert not Selector(None, st, ["BTC/USDT", "XRP/USDT"]).load(session)   # new coin -> refresh
