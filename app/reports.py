"""Weekly performance report (sent to Telegram, also via /report and GET /report).

On TESTNET the exchange charges no fees, so recorded PnL is optimistic. The report therefore also
shows PnL after *estimated* real Binance fees (FEE_RATE on both legs), which is the number to judge.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.models import BotState, Event, KeyValue, Position, StrategyScore

LAST_SENT_KEY = "weekly_report_last_sent"


def est_fees(p: Position) -> float:
    """Real-world fees the trade would have paid (only added for testnet, where fees are ~0)."""
    if p.mode != "testnet" or p.market != "spot":   # the spot testnet charges ~0 fees; futures test venues do charge
        return 0.0
    return (p.entry_cost + (p.exit_price or 0) * p.quantity) * settings.fee_rate


def net_pnl(p: Position) -> float:
    return (p.pnl or 0.0) - est_fees(p)


def _pct(p: Position) -> float:
    return net_pnl(p) / p.entry_cost * 100 if p.entry_cost else 0.0


def weekly_report(s, now: datetime | None = None, days: int = 7, exchange=None) -> str:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    mode = settings.trading_mode
    q = settings.quote_currency
    fee_note = " (after est. real fees)" if mode == "testnet" else ""

    closed_all = s.scalars(select(Position).where(Position.mode == mode, Position.market == settings.market, Position.status == "CLOSED")
                           .order_by(Position.closed_at)).all()
    week = [p for p in closed_all if p.closed_at and p.closed_at.replace(tzinfo=p.closed_at.tzinfo or timezone.utc) >= start]
    opened = s.scalars(select(Position).where(Position.mode == mode, Position.market == settings.market, Position.opened_at >= start)).all()
    open_now = s.scalars(select(Position).where(Position.mode == mode, Position.market == settings.market, Position.status == "OPEN")).all()
    scores = {(r.strategy, r.symbol): r for r in s.scalars(select(StrategyScore)).all()}
    row = s.get(BotState, 1)

    L = [f"📊 Weekly report {start:%d %b} – {now:%d %b %Y} · {mode.upper()}",
         f"State: {row.state if row else '?'}" + (f" ({row.reason})" if row and row.reason else ""), ""]

    # ---- this week
    if week:
        wins = sum(1 for p in week if net_pnl(p) > 0)
        gross = sum(p.pnl or 0 for p in week)
        net = sum(net_pnl(p) for p in week)
        avg = sum(_pct(p) for p in week) / len(week)
        L.append(f"This week: {len(week)} closed ({wins} won, {len(week) - wins} lost), {len(opened)} opened")
        if mode == "testnet":
            L.append(f"PnL recorded: {gross:+.4f} {q} → after est. fees: {net:+.4f} {q}")
        else:
            L.append(f"PnL: {net:+.4f} {q}")
        L.append(f"Avg per trade{fee_note}: {avg:+.2f}%")
        exp = [scores[(p.strategy, p.symbol)].avg_ret_pct for p in week if (p.strategy, p.symbol) in scores]
        if exp:
            L.append(f"Backtest expected for these pairs: {sum(exp) / len(exp):+.2f}%/trade")
        L.append("Exits: " + ", ".join(f"{k} {v}" for k, v in Counter(p.exit_reason for p in week).most_common()))
        by = defaultdict(list)
        for p in week:
            by[p.strategy].append(p)
        for st, ps in sorted(by.items()):
            L.append(f"  • {st}: {len(ps)} trades, {sum(net_pnl(p) for p in ps):+.4f} {q}")
        for p in week[-5:]:
            L.append(f"    {p.symbol} {p.exit_reason}: {_pct(p):+.2f}%")
    else:
        L.append(f"This week: no closed trades ({len(opened)} opened). Normal for slow 4h strategies.")

    # ---- open positions
    L.append("")
    if open_now:
        L.append(f"Open positions ({len(open_now)}):")
        for p in open_now:
            extra = ""
            if exchange is not None:
                try:
                    bid = float(exchange.fetch_ticker(p.symbol)["bid"])
                    extra = f", now {(bid / p.entry_price - 1) * 100:+.2f}%"
                except Exception:  # noqa: BLE001 - report must not fail on a quote
                    pass
            L.append(f"  • {p.symbol} [{p.strategy}] entry {p.entry_price:.6g}, stop {p.stop_price:.6g}, "
                     f"target {p.target_price:.6g}{extra}")
    else:
        L.append("Open positions: none")

    # ---- since start
    if closed_all:
        net = sum(net_pnl(p) for p in closed_all)
        wins = sum(1 for p in closed_all if net_pnl(p) > 0)
        first = closed_all[0].opened_at
        L += ["", f"Since {first:%d %b %Y}: {len(closed_all)} trades, {wins} won "
                  f"({wins / len(closed_all) * 100:.0f}%), net{fee_note} {net:+.4f} {q}"]
        if len(closed_all) < 30:
            L.append(f"(Only {len(closed_all)} trades — too few to judge; ~30+ needed.)")

    # ---- selector + health
    ok = sorted((r for r in scores.values() if r.eligible), key=lambda r: -r.score)
    L += ["", f"Selector: {len(ok)}/{len(scores)} pairs eligible"
              + (": " + ", ".join(f"{r.strategy.split('_')[0]}@{r.symbol.split('/')[0]}" for r in ok[:6]) if ok else "")]
    issues = s.scalars(select(Event).where(Event.created_at >= start,
                                           Event.level.in_(("WARNING", "CRITICAL")))).all()
    halts = sum(1 for e in issues if "HALTED" in e.message)
    L.append(f"Issues this week: {len(issues)} warnings/critical, {halts} halts" if issues else "Issues this week: none")
    return "\n".join(L)


def report_due(s, now: datetime | None = None) -> bool:
    if not settings.weekly_report_enabled:
        return False
    now = now or datetime.now(timezone.utc)
    if now.weekday() != settings.weekly_report_weekday or now.hour < settings.weekly_report_hour_utc:
        return False
    kv = s.get(KeyValue, LAST_SENT_KEY)
    return not kv or kv.value != now.date().isoformat()


def mark_sent(s, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    kv = s.get(KeyValue, LAST_SENT_KEY) or KeyValue(key=LAST_SENT_KEY)
    kv.value = now.date().isoformat()
    s.add(kv)
    s.commit()
