"""Strategy selector: the bot decides which strategy to trade on which symbol.

Every SELECTOR_REFRESH_HOURS it re-runs the backtester for every (strategy, symbol) pair on recent
history, using exactly the same cost model as live/paper, and enables only pairs that pass the
eligibility rules (min trades, profit factor, expectancy, drawdown). Eligible pairs are ranked by a
t-statistic-like score; when several fire at once the highest score wins. If nothing is eligible the
bot stays flat — that is the intended behaviour, not a bug.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.backtest import eligible, metrics, run_backtest, selection_params
from app.config import settings
from app.exchange import TF_MS, history
from app.models import StrategyScore, aware
from app.strategies import Strategy, active_strategies

log = logging.getLogger(__name__)


class Selector:
    def __init__(self, exchange, strategies: list[Strategy] | None = None, symbols: list[str] | None = None):
        self.ex = exchange
        self.strategies = strategies if strategies is not None else active_strategies()
        self.symbols = symbols or settings.trade_symbols
        self.scores: dict[tuple[str, str], dict] = {}
        self.computed_at: datetime | None = None

    # ---- persistence -------------------------------------------------------
    def load(self, s) -> bool:
        rows = s.scalars(select(StrategyScore)).all()
        names = {st.name for st in self.strategies}
        rows = [r for r in rows if r.strategy in names and r.symbol in self.symbols]
        expected = {(n, sym) for n in names for sym in self.symbols}
        if not rows or {(r.strategy, r.symbol) for r in rows} != expected:
            return False   # coin/strategy list changed -> must re-score everything
        self.scores = {(r.strategy, r.symbol): dict(
            n=r.n, win_rate=r.win_rate, avg_ret_pct=r.avg_ret_pct, profit_factor=r.profit_factor,
            total_ret_pct=r.total_ret_pct, max_dd_pct=r.max_dd_pct, score=r.score, eligible=r.eligible,
            timeframe=r.timeframe, lookback_days=r.lookback_days) for r in rows}
        self.computed_at = min(aware(r.computed_at) for r in rows)
        return True

    def due(self) -> bool:
        if self.computed_at is None:
            return True
        return datetime.now(timezone.utc) - self.computed_at >= timedelta(hours=settings.selector_refresh_hours)

    # ---- evaluation --------------------------------------------------------
    def refresh(self, s) -> dict:
        now_ms = int(time.time() * 1000)
        scores = {}
        by_tf: dict[str, list[Strategy]] = {}
        for st in self.strategies:
            by_tf.setdefault(st.timeframe, []).append(st)
        for sym in self.symbols:
            for tf, group in by_tf.items():
                need_days = max(selection_params(st.name)[0] + st.warmup * TF_MS[tf] / 86_400_000 + 1
                                for st in group)
                df = history(self.ex, sym, tf, need_days)
                for st in group:
                    lb, min_n = selection_params(st.name)
                    trades = [t for t in run_backtest(st, df, sym) if t.entry_ts >= now_ms - lb * 86_400_000]
                    m = metrics(trades)
                    m.update(eligible=eligible(m, min_n), timeframe=tf, lookback_days=lb)
                    scores[(st.name, sym)] = m
        self.scores = scores
        self.computed_at = datetime.now(timezone.utc)
        s.execute(delete(StrategyScore))
        for (name, sym), m in scores.items():
            s.add(StrategyScore(computed_at=self.computed_at, strategy=name, symbol=sym, timeframe=m["timeframe"],
                                lookback_days=m["lookback_days"], n=m["n"], win_rate=m["win_rate"],
                                avg_ret_pct=m["avg_ret_pct"], profit_factor=m["profit_factor"],
                                total_ret_pct=m["total_ret_pct"], max_dd_pct=m["max_dd_pct"],
                                score=m["score"], eligible=m["eligible"]))
        s.commit()
        log.info("Selector refreshed: %d/%d pairs eligible", sum(m["eligible"] for m in scores.values()), len(scores))
        return scores

    def eligible_pairs(self) -> list[tuple[str, str, float]]:
        """[(strategy, symbol, score)] best first. In always-on mode every pair may trade; the recent
        backtest score is then only used to rank simultaneous signals."""
        always = settings.effective_selection_mode == "always_on"
        out = [(k[0], k[1], m["score"]) for k, m in self.scores.items() if always or m.get("eligible")]
        return sorted(out, key=lambda x: -x[2])

    def summary(self) -> str:
        if settings.effective_selection_mode == "always_on":
            return (f"Always-on mode: all {len(self.scores)} strategy/coin pairs trade "
                    f"({', '.join(sorted({k[0] for k in self.scores}))}).")
        pairs = self.eligible_pairs()
        if not pairs:
            return "No strategy currently eligible — bot stays flat."
        return "\n".join(f"{st} @ {sym}: score {sc:.2f}, PF {self.scores[(st, sym)]['profit_factor']:.2f}, "
                         f"avg {self.scores[(st, sym)]['avg_ret_pct']:.2f}%/trade, n={self.scores[(st, sym)]['n']}"
                         for st, sym, sc in pairs)
