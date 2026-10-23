from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trading_engine.backtester import AcceptanceCriteria, BacktestConfig, Backtester, CostModel, evaluate_acceptance, walk_forward
from trading_engine.constants import Timeframe
from trading_engine.data import load_mt5_csv, synthetic_ohlc, validate_ohlc
from trading_engine.metrics import compute_metrics, max_drawdown
from trading_engine.mock_mt5 import DEFAULT_SPECS
from trading_engine.models import Check, Side, Signal, StrategyEvaluation
from trading_engine.strategy_engine import Strategy, TrendPullbackStrategy
from trading_engine.config import StrategyParams


@pytest.fixture(scope="module")
def data():
    return synthetic_ohlc(periods=9000, minutes=15, seed=3, volatility=0.0007, trend_strength=0.00008)


def cfg(**kw):
    return BacktestConfig(symbol="EURUSD", spec=DEFAULT_SPECS["EURUSD"], use_ai_filter=False, **kw)


def test_metrics_formulas():
    m = compute_metrics([100, -50, 200, -50], r_multiples=[2, -1, 4, -1], starting_equity=1000)
    assert m.trades == 4 and m.wins == 2
    assert m.win_rate == 50.0
    assert m.profit_factor == pytest.approx(3.0)
    assert m.expectancy == pytest.approx(50.0)
    assert m.expectancy_r == pytest.approx(1.0)
    assert m.avg_win == 150 and m.avg_loss == -50
    assert m.max_drawdown == pytest.approx(50)
    assert max_drawdown([100, 120, 90, 130, 65])[1] == pytest.approx(50.0)
    empty = compute_metrics([])
    assert empty.trades == 0 and empty.profit_factor is None


def test_costs_reduce_results(data):
    # Spread changes which setups pass the filters (path dependence), so hold it
    # fixed and compare commission + slippage on the identical set of entries.
    s = TrendPullbackStrategy()
    free = Backtester(s, cfg(costs=CostModel(spread_points=8, commission_per_lot_side=0, slippage_points=0))).run(data, Timeframe.M15)
    costly = Backtester(s, cfg(costs=CostModel(spread_points=8, commission_per_lot_side=5, slippage_points=2))).run(data, Timeframe.M15)
    assert free.metrics.trades > 0
    assert costly.costs["commission"] > 0 and free.costs["commission"] == 0
    matched = {t.entry_time: t for t in free.trades}
    pairs = [(matched[t.entry_time], t) for t in costly.trades if t.entry_time in matched]
    assert len(pairs) >= len(free.trades) * 0.8
    for f, c in pairs:
        assert c.pnl < f.pnl
    assert costly.metrics.net_profit < free.metrics.net_profit


def test_wider_spread_never_improves_identical_trade(data):
    s = TrendPullbackStrategy()
    a = Backtester(s, cfg(costs=CostModel(spread_points=5, commission_per_lot_side=0, slippage_points=0))).run(data, Timeframe.M15)
    b = Backtester(s, cfg(costs=CostModel(spread_points=12, commission_per_lot_side=0, slippage_points=0))).run(data, Timeframe.M15)
    by_entry = {t.entry_time: t for t in a.trades}
    for t in b.trades:
        if t.entry_time in by_entry and t.side == "BUY" and t.exit_reason == by_entry[t.entry_time].exit_reason == "take profit":
            assert t.entry_price > by_entry[t.entry_time].entry_price


def test_entries_fill_at_next_bar_open_not_signal_close(data):
    res = Backtester(TrendPullbackStrategy(), cfg(costs=CostModel(spread_points=0, slippage_points=0, commission_per_lot_side=0))).run(data, Timeframe.M15)
    by_time = data.set_index("time")
    for t in res.trades[:20]:
        entry_bar = by_time.loc[pd.Timestamp(t.entry_time)]
        assert t.entry_price == pytest.approx(entry_bar["open"])
        assert pd.Timestamp(t.entry_time) == pd.Timestamp(t.signal_time) + pd.Timedelta(minutes=15)


def test_future_data_does_not_change_past_trades(data):
    s = TrendPullbackStrategy()
    full = Backtester(s, cfg()).run(data, Timeframe.M15)
    cut_at = data["time"].iloc[6000]
    altered = data.copy()
    mask = altered["time"] > cut_at
    altered.loc[mask, ["open", "high", "low", "close"]] *= 1.05
    alt = Backtester(s, cfg()).run(altered, Timeframe.M15)
    def closed_before(res):
        return [(t.entry_time, t.exit_time, t.pnl) for t in res.trades if pd.Timestamp(t.exit_time) < cut_at - pd.Timedelta(hours=4)]
    assert closed_before(full) == closed_before(alt)
    assert closed_before(full)


