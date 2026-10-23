import numpy as np
import pandas as pd
import pytest

from trading_engine import indicators as ind


def test_ema_seeded_with_sma_and_recursive():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    out = ind.ema(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0)  # SMA of 1,2,3
    alpha = 0.5
    assert out.iloc[3] == pytest.approx(alpha * 4 + (1 - alpha) * 2.0)
    assert out.iloc[5] == pytest.approx(5.0)


def test_rsi_wilder_reference_values():
    # StockCharts' Wilder RSI worksheet. Their table shows 70.53/66.32/57.97
    # because intermediate averages are rounded; full precision gives these.
    closes = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
    r = ind.rsi(closes, 14)
    assert r.iloc[:14].isna().all()
    assert r.iloc[14] == pytest.approx(70.46, abs=0.05)
    assert r.iloc[15] == pytest.approx(66.25, abs=0.05)
    assert r.iloc[19] == pytest.approx(57.92, abs=0.05)


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert ind.rsi(up, 14).iloc[-1] == 100.0
    flat = pd.Series([5.0] * 30)
    assert ind.rsi(flat, 14).iloc[-1] == 50.0


def test_atr_true_range_and_smoothing():
    high = pd.Series([10.0, 11, 12, 11])
    low = pd.Series([9.0, 10, 10, 9])
    close = pd.Series([9.5, 10.5, 11.5, 9.5])
    tr = ind.true_range(high, low, close)
    assert list(tr) == [1.0, 1.5, 2.0, 2.5]
    a = ind.atr(high, low, close, 2)
    assert np.isnan(a.iloc[0])
    assert a.iloc[1] == pytest.approx(1.25)
    assert a.iloc[2] == pytest.approx(0.5 * 2.0 + 0.5 * 1.25)


def test_macd_relationship():
    s = pd.Series(np.linspace(1, 2, 100))
    m = ind.macd(s)
    last = m.iloc[-1]
    assert last["macd"] > 0
    assert last["macd_hist"] == pytest.approx(last["macd"] - last["macd_signal"])


def test_indicators_are_causal():
    rng = np.random.default_rng(1)
    close = pd.Series(100 + rng.standard_normal(400).cumsum())
    high, low = close + 0.5, close - 0.5
    full = pd.DataFrame({"ema": ind.ema(close, 50), "rsi": ind.rsi(close), "atr": ind.atr(high, low, close), "adx": ind.adx(high, low, close)["adx"]})
    cut = 300
    part = pd.DataFrame({"ema": ind.ema(close[:cut], 50), "rsi": ind.rsi(close[:cut]), "atr": ind.atr(high[:cut], low[:cut], close[:cut]), "adx": ind.adx(high[:cut], low[:cut], close[:cut])["adx"]})
    pd.testing.assert_frame_equal(full.iloc[:cut], part)


def test_pivots_confirmation_delay():
    high = pd.Series([1, 2, 3, 6, 3, 2, 1, 2, 3.0])
    low = high - 1
    p = ind.pivots(high, low, 2, 2)
    assert p["swing_high"].iloc[3]
    assert p["confirmed_at"].iloc[3] == 5
    # The last `right` bars can never be pivots: future bars are unknown.
    assert not p["swing_high"].iloc[-2:].any()


def test_adx_trend_strength():
    n = 200
    close = pd.Series(np.linspace(100, 150, n))
    a = ind.adx(close + 0.2, close - 0.2, close)
    assert a["adx"].iloc[-1] > 40
    assert a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]
