from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trading_engine.ai_decision_engine import AIDecisionEngine, HeuristicQualityModel, LogisticQualityModel, Regime, brier_score, classify_regime
from trading_engine.config import StrategyParams
from trading_engine.constants import Timeframe
from trading_engine.data import resample, synthetic_ohlc
from trading_engine.market_analyzer import MarketAnalyzer
from trading_engine.models import Side
from trading_engine.strategy_engine import TrendPullbackStrategy, build_strategy


@pytest.fixture(scope="module")
def m15():
    return synthetic_ohlc(periods=8000, minutes=15, seed=5, volatility=0.0007, trend_strength=0.00008)


@pytest.fixture(scope="module")
def prepared(m15):
    a = MarketAnalyzer()
    return a, {
        Timeframe.M15: a.prepare(m15, Timeframe.M15),
        Timeframe.H1: a.prepare(resample(m15, Timeframe.H1, 15, closed_only=False), Timeframe.H1),
        Timeframe.H4: a.prepare(resample(m15, Timeframe.H4, 15, closed_only=False), Timeframe.H4),
    }


def test_resample_drops_incomplete_last_bar(m15):
    h1 = resample(m15.iloc[:10], Timeframe.H1, 15)  # 2.5 hours of M15 -> 2 complete H1 bars
    assert len(h1) == 2
    first = m15.iloc[:4]
    assert h1.iloc[0]["high"] == first["high"].max()
    assert h1.iloc[0]["open"] == first["open"].iloc[0]
    assert h1.iloc[0]["close"] == first["close"].iloc[-1]


def test_snapshot_uses_only_closed_higher_timeframe_bars(prepared, m15):
    a, frames = prepared
    ts = m15["time"].iloc[3000] + pd.Timedelta(minutes=15)  # close of an M15 bar
    snap = a.snapshot_at("EURUSD", frames, ts)
    h1 = snap.frame("H1")
    assert h1 is not None
    assert h1.bar_time + pd.Timedelta(hours=1) <= ts


def test_no_lookahead_future_data_does_not_change_past_analysis(prepared, m15):
    a, frames = prepared
    i = 5000
    ts = m15["time"].iloc[i] + pd.Timedelta(minutes=15)
    full = a.snapshot_at("X", frames, ts)
    # Perturb everything after bar i and rebuild.
    cut = m15.copy()
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, len(cut) - i - 1)
    for col in ("open", "high", "low", "close"):
        cut.loc[i + 1 :, col] = cut.loc[i + 1 :, col].to_numpy() * (1 + noise)
    cut["high"] = cut[["open", "high", "low", "close"]].max(axis=1)
    cut["low"] = cut[["open", "high", "low", "close"]].min(axis=1)
    frames2 = {
        Timeframe.M15: a.prepare(cut, Timeframe.M15),
        Timeframe.H1: a.prepare(resample(cut, Timeframe.H1, 15, closed_only=False), Timeframe.H1),
        Timeframe.H4: a.prepare(resample(cut, Timeframe.H4, 15, closed_only=False), Timeframe.H4),
    }
    perturbed = a.snapshot_at("X", frames2, ts)
    assert full.to_dict() == perturbed.to_dict()


def test_frame_analysis_fields(prepared, m15):
    a, frames = prepared
    f = a.analyze_frame(frames[Timeframe.M15], 4000)
    assert f.trend in ("UP", "DOWN", "NEUTRAL")
    assert f.structure in ("BULLISH", "BEARISH", "MIXED", "UNKNOWN")
    assert f.atr > 0 and 0 <= f.atr_rank <= 1
    assert f.support is None or f.support < f.close
    assert f.resistance is None or f.resistance > f.close
    assert f.volume_kind == "tick"  # FX data has tick volume only
    assert a.analyze_frame(frames[Timeframe.M15], 50) is None  # not enough history


def _signals(prepared, m15, strategy, n=2500):
    a, frames = prepared
    out = []
    for i in range(3000, 3000 + n):
        ts = m15["time"].iloc[i] + pd.Timedelta(minutes=15)
        bar = m15.iloc[i]
        snap = a.snapshot_at("EURUSD", frames, ts)
        snap = replace(snap, bid=bar["close"], ask=bar["close"] + 0.00008, point=0.00001)
        ev = strategy.evaluate(snap, spread=0.00008, now_utc=datetime(2024, 1, 2, 12, tzinfo=timezone.utc))
        if ev.signal:
            out.append((ev, snap))
    return out


def test_strategy_signals_follow_documented_rules(prepared, m15):
    strategy = TrendPullbackStrategy()
    sigs = _signals(prepared, m15, strategy)
    assert sigs, "expected at least one setup on trending synthetic data"
    for ev, snap in sigs:
        s = ev.signal
        h1 = snap.frame("H1")
        assert (s.side is Side.BUY and h1.trend == "UP") or (s.side is Side.SELL and h1.trend == "DOWN")
        assert all(c.passed for c in s.checks if c.mandatory)
        assert (s.entry - s.stop_loss) * s.side.sign > 0
        assert s.rr == pytest.approx(strategy.params.rr_target, rel=0.02)
        assert s.reasons
        assert s.signal_id == replace(s).signal_id  # deterministic