class OneShot(Strategy):
    strategy_id = "oneshot"
    version = "0"

    def __init__(self, when, side, sl, tp):
        super().__init__(StrategyParams(exit_on_trend_flip=False, max_hold_bars=0))
        self.when, self.side, self.sl, self.tp = when, side, sl, tp

    def evaluate(self, snapshot, *, spread, now_utc):
        f = snapshot.frame("M15")
        if f is None or f.bar_time != self.when:
            return StrategyEvaluation(snapshot.symbol, self.strategy_id, None, [Check("time", False, "")])
        sig = Signal(self.strategy_id, self.version, snapshot.symbol, self.side, "M15", f.bar_time, f.close, self.sl, self.tp, f.atr)
        return StrategyEvaluation(snapshot.symbol, self.strategy_id, sig, [])

    def exit_reason(self, side, snapshot, bars_held):
        return None


def _flat_bars(n=4000, price=1.1):
    t = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(1)
    close = price + rng.normal(0, 0.00005, n).cumsum() * 0.1
    df = pd.DataFrame({"time": t, "open": close, "high": close + 0.0002, "low": close - 0.0002, "close": close, "tick_volume": 100, "spread": 0, "real_volume": 0})
    return df


def test_sl_assumed_first_when_both_hit_in_same_bar():
    df = _flat_bars()
    i = 3500
    when = df["time"].iloc[i]
    nxt = i + 1
    o = df.loc[nxt, "open"]
    df.loc[nxt + 1, ["open", "high", "low", "close"]] = [o, o + 0.01, o - 0.01, o]  # huge range bar
    strat = OneShot(when, Side.BUY, sl=o - 0.005, tp=o + 0.0075)
    res = Backtester(strat, cfg(costs=CostModel(spread_points=0, slippage_points=0, commission_per_lot_side=0), min_rr=1.0)).run(df, Timeframe.M15)
    assert len(res.trades) == 1
    assert res.trades[0].exit_reason == "stop loss"
    assert res.trades[0].r_multiple == pytest.approx(-1.0, abs=0.05)


def test_gap_through_stop_fills_at_open():
    df = _flat_bars()
    i = 3500
    when = df["time"].iloc[i]
    o = df.loc[i + 1, "open"]
    gap = o - 0.01
    df.loc[i + 2, ["open", "high", "low", "close"]] = [gap, gap + 0.0001, gap - 0.0001, gap]
    strat = OneShot(when, Side.BUY, sl=o - 0.002, tp=o + 0.004)
    res = Backtester(strat, cfg(costs=CostModel(spread_points=0, slippage_points=0, commission_per_lot_side=0))).run(df, Timeframe.M15)
    t = res.trades[0]
    assert t.exit_reason == "stop loss (gap)"
    assert t.exit_price == pytest.approx(gap)
    assert t.r_multiple < -4  # gaps can lose more than 1R


def test_sell_stop_uses_ask_prices():
    df = _flat_bars()
    df["spread"] = 20  # 2 pips
    i = 3500
    when = df["time"].iloc[i]
    o = df.loc[i + 1, "open"]
    # bid high never reaches SL but ask (bid + spread) does
    df.loc[i + 2, ["open", "high", "low", "close"]] = [o, o + 0.00015, o - 0.0001, o]
    strat = OneShot(when, Side.SELL, sl=o + 0.0003, tp=o - 0.0012)
    res = Backtester(strat, cfg(costs=CostModel(slippage_points=0, commission_per_lot_side=0), min_rr=1.0)).run(df, Timeframe.M15)
    assert res.trades[0].exit_reason == "stop loss"


def test_walk_forward_produces_oos_folds_and_labels_synthetic(data):
    wf = walk_forward(data, Timeframe.M15, cfg(), param_grid=[{"rr_target": 1.5}, {"rr_target": 2.0}], n_folds=2, synthetic=True)
    assert len(wf.folds) == 2
    for f in wf.folds:
        assert pd.Timestamp(f.train_end) < pd.Timestamp(f.test_start)
    assert wf.status == "FAILED"
    assert any("synthetic" in r for r in wf.reasons)


def test_acceptance_criteria():
    good = compute_metrics([100, -50] * 20, r_multiples=[2, -1] * 20, starting_equity=10000)
    assert evaluate_acceptance(good, AcceptanceCriteria()) == []
    bad = compute_metrics([50, -60] * 20, r_multiples=[1, -1.2] * 20, starting_equity=10000)
    assert evaluate_acceptance(bad, AcceptanceCriteria())


def test_mt5_csv_import(tmp_path):
    p = tmp_path / "EURUSD_M15.csv"
    p.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                 "2024.01.02\t00:00:00\t1.10000\t1.10100\t1.09900\t1.10050\t120\t0\t8\n"
                 "2024.01.02\t00:15:00\t1.10050\t1.10200\t1.10000\t1.10150\t130\t0\t7\n")
    df = load_mt5_csv(p)
    assert len(df) == 2
    assert df["time"].iloc[1] == pd.Timestamp("2024-01-02 00:15", tz="UTC")
    assert df["spread"].tolist() == [8, 7]


def test_corrupt_bars_rejected():
    df = _flat_bars(10)
    df.loc[3, "high"] = df.loc[3, "low"] - 1
    with pytest.raises(ValueError):
        validate_ohlc(df)
    df = _flat_bars(10)
    df.loc[2, "close"] = 0
    with pytest.raises(ValueError):
        validate_ohlc(df)
