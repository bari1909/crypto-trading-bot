"""Research: can we profit when prices fall? (Binance USDⓈ-M perpetual futures)

    docker compose run --rm -u root --entrypoint python backtest -m app.research_futures --days 1095

Part A - directional: long + short trend strategies on perps (fees 0.05% taker, funding applied),
         per-strategy stats, stability across the two halves of the period, walk-forward selector.
Part B - funding carry: long spot + short perp (market-neutral), earns funding when it is positive.
Research only: nothing here places orders.
"""
import argparse
import time
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

from app.backtest import metrics, run_backtest, simulate_portfolio, walk_forward
from app.config import settings
from app.exchange import cached_history
from app.strategies.futures import futures_strategies

FUT_FEE, SLIP = 0.0005, 0.0002
SPOT_FEE = 0.001


def funding_history(fx, sym: str, days: float, cache_dir="data") -> pd.DataFrame:
    path = Path(cache_dir) / f"fund_{sym.replace('/', '').replace(':', '_')}.csv.gz"
    since = int(time.time() * 1000 - days * 86_400_000)
    if path.exists():
        df = pd.read_csv(path)
        if df.ts.min() <= since + 86_400_000 and df.ts.max() > time.time() * 1000 - 86_400_000:
            return df[df.ts >= since].reset_index(drop=True)
    rows, t = [], since
    while True:
        batch = fx.fetch_funding_rate_history(sym, since=t, limit=1000)
        if not batch:
            break
        rows += [(b["timestamp"], b["fundingRate"]) for b in batch]
        if len(batch) < 1000:
            break
        t = batch[-1]["timestamp"] + 1
    df = pd.DataFrame(rows, columns=["ts", "rate"]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


def fmt(m):
    return (f"n={m['n']:4d} win={m['win_rate']:5.1f}% avg={m['avg_ret_pct']:+.3f}% PF={m['profit_factor']:.2f} "
            f"t={m['tstat']:+.2f}")


def part_a(fx, syms, days, fund):
    print("\n" + "=" * 100 + "\nPART A — directional long/short trend strategies on perpetual futures\n" + "=" * 100)
    data = {s: cached_history(fx, f"{s}:USDT", "4h", days, prefix="fut_") for s in syms}
    start = max(d.ts.iloc[0] for d in data.values())
    end = min(d.ts.iloc[-1] for d in data.values())
    mid = start + (end - start) // 2
    print(f"Data {pd.Timestamp(start, unit='ms'):%Y-%m-%d} -> {pd.Timestamp(end, unit='ms'):%Y-%m-%d}, "
          f"{len(syms)} coins, fee {FUT_FEE:.2%} + slip {SLIP:.2%} per side, funding applied\n")
    for s, d in data.items():
        print(f"  {s:10s} {d.close.iloc[0]:>10.4g} -> {d.close.iloc[-1]:<10.4g} ({(d.close.iloc[-1] / d.close.iloc[0] - 1) * 100:+.0f}%)")

    all_trades = []
    print(f"\n{'strategy':26s} {'all 3 years':54s} | {'1st half avg':>12s} {'2nd half avg':>12s} | pairs+ | funding/trade")
    for st in futures_strategies():
        tr = []
        for s in syms:
            f = fund[s]
            tr += [t for t in run_backtest(st, data[s], s, fee=FUT_FEE, slip=SLIP,
                                           funding=(f.ts.to_numpy(), f.rate.to_numpy()))
                   if start <= t.entry_ts]
        all_trades += tr
        m = metrics(tr)
        h1 = metrics([t for t in tr if t.entry_ts < mid])
        h2 = metrics([t for t in tr if t.entry_ts >= mid])
        pos = sum(1 for s in syms if metrics([t for t in tr if t.symbol == s])["total_ret_pct"] > 0)
        fund_avg = np.mean([t.funding for t in tr]) * 100 if tr else 0
        print(f"{st.name:26s} {fmt(m):54s} | {h1['avg_ret_pct']:+11.3f}% {h2['avg_ret_pct']:+11.3f}% | "
              f"{pos:2d}/{len(syms)}  | {fund_avg:+.3f}%")

    print("\nWalk-forward selector (selects on past trades only, weekly), equity 100, 0.5% risk, max 2 positions:")
    longs = {"trend_follow_4h", "turtle_breakout_4h"}
    for label, keep in [("long only (futures)", lambda t: t.strategy in longs),
                        ("short only", lambda t: t.strategy not in longs),
                        ("long + short", lambda t: True)]:
        sub = [t for t in all_trades if keep(t)]
        wf = walk_forward(sub, start, end, step_days=7)
        tm = wf["trade_metrics"]
        print(f"  {label:22s} return {wf['return_pct']:+7.2f}%  maxDD {wf['max_dd_pct']:5.2f}%  trades {wf['trades']:4d}  "
              f"avg/trade {tm['avg_ret_pct']:+.3f}%  t={tm['tstat']:+.2f}")
        naive = simulate_portfolio([t for t in sub if t.entry_ts >= start + settings.selection_lookback_days * 86_400_000])
        print(f"  {'  (no selector)':22s} return {naive['return_pct']:+7.2f}%  maxDD {naive['max_dd_pct']:5.2f}%  "
              f"trades {naive['trades']:4d}")
    btc = data["BTC/USDT"]
    print(f"\n  Reference: buy & hold BTC {(btc.close.iloc[-1] / btc.close.iloc[0] - 1) * 100:+.1f}% over the period")
    return all_trades


def part_b(syms, fund, days):
    print("\n" + "=" * 100 + "\nPART B — funding carry: long spot + short perp (price-neutral)\n" + "=" * 100)
    print("Capital C is split: C/2 buys spot, C/2 is margin for a 1x short perp of the same size.")
    print("Income = funding on C/2. Round trip costs 2*(spot 0.10% + perp 0.05%) on C/2 = 0.15% of C.\n")
    rt_cost = 2 * (SPOT_FEE + FUT_FEE) / 2      # fraction of capital per round trip
    years = days / 365
    rows = []
    for s in syms:
        f = fund[s]
        r = f.rate.to_numpy()
        always = (r.sum() / 2 - rt_cost) / years * 100
        # switched: in when trailing 7-day mean > 0.005%/8h (~5.5% APR), out when trailing mean < 0
        trail = pd.Series(r).rolling(21).mean().shift(1).to_numpy()   # decide on PAST funding only
        inside, inc, switches, held = False, 0.0, 0, 0
        for k in range(len(r)):
            if not inside and trail[k] > 0.00005:
                inside, switches = True, switches + 1
            elif inside and trail[k] < 0:
                inside = False
            if inside:
                inc += r[k] / 2
                held += 1
        switched = (inc - switches * rt_cost) / years * 100
        neg = (r < 0).mean() * 100
        worst30 = pd.Series(r).rolling(90).sum().min() / 2 * 100
        rows.append((s, r.mean() * 3 * 365 * 100, neg, always, switched, held / len(r) * 100, switches, worst30))
    print(f"{'coin':10s} {'funding APR':>11s} {'neg %':>6s} | {'always-on APR':>13s} | {'switched APR':>12s} "
          f"{'time in':>7s} {'trips':>5s} | worst 30d")
    for s, apr, neg, a, b, tin, sw, w in rows:
        print(f"{s:10s} {apr:10.1f}% {neg:5.0f}% | {a:12.2f}% | {b:11.2f}% {tin:6.0f}% {sw:5d} | {w:+.2f}%")
    a = np.mean([x[3] for x in rows])
    b = np.mean([x[4] for x in rows])
    print(f"{'AVERAGE':10s} {'':>11s} {'':>6s} | {a:12.2f}% | {b:11.2f}%   (return on capital per year, before "
          f"basis/borrow effects)")
    # yearly breakdown, equal-weight always-on
    allr = pd.concat([fund[s].assign(sym=s) for s in syms])
    allr["year"] = pd.to_datetime(allr.ts, unit="ms").dt.to_period("Q")
    q = allr.groupby("year").rate.mean() * 3 * 365 / 2 * 100
    print("\nEqual-weight carry APR on capital by quarter (always-on, before costs):")
    print("  " + "  ".join(f"{p}: {v:+.1f}%" for p, v in q.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1095)
    args = ap.parse_args()
    fx = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 20000})
    fx.load_markets()
    syms = [s for s in settings.symbol_list if f"{s}:USDT" in fx.markets]
    fund = {s: funding_history(fx, f"{s}:USDT", args.days) for s in syms}
    part_a(fx, syms, args.days, fund)
    part_b(syms, fund, args.days)


if __name__ == "__main__":
    main()
