"""Historical data utilities: MT5 CSV import, resampling and synthetic series.

Synthetic data exists ONLY to exercise the pipeline in tests and in the mock
terminal. Results computed on it say nothing about real-market profitability.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .constants import Timeframe
from .gateway import RATES_COLUMNS


def normalize_rates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    for col in RATES_COLUMNS:
        if col not in out.columns:
            out[col] = 0
    out = out[RATES_COLUMNS].sort_values("time").drop_duplicates("time").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float)
    return out


def load_mt5_csv(path: str | Path) -> pd.DataFrame:
    """Load bars exported from MT5 (History Center / "Export bars") or a generic CSV.

    MT5 export: tab separated, header ``<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>``.
    Generic: comma separated with ``time,open,high,low,close[,tick_volume,spread,real_volume]``.
    """
    path = Path(path)
    head = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    if "<DATE>" in head.upper():
        raw = pd.read_csv(path, sep="\t")
        raw.columns = [c.strip("<>").lower() for c in raw.columns]
        if "time" in raw.columns:
            stamp = raw["date"].astype(str) + " " + raw["time"].astype(str)
        else:
            stamp = raw["date"].astype(str)
        df = pd.DataFrame(
            {
                "time": pd.to_datetime(stamp, format="mixed", utc=True),
                "open": raw["open"],
                "high": raw["high"],
                "low": raw["low"],
                "close": raw["close"],
                "tick_volume": raw.get("tickvol", 0),
                "spread": raw.get("spread", 0),
                "real_volume": raw.get("vol", 0),
            }
        )
    else:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
    df = normalize_rates(df)
    validate_ohlc(df)
    return df


def validate_ohlc(df: pd.DataFrame) -> None:
    """Reject corrupt bars instead of silently trading on them."""
    if df.empty:
        return
    prices = df[["open", "high", "low", "close"]]
    if (prices <= 0).any().any() or prices.isna().any().any():
        raise ValueError("bars contain non-positive or missing prices")
    bad = (df["high"] < df[["open", "close"]].max(axis=1) - 1e-12) | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-12)
    if bad.any():
        raise ValueError(f"{int(bad.sum())} bars have high/low inconsistent with open/close")


def resample(df: pd.DataFrame, timeframe: Timeframe, base_minutes: int, *, closed_only: bool = True) -> pd.DataFrame:
    """Aggregate lower-timeframe bars into ``timeframe`` bars (labelled by open time).

    With ``closed_only`` the last higher bar is dropped unless its full period is
    covered by the source data, so a partially formed bar is never used.
    """
    if df.empty:
        return df.copy()
    src = df.set_index("time")
    rule = timeframe.pandas_rule
    agg = src.resample(rule, label="left", closed="left", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum", "spread": "mean", "real_volume": "sum"}
    )
    agg = agg.dropna(subset=["open"])
    if closed_only and not agg.empty:
        last_source_close = df["time"].iloc[-1] + pd.Timedelta(minutes=base_minutes)
        if agg.index[-1] + pd.Timedelta(minutes=timeframe.minutes) > last_source_close:
            agg = agg.iloc[:-1]
    agg = agg.reset_index()
    agg["spread"] = agg["spread"].round().astype(int)
    return agg[RATES_COLUMNS]


def synthetic_ohlc(
    *,
    start: datetime | None = None,
    periods: int = 20_000,
    minutes: int = 5,
    base_price: float = 1.1000,
    digits: int = 5,
    volatility: float = 0.00035,
    trend_strength: float = 0.00004,
    regime_length: tuple[int, int] = (300, 1500),
    spread_points: int = 8,
    seed: int = 7,
    skip_weekends: bool = True,
) -> pd.DataFrame:
    """Regime-switching random walk with volatility clustering.

    Drift alternates between up, down and flat regimes so trend-following logic
    has something to react to. This is NOT market data.
    """
    rng = np.random.default_rng(seed)
    start = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    times: list[datetime] = []
    t = start
    step = timedelta(minutes=minutes)
    while len(times) < periods:
        if not (skip_weekends and t.weekday() >= 5):
            times.append(t)
        t += step

    drift = np.empty(periods)
    i = 0
    while i < periods:
        length = int(rng.integers(regime_length[0], regime_length[1]))
        direction = rng.choice([-1.0, 0.0, 1.0], p=[0.35, 0.3, 0.35])
        drift[i : i + length] = direction * trend_strength
        i += length

    vol = np.empty(periods)
    v = volatility
    for k in range(periods):
        v = 0.97 * v + 0.03 * volatility * float(rng.lognormal(0.0, 0.5))
        vol[k] = v

    sub = 4  # intrabar steps to build a realistic high/low
    shocks = rng.standard_normal((periods, sub)) * (vol[:, None] / np.sqrt(sub)) + (drift[:, None] / sub)
    log_path = np.cumsum(shocks.reshape(-1))
    prices = base_price * np.exp(log_path).reshape(periods, sub)
    opens = np.concatenate([[base_price], prices[:-1, -1]])
    closes = prices[:, -1]
    highs = np.maximum(prices.max(axis=1), opens)
    lows = np.minimum(prices.min(axis=1), opens)
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open": np.round(opens, digits),
            "high": np.round(highs, digits),
            "low": np.round(lows, digits),
            "close": np.round(closes, digits),
            "tick_volume": rng.integers(50, 500, periods),
            "spread": np.maximum(1, rng.poisson(spread_points, periods)),
            "real_volume": 0,
        }
    )
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    return df[RATES_COLUMNS]
