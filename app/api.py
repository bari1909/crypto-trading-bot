"""Read-only status API plus token-protected control endpoints. Bind to localhost only."""
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import BotState, Event, Position, StrategyScore, aware
from app.notify import telegram_send

@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="AI Crypto Trader API", version="2.0.0", lifespan=lifespan)


def require_token(x_api_token: str = Header(default="")):
    if not settings.api_token:
        raise HTTPException(403, "Control disabled: set API_TOKEN in .env")
    if not hmac.compare_digest(x_api_token, settings.api_token):
        raise HTTPException(401, "Invalid API token")


def _pos(p: Position) -> dict:
    return {k: getattr(p, k) for k in ("id", "symbol", "strategy", "timeframe", "mode", "market", "side", "status",
                                       "entry_price", "funding",
                                       "quantity", "entry_cost", "stop_price", "target_price", "opened_at",
                                       "max_hold_until", "exit_price", "exit_reason", "closed_at", "pnl", "pnl_pct")}


@app.get("/health")
def health():
    with SessionLocal() as s:
        row = s.get(BotState, 1)
        hb = aware(row.heartbeat_at) if row else None
    age = (datetime.now(timezone.utc) - hb).total_seconds() if hb else None
    ok = age is not None and age < max(120, settings.scan_interval_seconds * 10)
    return {"ok": ok, "heartbeat_age_s": age}


@app.get("/status")
def status():
    with SessionLocal() as s:
        row = s.get(BotState, 1)
        closed = s.scalars(select(Position).where(Position.status == "CLOSED",
                                                  Position.mode == settings.trading_mode,
                                                  Position.market == settings.market)).all()
        realized = sum(p.pnl or 0 for p in closed)
        return {"state": row.state if row else "INITIALIZING", "mode": settings.trading_mode,
                "reason": row.reason if row else "", "realized_pnl": realized, "closed_trades": len(closed),
                "wins": sum(1 for p in closed if (p.pnl or 0) > 0),
                "paper_equity": settings.starting_equity + realized if settings.trading_mode == "paper" else None}


@app.get("/positions")
def positions(status: str = "OPEN", limit: int = 200):
    with SessionLocal() as s:
        q = select(Position).order_by(Position.id.desc()).limit(min(limit, 1000))
        if status != "ALL":
            q = q.where(Position.status == status)
        return [_pos(p) for p in s.scalars(q).all()]


@app.get("/strategies")
def strategies():
    with SessionLocal() as s:
        rows = s.scalars(select(StrategyScore).order_by(StrategyScore.eligible.desc(),
                                                        StrategyScore.score.desc())).all()
        return [{k: getattr(r, k) for k in ("strategy", "symbol", "timeframe", "lookback_days", "n", "win_rate",
                                            "avg_ret_pct", "profit_factor", "total_ret_pct", "max_dd_pct",
                                            "score", "eligible", "computed_at")} for r in rows]


@app.get("/report")
def report(days: int = 7):
    from app.reports import weekly_report
    with SessionLocal() as s:
        return {"text": weekly_report(s, days=min(max(days, 1), 365))}


@app.get("/events")
def events(limit: int = 100):
    with SessionLocal() as s:
        rows = s.scalars(select(Event).order_by(Event.id.desc()).limit(min(limit, 1000))).all()
        return [{"time": e.created_at, "level": e.level, "message": e.message} for e in rows]


@app.post("/control/{action}", dependencies=[Depends(require_token)])
def control(action: str):
    if action not in ("pause", "resume", "stop", "reset"):
        raise HTTPException(400, "Unsupported action (pause|resume|stop|reset)")
    with SessionLocal() as s:
        row = s.get(BotState, 1)
        if not row:
            row = BotState(id=1, state="PAUSED", mode=settings.trading_mode)
            s.add(row)
        if action == "stop":
            row.state, row.reason = "HALTED", "Manual emergency stop"
        elif action == "pause":
            row.state, row.reason = "PAUSED", "Manual pause"
        elif action == "reset":
            if row.state != "HALTED":
                raise HTTPException(409, "Only HALTED can be reset")
            row.state, row.reason = "PAUSED", "Halt cleared by operator; review, then resume"
        else:
            if row.state == "HALTED":
                raise HTTPException(409, "HALTED: check the exchange, then POST /control/reset first")
            row.state, row.reason = "RUNNING", "Manual resume"
        row.updated_at = datetime.now(timezone.utc)
        s.commit()
        telegram_send(f"Bot control via API: {action.upper()} -> {row.state}")
        return {"state": row.state, "reason": row.reason}
