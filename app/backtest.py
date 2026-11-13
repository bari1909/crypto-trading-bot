"""Bar-by-bar backtester, metrics, portfolio simulation and walk-forward evaluation.

The same fill rules are used by the live selector and mirrored by the paper broker:
  entry  = next bar open * (1 + slippage)
  stop   = checked first each bar (conservative); gap below stop fills at the open
  target = limit fill at target (or at the open if it gapped above)
  signal / time exits fill at bar close * (1 - slippage)
  fees   = fee_rate on both legs
"""
from __future__ import annotations

import argparse
import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from app.config import settings
from app.strategies import Strategy, all_strategies, by_name


@dataclass
class SimTrade:
    strategy: str
    symbol: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    ret: float          # net fractional return on notional, after fees+slippage (+funding for perps)
    stop_frac: float    # stop distance / entry
    reason: str
    bars: int
    side: str = "long"
    funding: float = 0.0  # funding received (+) or paid (-) as a fraction of notional


def run_backtest(strategy: Strategy, df: pd.DataFrame, symbol: str,
                 fee: float | None = None, slip: float | None = None,
                 funding: tuple[np.ndarray, np.ndarray] | None = None) -> list[SimTrade]:
    """Simulate one strategy on one symbol.

    `strategy.side` may be "long" or "short" (shorts only make sense on perpetual futures).
    `funding` = (funding_times_ms, rates) for perpetuals: longs pay positive funding, shorts receive it.
    """
    fee = settings.fee_rate if fee is None else fee
    slip = settings.slippage_rate if slip is None else slip
    short = getattr(strategy, "side", "long") == "short"
    if len(df) < strategy.warmup + 2:
        return []
    sig = strategy.signals(df)
    ts, o, h, l, c = (df[k].to_numpy() for k in ("ts", "open", "high", "low", "close"))
    entry, sd, td, ex = (sig[k].to_numpy() for k in ("entry", "stop_dist", "target_dist", "exit"))
    bar_ms = int(ts[1] - ts[0]) if len(ts) > 1 else 0
    n, i, out = len(df), 0, []
    while i < n - 1:
        if not entry[i]:
            i += 1
            continue
        x = reason = None
        last = min(i + strategy.max_hold_bars, n - 1)
        if short:
            e = o[i + 1] * (1 - slip)
            stop, tgt = e + sd[i], e - td[i]
            for j in range(i + 1, last + 1):
                if h[j] >= stop:
                    x, reason = max(o[j], stop) * (1 + slip), "stop"
                elif l[j] <= tgt:
                    x, reason = min(o[j], tgt), "target"
                elif ex[j]:
                    x, reason = c[j] * (1 + slip), "signal"
                elif j - i >= strategy.max_hold_bars:
                    x, reason = c[j] * (1 + slip), "time"
                if x is not None:
                    break
        else:
            e = o[i + 1] * (1 + slip)
            stop, tgt = e - sd[i], e + td[i]
            for j in range(i + 1, last + 1):
                if l[j] <= stop:
                    x, reason = min(o[j], stop) * (1 - slip), "stop"
                elif h[j] >= tgt:
                    x, reason = max(o[j], tgt), "target"
                elif ex[j]:
                    x, reason = c[j] * (1 - slip), "signal"
                elif j - i >= strategy.max_hold_bars:
                    x, reason = c[j] * (1 - slip), "time"
                if x is not None:
                    break
        if x is None:      # still open at end of data -> not a completed trade
            break
        ret = (1 - x / e if short else x / e - 1) - fee * (1 + x / e)
        fund = 0.0
        if funding is not None:
            ft, fr = funding
            a, b = np.searchsorted(ft, ts[i + 1], "right"), np.searchsorted(ft, ts[j] + bar_ms, "right")
            fund = float(fr[a:b].sum()) * (1 if short else -1)
        out.append(SimTrade(strategy.name, symbol, int(ts[i + 1]), int(ts[j]), e, x, ret + fund,
                            sd[i] / e, reason, j - i, "short" if short else "long", fund))
        i = j
    return out


