"""Modular strategies. A strategy turns a MarketSnapshot into a Signal or a
documented list of reasons why no signal was produced. Strategies never size
positions or send orders.

Trend-pullback v1 rules (BUY; SELL is the mirror image):

1. H1 trend UP: close > EMA200, EMA20 > EMA50 > EMA200, EMA50 slope > 0.
2. H1 market structure is not bearish (not LH+LL).
3. M15 aligned: EMA20 > EMA50 and close > EMA50; M15 structure not bearish.
4. Pullback: within the last N M15 bars the low touched the EMA zone
   (EMA ± tolerance·ATR) without breaking EMA50 by more than max depth·ATR.
5. Confirmation: last closed M15 candle is bullish, closes above the pullback
   EMA and above the previous close; RSI in [min, max] and rising; MACD
   histogram rising (optional).
6. H4 context is not in a DOWN trend (optional filter).
7. Volatility is not extreme (ATR rank < 0.95 on M15 and H1), spread is small
   relative to ATR, and the bar closed inside the configured UTC session.
8. SL = lowest low of the pullback window - buffer·ATR, at least min_sl·ATR
   away and at most max_sl·ATR. TP = entry + rr_target·risk.
9. The nearest resistance must leave at least min_room_rr·risk of room.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from .config import StrategyParams
from .market_analyzer import MarketSnapshot, TimeframeAnalysis
from .models import Check, Side, Signal, StrategyEvaluation


class Strategy(ABC):
    strategy_id: str
    version: str

    def __init__(self, params: StrategyParams | None = None):
        self.params = params or StrategyParams()

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot, *, spread: float | None, now_utc: datetime) -> StrategyEvaluation: ...

    @abstractmethod
    def exit_reason(self, side: Side, snapshot: MarketSnapshot, bars_held: int) -> str | None: ...


def _fmt(x: float | None, digits: int = 5) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _digits(point: float | None) -> int:
    return max(0, round(-math.log10(point))) if point else 5


class TrendPullbackStrategy(Strategy):
    strategy_id = "trend_pullback"
    version = "1.0.0"

    def evaluate(self, snapshot: MarketSnapshot, *, spread: float | None, now_utc: datetime) -> StrategyEvaluation:
        p = self.params
        trend = snapshot.frame(p.trend_tf)
        entry_tf = snapshot.frame(p.entry_tf)
        context = snapshot.frame(p.context_tf)
        checks: list[Check] = []
        d = _digits(snapshot.point)

        def fmt(x: float | None) -> str:
            return _fmt(x, d)

        if trend is None or entry_tf is None:
            checks.append(Check("data_available", False, f"missing {p.trend_tf if trend is None else p.entry_tf} analysis (not enough closed bars)"))
            return StrategyEvaluation(snapshot.symbol, self.strategy_id, None, checks)
        checks.append(Check("data_available", True, f"{p.trend_tf}/{p.entry_tf} bars available"))

        if trend.trend == "UP":
            side = Side.BUY
        elif trend.trend == "DOWN":
            side = Side.SELL
        else:
            checks.append(Check("h1_trend", False, f"{p.trend_tf} trend is NEUTRAL (EMA alignment/slope not consistent)"))
            return StrategyEvaluation(snapshot.symbol, self.strategy_id, None, checks)

        long = side is Side.BUY
        sgn = side.sign
        checks.append(Check("h1_trend", True, f"{p.trend_tf} trend {trend.trend}: close {fmt(trend.close)} vs EMA200 {fmt(trend.ema_slow)}, EMA50 slope {trend.ema_mid_slope_atr:+.2f} ATR"))
        bad_structure = "BEARISH" if long else "BULLISH"
        checks.append(Check("h1_structure", trend.structure != bad_structure, f"{p.trend_tf} structure {trend.structure} {'/'.join(trend.structure_labels)}"))

        aligned = (entry_tf.ema_fast - entry_tf.ema_mid) * sgn > 0 and (entry_tf.close - entry_tf.ema_mid) * sgn > 0
        checks.append(Check("m15_alignment", aligned, f"{p.entry_tf} EMA20 {fmt(entry_tf.ema_fast)} / EMA50 {fmt(entry_tf.ema_mid)}, close {fmt(entry_tf.close)}"))
        checks.append(Check("m15_structure", entry_tf.structure != bad_structure, f"{p.entry_tf} structure {entry_tf.structure}"))

        atr = entry_tf.atr
        ref_ema = entry_tf.ema_fast if p.pullback_ema == "fast" else entry_tf.ema_mid
        lows = entry_tf.recent_lows[-p.pullback_lookback :]
        highs = entry_tf.recent_highs[-p.pullback_lookback :]
        if long:
            extreme = min(lows)
            touched = extreme <= ref_ema + p.pullback_tolerance_atr * atr
            too_deep = extreme < entry_tf.ema_mid - p.max_pullback_depth_atr * atr
        else:
            extreme = max(highs)
            touched = extreme >= ref_ema - p.pullback_tolerance_atr * atr
            too_deep = extreme > entry_tf.ema_mid + p.max_pullback_depth_atr * atr
        checks.append(Check("pullback", touched, f"{'low' if long else 'high'} of last {p.pullback_lookback} bars {fmt(extreme)} vs EMA{20 if p.pullback_ema == 'fast' else 50} {fmt(ref_ema)} ± {p.pullback_tolerance_atr} ATR"))
        checks.append(Check("pullback_depth", not too_deep, f"pullback {'did not break' if not too_deep else 'broke'} EMA50 by more than {p.max_pullback_depth_atr} ATR"))

        candle_dir = (entry_tf.close - entry_tf.open) * sgn > 0
        reclaimed = (entry_tf.close - ref_ema) * sgn > 0 and (entry_tf.close - entry_tf.prev_close) * sgn > 0
        checks.append(Check("confirmation_candle", candle_dir and reclaimed, f"last {p.entry_tf} candle {'bullish' if entry_tf.close > entry_tf.open else 'bearish'}, close {fmt(entry_tf.close)} vs prev {fmt(entry_tf.prev_close)}"))

        lo, hi = (p.rsi_long_min, p.rsi_long_max) if long else (p.rsi_short_min, p.rsi_short_max)
        rsi_ok = lo <= entry_tf.rsi <= hi and (entry_tf.rsi - entry_tf.rsi_prev) * sgn > 0
        checks.append(Check("rsi", rsi_ok, f"RSI {entry_tf.rsi:.1f} (prev {entry_tf.rsi_prev:.1f}), allowed [{lo:.0f}, {hi:.0f}] and turning {'up' if long else 'down'}"))
        if p.require_macd_confirmation:
            macd_ok = (entry_tf.macd_hist - entry_tf.macd_hist_prev) * sgn > 0
            checks.append(Check("macd", macd_ok, f"MACD hist {entry_tf.macd_hist:.6f} vs prev {entry_tf.macd_hist_prev:.6f}"))

        if p.use_context_filter:
            if context is None:
                checks.append(Check("h4_context", True, f"{p.context_tf} unavailable, filter skipped", mandatory=False))
            else:
                opposite = "DOWN" if long else "UP"
                checks.append(Check("h4_context", context.trend != opposite, f"{p.context_tf} trend {context.trend}"))

        if p.block_high_volatility:
            calm = entry_tf.atr_rank < 0.95 and trend.atr_rank < 0.95
            checks.append(Check("volatility", calm, f"ATR rank {p.entry_tf} {entry_tf.atr_rank:.2f}, {p.trend_tf} {trend.atr_rank:.2f} (block >= 0.95)"))

        hour = now_utc.astimezone(timezone.utc).hour
        in_session = p.session_start_utc <= hour < p.session_end_utc
        checks.append(Check("session", in_session, f"UTC hour {hour}, allowed [{p.session_start_utc}, {p.session_end_utc})"))

        if spread is None:
            checks.append(Check("spread", False, "spread unknown"))
            spread_val = 0.0
        else:
            spread_val = spread
            ratio = spread / atr if atr > 0 else float("inf")
            checks.append(Check("spread", ratio <= p.max_spread_atr_ratio, f"spread {spread:.5f} = {ratio:.2f} ATR (max {p.max_spread_atr_ratio})"))

        # Entry price: BUY fills at ask, SELL at bid. Bars are bid prices.
        if long:
            entry = (snapshot.ask if snapshot.ask is not None else entry_tf.close + spread_val)
            structural_sl = min(lows) - p.sl_atr_buffer * atr
            risk = max(entry - structural_sl, p.min_sl_atr * atr)
            sl = entry - risk
        else:
            entry = snapshot.bid if snapshot.bid is not None else entry_tf.close
            # A SELL stop triggers on the ask, so add the spread.
            structural_sl = max(highs) + p.sl_atr_buffer * atr + spread_val
            risk = max(structural_sl - entry, p.min_sl_atr * atr)
            sl = entry + risk
        checks.append(Check("stop_distance", risk <= p.max_sl_atr * atr, f"SL {fmt(sl)} risk {risk / atr:.2f} ATR (allowed [{p.min_sl_atr}, {p.max_sl_atr}])"))
        tp = entry + sgn * p.rr_target * risk
        if snapshot.point:
            digits = max(0, round(-math.log10(snapshot.point)))
            entry, sl, tp = round(float(entry), digits), round(float(sl), digits), round(float(tp), digits)

        opposing = (entry_tf.resistance if long else entry_tf.support)
        htf_opposing = (trend.resistance if long else trend.support)
        candidates = [lv for lv in (opposing, htf_opposing) if lv is not None and (lv - entry) * sgn > 0]
        if candidates:
            nearest = min(candidates, key=lambda lv: abs(lv - entry))
            room = abs(nearest - entry) / risk
            checks.append(Check("room_to_target", room >= p.min_room_rr, f"nearest {'resistance' if long else 'support'} {fmt(nearest)} is {room:.2f}R away (min {p.min_room_rr}R)"))
        else:
            checks.append(Check("room_to_target", True, "no opposing level within lookback", mandatory=False))

        checks.append(Check("risk_reward", p.rr_target >= p.min_room_rr, f"target RR {p.rr_target:.2f}"))
        if entry_tf.retest and entry_tf.breakout == ("UP" if long else "DOWN"):
            checks.append(Check("breakout_retest", True, f"retest of broken level {fmt(entry_tf.breakout_level)}", mandatory=False))
        if entry_tf.volume_kind:
            checks.append(Check("volume", True, f"{entry_tf.volume_kind} volume ratio {entry_tf.volume_ratio:.2f} (informational)", mandatory=False))

        failed = [c for c in checks if c.mandatory and not c.passed]
        if failed:
            return StrategyEvaluation(snapshot.symbol, self.strategy_id, None, checks, side_considered=side)

        signal = Signal(
            strategy_id=self.strategy_id, strategy_version=self.version, symbol=snapshot.symbol, side=side,
            timeframe=p.entry_tf, bar_time=entry_tf.bar_time, entry=float(entry), stop_loss=float(sl), take_profit=float(tp),
            atr=float(atr), checks=checks, features=self.features(side, trend, entry_tf, context, spread_val, risk),
        )
        return StrategyEvaluation(snapshot.symbol, self.strategy_id, signal, checks, side_considered=side)

    @staticmethod
    def features(side: Side, trend: TimeframeAnalysis, entry: TimeframeAnalysis, context: TimeframeAnalysis | None, spread: float, risk: float) -> dict[str, float]:
        sgn = side.sign
        f = {
            "h1_adx": trend.adx,
            "h1_slope_atr": trend.ema_mid_slope_atr * sgn,
            "h1_structure_aligned": 1.0 if trend.structure == ("BULLISH" if sgn > 0 else "BEARISH") else 0.0,
            "m15_adx": entry.adx,
            "m15_rsi_centered": (entry.rsi - 50) * sgn,
            "m15_atr_rank": entry.atr_rank,
            "h1_atr_rank": trend.atr_rank,
            "dist_ema_fast_atr": (entry.close - entry.ema_fast) * sgn / entry.atr,
            "spread_risk_ratio": spread / risk if risk > 0 else 1.0,
            "risk_atr": risk / entry.atr,
            "h4_aligned": 0.5 if context is None else (1.0 if context.trend == ("UP" if sgn > 0 else "DOWN") else 0.0),
            "retest": 1.0 if entry.retest and entry.breakout == ("UP" if sgn > 0 else "DOWN") else 0.0,
        }
        return {k: float(v) for k, v in f.items()}

    def exit_reason(self, side: Side, snapshot: MarketSnapshot, bars_held: int) -> str | None:
        p = self.params
        trend = snapshot.frame(p.trend_tf)
        if p.exit_on_trend_flip and trend is not None:
            if (side is Side.BUY and trend.trend == "DOWN") or (side is Side.SELL and trend.trend == "UP"):
                return f"{p.trend_tf} trend flipped to {trend.trend}"
        if p.max_hold_bars and bars_held >= p.max_hold_bars:
            return f"max holding time reached ({bars_held} {p.entry_tf} bars)"
        return None


STRATEGIES: dict[str, type[Strategy]] = {TrendPullbackStrategy.strategy_id: TrendPullbackStrategy}


def build_strategy(strategy_id: str, params: dict | None = None) -> Strategy:
    try:
        cls = STRATEGIES[strategy_id]
    except KeyError as exc:
        raise ValueError(f"unknown strategy {strategy_id!r}") from exc
    return cls(StrategyParams.from_dict(params or {}))
