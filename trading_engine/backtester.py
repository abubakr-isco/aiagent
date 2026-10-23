"""Reproducible bar-based backtesting with trading costs and walk-forward testing.

Execution model (deliberately conservative):

* Bars are BID prices (MT5 convention). BUY fills at ask = bid + spread, SELL
  at bid; a SELL stop/target is triggered by the ask.
* A signal computed at the close of bar *i* is filled at the OPEN of bar
  *i+1* plus slippage - never at the close that produced it.
* Higher-timeframe data is used only once that bar has closed.
* If SL and TP are both inside one bar the SL is assumed to be hit first.
  A gap through the SL fills at the (worse) open price.
* Commission is charged per lot per side; spread is embedded in prices.
* The daily loss limit and one-position-at-a-time are enforced.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from typing import Any

import pandas as pd

from .ai_decision_engine import AIDecisionEngine
from .config import StrategyParams
from .constants import Timeframe
from .data import resample
from .market_analyzer import MarketAnalyzer, MarketSnapshot, PreparedFrame
from .metrics import PerformanceReport, compute_metrics
from .models import Side, SymbolInfo
from .risk_manager import floor_to_step
from .strategy_engine import Strategy, build_strategy


@dataclass(frozen=True)
class CostModel:
    spread_points: float | None = None  # None: use the bar 'spread' column
    min_spread_points: float = 0.0
    commission_per_lot_side: float = 3.5
    slippage_points: float = 1.0


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    spec: SymbolInfo
    account_currency: str = "USD"
    starting_equity: float = 10_000.0
    risk_pct: float = 0.5
    min_rr: float = 1.5
    daily_loss_limit_pct: float = 2.0
    server_utc_offset_hours: int = 0
    costs: CostModel = field(default_factory=CostModel)
    use_ai_filter: bool = True


@dataclass
class BacktestTrade:
    signal_id: str
    side: str
    signal_time: str
    entry_time: str
    entry_price: float
    sl: float
    tp: float
    volume: float
    risk_money: float
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    r_multiple: float = 0.0
    commission: float = 0.0
    bars_held: int = 0
    ai_score: float | None = None
    features: dict[str, float] = field(default_factory=dict)


@dataclass
class BacktestResult:
    symbol: str
    strategy_id: str
    strategy_version: str
    params: dict[str, Any]
    period_start: str
    period_end: str
    trades: list[BacktestTrade]
    equity_curve: list[tuple[str, float]]
    metrics: PerformanceReport
    signals: int
    ai_rejected: int
    skipped: dict[str, int]
    rejections: dict[str, int]
    costs: dict[str, Any]
    synthetic_data: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "params": self.params,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "trades": [asdict(t) for t in self.trades],
            "equity_curve": self.equity_curve,
            "metrics": self.metrics.to_dict(),
            "signals": self.signals,
            "ai_rejected": self.ai_rejected,
            "skipped": self.skipped,
            "rejections": self.rejections,
            "costs": self.costs,
            "synthetic_data": self.synthetic_data,
        }


def _tf(value: str) -> Timeframe:
    return Timeframe(value)


class Backtester:
    def __init__(self, strategy: Strategy, cfg: BacktestConfig, *, analyzer: MarketAnalyzer | None = None, ai: AIDecisionEngine | None = None):
        self.strategy = strategy
        self.cfg = cfg
        self.analyzer = analyzer or MarketAnalyzer()
        self.ai = ai or AIDecisionEngine()

    # ---------------------------------------------------------------- pricing
    def _to_account(self, amount: float, price: float) -> float:
        spec = self.cfg.spec
        if spec.currency_profit == self.cfg.account_currency or not spec.currency_profit:
            return amount
        if spec.currency_base == self.cfg.account_currency:
            return amount / price
        return amount * spec.trade_tick_value / (spec.trade_tick_size * spec.trade_contract_size)

    def pnl(self, side: Side, volume: float, entry: float, exit_: float) -> float:
        raw = (exit_ - entry) * side.sign * volume * self.cfg.spec.trade_contract_size
        return self._to_account(raw, exit_)

    def _spread(self, bar_spread: float) -> float:
        c = self.cfg.costs
        pts = c.spread_points if c.spread_points is not None else float(bar_spread)
        return max(pts, c.min_spread_points) * self.cfg.spec.point

    # ---------------------------------------------------------------- frames
    def prepare(self, df: pd.DataFrame, base_tf: Timeframe) -> dict[Timeframe, PreparedFrame]:
        p = self.strategy.params
        out: dict[Timeframe, PreparedFrame] = {}
        for name in {p.entry_tf, p.trend_tf, p.context_tf}:
            tf = _tf(name)
            if tf.minutes < base_tf.minutes:
                raise ValueError(f"base data {base_tf.value} is coarser than required {tf.value}")
            frame = df if tf is base_tf else resample(df, tf, base_tf.minutes, closed_only=False)
            out[tf] = self.analyzer.prepare(frame, tf)
        return out

    # ------------------------------------------------------------------- run
    def run(
        self,
        df: pd.DataFrame,
        base_tf: Timeframe,
        *,
        trade_from: pd.Timestamp | None = None,
        trade_to: pd.Timestamp | None = None,
        prepared: dict[Timeframe, PreparedFrame] | None = None,
        synthetic: bool = False,
    ) -> BacktestResult:
        cfg, spec, p = self.cfg, self.cfg.spec, self.strategy.params
        prepared = prepared or self.prepare(df, base_tf)
        entry_tf = _tf(p.entry_tf)
        ef = prepared[entry_tf]
        bars = ef.df
        n = len(bars)
        trade_from = trade_from if trade_from is not None else bars["time"].iloc[0]
        trade_to = trade_to if trade_to is not None else bars["time"].iloc[-1]

        equity = cfg.starting_equity
        curve: list[tuple[str, float]] = []
        trades: list[BacktestTrade] = []
        position: BacktestTrade | None = None
        pos_side: Side | None = None
        entry_idx = 0
        pending_entry: tuple[Any, Any] | None = None  # (signal, ai_score)
        pending_exit: str | None = None
        signals = ai_rejected = 0
        skipped: dict[str, int] = {}
        rejections: dict[str, int] = {}
        day_key, day_start_equity, day_locked = None, equity, False
        commission_total = slippage_total = 0.0
        slip = cfg.costs.slippage_points * spec.point
        offset = timedelta(hours=cfg.server_utc_offset_hours)

        def close_trade(t: BacktestTrade, price: float, when: pd.Timestamp, reason: str, j: int) -> None:
            nonlocal equity, commission_total
            side = Side(t.side)
            comm = cfg.costs.commission_per_lot_side * t.volume
            gross = self.pnl(side, t.volume, t.entry_price, price)
            t.exit_price = round(price, spec.digits)
            t.exit_time = when.isoformat()
            t.exit_reason = reason
            t.commission = round(t.commission + comm, 2)
            t.pnl = round(gross - t.commission, 2)
            t.r_multiple = round(t.pnl / t.risk_money, 3) if t.risk_money > 0 else 0.0
            t.bars_held = j - entry_idx
            commission_total += comm
            equity += gross - comm  # entry commission already deducted
            trades.append(t)
            curve.append((when.isoformat(), round(equity, 2)))

        for j in range(n):
            bar = bars.iloc[j]
            t_open = bar["time"]
            t_close = t_open + pd.Timedelta(minutes=entry_tf.minutes)
            spread = self._spread(bar["spread"])

            day = (t_open - offset).date()
            if day != day_key:
                day_key, day_start_equity, day_locked = day, equity, False

            # (a) orders scheduled at the previous close execute at this open
            if position is not None and pending_exit:
                side = Side(position.side)
                price = bar["open"] - slip if side is Side.BUY else bar["open"] + spread + slip
                slippage_total += slip * position.volume * spec.trade_contract_size
                close_trade(position, price, t_open, pending_exit, j)
                position, pending_exit = None, None
            if position is None and pending_entry is not None:
                signal, score = pending_entry
                pending_entry = None
                side = signal.side
                entry = bar["open"] + spread + slip if side is Side.BUY else bar["open"] - slip
                risk_dist = (entry - signal.stop_loss) * side.sign
                reward_dist = (signal.take_profit - entry) * side.sign
                if risk_dist <= 0 or reward_dist / risk_dist < cfg.min_rr:
                    skipped["rr_at_fill"] = skipped.get("rr_at_fill", 0) + 1
                else:
                    loss_per_lot = abs(self.pnl(side, 1.0, entry, signal.stop_loss))
                    volume = floor_to_step(min(equity * cfg.risk_pct / 100 / loss_per_lot, spec.volume_max), spec.volume_step) if loss_per_lot > 0 else 0
                    if volume < spec.volume_min:
                        skipped["volume_below_min"] = skipped.get("volume_below_min", 0) + 1
                    else:
                        comm = cfg.costs.commission_per_lot_side * volume
                        equity -= comm
                        commission_total += comm
                        slippage_total += slip * volume * spec.trade_contract_size
                        position = BacktestTrade(
                            signal_id=signal.signal_id, side=side.value, signal_time=signal.bar_time.isoformat(),
                            entry_time=t_open.isoformat(), entry_price=round(entry, spec.digits), sl=signal.stop_loss,
                            tp=signal.take_profit, volume=volume, risk_money=round(volume * loss_per_lot, 2),
                            commission=round(comm, 2), ai_score=score, features=signal.features,
                        )
                        pos_side, entry_idx = side, j

            # (b) stops and targets inside this bar
            if position is not None:
                side = pos_side
                if side is Side.BUY:
                    if bar["open"] <= position.sl:
                        close_trade(position, bar["open"] - slip, t_open, "stop loss (gap)", j); position = None
                    elif bar["low"] <= position.sl:
                        close_trade(position, position.sl - slip, t_close, "stop loss", j); position = None
                    elif bar["high"] >= position.tp:
                        close_trade(position, position.tp, t_close, "take profit", j); position = None
                else:
                    ask_open, ask_high, ask_low = bar["open"] + spread, bar["high"] + spread, bar["low"] + spread
                    if ask_open >= position.sl:
                        close_trade(position, ask_open + slip, t_open, "stop loss (gap)", j); position = None
                    elif ask_high >= position.sl:
                        close_trade(position, position.sl + slip, t_close, "stop loss", j); position = None
                    elif ask_low <= position.tp:
                        close_trade(position, position.tp, t_close, "take profit", j); position = None
                if position is None and day_start_equity > 0 and (day_start_equity - equity) / day_start_equity * 100 >= cfg.daily_loss_limit_pct:
                    day_locked = True

            # (c) decisions at the close of this bar (only data closed by t_close)
            if j + 1 >= n:
                break
            if position is not None:
                snap = self.analyzer.snapshot_at(cfg.symbol, {k: v for k, v in prepared.items() if k.value == p.trend_tf}, t_close)
                reason = self.strategy.exit_reason(pos_side, snap, j - entry_idx + 1)
                if reason:
                    pending_exit = reason
                continue
            if day_locked or t_close < trade_from or t_open > trade_to:
                continue
            snap = self.analyzer.snapshot_at(cfg.symbol, prepared, t_close)
            snap = replace(snap, bid=float(bar["close"]), ask=float(bar["close"]) + spread, point=spec.point)
            evaluation = self.strategy.evaluate(snap, spread=spread, now_utc=(t_close - offset).to_pydatetime())
            if evaluation.signal is None:
                for c in evaluation.checks:
                    if c.mandatory and not c.passed:
                        rejections[c.name] = rejections.get(c.name, 0) + 1
                        break
                continue
            signals += 1
            score = None
            if cfg.use_ai_filter:
                decision = self.ai.evaluate(evaluation.signal, snap)
                score = decision.score
                if not decision.approve:
                    ai_rejected += 1
                    continue
            pending_entry = (evaluation.signal, score)

        if position is not None:  # mark-to-market close at the last bar
            last = bars.iloc[-1]
            side = pos_side
            spread = self._spread(last["spread"])
            price = last["close"] if side is Side.BUY else last["close"] + spread
            close_trade(position, price, last["time"] + pd.Timedelta(minutes=entry_tf.minutes), "end of data", n - 1)

        metrics = compute_metrics([t.pnl for t in trades], r_multiples=[t.r_multiple for t in trades], starting_equity=cfg.starting_equity, costs=[t.commission for t in trades])
        return BacktestResult(
            symbol=cfg.symbol, strategy_id=self.strategy.strategy_id, strategy_version=self.strategy.version,
            params=self.strategy.params.to_dict(), period_start=str(trade_from), period_end=str(trade_to), trades=trades,
            equity_curve=curve, metrics=metrics, signals=signals, ai_rejected=ai_rejected, skipped=skipped, rejections=rejections,
            costs={"commission": round(commission_total, 2), "slippage_estimate": round(self._to_account(slippage_total, 1.0), 2),
                   "spread": "embedded in fill prices", **asdict(cfg.costs)},
            synthetic_data=synthetic,
        )


# ------------------------------------------------------------- walk-forward
@dataclass(frozen=True)
class AcceptanceCriteria:
    min_trades: int = 30
    min_profit_factor: float = 1.1
    min_expectancy_r: float = 0.0
    max_drawdown_pct: float = 10.0


@dataclass
class WalkForwardFold:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    chosen_params: dict[str, Any]
    train_metrics: dict[str, Any]
    test_metrics: dict[str, Any]


@dataclass
class WalkForwardResult:
    folds: list[WalkForwardFold]
    oos_metrics: PerformanceReport
    oos_trades: list[BacktestTrade]
    status: str  # PASSED | FAILED
    reasons: list[str]
    synthetic_data: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "folds": [asdict(f) for f in self.folds],
            "oos_metrics": self.oos_metrics.to_dict(),
            "oos_trades": [asdict(t) for t in self.oos_trades],
            "status": self.status,
            "reasons": self.reasons,
            "synthetic_data": self.synthetic_data,
        }


def evaluate_acceptance(m: PerformanceReport, crit: AcceptanceCriteria) -> list[str]:
    reasons = []
    if m.trades < crit.min_trades:
        reasons.append(f"only {m.trades} OOS trades (< {crit.min_trades}); not statistically meaningful")
    pf = m.profit_factor if m.profit_factor is not None else 0.0
    if pf < crit.min_profit_factor:
        reasons.append(f"profit factor {pf:.2f} < {crit.min_profit_factor}")
    if m.expectancy_r is None or m.expectancy_r <= crit.min_expectancy_r:
        reasons.append(f"expectancy {m.expectancy_r}R <= {crit.min_expectancy_r}R after costs")
    if not math.isnan(m.max_drawdown_pct) and m.max_drawdown_pct > crit.max_drawdown_pct:
        reasons.append(f"max drawdown {m.max_drawdown_pct:.1f}% > {crit.max_drawdown_pct}%")
    return reasons


def walk_forward(
    df: pd.DataFrame,
    base_tf: Timeframe,
    cfg: BacktestConfig,
    *,
    strategy_id: str = "trend_pullback",
    base_params: dict[str, Any] | None = None,
    param_grid: list[dict[str, Any]] | None = None,
    n_folds: int = 4,
    train_fraction: float = 0.6,
    embargo_bars: int = 96,
    criteria: AcceptanceCriteria | None = None,
    synthetic: bool = False,
) -> WalkForwardResult:
    """Rolling walk-forward: choose parameters on each train window, test on the following unseen window.

    Windows never overlap with their test period; an embargo gap separates
    them so that trades open at the end of training cannot leak into the test.
    """
    criteria = criteria or AcceptanceCriteria()
    base_params = base_params or {}
    param_grid = param_grid or [{}]
    entry_tf = Timeframe(StrategyParams.from_dict(base_params).entry_tf)
    entry_df = df if entry_tf is base_tf else resample(df, entry_tf, base_tf.minutes, closed_only=False)
    times = entry_df["time"].reset_index(drop=True)
    warmup = 250 * (240 // entry_tf.minutes)  # enough history for H4 EMA200
    usable = len(times) - warmup
    if usable <= 0:
        raise ValueError("not enough data for walk-forward (need warm-up for H4 EMA200)")
    # Train windows cover ``train_fraction`` of the usable data; the remaining
    # tail is split into ``n_folds`` consecutive out-of-sample test windows.
    train_len = int(usable * train_fraction)
    fold_len = (usable - train_len) // n_folds
    if fold_len <= 0:
        raise ValueError("not enough data for the requested number of folds")

    folds: list[WalkForwardFold] = []
    oos: list[BacktestTrade] = []
    prepared_cache: dict[str, dict[Timeframe, PreparedFrame]] = {}

    def run(params: dict[str, Any], start: pd.Timestamp, end: pd.Timestamp) -> BacktestResult:
        merged = {**base_params, **params}
        strategy = build_strategy(strategy_id, merged)
        bt = Backtester(strategy, cfg)
        key = repr(sorted((k, merged.get(k)) for k in ("entry_tf", "trend_tf", "context_tf")))
        if key not in prepared_cache:
            prepared_cache[key] = bt.prepare(df, base_tf)
        return bt.run(df, base_tf, trade_from=start, trade_to=end, prepared=prepared_cache[key], synthetic=synthetic)

    for k in range(n_folds):
        test_start_i = warmup + train_len + k * fold_len
        test_end_i = min(test_start_i + fold_len - 1, len(times) - 1)
        train_end_i = test_start_i - embargo_bars - 1
        train_start_i = max(warmup, train_end_i - train_len)
        if train_end_i <= train_start_i or test_start_i >= len(times):
            break
        tr_s, tr_e = times[train_start_i], times[train_end_i]
        te_s, te_e = times[test_start_i], times[test_end_i]
        best, best_score, best_metrics = param_grid[0], -math.inf, None
        for params in param_grid:
            res = run(params, tr_s, tr_e)
            m = res.metrics
            score = (m.expectancy_r or -1.0) if m.trades >= 10 else -math.inf
            if score > best_score:
                best, best_score, best_metrics = params, score, m
        test = run(best, te_s, te_e)
        oos.extend(test.trades)
        folds.append(WalkForwardFold(
            fold=k + 1, train_start=str(tr_s), train_end=str(tr_e), test_start=str(te_s), test_end=str(te_e),
            chosen_params=best, train_metrics=(best_metrics.to_dict() if best_metrics else {}), test_metrics=test.metrics.to_dict(),
        ))

    metrics = compute_metrics([t.pnl for t in oos], r_multiples=[t.r_multiple for t in oos], starting_equity=cfg.starting_equity, costs=[t.commission for t in oos])
    reasons = evaluate_acceptance(metrics, criteria)
    if synthetic:
        reasons.append("synthetic data: results cannot validate the strategy for real trading")
    status = "PASSED" if not reasons else "FAILED"
    return WalkForwardResult(folds=folds, oos_metrics=metrics, oos_trades=oos, status=status, reasons=reasons, synthetic_data=synthetic)
