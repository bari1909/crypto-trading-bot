"""Exchange access: ccxt construction, closed candles, history pagination, quotes."""
import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from app.config import settings

log = logging.getLogger(__name__)

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLS = ["ts", "open", "high", "low", "close", "volume"]

# Errors worth retrying; anything else from the exchange is treated as fatal by the worker.
TRANSIENT_ERRORS = (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout,
                    ccxt.DDoSProtection, ccxt.RateLimitExceeded)


class StaleDataError(RuntimeError):
    """Raised for bad/stale market data; treated as transient."""


def _client(**extra):
    cfg = {"enableRateLimit": True, "timeout": 15000, **extra}
    if settings.market == "futures":
        return ccxt.binanceusdm(cfg)
    cfg.setdefault("options", {})["defaultType"] = "spot"
    return ccxt.binance(cfg)


def make_exchange(authenticated: bool = False):
    """Public MAINNET client for market data (candles, selector, paper quotes) of the configured market.
    Never sandboxed: test-venue candles are thin/synthetic and would make strategy selection meaningless."""
    if authenticated:
        return make_order_exchange()
    return _client()


def make_order_exchange():
    """Authenticated client where orders are sent (spot testnet / futures testnet or demo / mainnet)."""
    if not settings.uses_exchange_orders:
        raise RuntimeError("Order exchange is only used in testnet/live mode")
    key, secret = settings.order_api_credentials
    ex = _client(apiKey=key, secret=secret, options={"adjustForTimeDifference": True})
    if settings.trading_mode == "testnet":
        if settings.market == "futures" and settings.futures_testnet_venue == "demo":
            ex.enable_demo_trading(True)
        else:
            ex.set_sandbox_mode(True)
    return ex


def is_sandbox(ex) -> bool:
    """True if EVERY private endpoint points at a test venue (testnet or demo)."""
    urls = [v for k, v in (ex.urls.get("api") or {}).items() if isinstance(v, str) and "rivate" in k]
    return bool(urls) and all(("testnet" in u) or ("demo-" in u) for u in urls)


def _to_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLS)
    df[COLS[1:]] = df[COLS[1:]].astype(float)
    df["ts"] = df["ts"].astype("int64")
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def drop_unclosed(df: pd.DataFrame, timeframe: str, now_ms: int | None = None) -> pd.DataFrame:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return df[df.ts + TF_MS[timeframe] <= now_ms].reset_index(drop=True)


def closed_candles(ex, symbol: str, timeframe: str, limit: int = 400) -> pd.DataFrame:
    df = drop_unclosed(_to_df(ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit + 1)), timeframe)
    if df.empty:
        raise StaleDataError(f"No closed candles for {symbol} {timeframe}")
    age = time.time() * 1000 - (df.ts.iloc[-1] + TF_MS[timeframe])
    if age > 2 * TF_MS[timeframe]:
        raise StaleDataError(f"Stale candles for {symbol} {timeframe}: last close {age/1000:.0f}s ago")
    return df


def history(ex, symbol: str, timeframe: str, days: float) -> pd.DataFrame:
    """Paginated closed-candle history for the last `days` days."""
    step = TF_MS[timeframe]
    since = int(time.time() * 1000 - days * 86_400_000)
    rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + step
        if nxt <= since or len(batch) < 2:
            break
        since = nxt
        if since > time.time() * 1000:
            break
    return drop_unclosed(_to_df(rows), timeframe) if rows else pd.DataFrame(columns=COLS)


def cached_history(ex, symbol: str, timeframe: str, days: float, cache_dir="data", prefix="") -> pd.DataFrame:
    """History with an on-disk CSV cache (used by the backtest/research CLIs)."""
    safe = symbol.replace("/", "").replace(":", "_")
    path = Path(cache_dir) / f"{prefix}{safe}_{timeframe}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    start_ms = int(time.time() * 1000 - days * 86_400_000)
    df = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=COLS)
    if df.empty or df.ts.min() > start_ms + TF_MS[timeframe]:
        df = history(ex, symbol, timeframe, days)
    else:
        newer = history(ex, symbol, timeframe, (time.time() * 1000 - df.ts.max()) / 86_400_000 + 0.01)
        df = _to_df(pd.concat([df, newer])[COLS].values.tolist())
    df.to_csv(path, index=False)
    return df[df.ts >= start_ms].reset_index(drop=True)


def quote(ex, symbol: str) -> dict:
    t = ex.fetch_ticker(symbol)
    bid, ask = t.get("bid"), t.get("ask")
    if not bid or not ask:
        # Binance USDⓈ-M 24h tickers carry no bid/ask (mainnet and demo): use the top of the order book.
        ob = ex.fetch_order_book(symbol, 5)
        bid = ob["bids"][0][0] if ob.get("bids") else None
        ask = ob["asks"][0][0] if ob.get("asks") else None
    if not bid or not ask or ask <= bid:
        raise StaleDataError(f"Invalid bid/ask for {symbol}: {bid}/{ask}")
    bid, ask = float(bid), float(ask)
    return {"symbol": symbol, "bid": bid, "ask": ask, "last": float(t.get("last") or bid),
            "spread_pct": (ask - bid) / ((ask + bid) / 2)}
