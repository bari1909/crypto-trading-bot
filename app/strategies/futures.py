"""Short-side strategies for USDⓈ-M perpetual futures. RESEARCH ONLY.

They are deliberately NOT in all_strategies(): the bot only has spot execution, and
active_strategies() additionally filters out side == "short". Each one mirrors a long strategy
so we are not inventing new rules after looking at the data.
"""
from app import indicators as ta
from app.strategies.base import Strategy
from app.strategies.library import TrendFollow, TurtleBreakout, _frame


class TrendFollowShort(Strategy):
    """Mirror of trend_follow: EMA20 crosses BELOW EMA50 while below EMA200; cover on cross up."""

    def __init__(self, timeframe="4h"):
        super().__init__(name=f"trend_follow_short_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=180, description="EMA20/50 cross down below EMA200; cover on cross up",
                         lookback_days=240, min_trades=6, side="short")

    def compute(self, df):
        c = df.close
        e20, e50, e200 = ta.ema(c, 20), ta.ema(c, 50), ta.ema(c, 200)
        a = ta.atr(df)
        cross_dn = (e20 < e50) & (e20.shift() >= e50.shift())
        return _frame(df, cross_dn & (c < e200), 3.0 * a, 12.0 * a, e20 > e50)


class TurtleBreakdown(Strategy):
    """Mirror of turtle_breakout: 55-bar LOW breakdown below EMA200; cover on a 20-bar high."""

    def __init__(self, timeframe="4h"):
        super().__init__(name=f"turtle_breakdown_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=240, description="55-bar low breakdown; cover at 20-bar high",
                         lookback_days=240, min_trades=6, side="short")

    def compute(self, df):
        c = df.close
        a = ta.atr(df)
        low55 = df.low.shift(1).rolling(55).min()
        high20 = df.high.shift(1).rolling(20).max()
        entry = (c < low55) & (c < ta.ema(c, 200))
        return _frame(df, entry, 2.5 * a, 12.0 * a, c > high20)


class RallyFade(Strategy):
    """Mirror of trend_pullback: short a bounce up to EMA20 inside an established downtrend."""

    def __init__(self, timeframe="4h"):
        super().__init__(name=f"rally_fade_{timeframe}", timeframe=timeframe, warmup=250,
                         max_hold_bars=64, description="Bounce to EMA20 in an EMA50<EMA200 downtrend",
                         lookback_days=240, min_trades=6, side="short")

    def compute(self, df):
        c = df.close
        e20, e50, e200 = ta.ema(c, 20), ta.ema(c, 50), ta.ema(c, 200)
        a, r = ta.atr(df), ta.rsi(c)
        downtrend = (c < e200) & (e50 < e200) & (e50 < e50.shift(10))
        touched = df.high.rolling(3).max() >= e20 * 0.999
        trigger = (c < e20) & (c < df.open) & r.between(35, 55)
        return _frame(df, downtrend & touched & trigger, 1.5 * a, 3.0 * a, c > e50)


def futures_strategies() -> list[Strategy]:
    """Long + short set evaluated on perpetual futures (lower fees, funding applied)."""
    return [TrendFollow("4h"), TurtleBreakout("4h"),
            TrendFollowShort("4h"), TurtleBreakdown("4h"), RallyFade("4h")]


__all__ = ["TrendFollowShort", "TurtleBreakdown", "RallyFade", "futures_strategies"]
