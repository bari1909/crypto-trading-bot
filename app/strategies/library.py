"""Strategy library. All long-only, spot, closed-candle, ATR-scaled risk."""
import pandas as pd

from app import indicators as ta
from app.strategies.base import Strategy


def _frame(df, entry, stop_dist, target_dist, exit_=None):
    return pd.DataFrame({
        "entry": entry,
        "stop_dist": stop_dist,
        "target_dist": target_dist,
        "exit": exit_ if exit_ is not None else False,
    }, index=df.index)


class TrendPullback(Strategy):
    """Buy a pullback to the 20 EMA inside an established uptrend (EMA50 > EMA200, rising)."""

    def __init__(self, timeframe="15m"):
        super().__init__(name=f"trend_pullback_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=64, description="Pullback to EMA20 in an EMA50>EMA200 uptrend")

    def compute(self, df):
        c = df.close
        e20, e50, e200 = ta.ema(c, 20), ta.ema(c, 50), ta.ema(c, 200)
        a, r = ta.atr(df), ta.rsi(c)
        uptrend = (c > e200) & (e50 > e200) & (e50 > e50.shift(10))
        touched = df.low.rolling(3).min() <= e20 * 1.001
        trigger = (c > e20) & (c > df.open) & r.between(45, 65)
        return _frame(df, uptrend & touched & trigger, 1.5 * a, 3.0 * a, c < e50)


class MeanReversion(Strategy):
    """Buy oversold stretches below the lower Bollinger band when the market is ranging."""

    def __init__(self, timeframe="15m"):
        super().__init__(name=f"mean_reversion_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=24, description="Lower-BB + RSI<30 in a low-ADX range; exit at mid band")

    def compute(self, df):
        c = df.close
        lo, mid, _ = ta.bollinger(c, 20, 2.0)
        a, r, x = ta.atr(df), ta.rsi(c), ta.adx(df)
        entry = (c < lo) & (r < 30) & (x < 25)
        target = (mid - c).clip(lower=0.5 * a)
        return _frame(df, entry, 1.5 * a, target, c >= mid)


class DonchianBreakout(Strategy):
    """Breakout above the prior 20-bar high with volume, in an uptrend."""

    def __init__(self, timeframe="1h"):
        super().__init__(name=f"donchian_breakout_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=72, description="20-bar high breakout + volume + ADX, trend filter")

    def compute(self, df):
        c = df.close
        a, x = ta.atr(df), ta.adx(df)
        e20, e200 = ta.ema(c, 20), ta.ema(c, 200)
        entry = (c > ta.donchian_high(df, 20)) & (df.volume > 1.5 * ta.sma(df.volume, 20)) & (c > e200) & (x > 18)
        return _frame(df, entry, 2.0 * a, 5.0 * a, c < e20)


class Rsi2Dip(Strategy):
    """Connors-style RSI(2) dip-buy above the 200 EMA; exit on close above SMA5."""

    def __init__(self, timeframe="1h"):
        super().__init__(name=f"rsi2_dip_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=24, description="RSI(2)<10 above EMA200; exit when close > SMA5")

    def compute(self, df):
        c = df.close
        a = ta.atr(df)
        entry = (c > ta.ema(c, 200)) & (ta.rsi(c, 2) < 10)
        return _frame(df, entry, 3.0 * a, 6.0 * a, c > ta.sma(c, 5))


class SqueezeBreakout(Strategy):
    """Volatility squeeze (narrow Bollinger width) resolving upward."""

    def __init__(self, timeframe="1h"):
        super().__init__(name=f"squeeze_breakout_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=48, description="BB width in bottom 20% of last 120 bars, close > upper band")

    def compute(self, df):
        c = df.close
        lo, mid, up = ta.bollinger(c, 20, 2.0)
        width = (up - lo) / mid
        squeezed = width.shift(1) <= width.shift(1).rolling(120).quantile(0.2)
        a = ta.atr(df)
        entry = squeezed & (c > up) & (c > ta.ema(c, 200))
        return _frame(df, entry, 1.5 * a, 3.0 * a, c < mid)


class TrendFollow(Strategy):
    """Slow trend following: EMA20 crosses above EMA50 while above EMA200; ride until EMA20 < EMA50."""

    def __init__(self, timeframe="4h"):
        super().__init__(name=f"trend_follow_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=180, description="EMA20/50 cross above EMA200; exit on cross down",
                         lookback_days=240, min_trades=6)

    def compute(self, df):
        c = df.close
        e20, e50, e200 = ta.ema(c, 20), ta.ema(c, 50), ta.ema(c, 200)
        a = ta.atr(df)
        cross_up = (e20 > e50) & (e20.shift() <= e50.shift())
        return _frame(df, cross_up & (c > e200), 3.0 * a, 12.0 * a, e20 < e50)


class TurtleBreakout(Strategy):
    """Classic 55-bar breakout, exit on a 20-bar low (Donchian channel system)."""

    def __init__(self, timeframe="4h"):
        super().__init__(name=f"turtle_breakout_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=240, description="55-bar high breakout; exit at 20-bar low",
                         lookback_days=240, min_trades=6)

    def compute(self, df):
        c = df.close
        a = ta.atr(df)
        low20 = df.low.shift(1).rolling(20).min()
        entry = (c > ta.donchian_high(df, 55)) & (c > ta.ema(c, 200))
        return _frame(df, entry, 2.5 * a, 12.0 * a, c < low20)


class EmaMomentum(Strategy):
    """The original v1 momentum scalp (EMA9>21, RSI 50-68, above VWAP, volume). Kept as a baseline."""

    def __init__(self, timeframe="5m"):
        super().__init__(name=f"ema_momentum_{timeframe}", timeframe=timeframe, warmup=120,
                         max_hold_bars=12, description="v1 baseline: EMA9>EMA21, RSI 50-68, >VWAP, volume spike")

    def compute(self, df):
        c = df.close
        a, r = ta.atr(df), ta.rsi(c)
        entry = (ta.ema(c, 9) > ta.ema(c, 21)) & r.between(50, 68, inclusive="neither") \
            & (c > ta.rolling_vwap(df, 99)) & (df.volume > ta.sma(df.volume, 20))
        return _frame(df, entry, 1.0 * a, 2.0 * a)
