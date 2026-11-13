from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ACK = "I_ACCEPT_LIVE_TRADING_RISK"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Mode / live gates -------------------------------------------------
    # paper   : simulated fills, mainnet public data
    # testnet : real orders on Binance Spot Testnet (fake funds), signals from mainnet public data
    # live    : real money
    trading_mode: Literal["paper", "testnet", "live"] = "paper"
    enable_live_trading: bool = False
    live_armed: bool = False
    live_trading_ack: str = ""
    live_max_quote_allocation: float = 100.0

    binance_api_key: str = ""               # MAINNET key (live mode only)
    binance_api_secret: str = ""
    binance_testnet_api_key: str = ""       # https://testnet.binance.vision key (testnet mode only)
    binance_testnet_api_secret: str = ""
    binance_testnet: bool = False           # deprecated v1 flag; use TRADING_MODE=testnet

    # --- Market --------------------------------------------------------------
    # spot    : long-only, Binance Spot
    # futures : long + short, Binance USDⓈ-M perpetuals, ALWAYS 1x leverage, isolated margin
    market: Literal["spot", "futures"] = "spot"
    futures_fee_rate: float = 0.0005        # USDⓈ-M taker; replaces FEE_RATE when MARKET=futures
    futures_strategies: str = "trend_follow_4h,turtle_breakout_4h,trend_follow_short_4h"
    # Futures test venue: "testnet" = testnet.binancefuture.com, "demo" = Binance Demo Trading
    futures_testnet_venue: Literal["testnet", "demo"] = "demo"   # testnet.binancefuture.com now redirects to Demo Trading
    binance_futures_testnet_api_key: str = ""
    binance_futures_testnet_api_secret: str = ""
    # auto -> "always_on" for futures (research: weekly re-selection hurt trend strategies), "selector" for spot
    selection_mode: Literal["auto", "selector", "always_on"] = "auto"

    @property
    def uses_exchange_orders(self) -> bool:
        return self.trading_mode in ("testnet", "live")

    @property
    def effective_selection_mode(self) -> str:
        if self.selection_mode != "auto":
            return self.selection_mode
        return "always_on" if self.market == "futures" else "selector"

    @property
    def trade_symbols(self) -> list[str]:
        """Exchange symbols actually traded: 'BTC/USDT' on spot, 'BTC/USDT:USDT' on perpetuals."""
        if self.market == "futures":
            return [s if ":" in s else f"{s}:{self.quote_currency}" for s in self.symbol_list]
        return self.symbol_list

    @property
    def order_api_credentials(self) -> tuple[str, str]:
        if self.trading_mode == "testnet" and self.market == "futures":
            return self.binance_futures_testnet_api_key, self.binance_futures_testnet_api_secret
        if self.trading_mode == "testnet":
            return self.binance_testnet_api_key, self.binance_testnet_api_secret
        if self.trading_mode == "live":
            return self.binance_api_key, self.binance_api_secret
        return "", ""

    # --- Universe ----------------------------------------------------------
    symbols: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,BNB/USDT,NEAR/USDT,UNI/USDT,DOGE/USDT,SUI/USDT,TRX/USDT,LINK/USDT,AVAX/USDT,ADA/USDT,LTC/USDT"
    quote_currency: str = "USDT"
    # Comma list of strategy names the selector may choose from; empty = all registered strategies.
    # Default = the 4h strategies: the only family near breakeven after costs in the 14-coin walk-forward.
    enabled_strategies: str = "trend_follow_4h,turtle_breakout_4h"

    # --- Account / risk ----------------------------------------------------
    starting_equity: float = 100.0          # paper equity (live uses real balance, capped)
    risk_per_trade: float = 0.005           # fraction of equity lost if stop is hit (incl. costs)
    max_position_fraction: float = 0.95     # spot: never more than this fraction of equity in one position
    daily_loss_limit: float = 0.02
    weekly_loss_limit: float = 0.05
    max_open_positions: int = 2
    max_trades_per_day: int = 8
    max_consecutive_losses: int = 3
    cooldown_minutes: int = 240             # pause after consecutive losses, then auto-resume
    max_spread_pct: float = 0.001

    # --- Costs (used identically by backtester, selector and paper broker) --
    fee_rate: float = 0.001                 # Binance spot taker without BNB discount
    slippage_rate: float = 0.0002

    # --- Strategy selector -------------------------------------------------
    selection_lookback_days: int = 60
    selector_refresh_hours: float = 6.0
    selector_min_trades: int = 12
    selector_min_profit_factor: float = 1.2
    selector_min_expectancy_pct: float = 0.05   # avg net % per trade required (0.05 = 0.05%)
    selector_max_drawdown_pct: float = 15.0

    # --- Loop --------------------------------------------------------------
    scan_interval_seconds: int = 20
    max_consecutive_errors_before_alert: int = 10

    # --- Infra -------------------------------------------------------------
    database_url: str = "postgresql+psycopg://scalper:scalper@db:5432/scalper"
    api_token: str = ""                     # required for /control endpoints
    openai_api_key: str = ""
    openai_model: str = ""
    ai_veto_enabled: bool = False           # AI can only veto, never create trades
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""
    log_level: str = "INFO"

    # --- Weekly report (Telegram) --------------------------------------------
    weekly_report_enabled: bool = True
    weekly_report_weekday: int = 0          # 0 = Monday ... 6 = Sunday (UTC)
    weekly_report_hour_utc: int = 8

    @property
    def symbol_list(self) -> list[str]:
        return [x.strip() for x in self.symbols.split(",") if x.strip()]

    @property
    def strategy_filter(self) -> set[str]:
        raw = self.futures_strategies if self.market == "futures" else self.enabled_strategies
        return {x.strip() for x in raw.split(",") if x.strip()}

    @property
    def allowed_chat_ids(self) -> list[str]:
        return [x.strip() for x in self.telegram_allowed_chat_ids.split(",") if x.strip()]

    @model_validator(mode="after")
    def live_gates(self):
        if self.trading_mode == "live":
            if not self.enable_live_trading or not self.live_armed:
                raise ValueError("Live mode requires ENABLE_LIVE_TRADING=true and LIVE_ARMED=true")
            if self.live_trading_ack != ACK:
                raise ValueError("Missing exact LIVE_TRADING_ACK acknowledgement")
            if not self.binance_api_key or not self.binance_api_secret:
                raise ValueError("Live mode requires Binance API credentials")
            if self.live_max_quote_allocation > 100:
                raise ValueError("This build caps live quote allocation at 100 USDT")
            if self.binance_testnet:
                raise ValueError("BINANCE_TESTNET=true conflicts with TRADING_MODE=live; use TRADING_MODE=testnet")
        if self.trading_mode == "testnet" and self.market == "futures":
            if not self.binance_futures_testnet_api_key or not self.binance_futures_testnet_api_secret:
                raise ValueError("Futures testnet mode requires BINANCE_FUTURES_TESTNET_API_KEY and "
                                 "BINANCE_FUTURES_TESTNET_API_SECRET (https://testnet.binancefuture.com)")
        elif self.trading_mode == "testnet":
            if not self.binance_testnet_api_key or not self.binance_testnet_api_secret:
                raise ValueError("Testnet mode requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET "
                                 "(create them at https://testnet.binance.vision)")
        if self.market == "futures":
            # One cost model everywhere (backtest, selector, sizing, paper fills, reports).
            self.fee_rate = self.futures_fee_rate
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("RISK_PER_TRADE must be in (0, 0.02]")
        if self.ai_veto_enabled and not (self.openai_api_key and self.openai_model):
            raise ValueError("AI_VETO_ENABLED requires OPENAI_API_KEY and OPENAI_MODEL")
        return self


settings = Settings()
