from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from trading_engine.backtester import BacktestConfig, Backtester
from trading_engine.config import StrategyParams
from trading_engine.constants import Timeframe
from trading_engine.data import resample, synthetic_ohlc
from trading_engine.market_analyzer import MarketAnalyzer, MarketSnapshot
from trading_engine.mock_mt5 import DEFAULT_SPECS
from trading_engine.models import Side
from trading_engine.strategy_engine import AsianRangeBreakoutStrategy, build_strategy

UTC = timezone.utc
NOW = datetime(2026, 3, 3, 12, tzinfo=UTC)
# 28 M15 bars between 02:00 and 09:00: range 1990 - 2010
RANGE = [(h, 2010.0, 1990.0, 2000.0) for h in range(2, 9) for _ in range(4)]


@pytest.fixture(scope="module")
def m15():
    return synthetic_ohlc(periods=4000, minutes=15, seed=11, volatility=0.0007, trend_strength=0.00008)


@pytest.fixture(scope="module")
def base(m15):
    frames = {Timeframe.M15: m15, Timeframe.H1: resample(m15, Timeframe.H1, 15), Timeframe.H4: resample(m15, Timeframe.H4, 15)}
    return MarketAnalyzer().snapshot("XAUUSD", frames, now=NOW)


def snap_with(base, day, *, at, spread=0.3):
    """Snapshot whose last closed M15 bar is the last entry of ``day`` (hour, high, low, close)."""
    bar = replace(
        base.frame("M15"), bar_time=at, close=day[-1][3], atr=8.0,
        day_hours=tuple(d[0] for d in day), day_highs=tuple(d[1] for d in day),
        day_lows=tuple(d[2] for d in day), day_closes=tuple(d[3] for d in day),
    )
    close = day[-1][3]
    return MarketSnapshot("XAUUSD", NOW, {**base.frames, "M15": bar}, bid=close, ask=close + spread, point=0.01)


def failed(ev):
    return [c.name for c in ev.checks if c.mandatory and not c.passed]


def test_analyzer_exposes_todays_bars(m15):
    a = MarketAnalyzer()
    idx = 2500
    f = a.analyze_frame(a.prepare(m15, Timeframe.M15), idx)
    t = pd.to_datetime(m15["time"], utc=True)
    today = t[(t.dt.normalize() == t.iloc[idx].normalize()) & (t.index <= idx)]
    assert len(f.day_hours) == len(today) == len(f.day_highs) == len(f.day_lows) == len(f.day_closes)
    assert f.day_hours[-1] == t.iloc[idx].hour and f.day_closes[-1] == f.close
    assert list(f.day_hours) == sorted(f.day_hours)


def test_buy_on_first_close_above_range(base):
    s = snap_with(base, RANGE + [(9, 2013.0, 2004.0, 2012.0)], at=datetime(2026, 3, 3, 9, 0, tzinfo=UTC))
    sig = AsianRangeBreakoutStrategy().evaluate(s, spread=0.3, now_utc=NOW).signal
    assert sig is not None and sig.side is Side.BUY
    assert sig.entry == pytest.approx(2012.3)
    assert sig.stop_loss == pytest.approx(1990.0)
    assert sig.take_profit == pytest.approx(2012.3 + 2 * 22.3)
    assert all(c.passed for c in sig.checks if c.mandatory)


def test_sell_mirror_with_mid_stop(base):
    s = snap_with(base, RANGE + [(9, 1995.0, 1985.0, 1987.0)], at=datetime(2026, 3, 3, 9, 0, tzinfo=UTC))
    sig = AsianRangeBreakoutStrategy(StrategyParams(stop_mode="mid")).evaluate(s, spread=0.3, now_utc=NOW).signal
    assert sig is not None and sig.side is Side.SELL
    assert sig.entry == pytest.approx(1987.0)
    assert sig.stop_loss == pytest.approx(2000.3)  # range middle + spread: a SELL stop triggers on the ask
    assert sig.take_profit == pytest.approx(1987.0 - 2 * 13.3)


def test_only_first_break_of_the_day_trades(base):
    post = [(9, 2013.0, 2004.0, 2012.0), (9, 2009.0, 2005.0, 2008.0), (9, 2015.0, 2008.0, 2014.0)]
    ev = AsianRangeBreakoutStrategy().evaluate(snap_with(base, RANGE + post, at=datetime(2026, 3, 3, 9, 30, tzinfo=UTC)), spread=0.3, now_utc=NOW)
    assert ev.signal is None and failed(ev) == ["first_break"]


def test_rejections_outside_window_inside_range_and_short_range(base):
    strategy = AsianRangeBreakoutStrategy()
    late = strategy.evaluate(snap_with(base, RANGE + [(14, 2013.0, 2004.0, 2012.0)], at=datetime(2026, 3, 3, 14, 0, tzinfo=UTC)), spread=0.3, now_utc=NOW)
    assert late.signal is None and failed(late) == ["entry_window"]
    inside = strategy.evaluate(snap_with(base, RANGE + [(9, 2008.0, 2001.0, 2005.0)], at=datetime(2026, 3, 3, 9, 0, tzinfo=UTC)), spread=0.3, now_utc=NOW)
    assert inside.signal is None and failed(inside) == ["breakout"]
    short = strategy.evaluate(snap_with(base, RANGE[-10:] + [(9, 2013.0, 2004.0, 2012.0)], at=datetime(2026, 3, 3, 9, 0, tzinfo=UTC)), spread=0.3, now_utc=NOW)
    assert short.signal is None and failed(short) == ["range_complete"]


def test_exit_at_session_end(base):
    st = AsianRangeBreakoutStrategy()

    def at(tf, hour, minute):
        return MarketSnapshot("XAUUSD", NOW, {tf: replace(base.frame(tf), bar_time=datetime(2026, 3, 3, hour, minute, tzinfo=UTC))})

    assert "session end" in st.exit_reason(Side.BUY, at("M15", 21, 45), 10)
    assert st.exit_reason(Side.BUY, at("M15", 20, 45), 10) is None
    assert "session end" in st.exit_reason(Side.SELL, at("H1", 21, 0), 10)  # backtests pass only the trend frame
    assert st.exit_reason(Side.BUY, at("M15", 20, 45), 96).startswith("max holding")


def test_backtest_trades_at_most_once_per_day(m15):
    strategy = build_strategy("asian_breakout", {"max_spread_atr_ratio": 1.0})
    assert isinstance(strategy, AsianRangeBreakoutStrategy)
    res = Backtester(strategy, BacktestConfig(symbol="EURUSD", spec=DEFAULT_SPECS["EURUSD"], use_ai_filter=False)).run(m15, Timeframe.M15)
    assert res.trades, "expected breakouts on synthetic data"
    days = [pd.Timestamp(t.entry_time).date() for t in res.trades]
    assert len(days) == len(set(days))
    for t in res.trades:
        assert 9 <= pd.Timestamp(t.entry_time).hour < 15  # signal bar closes by 14:00, fill at the next open
        assert t.exit_reason in ("stop loss", "stop loss (gap)", "take profit", "end of data") or t.exit_reason.startswith("session end")
