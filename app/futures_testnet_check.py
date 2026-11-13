"""Binance USDⓈ-M FUTURES test-venue end-to-end check of the real order path (fake funds only).

    docker compose run --rm --no-deps -e MARKET=futures -e TRADING_MODE=testnet futures-worker \
        python -m app.futures_testnet_check [--symbol SOL/USDT:USDT] [--notional 12] [--side short]

Steps: connect -> 1x/isolated setup -> market entry -> reduce-only STOP_MARKET + TAKE_PROFIT_MARKET ->
verify on exchange -> cancel + reduce-only market close -> flat with no orders left.
Uses a throwaway in-memory DB. Refuses unless MARKET=futures, TRADING_MODE=testnet and the client is a test venue.
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
from app.futures_execution import FuturesLiveBroker
from app.models import Event


def ok(msg):
    print(f"    PASS  {msg}")


def fail(msg):
    print(f"    FAIL  {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SOL/USDT:USDT")
    ap.add_argument("--notional", type=float, default=12.0)
    ap.add_argument("--side", choices=["long", "short"], default="short")
    args = ap.parse_args()
    if settings.market != "futures" or settings.trading_mode != "testnet":
        fail("Run with MARKET=futures and TRADING_MODE=testnet")
    ex = make_order_exchange()
    if not is_sandbox(ex):
        fail("Order client is NOT a futures test venue — aborting")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()

    print(f"\n[1] Connect to {ex.urls['api']['fapiPrivate']} ({settings.futures_testnet_venue})")
    ex.load_markets()
    bal = ex.fetch_balance()
    ok(f"authenticated; USDT total={float(bal['total'].get('USDT') or 0):.2f}")
    q = quote(ex, args.symbol)
    ok(f"{args.symbol} bid={q['bid']} ask={q['ask']}")

    print(f"\n[2] {args.side.upper()} {args.notional} USDT at 1x isolated + reduce-only stop (2%) / target (3%)")
    st = types.SimpleNamespace(name="futures_check", timeframe="1h", max_hold_bars=1, side=args.side)
    ref = q["ask"] if args.side == "long" else q["bid"]
    sig = {"stop_dist": ref * 0.02, "target_dist": ref * 0.03}
    b = FuturesLiveBroker(ex, s)
    p = b.open(args.symbol, st, sig, int(time.time() * 1000) - 3_600_000, args.notional, q)
    if p is None:
        fail("trade skipped (below exchange minimum?) — try a larger --notional")
    lev = [x for x in ex.fetch_positions([args.symbol]) if x.get("symbol") == args.symbol]
    ok(f"filled {p.side} qty={p.quantity} avg={p.entry_price} (leverage on exchange: "
       f"{lev[0].get('leverage') if lev else '?'}x, margin {lev[0].get('marginMode') if lev else '?'})")
    if not p.protection_id:
        fail("no protective order ids recorded")
    ok(f"protective orders: {p.protection_id}")

    print("\n[3] Verify on exchange")
    try:
        orders = ex.fetch_open_orders(args.symbol, None, None, {"trigger": True})
    except Exception:  # noqa: BLE001
        orders = ex.fetch_open_orders(args.symbol)
    for o in orders:
        print(f"    order {o.get('id')}: {o.get('type')} {o.get('side')} trigger={o.get('triggerPrice') or o.get('stopPrice')} "
              f"reduceOnly={o.get('reduceOnly')}")
    if len(orders) < 2:
        fail(f"expected 2 protective orders, found {len(orders)}")
    ok("stop + take-profit are live on the exchange")
    if b.check_exits(p, q):
        fail(f"position unexpectedly closed ({p.exit_reason})")
    ok("position still open")

    print("\n[4] Exit path: cancel protection, reduce-only market close")
    b.close(p, "time", quote(ex, args.symbol))
    if p.status != "CLOSED":
        fail(f"not closed (status {p.status})")
    ok(f"closed @ {p.exit_price}; PnL {p.pnl:+.4f} USDT incl. fees, funding {p.funding:+.4f}")

    print("\n[5] Clean-up checks")
    if b._position_size(args.symbol) > 0:
        fail("position still open on exchange")
    try:
        left = ex.fetch_open_orders(args.symbol, None, None, {"trigger": True}) + ex.fetch_open_orders(args.symbol)
    except Exception:  # noqa: BLE001
        left = ex.fetch_open_orders(args.symbol)
    if left:
        fail(f"{len(left)} orders left open")
    ok("flat, no orders left")
    for e in s.query(Event).order_by(Event.id):
        print(f"    log {e.level}: {e.message}")
    print("\nFUTURES TESTNET CHECK PASSED")


if __name__ == "__main__":
    main()
