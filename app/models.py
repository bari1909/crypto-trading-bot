from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow():
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; normalise everything to UTC-aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class BotState(Base):
    __tablename__ = "bot_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(20), default="RUNNING")   # RUNNING | PAUSED | HALTED
    mode: Mapped[str] = mapped_column(String(10), default="paper")
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(30))
    strategy: Mapped[str] = mapped_column(String(60))
    timeframe: Mapped[str] = mapped_column(String(5))
    mode: Mapped[str] = mapped_column(String(10))
    market: Mapped[str] = mapped_column(String(10), default="spot")      # spot | futures
    side: Mapped[str] = mapped_column(String(5), default="long")         # long | short
    status: Mapped[str] = mapped_column(String(12), default="OPEN")      # OPEN | CLOSED | ERROR
    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)                      # base qty actually held
    entry_cost: Mapped[float] = mapped_column(Float)                    # quote spent incl. fees
    stop_price: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    stop_frac: Mapped[float] = mapped_column(Float, default=0.0)
    signal_bar_ts: Mapped[int] = mapped_column(BigInteger, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    max_hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_order_id: Mapped[str] = mapped_column(String(100), default="")
    protection_id: Mapped[str] = mapped_column(String(100), default="")   # Binance orderListId
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(30), default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding: Mapped[float] = mapped_column(Float, default=0.0)           # perps: funding received(+)/paid(-)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategyScore(Base):
    __tablename__ = "strategy_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    strategy: Mapped[str] = mapped_column(String(60))
    symbol: Mapped[str] = mapped_column(String(30))
    timeframe: Mapped[str] = mapped_column(String(5))
    lookback_days: Mapped[float] = mapped_column(Float)
    n: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    avg_ret_pct: Mapped[float] = mapped_column(Float)
    profit_factor: Mapped[float] = mapped_column(Float)
    total_ret_pct: Mapped[float] = mapped_column(Float)
    max_dd_pct: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)


class KeyValue(Base):
    """Small persistent key/value store (e.g. when the last weekly report was sent)."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
