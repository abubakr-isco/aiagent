"""Technical indicators implemented from their textbook definitions.

All functions are causal: the value at index ``i`` depends only on inputs at
indices ``<= i``. Warm-up values are NaN rather than fabricated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def _seeded_recursive(values: np.ndarray, period: int, alpha: float) -> np.ndarray:
    """Exponential smoothing seeded with the SMA of the first ``period`` valid values."""
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < period:
        return out
    start = valid[0]
    seed_end = start + period
    if np.isnan(values[start:seed_end]).any():
        return out
    out[seed_end - 1] = values[start:seed_end].mean()
    prev = out[seed_end - 1]
    for i in range(seed_end, len(values)):
        v = values[i]
        if np.isnan(v):
            out[i] = prev
            continue
        prev = alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def ema(series: pd.Series, period: int) -> pd.Series:
    """EMA with alpha = 2/(period+1), seeded by SMA(period)."""
    values = series.to_numpy(dtype=float)
    return pd.Series(_seeded_recursive(values, period, 2.0 / (period + 1)), index=series.index)


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (alpha = 1/period), seeded by SMA(period)."""
    values = series.to_numpy(dtype=float)
    return pd.Series(_seeded_recursive(values, period, 1.0 / period), index=series.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out.where(avg_gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), period)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan
    tr = true_range(high, low, close)
    tr.iloc[0] = np.nan
    atr_ = rma(tr, period)
    plus_di = 100 * rma(plus_dm, period) / atr_
    minus_di = 100 * rma(minus_dm, period) / atr_
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return pd.DataFrame({"adx": rma(dx, period), "plus_di": plus_di, "minus_di": minus_di})


def pivots(high: pd.Series, low: pd.Series, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Fractal swing points.

    A swing high at bar ``i`` is only *known* at bar ``i + right``; column
    ``confirmed_at`` records that index so callers cannot peek into the future.
    """
    n = len(high)
    h = high.to_numpy(dtype=float)
    lo = low.to_numpy(dtype=float)
    is_high = np.zeros(n, dtype=bool)
    is_low = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        window_h = h[i - left : i + right + 1]
        window_l = lo[i - left : i + right + 1]
        if h[i] == window_h.max() and (window_h[:left] < h[i]).all():
            is_high[i] = True
        if lo[i] == window_l.min() and (window_l[:left] > lo[i]).all():
            is_low[i] = True
    return pd.DataFrame(
        {"swing_high": is_high, "swing_low": is_low, "confirmed_at": np.arange(n) + right},
        index=high.index,
    )