def test_strategy_explains_rejections(prepared, m15):
    a, frames = prepared
    ts = m15["time"].iloc[3000] + pd.Timedelta(minutes=15)
    snap = a.snapshot_at("EURUSD", frames, ts)
    ev = TrendPullbackStrategy().evaluate(snap, spread=None, now_utc=datetime(2024, 1, 2, 3, tzinfo=timezone.utc))
    assert ev.signal is None
    assert ev.rejection_reasons


def test_session_filter_blocks_outside_hours(prepared, m15):
    strategy = TrendPullbackStrategy(StrategyParams(session_start_utc=0, session_end_utc=24))
    sigs = _signals(prepared, m15, strategy, n=1500)
    assert sigs
    ev, snap = sigs[0]
    night = strategy.evaluate(snap, spread=0.00008, now_utc=datetime(2024, 1, 2, 2, tzinfo=timezone.utc))
    restricted = TrendPullbackStrategy(StrategyParams(session_start_utc=7, session_end_utc=20)).evaluate(snap, spread=0.00008, now_utc=datetime(2024, 1, 2, 2, tzinfo=timezone.utc))
    assert night.signal is not None
    assert restricted.signal is None and any("session" in r for r in restricted.rejection_reasons)


def test_wide_spread_blocks_signal(prepared, m15):
    strategy = TrendPullbackStrategy()
    ev, snap = _signals(prepared, m15, strategy)[0]
    wide = strategy.evaluate(snap, spread=snap.frame("M15").atr, now_utc=datetime(2024, 1, 2, 12, tzinfo=timezone.utc))
    assert wide.signal is None and any(r.startswith("spread") for r in wide.rejection_reasons)


def test_build_strategy_registry():
    s = build_strategy("trend_pullback", {"rr_target": 2.5, "unknown": 1})
    assert s.params.rr_target == 2.5
    with pytest.raises(ValueError):
        build_strategy("martingale")


def test_ai_engine_can_only_veto_and_explains(prepared, m15):
    strategy = TrendPullbackStrategy()
    ev, snap = _signals(prepared, m15, strategy)[0]
    sig = ev.signal
    before = (sig.entry, sig.stop_loss, sig.take_profit)
    d = AIDecisionEngine(min_quality=0.0, allowed_regimes=(Regime.TREND, Regime.RANGE, Regime.HIGH_VOLATILITY)).evaluate(sig, snap)
    assert d.approve
    assert (sig.entry, sig.stop_loss, sig.take_profit) == before  # untouched
    strict = AIDecisionEngine(min_quality=0.99).evaluate(sig, snap)
    assert not strict.approve
    assert any("quality score" in r for r in strict.reasons_against)


def test_ai_engine_uses_past_performance():
    from trading_engine.ai_decision_engine import PerformanceStats
    from trading_engine.models import Signal

    engine = AIDecisionEngine(min_quality=0.0, allowed_regimes=(Regime.UNKNOWN,), performance={("trend_pullback", "EURUSD"): PerformanceStats("BACKTEST_OOS", 80, -0.12, 0.85, False)})
    sig = Signal("trend_pullback", "1.0.0", "EURUSD", Side.BUY, "M15", datetime(2024, 1, 1, tzinfo=timezone.utc), 1.1, 1.09, 1.12, 0.001)
    from trading_engine.market_analyzer import MarketSnapshot
    d = engine.evaluate(sig, MarketSnapshot("EURUSD", datetime.now(timezone.utc), {}))
    assert not d.approve
    assert any("expectancy" in r for r in d.reasons_against)


def test_regime_classification(prepared, m15):
    a, frames = prepared
    seen = set()
    for i in range(3000, 7000, 50):
        snap = a.snapshot_at("X", frames, m15["time"].iloc[i] + pd.Timedelta(minutes=15))
        seen.add(classify_regime(snap).regime)
    assert Regime.TREND in seen and Regime.RANGE in seen


def test_heuristic_score_bounds():
    m = HeuristicQualityModel()
    assert 0 <= m.score({}) <= 1
    great = {"h1_adx": 40, "h1_slope_atr": 1.5, "h1_structure_aligned": 1, "h4_aligned": 1, "m15_rsi_centered": 7, "spread_risk_ratio": 0, "m15_atr_rank": 0.5, "retest": 1}
    assert m.score(great) == pytest.approx(1.0)


def test_logistic_model_learns_and_roundtrips(tmp_path):
    rng = np.random.default_rng(0)
    rows = [{"a": float(x), "b": float(rng.normal())} for x in rng.normal(size=400)]
    labels = [int(r["a"] + 0.3 * rng.normal() > 0) for r in rows]
    model = LogisticQualityModel(feature_names=["a", "b"]).fit(rows, labels)
    assert model.score({"a": 2.0, "b": 0}) > 0.8
    assert model.score({"a": -2.0, "b": 0}) < 0.2
    model.save(tmp_path / "m.json")
    again = LogisticQualityModel.load(tmp_path / "m.json")
    assert again.score({"a": 1.0, "b": 0.0}) == pytest.approx(model.score({"a": 1.0, "b": 0.0}))
    assert brier_score([0.9, 0.1], [1, 0]) == pytest.approx(0.01)
