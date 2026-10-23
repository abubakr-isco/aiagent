"""Multi-timeframe feature extraction.

The same code path is used live and in the backtester: indicators are
pre-computed on a (causal) frame, then :meth:`MarketAnalyzer.analyze_frame`
reads only rows ``<= idx``. Swing points are used only once confirmed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind
from .constants import Timeframe
from .models import Tick


@dataclass(frozen=True)
class AnalyzerConfig:
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    slope_bars: int = 5
    pivot_left: int = 3
    pivot_right: int = 3
    sr_lookback: int = 150
    sr_cluster_atr: float = 0.5
    breakout_lookback: int = 10
    retest_tolerance_atr: float = 0.3
    atr_rank_window: int = 100
    volume_window: int = 20

    @property
    def min_bars(self) -> int:
        return self.ema_slow + self.slope_bars + 5


@dataclass(frozen=True)
class Level:
    price: float
    touches: int
    kind: str  # support | resistance


@dataclass(frozen=True)
class TimeframeAnalysis:
    timeframe: str
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    prev_high: float
    prev_low: float
    ema_fast: float
    ema_mid: float
    ema_slow: float
    ema_mid_slope_atr: float
    rsi: float
    rsi_prev: float
    macd_hist: float
    macd_hist_prev: float
    atr: float
    atr_rank: float
    adx: float
    plus_di: float
    minus_di: float
    trend: str  # UP | DOWN | NEUTRAL
    structure: str  # BULLISH | BEARISH | MIXED | UNKNOWN
    structure_labels: tuple[str, ...]
    last_swing_high: float | None
    last_swing_low: float | None
    support: float | None
    resistance: float | None
    breakout: str | None  # UP | DOWN
    breakout_level: float | None
    retest: bool
    volume_kind: str | None  # real | tick | None
    volume_ratio: float | None
    recent_lows: tuple[float, ...] = field(default=())
    recent_highs: tuple[float, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bar_time"] = self.bar_time.isoformat()
        return {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in d.items()}


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    time: datetime
    frames: dict[str, TimeframeAnalysis]
    bid: float | None = None
    ask: float | None = None
    point: float | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    def frame(self, tf: Timeframe | str) -> TimeframeAnalysis | None:
        return self.frames.get(tf.value if isinstance(tf, Timeframe) else tf)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "time": self.time.isoformat(),
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "frames": {k: v.to_dict() for k, v in self.frames.items()},
        }


class PreparedFrame:
    """Indicator columns as numpy arrays for fast, index-based causal access."""

    def __init__(self, df: pd.DataFrame, timeframe: Timeframe, cfg: AnalyzerConfig):
        self.timeframe = timeframe
        self.df = df.reset_index(drop=True)
        d = self.df
        self.time = pd.to_datetime(d["time"], utc=True)
        self.close_time = self.time + pd.Timedelta(minutes=timeframe.minutes)
        close, high, low = d["close"].astype(float), d["high"].astype(float), d["low"].astype(float)
        self.o = d["open"].to_numpy(float)
        self.h = high.to_numpy()
        self.l = low.to_numpy()
        self.c = close.to_numpy()
        self.ema_fast = ind.ema(close, cfg.ema_fast).to_numpy()
        self.ema_mid = ind.ema(close, cfg.ema_mid).to_numpy()
        self.ema_slow = ind.ema(close, cfg.ema_slow).to_numpy()
        self.rsi = ind.rsi(close, cfg.rsi_period).to_numpy()
        m = ind.macd(close)
        self.macd_hist = m["macd_hist"].to_numpy()
        self.atr = ind.atr(high, low, close, cfg.atr_period).to_numpy()
        a = ind.adx(high, low, close, cfg.adx_period)
        self.adx = a["adx"].to_numpy()
        self.plus_di = a["plus_di"].to_numpy()
        self.minus_di = a["minus_di"].to_numpy()
        piv = ind.pivots(high, low, cfg.pivot_left, cfg.pivot_right)
        self.swing_high_idx = np.flatnonzero(piv["swing_high"].to_numpy())
        self.swing_low_idx = np.flatnonzero(piv["swing_low"].to_numpy())
        self.tick_volume = d["tick_volume"].to_numpy(float) if "tick_volume" in d else np.zeros(len(d))
        self.real_volume = d["real_volume"].to_numpy(float) if "real_volume" in d else np.zeros(len(d))

    def __len__(self) -> int:
        return len(self.c)

    def index_closed_by(self, ts: pd.Timestamp) -> int:
        """Index of the last bar fully closed at ``ts`` (-1 if none)."""
        return int(self.close_time.searchsorted(ts, side="right")) - 1


class MarketAnalyzer:
    def __init__(self, config: AnalyzerConfig | None = None):
        self.cfg = config or AnalyzerConfig()

    def prepare(self, df: pd.DataFrame, timeframe: Timeframe) -> PreparedFrame:
        return PreparedFrame(df, timeframe, self.cfg)

    # ------------------------------------------------------------------ levels
    def _confirmed(self, idx_array: np.ndarray, idx: int) -> np.ndarray:
        return idx_array[idx_array + self.cfg.pivot_right <= idx]

    def _cluster(self, prices: list[float], atr: float, kind: str) -> list[Level]:
        if not prices:
            return []
        tol = self.cfg.sr_cluster_atr * atr
        levels: list[list[float]] = []
        for p in sorted(prices):
            if levels and p - levels[-1][-1] <= tol:
                levels[-1].append(p)
            else:
                levels.append([p])
        return [Level(price=float(np.mean(group)), touches=len(group), kind=kind) for group in levels]

    def analyze_frame(self, pf: PreparedFrame, idx: int) -> TimeframeAnalysis | None:
        cfg = self.cfg
        if idx < cfg.min_bars or idx >= len(pf) or np.isnan(pf.ema_slow[idx]) or np.isnan(pf.atr[idx]):
            return None
        close = pf.c[idx]
        atr = float(pf.atr[idx])
        slope_ref = pf.ema_mid[idx - cfg.slope_bars]
        slope = float((pf.ema_mid[idx] - slope_ref) / atr) if atr > 0 else 0.0

        if close > pf.ema_slow[idx] and pf.ema_fast[idx] > pf.ema_mid[idx] > pf.ema_slow[idx] and slope > 0:
            trend = "UP"
        elif close < pf.ema_slow[idx] and pf.ema_fast[idx] < pf.ema_mid[idx] < pf.ema_slow[idx] and slope < 0:
            trend = "DOWN"
        else:
            trend = "NEUTRAL"

        highs_idx = self._confirmed(pf.swing_high_idx, idx)
        lows_idx = self._confirmed(pf.swing_low_idx, idx)
        labels: list[str] = []
        structure = "UNKNOWN"
        if len(highs_idx) >= 2 and len(lows_idx) >= 2:
            h1, h2 = pf.h[highs_idx[-2]], pf.h[highs_idx[-1]]
            l1, l2 = pf.l[lows_idx[-2]], pf.l[lows_idx[-1]]
            labels = ["HH" if h2 > h1 else "LH", "HL" if l2 > l1 else "LL"]
            if labels == ["HH", "HL"]:
                structure = "BULLISH"
            elif labels == ["LH", "LL"]:
                structure = "BEARISH"
            else:
                structure = "MIXED"

        window_start = idx - cfg.sr_lookback
        recent_h = [float(pf.h[i]) for i in highs_idx if i >= window_start]
        recent_l = [float(pf.l[i]) for i in lows_idx if i >= window_start]
        levels = self._cluster(recent_h, atr, "resistance") + self._cluster(recent_l, atr, "support")
        above = [lv.price for lv in levels if lv.price > close]
        below = [lv.price for lv in levels if lv.price < close]
        resistance = min(above) if above else None
        support = max(below) if below else None

        breakout, breakout_level, retest = self._breakout(pf, idx, highs_idx, lows_idx, atr)

        atr_hist = pf.atr[max(0, idx - cfg.atr_rank_window + 1) : idx + 1]
        atr_hist = atr_hist[~np.isnan(atr_hist)]
        atr_rank = float((atr_hist <= atr).mean()) if len(atr_hist) else 0.5

        volume_kind, volume_ratio = None, None
        vol_src = pf.real_volume if pf.real_volume[max(0, idx - cfg.volume_window) : idx + 1].sum() > 0 else pf.tick_volume
        window = vol_src[max(0, idx - cfg.volume_window) : idx]
        if window.sum() > 0:
            volume_kind = "real" if vol_src is pf.real_volume else "tick"
            volume_ratio = float(vol_src[idx] / window.mean())

        return TimeframeAnalysis(
            timeframe=pf.timeframe.value,
            bar_time=pf.time.iloc[idx].to_pydatetime(),
            open=float(pf.o[idx]), high=float(pf.h[idx]), low=float(pf.l[idx]), close=float(close),
            prev_close=float(pf.c[idx - 1]), prev_high=float(pf.h[idx - 1]), prev_low=float(pf.l[idx - 1]),
            ema_fast=float(pf.ema_fast[idx]), ema_mid=float(pf.ema_mid[idx]), ema_slow=float(pf.ema_slow[idx]),
            ema_mid_slope_atr=slope, rsi=float(pf.rsi[idx]), rsi_prev=float(pf.rsi[idx - 1]),
            macd_hist=float(pf.macd_hist[idx]), macd_hist_prev=float(pf.macd_hist[idx - 1]),
            atr=atr, atr_rank=atr_rank, adx=float(pf.adx[idx]), plus_di=float(pf.plus_di[idx]), minus_di=float(pf.minus_di[idx]),
            trend=trend, structure=structure, structure_labels=tuple(labels),
            last_swing_high=float(pf.h[highs_idx[-1]]) if len(highs_idx) else None,
            last_swing_low=float(pf.l[lows_idx[-1]]) if len(lows_idx) else None,
            support=support, resistance=resistance, breakout=breakout, breakout_level=breakout_level, retest=retest,
            volume_kind=volume_kind, volume_ratio=volume_ratio,
            recent_lows=tuple(float(x) for x in pf.l[max(0, idx - 9) : idx + 1]),
            recent_highs=tuple(float(x) for x in pf.h[max(0, idx - 9) : idx + 1]),
        )

    def _breakout(self, pf: PreparedFrame, idx: int, highs_idx: np.ndarray, lows_idx: np.ndarray, atr: float) -> tuple[str | None, float | None, bool]:
        lb = self.cfg.breakout_lookback
        start = idx - lb
        if start < 1:
            return None, None, False
        tol = self.cfg.retest_tolerance_atr * atr
        # Levels must have been known before the breakout window started.
        old_highs = [pf.h[i] for i in highs_idx if i + self.cfg.pivot_right <= start and i >= start - self.cfg.sr_lookback]
        old_lows = [pf.l[i] for i in lows_idx if i + self.cfg.pivot_right <= start and i >= start - self.cfg.sr_lookback]
        closes = pf.c[start : idx + 1]
        best: tuple[str | None, float | None, bool] = (None, None, False)
        best_dist = np.inf
        for level in old_highs:
            if closes[0] < level and pf.c[idx] > level:
                cross = start + int(np.argmax(closes > level))
                retest = bool(pf.l[cross : idx + 1].min() <= level + tol)
                if abs(pf.c[idx] - level) < best_dist:
                    best, best_dist = ("UP", float(level), retest), abs(pf.c[idx] - level)
        for level in old_lows:
            if closes[0] > level and pf.c[idx] < level:
                cross = start + int(np.argmax(closes < level))
                retest = bool(pf.h[cross : idx + 1].max() >= level - tol)
                if abs(pf.c[idx] - level) < best_dist:
                    best, best_dist = ("DOWN", float(level), retest), abs(pf.c[idx] - level)
        return best

    # ---------------------------------------------------------------- snapshot
    def snapshot(
        self,
        symbol: str,
        frames: dict[Timeframe, pd.DataFrame],
        *,
        tick: Tick | None = None,
        point: float | None = None,
        now: datetime | None = None,
    ) -> MarketSnapshot:
        """Analyse the last closed bar of every timeframe (live usage)."""
        out: dict[str, TimeframeAnalysis] = {}
        for tf, df in frames.items():
            if df is None or df.empty:
                continue
            pf = self.prepare(df, tf)
            analysis = self.analyze_frame(pf, len(pf) - 1)
            if analysis is not None:
                out[tf.value] = analysis
        return MarketSnapshot(
            symbol=symbol, time=now or (tick.time if tick else datetime.now().astimezone()), frames=out,
            bid=tick.bid if tick else None, ask=tick.ask if tick else None, point=point,
        )

    def snapshot_at(self, symbol: str, prepared: dict[Timeframe, PreparedFrame], ts: pd.Timestamp) -> MarketSnapshot:
        """Analyse every timeframe as of time ``ts`` using only bars closed by then (backtests)."""
        out: dict[str, TimeframeAnalysis] = {}
        for tf, pf in prepared.items():
            idx = pf.index_closed_by(ts)
            if idx < 0:
                continue
            analysis = self.analyze_frame(pf, idx)
            if analysis is not None:
                out[tf.value] = analysis
        return MarketSnapshot(symbol=symbol, time=ts.to_pydatetime(), frames=out)