def metrics(trades: list[SimTrade]) -> dict:
    if not trades:
        return dict(n=0, win_rate=0.0, avg_ret_pct=0.0, profit_factor=0.0, total_ret_pct=0.0,
                    max_dd_pct=0.0, tstat=0.0, score=0.0)
    r = np.array([t.ret for t in trades])
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    curve = np.cumsum(r) * 100
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], curve]))[1:] - curve)) if len(curve) else 0.0
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    tstat = float(r.mean() / sd * math.sqrt(len(r))) if sd > 0 else 0.0
    return dict(n=len(r), win_rate=round(float((r > 0).mean() * 100), 1),
                avg_ret_pct=round(float(r.mean() * 100), 4),
                profit_factor=round(float(wins / losses), 3) if losses > 0 else (99.0 if wins > 0 else 0.0),
                total_ret_pct=round(float(r.sum() * 100), 2), max_dd_pct=round(dd, 2),
                tstat=round(tstat, 2), score=round(tstat, 2))


def selection_params(strategy_name: str) -> tuple[float, int]:
    """(lookback_days, min_trades) for a strategy, honouring per-strategy overrides."""
    try:
        st = by_name(strategy_name)
    except KeyError:
        return settings.selection_lookback_days, settings.selector_min_trades
    return (st.lookback_days or settings.selection_lookback_days,
            st.min_trades or settings.selector_min_trades)


def eligible(m: dict, min_trades: int | None = None) -> bool:
    return (m["n"] >= (min_trades or settings.selector_min_trades)
            and m["profit_factor"] >= settings.selector_min_profit_factor
            and m["avg_ret_pct"] >= settings.selector_min_expectancy_pct
            and m["max_dd_pct"] <= settings.selector_max_drawdown_pct)


def simulate_portfolio(trades: list[SimTrade], scores: dict | None = None, equity0: float | None = None,
                       max_open: int | None = None) -> dict:
    """Risk-sized, cash-constrained, max-N-positions, one-per-symbol portfolio replay."""
    equity = equity0 or settings.starting_equity
    max_open = max_open or settings.max_open_positions
    scores = scores or {}
    cost = 2 * (settings.fee_rate + settings.slippage_rate)
    order = sorted(trades, key=lambda t: (t.entry_ts, -scores.get((t.strategy, t.symbol), 0)))
    open_, taken, curve, peak, mdd = [], [], [], equity, 0.0
    for t in order:
        for p in [p for p in open_ if p[0].exit_ts <= t.entry_ts]:
            equity += p[1] * p[0].ret
            open_.remove(p)
            curve.append((p[0].exit_ts, equity))
            peak = max(peak, equity)
            mdd = max(mdd, (peak - equity) / peak * 100)
        if len(open_) >= max_open or any(p[0].symbol == t.symbol for p in open_):
            continue
        free = equity - sum(p[1] for p in open_)
        notional = min(equity * settings.risk_per_trade / (t.stop_frac + cost),
                       equity * settings.max_position_fraction, free)
        if notional < 5:     # Binance min notional
            continue
        open_.append((t, notional))
        taken.append(t)
    for p in sorted(open_, key=lambda p: p[0].exit_ts):
        equity += p[1] * p[0].ret
        curve.append((p[0].exit_ts, equity))
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak * 100)
    e0 = equity0 or settings.starting_equity
    return dict(trades=len(taken), final_equity=round(equity, 2),
                return_pct=round((equity / e0 - 1) * 100, 2), max_dd_pct=round(mdd, 2),
                trade_metrics=metrics(taken), curve=curve, taken=taken)


