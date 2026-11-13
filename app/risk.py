"""Risk gates and position sizing. Every block auto-expires (next UTC day / cooldown), so the bot
never gets stuck in a state that only a database edit can clear."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.models import Position, aware


class RiskManager:
    def __init__(self, session, mode: str | None = None):
        self.s = session
        self.mode = mode or settings.trading_mode

    # ---- ledger -----------------------------------------------------------
    def _closed(self, since: datetime | None = None):
        q = select(Position).where(Position.status == "CLOSED", Position.mode == self.mode,
                                   Position.market == settings.market)
        if since is not None:
            q = q.where(Position.closed_at >= since)
        return self.s.scalars(q.order_by(Position.closed_at.desc())).all()

    def open_positions(self):
        return self.s.scalars(select(Position).where(Position.status == "OPEN", Position.mode == self.mode,
                                   Position.market == settings.market)).all()

    def realized(self, since: datetime | None = None) -> float:
        return sum(p.pnl or 0.0 for p in self._closed(since))

    def paper_equity(self) -> float:
        return settings.starting_equity + self.realized()

    # ---- gates ------------------------------------------------------------
    def entry_block_reason(self, equity: float, now: datetime | None = None) -> str | None:
        now = now or datetime.now(timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.realized(day0) <= -equity * settings.daily_loss_limit:
            return "daily loss limit reached (resets 00:00 UTC)"
        if self.realized(now - timedelta(days=7)) <= -equity * settings.weekly_loss_limit:
            return "weekly loss limit reached (rolling 7 days)"
        opened_today = self.s.scalar(select(func.count()).select_from(Position).where(
            Position.mode == self.mode, Position.market == settings.market, Position.opened_at >= day0)) or 0
        if opened_today >= settings.max_trades_per_day:
            return "max trades per day reached"
        last = self._closed()[: settings.max_consecutive_losses]
        if len(last) == settings.max_consecutive_losses and all((p.pnl or 0) < 0 for p in last):
            until = aware(last[0].closed_at) + timedelta(minutes=settings.cooldown_minutes)
            if now < until:
                return f"{settings.max_consecutive_losses} consecutive losses: cooldown until {until:%H:%M} UTC"
        if len(self.open_positions()) >= settings.max_open_positions:
            return "max open positions"
        return None

    def symbol_busy(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.open_positions())

    # ---- sizing -----------------------------------------------------------
    def position_notional(self, stop_frac: float, equity: float, free_quote: float) -> float:
        cost = 2 * (settings.fee_rate + settings.slippage_rate)
        risk_notional = equity * settings.risk_per_trade / max(stop_frac + cost, 1e-6)
        cap = equity * settings.max_position_fraction
        if self.mode in ("live", "testnet"):
            cap = min(cap, settings.live_max_quote_allocation)
        return max(0.0, min(risk_notional, cap, free_quote * 0.995))
