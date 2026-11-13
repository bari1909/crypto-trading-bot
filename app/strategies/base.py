from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Strategy:
    """A long-only spot strategy evaluated on CLOSED candles.

    `compute(df)` must return a DataFrame aligned to `df` with columns:
      entry       bool   - open a long at the next bar's open
      stop_dist   float  - absolute price distance of the stop below entry
      target_dist float  - absolute price distance of the take-profit above entry
      exit        bool   - signal-based exit at this bar's close (optional logic)
    Values at row i may only depend on rows <= i.
    """
    name: str = "base"
    timeframe: str = "15m"
    warmup: int = 250
    max_hold_bars: int = 48
    description: str = ""
    params: dict = field(default_factory=dict)
    # Slow strategies trade rarely, so they need a longer selection window to be judged.
    lookback_days: float | None = None   # None -> settings.selection_lookback_days
    min_trades: int | None = None        # None -> settings.selector_min_trades
    side: str = "long"                   # "short" strategies need perpetual futures (research only)

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover - abstract
        raise NotImplementedError

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self.compute(df).reindex(df.index)
        out["entry"] = out["entry"].fillna(False).astype(bool)
        out["exit"] = out.get("exit", pd.Series(False, index=df.index)).fillna(False).astype(bool)
        valid = (out.stop_dist > 0) & (out.target_dist > 0)
        out["entry"] &= valid.fillna(False)
        out.iloc[: self.warmup, out.columns.get_loc("entry")] = False
        return out