def walk_forward(all_trades: list[SimTrade], start_ms: int, end_ms: int, step_days: float = 7) -> dict:
    """Re-select strategies every `step_days` using ONLY trades closed before that point,
    then trade the selected (strategy, symbol) pairs out-of-sample for the next step."""
    step = step_days * 86_400_000
    groups: dict = {}
    for x in all_trades:
        groups.setdefault((x.strategy, x.symbol), []).append(x)
    params = {k: selection_params(k[0]) for k in groups}
    t, oos, log = start_ms + settings.selection_lookback_days * 86_400_000, [], []
    while t < end_ms:
        chosen = {}
        for k, trades in groups.items():
            lb, min_n = params[k]
            m = metrics([x for x in trades if t - lb * 86_400_000 <= x.entry_ts and x.exit_ts < t])
            if eligible(m, min_n):
                chosen[k] = m["score"]
        oos += [(x, chosen[k]) for k in chosen for x in groups[k] if t <= x.entry_ts < t + step]
        log.append((t, sorted(chosen, key=lambda k: -chosen[k])))
        t += step
    score_map = {(x.strategy, x.symbol): s for x, s in oos}
    port = simulate_portfolio([x for x, _ in oos], score_map)
    port["selection_log"] = log
    return port


def _fmt_ts(ms):
    return pd.Timestamp(ms, unit="ms").strftime("%Y-%m-%d")


def main():
    from app.exchange import cached_history, make_exchange

    ap = argparse.ArgumentParser(description="Backtest + walk-forward of every strategy")
    ap.add_argument("--days", type=float, default=240)
    ap.add_argument("--symbols", default=settings.symbols)
    ap.add_argument("--step", type=float, default=7)
    ap.add_argument("--strategies", default="", help="comma list of strategy names (default: all)")
    args = ap.parse_args()

    ex = make_exchange()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    wanted = {s.strip() for s in args.strategies.split(",") if s.strip()}
    strategies = [s for s in all_strategies() if not wanted or s.name in wanted]
    all_trades: list[SimTrade] = []
    data_cache, start_ms, end_ms = {}, None, None
    for sym in syms:
        for st in strategies:
            key = (sym, st.timeframe)
            if key not in data_cache:
                data_cache[key] = cached_history(ex, sym, st.timeframe, args.days)
            df = data_cache[key]
            start_ms = df.ts.iloc[0] if start_ms is None else max(start_ms, df.ts.iloc[0])
            end_ms = df.ts.iloc[-1] if end_ms is None else min(end_ms, df.ts.iloc[-1])
            all_trades += run_backtest(st, df, sym)

    print(f"\nData {_fmt_ts(start_ms)} -> {_fmt_ts(end_ms)} | fee {settings.fee_rate:.4%} "
          f"slip {settings.slippage_rate:.4%} per side\n")
    print(f"{'strategy':28s} {'symbol':10s} {'n':>5s} {'win%':>6s} {'avg%':>8s} {'PF':>6s} "
          f"{'sum%':>8s} {'DD%':>6s} {'t':>6s}")
    for st in strategies:
        for sym in syms:
            m = metrics([t for t in all_trades if t.strategy == st.name and t.symbol == sym])
            print(f"{st.name:28s} {sym:10s} {m['n']:5d} {m['win_rate']:6.1f} {m['avg_ret_pct']:8.3f} "
                  f"{m['profit_factor']:6.2f} {m['total_ret_pct']:8.2f} {m['max_dd_pct']:6.2f} {m['tstat']:6.2f}")

    wf = walk_forward(all_trades, start_ms, end_ms, step_days=args.step)
    wf_start = start_ms + settings.selection_lookback_days * 86_400_000
    naive = simulate_portfolio([t for t in all_trades if t.entry_ts >= wf_start])
    print("\n=== Walk-forward (auto-selector, out-of-sample only) ===")
    print({k: v for k, v in wf.items() if k not in ("curve", "taken", "selection_log")})
    print("=== Same period, every strategy with no selection ===")
    print({k: v for k, v in naive.items() if k not in ("curve", "taken")})
    print("\nSelections per step:")
    for t, chosen in wf["selection_log"]:
        print(" ", _fmt_ts(t), [f"{s}@{y}" for s, y in chosen] or "— none eligible, bot stays flat —")


if __name__ == "__main__":
    main()
