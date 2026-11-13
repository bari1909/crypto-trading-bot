from app.strategies.base import Strategy
from app.strategies.library import (DonchianBreakout, EmaMomentum, MeanReversion, Rsi2Dip,
                                    SqueezeBreakout, TrendFollow, TrendPullback, TurtleBreakout)


def all_strategies() -> list[Strategy]:
    return [
        TrendFollow("4h"), TurtleBreakout("4h"), TrendPullback("4h"),
        TrendPullback("15m"), TrendPullback("1h"),
        MeanReversion("15m"), MeanReversion("1h"),
        DonchianBreakout("1h"),
        Rsi2Dip("1h"),
        SqueezeBreakout("1h"),
        EmaMomentum("5m"),
    ]


def active_strategies() -> list[Strategy]:
    from app.config import settings
    wanted = settings.strategy_filter
    if settings.market == "futures":
        from app.strategies.futures import futures_strategies
        return [s for s in futures_strategies() if not wanted or s.name in wanted]
    # Spot execution can only buy: never hand a short strategy to the spot bot.
    return [s for s in all_strategies() if (not wanted or s.name in wanted) and s.side == "long"]


def by_name(name: str) -> Strategy:
    from app.strategies.futures import futures_strategies
    for s in all_strategies() + futures_strategies():
        if s.name == name:
            return s
    raise KeyError(name)


__all__ = ["Strategy", "all_strategies", "active_strategies", "by_name"]
