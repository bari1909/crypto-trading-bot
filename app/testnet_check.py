"""Binance Spot TESTNET end-to-end check of the real order path (fake funds only).

    docker compose run --rm worker python -m app.testnet_check [--symbol BTC/USDT] [--notional 15]

Steps: connect + balances -> market BUY by quote cost -> OCO protection placed -> OCO status ->
cancel OCO + market SELL (the same path a time/signal exit uses) -> no open orders left.
Uses a throwaway in-memory database, so the bot's real ledger is untouched.
Refuses to run unless TRADING_MODE=testnet and the order client points at testnet.binance.vision.
"""
import argparse
import sys
import time
import types

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.exchange import is_sandbox, make_order_exchange, quote
from app.execution import LiveBroker
from app.models import Event


def step(n, msg):
    print(f"\n[{n}] {msg}")


def ok(msg):
    print(f"    PASS  {msg}")


def fail(msg):
    print(f"    FAIL  {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--notional", type=float, default=15.0)
    args = ap.parse_args()

    if settings.trading_mode != "testnet":
        fail("Set TRADING_MODE=testnet in .env first")
    ex = make_order_exchange()
    if not is_sandbox(ex):
        fail("Order client is NOT pointed at the testnet — aborting")

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()

    step(1, f"Connect to {ex.urls['api']['private']}")
    ex.load_markets()
    m = ex.market(args.symbol)
    bal = ex.fetch_balance()
    q0 = float((bal.get("free") or {}).get(m["quote"]) or 0)
    b0 = float((bal.get("free") or {}).get(m["base"]) or 0)
    ok(f"authenticated; free {m['quote']}={q0:.2f}, {m['base']}={b0:.8f}")
    if q0 < args.notional * 1.2:
        fail(f"Not enough testnet {m['quote']} (need ~{args.notional * 1.2:.0f})")

    q = quote(ex, args.symbol)
    ok(f"testnet {args.symbol} bid={q['bid']} ask={q['ask']} spread={q['spread_pct']:.4%}")

    step(2, f"Market BUY {args.notional} {m['quote']} of {args.symbol} + OCO protection (stop -2%, target +3%)")
    strat = types.SimpleNamespace(name="testnet_check", timeframe="1h", max_hold_bars=1)
    sig = {"stop_dist": q["ask"] * 0.02, "target_dist": q["ask"] * 0.03}
    broker = LiveBroker(ex, s)
    p = broker.open(args.symbol, strat, sig, int(time.time() * 1000) - 3_600_000, args.notional, q)
    if p is None:
        fail("open() skipped the trade (below minimum notional?)")
    ok(f"filled qty={p.quantity} avg={p.entry_price} cost={p.entry_cost:.4f} (base fee deducted)")
    if p.status == "CLOSED":
        fail(f"position closed immediately ({p.exit_reason}) — price moved through a level")
    if not p.protection_id:
        fail("no OCO id recorded")
    ok(f"OCO orderListId={p.protection_id}")

    step(3, "Verify OCO on the exchange")
    st = ex.private_get_orderlist({"orderListId": p.protection_id})
    legs = [ex.fetch_order(str(o["orderId"]), args.symbol) for o in st.get("orders", [])]
    print(f"    listOrderStatus={st.get('listOrderStatus')}")
    for o in legs:
        print(f"    leg {o['id']}: type={o.get('type')} status={o.get('status')} price={o.get('price')} "
              f"stop={o.get('stopPrice') or o.get('triggerPrice')} amount={o.get('amount')}")
    if st.get("listOrderStatus") not in ("EXECUTING", "ALL_DONE") or len(legs) != 2:
        fail("OCO not live with 2 legs")
    ok("OCO live with stop-limit + limit-maker legs")
    if broker.check_exits(p, quote(ex, args.symbol)):
        ok(f"(OCO already filled: {p.exit_reason}) — skipping manual exit")
    else:
        ok("check_exits() sees the position still open")

        step(4, "Exit path: cancel OCO, market SELL")
        broker.close(p, "time", quote(ex, args.symbol))
        if p.status != "CLOSED":
            fail(f"position not closed (status={p.status})")
        ok(f"sold @ {p.exit_price}; PnL {p.pnl:+.4f} {m['quote']} ({p.pnl_pct:+.3f}%) — fees + spread only")

    step(5, "Clean-up checks")
    left = ex.fetch_open_orders(args.symbol)
    if left:
        fail(f"{len(left)} open orders left on {args.symbol}")
    ok("no open orders left")
    bal = ex.fetch_balance()
    print(f"    free {m['quote']}={float(bal['free'].get(m['quote']) or 0):.2f} "
          f"{m['base']}={float(bal['free'].get(m['base']) or 0):.8f} (dust from rounding is normal)")
    for e in s.query(Event).order_by(Event.id):
        print(f"    log {e.level}: {e.message}")
    print("\nTESTNET CHECK PASSED")


if __name__ == "__main__":
    main()
