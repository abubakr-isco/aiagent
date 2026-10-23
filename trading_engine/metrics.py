"""Performance statistics shared by backtests, demo and live reports."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class PerformanceReport:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    net_profit: float
    profit_factor: float | None
    avg_win: float
    avg_loss: float
    expectancy: float
    expectancy_r: float | None
    max_drawdown: float
    max_drawdown_pct: float
    total_costs: float

    def to_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v) for k, v in asdict(self).items()}


def max_drawdown(equity: Iterable[float]) -> tuple[float, float]:
    peak = None
    dd, dd_pct = 0.0, 0.0
    for value in equity:
        peak = value if peak is None else max(peak, value)
        dd = max(dd, peak - value)
        if peak > 0:
            dd_pct = max(dd_pct, (peak - value) / peak * 100)
    return dd, dd_pct


def compute_metrics(pnls: list[float], *, r_multiples: list[float] | None = None, starting_equity: float = 0.0, costs: list[float] | None = None) -> PerformanceReport:
    pnls = [float(p) for p in pnls]
    r_multiples = [float(r) for r in r_multiples] if r_multiples else r_multiples
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    curve = [starting_equity]
    for p in pnls:
        curve.append(curve[-1] + p)
    dd, dd_pct = max_drawdown(curve)
    if starting_equity <= 0:
        dd_pct = float("nan")  # no capital base: only the absolute drawdown is meaningful
    n = len(pnls)
    return PerformanceReport(
        trades=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / n * 100, 2) if n else 0.0,
        gross_profit=round(gp, 2),
        gross_loss=round(gl, 2),
        net_profit=round(gp - gl, 2),
        profit_factor=round(gp / gl, 3) if gl > 0 else (None if gp == 0 else float("inf")),
        avg_win=round(gp / len(wins), 2) if wins else 0.0,
        avg_loss=round(-gl / len(losses), 2) if losses else 0.0,
        expectancy=round((gp - gl) / n, 2) if n else 0.0,
        expectancy_r=round(sum(r_multiples) / len(r_multiples), 3) if r_multiples else None,
        max_drawdown=round(dd, 2),
        max_drawdown_pct=round(dd_pct, 2) if not math.isnan(dd_pct) else float("nan"),
        total_costs=round(sum(costs), 2) if costs else 0.0,
    )
