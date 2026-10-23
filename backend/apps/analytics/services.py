"""Backtest jobs and performance reports."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from apps.execution.models import ConnectionSettings, Execution
from apps.strategies.models import StrategyConfig
from trading_engine.backtester import BacktestConfig, Backtester, CostModel, walk_forward
from trading_engine.codec import encode
from trading_engine.constants import Timeframe
from trading_engine.data import load_mt5_csv, synthetic_ohlc
from trading_engine.metrics import compute_metrics
from trading_engine.mock_mt5 import DEFAULT_SPECS
from trading_engine.strategy_engine import build_strategy

from .models import BacktestRun

log = logging.getLogger(__name__)


def safe_data_path(relative: str) -> Path:
    """CSV files must live inside DATA_DIR (no path traversal)."""
    base = Path(settings.DATA_DIR).resolve()
    path = (base / relative).resolve()
    if base not in path.parents and path != base:
        raise ValueError("data_path must be inside DATA_DIR")
    if not path.is_file():
        raise ValueError(f"file not found: {relative}")
    return path


def load_data(run: BacktestRun) -> tuple[pd.DataFrame, Timeframe, object]:
    tf = Timeframe(run.base_timeframe)
    spec = DEFAULT_SPECS.get(run.symbol)
    if run.data_source == "synthetic":
        base = DEFAULT_SPECS.get(run.symbol, DEFAULT_SPECS["EURUSD"])
        df = synthetic_ohlc(periods=int(run.params.get("_synthetic_bars", 20_000)), minutes=tf.minutes, seed=int(run.params.get("_seed", 7)),
                            base_price={"XAUUSD": 2350.0, "USDJPY": 150.0, "GBPUSD": 1.27}.get(run.symbol, 1.1), digits=base.digits,
                            volatility=0.0007, trend_strength=0.00008)
        return df, tf, spec or base
    if run.data_source == "csv":
        df = load_mt5_csv(safe_data_path(run.data_path))
    else:
        from apps.execution.runner import build_gateway

        gw = build_gateway(ConnectionSettings.load())
        gw.connect()
        spec = gw.symbol_info(run.symbol) or spec
        df = gw.rates_range(run.symbol, tf, run.date_from, run.date_to)
    if run.date_from is not None:
        df = df[df["time"] >= pd.Timestamp(run.date_from)]
    if run.date_to is not None:
        df = df[df["time"] <= pd.Timestamp(run.date_to)]
    if spec is None:
        raise ValueError(f"no contract specification for {run.symbol}; use the 'gateway' data source")
    return df.reset_index(drop=True), tf, spec


def execute_backtest(run_id: int) -> BacktestRun:
    run = BacktestRun.objects.get(pk=run_id)
    run.status = "RUNNING"
    run.save(update_fields=["status"])
    try:
        df, tf, spec = load_data(run)
        synthetic = run.data_source == "synthetic"
        params = {k: v for k, v in run.params.items() if not k.startswith("_")}
        costs = CostModel(**run.costs) if run.costs else CostModel()
        cfg = BacktestConfig(symbol=run.symbol, spec=spec, starting_equity=run.starting_equity, costs=costs,
                             min_rr=float(run.params.get("_min_rr", 1.5)), risk_pct=float(run.params.get("_risk_pct", 0.5)))
        strategy = build_strategy(run.strategy_id, params)
        run.strategy_version = strategy.version
        if run.kind == "WALK_FORWARD":
            wf = walk_forward(df, tf, cfg, strategy_id=run.strategy_id, base_params=params, param_grid=run.param_grid or [{}], synthetic=synthetic)
            run.metrics = wf.oos_metrics.to_dict()
            run.result = encode(wf.to_dict())
            run.validation_status = wf.status
            run.reasons = wf.reasons
            strat = StrategyConfig.objects.filter(strategy_id=run.strategy_id).first()
            if strat is not None and not synthetic:
                strat.validation_status = wf.status
                strat.validation_run = run
                strat.save(update_fields=["validation_status", "validation_run", "updated_at"])
        else:
            res = Backtester(strategy, cfg).run(df, tf, synthetic=synthetic)
            run.metrics = res.metrics.to_dict()
            run.result = encode(res.to_dict())
            run.reasons = ["synthetic data: pipeline check only"] if synthetic else ["single in-sample run: not a validation, use WALK_FORWARD"]
        run.synthetic = synthetic
        run.status = "DONE"
    except Exception as exc:  # noqa: BLE001 - failure is stored on the run
        log.exception("backtest %s failed", run_id)
        run.status = "FAILED"
        run.error = str(exc)
    run.finished_at = timezone.now()
    run.save()
    return run


def start_backtest(run: BacktestRun) -> None:
    if getattr(settings, "BACKTEST_SYNC", False):
        execute_backtest(run.pk)
        return

    def job():
        close_old_connections()
        try:
            execute_backtest(run.pk)
        finally:
            close_old_connections()

    threading.Thread(target=job, name=f"backtest-{run.pk}", daemon=True).start()


def performance_report(source: str, group_by: str | None = None) -> dict:
    """Metrics for DEMO / LIVE (from executions) or BACKTEST (latest runs), never mixed."""
    if source == "BACKTEST":
        rows = []
        for run in BacktestRun.objects.filter(status="DONE").order_by("-created_at")[:20]:
            trades = (run.result or {}).get("oos_trades") or (run.result or {}).get("trades") or []
            rows += [{"symbol": run.symbol, "strategy": run.strategy_id, "pnl": t["pnl"], "r": t["r_multiple"], "cost": t["commission"]} for t in trades]
    else:
        kind = "REAL" if source == "LIVE" else "DEMO"
        qs = Execution.objects.filter(status="CLOSED", account_kind=kind, realized_pnl__isnull=False)
        rows = [{"symbol": e.symbol, "strategy": e.strategy_id, "pnl": e.realized_pnl,
                 "r": (e.realized_pnl / e.risk_snapshot["risk_money"]) if e.risk_snapshot.get("risk_money") else None,
                 "cost": -(e.commission + e.swap)} for e in qs]

    def metrics(items):
        rs = [i["r"] for i in items if i["r"] is not None]
        return compute_metrics([i["pnl"] for i in items], r_multiples=rs or None, costs=[i["cost"] for i in items]).to_dict()

    out = {"source": source, "overall": metrics(rows)}
    if group_by in ("symbol", "strategy"):
        groups = defaultdict(list)
        for r in rows:
            groups[r[group_by]].append(r)
        out["groups"] = {k: metrics(v) for k, v in groups.items()}
    return out
