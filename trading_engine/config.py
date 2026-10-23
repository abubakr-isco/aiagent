"""Engine configuration objects.

Defaults are conservative starting values for DEMO testing - they are not
universally "safe". Every value is overridable from the backend settings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_total_open_risk_pct: float = 1.5
    daily_loss_limit_pct: float = 2.0
    # Also block a new trade if (today's loss + remaining risk of open positions
    # + this trade's risk) could breach the daily limit when all stops are hit.
    daily_limit_includes_open_risk: bool = True
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1  # forbids grids / averaging
    min_rr: float = 1.5
    max_spread_points: dict[str, int] = field(default_factory=dict)  # per-symbol hard cap, optional
    max_spread_atr_ratio: float = 0.25  # spread must not exceed this share of entry-TF ATR
    spread_anomaly_factor: float = 3.0  # spread > factor * rolling median -> anomaly
    max_quote_age_s: float = 60.0  # local time since the tick last changed
    max_margin_usage_pct: float = 30.0  # used margin after the trade, % of equity
    min_margin_level_pct: float = 300.0
    risk_calc_tolerance_pct: float = 15.0  # cross-check of order_calc_profit vs tick value
    block_if_foreign_position_without_sl: bool = True
    approval_ttl_s: float = 10.0
    trading_day_utc_offset_hours: int = 0  # broker day boundary relative to UTC

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskConfig":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        errors = []
        if not 0 < self.risk_per_trade_pct <= 2.0:
            errors.append("risk_per_trade_pct must be in (0, 2]")
        if self.max_total_open_risk_pct < self.risk_per_trade_pct:
            errors.append("max_total_open_risk_pct must be >= risk_per_trade_pct")
        if not 0 < self.daily_loss_limit_pct <= 10:
            errors.append("daily_loss_limit_pct must be in (0, 10]")
        if self.max_open_positions < 1:
            errors.append("max_open_positions must be >= 1")
        if self.max_positions_per_symbol != 1:
            errors.append("max_positions_per_symbol must be 1 (grids/averaging are forbidden)")
        if self.min_rr < 1.0:
            errors.append("min_rr must be >= 1.0")
        return errors


@dataclass(frozen=True)
class StrategyParams:
    trend_tf: str = "H1"
    entry_tf: str = "M15"
    context_tf: str = "H4"
    trigger_tf: str = "M5"  # informational only in v1
    pullback_ema: str = "fast"  # fast (EMA20) | mid (EMA50)
    pullback_lookback: int = 5
    pullback_tolerance_atr: float = 0.3
    max_pullback_depth_atr: float = 0.5  # closes beyond EMA50 by more than this cancel the setup
    rsi_long_min: float = 40.0
    rsi_long_max: float = 70.0
    rsi_short_min: float = 30.0
    rsi_short_max: float = 60.0
    require_macd_confirmation: bool = True
    sl_atr_buffer: float = 0.2
    min_sl_atr: float = 0.5
    max_sl_atr: float = 3.0
    rr_target: float = 2.0
    min_room_rr: float = 1.5  # nearest opposing level must be at least this many R away
    use_context_filter: bool = True
    block_high_volatility: bool = True
    session_start_utc: int = 7
    session_end_utc: int = 20
    max_spread_atr_ratio: float = 0.25
    # exit management
    exit_on_trend_flip: bool = True
    max_hold_bars: int = 96  # entry-TF bars (24h on M15)
    breakeven_at_r: float = 0.0  # 0 disables

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyParams":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionConfig:
    magic: int = 26_102_301
    deviation_points: int = 10
    max_price_retries: int = 2
    max_slippage_points: int = 20
    verify_attempts: int = 5
    verify_interval_s: float = 0.5
    timeout_retry_policy: str = "after_verification"  # never | after_verification
    comment_prefix: str = "AIA"


@dataclass(frozen=True)
class AgentConfig:
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "XAUUSD")
    loop_interval_s: float = 15.0
    bars_per_timeframe: int = 600
    reconnect_backoff_s: tuple[float, ...] = (1, 2, 5, 10, 30, 60)
    ai_min_quality: float = 0.55
    allow_live_env: bool = False  # mirrors ALLOW_LIVE_TRADING env flag
