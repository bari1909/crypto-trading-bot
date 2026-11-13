"""Telegram long-polling command bot. Only TELEGRAM_ALLOWED_CHAT_IDS may issue commands."""
import logging
import time
from datetime import datetime, timezone

import requests
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import BotState, Position, StrategyScore

log = logging.getLogger("telegram")
HELP = ("/status – state, mode, PnL\n/report – weekly performance report\n/positions – open positions\n/pnl – last 10 closed trades\n"
        "/strategies – what the selector currently allows\n/pause – block new entries\n"
        "/resume – allow entries\n/stop – emergency HALT (exits still managed)\n"
        "/reset – clear HALTED -> PAUSED (check the exchange first)\n/help")


def handle(cmd: str) -> str:
    with SessionLocal() as s:
        row = s.get(BotState, 1)
        if not row:
            row = BotState(id=1, state="PAUSED", mode=settings.trading_mode)
            s.add(row)
            s.commit()
        mode = settings.trading_mode
        if cmd == "/status":
            closed = s.scalars(select(Position).where(Position.status == "CLOSED", Position.mode == mode,
                                                    Position.market == settings.market)).all()
            pnl = sum(p.pnl or 0 for p in closed)
            wins = sum(1 for p in closed if (p.pnl or 0) > 0)
            return (f"State: {row.state} ({row.reason})\nMode: {mode}\nClosed trades: {len(closed)} "
                    f"(wins {wins})\nRealized PnL: {pnl:+.4f} {settings.quote_currency}")
        if cmd == "/report":
            from app.reports import weekly_report
            return weekly_report(s)
        if cmd == "/positions":
            ps = s.scalars(select(Position).where(Position.status == "OPEN")).all()
            return "\n".join(f"{p.symbol} {p.side.upper()} [{p.strategy}] qty={p.quantity:.6g} entry={p.entry_price:.6g} "
                             f"stop={p.stop_price:.6g} target={p.target_price:.6g}" for p in ps) or "No open positions."
        if cmd == "/pnl":
            ps = s.scalars(select(Position).where(Position.status == "CLOSED", Position.mode == mode,
                                                    Position.market == settings.market)
                           .order_by(Position.closed_at.desc()).limit(10)).all()
            return "\n".join(f"{p.symbol} [{p.strategy}] {p.exit_reason}: {p.pnl:+.4f} ({p.pnl_pct:+.2f}%)"
                             for p in ps) or "No closed trades yet."
        if cmd == "/strategies":
            rows = s.scalars(select(StrategyScore).order_by(StrategyScore.score.desc())).all()
            ok = [r for r in rows if r.eligible]
            head = f"{len(ok)}/{len(rows)} strategy-symbol pairs eligible"
            return head + ("\n" + "\n".join(f"✅ {r.strategy} @ {r.symbol}: score {r.score:.2f}, PF {r.profit_factor:.2f},"
                                            f" n={r.n}" for r in ok) if ok else "\nNone eligible – bot stays flat.")
        if cmd in ("/pause", "/stop", "/resume", "/reset"):
            if cmd == "/pause":
                row.state, row.reason = "PAUSED", "Telegram pause"
            elif cmd == "/stop":
                row.state, row.reason = "HALTED", "Telegram emergency stop"
            elif cmd == "/reset":
                if row.state != "HALTED":
                    return f"Not HALTED (state {row.state})."
                row.state, row.reason = "PAUSED", "Halt cleared via Telegram"
            else:
                if row.state == "HALTED":
                    return "HALTED. Check open orders/balances on Binance, then /reset, then /resume."
                row.state, row.reason = "RUNNING", "Telegram resume"
            row.updated_at = datetime.now(timezone.utc)
            s.commit()
            return f"OK → {row.state}"
        return HELP


def main():
    logging.basicConfig(level=settings.log_level)
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN not set; telegram service idle")
        while True:
            time.sleep(3600)
    init_db()
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    offset = None
    while True:
        try:
            data = requests.get(base + "/getUpdates", params={"timeout": 25, "offset": offset}, timeout=35).json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                words = (msg.get("text") or "").split()
                if chat not in settings.allowed_chat_ids or not words:
                    continue
                reply = handle(words[0].lower().split("@")[0])
                requests.post(base + "/sendMessage", json={"chat_id": chat, "text": reply}, timeout=8)
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram polling error: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
