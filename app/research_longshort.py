"""Sensitivity check: always-on (no selector) long/short trend portfolios on perps.

    docker compose run --rm -u root --entrypoint python backtest -m app.research_longshort
"""
import pandas as pd, ccxt
from app.backtest import run_backtest, simulate_portfolio, metrics
from app.config import settings
from app.exchange import cached_history
from app.research_futures import funding_history
from app.strategies.futures import futures_strategies
fx = ccxt.binanceusdm(); fx.load_markets(); spot = ccxt.binance()
syms = settings.symbol_list
fund = {s: funding_history(fx, f"{s}:USDT", 1095) for s in syms}
fut = {s: cached_history(fx, f"{s}:USDT", "4h", 1095, prefix="fut_") for s in syms}
strats = {st.name: st for st in futures_strategies()}
start = max(d.ts.iloc[0] for d in fut.values()) + 60*86400000; end = min(d.ts.iloc[-1] for d in fut.values()); mid = start + (end-start)//2
def trades(names, fee, use_fund=True):
    out=[]
    for n in names:
        for s in syms:
            f=fund[s]
            out += [t for t in run_backtest(strats[n], fut[s], s, fee=fee, slip=0.0002,
                    funding=(f.ts.to_numpy(), f.rate.to_numpy()) if use_fund else None) if t.entry_ts>=start]
    return out
L=["trend_follow_4h","turtle_breakout_4h"]; S=["trend_follow_short_4h"]
sets={"longs (2)":L, "longs+trend short (3)":L+S, "all 5":list(strats)}
print("Always-on (no selector) portfolio, equity 100, 0.5% risk/trade")
print(f"{'set':24s} {'fees':14s} {'maxpos':6s} {'full':>8s} {'DD':>6s} {'1st half':>9s} {'2nd half':>9s} {'trades':>6s}")
for name, ns in sets.items():
    for flabel, fee, uf in [("futures 0.05%",0.0005,True),("spot 0.10%",0.001,False)]:
        if flabel.startswith("spot") and name!="longs (2)": continue
        tr=trades(ns, fee, uf)
        for mo in (1,2,3,4):
            full=simulate_portfolio(tr, max_open=mo)
            h1=simulate_portfolio([t for t in tr if t.entry_ts<mid], max_open=mo)
            h2=simulate_portfolio([t for t in tr if t.entry_ts>=mid], max_open=mo)
            print(f"{name:24s} {flabel:14s} {mo:6d} {full['return_pct']:+7.1f}% {full['max_dd_pct']:5.1f}% {h1['return_pct']:+8.1f}% {h2['return_pct']:+8.1f}% {full['trades']:6d}")
tr=trades(L+S,0.0005)
print("\nPer-trade by year (longs + trend short, futures):")
df=pd.DataFrame([(pd.Timestamp(t.entry_ts,unit='ms').year,t.side,t.ret) for t in tr],columns=['y','side','r'])
print((df.groupby(['y','side']).r.agg(['count','mean'])*[1,100]).round(3).to_string())
