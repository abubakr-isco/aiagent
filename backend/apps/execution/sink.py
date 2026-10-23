"""Persists everything the agent produces (snapshots, signals, status)."""

from __future__ import annotations

from django.utils import timezone

from apps.market_data.models import MarketSnapshot
from apps.strategies.models import Prediction, Signal
from trading_engine.agent import AgentStatus, SignalStatus
from trading_engine.codec import encode
from trading_engine.journal import ExecStatus

from .models import AgentControl, Execution


class DjangoSink:
    def __init__(self, runner_name: str = ""):
        self.runner_name = runner_name

    def on_snapshot(self, snapshot) -> None:
        MarketSnapshot.objects.update_or_create(
            symbol=snapshot.symbol,
            defaults={"time": snapshot.time, "bid": snapshot.bid, "ask": snapshot.ask, "data": encode(snapshot.to_dict())},
        )

    def on_evaluation(self, evaluation) -> None:
        MarketSnapshot.objects.filter(symbol=evaluation.symbol).update(
            evaluation={
                "strategy_id": evaluation.strategy_id,
                "has_signal": evaluation.signal is not None,
                "side_considered": evaluation.side_considered.value if evaluation.side_considered else None,
                "checks": [c.to_dict() for c in evaluation.checks],
                "rejection_reasons": evaluation.rejection_reasons,
            }
        )

    def on_signal(self, signal, ai, status: SignalStatus, risk, execution, note: str) -> None:
        existing = Signal.objects.filter(signal_id=signal.signal_id).first()
        # Never downgrade a signal that was executed to a later DUPLICATE/BLOCKED view.
        if existing and existing.status in (SignalStatus.EXECUTED.value, SignalStatus.UNKNOWN.value) and status is not SignalStatus.EXECUTED:
            return
        control = AgentControl.load()
        values = {
            "strategy_id": signal.strategy_id, "strategy_version": signal.strategy_version, "symbol": signal.symbol,
            "side": signal.side.value, "timeframe": signal.timeframe, "bar_time": signal.bar_time, "entry": signal.entry,
            "stop_loss": signal.stop_loss, "take_profit": signal.take_profit, "rr": round(signal.rr, 3), "atr": signal.atr,
            "checks": [c.to_dict() for c in signal.checks], "features": encode(signal.features), "ai": encode(ai.to_dict()),
            "ai_score": ai.score, "ai_approved": ai.approve, "regime": ai.regime, "status": status.value, "note": note[:2000],
            "mode": control.mode,
        }
        if risk is not None:
            values.update(risk=encode(risk.to_dict()), volume=risk.volume, risk_money=round(risk.risk_money, 2), risk_pct=round(risk.risk_pct, 4))
        row, _ = Signal.objects.update_or_create(signal_id=signal.signal_id, defaults=values)
        if status is SignalStatus.EXECUTED:
            Prediction.objects.get_or_create(signal=row, defaults={"score": ai.score, "model_version": ai.model_version})

    def on_status(self, status: AgentStatus) -> None:
        control = AgentControl.load()
        control.state = status.state.value
        control.mode = status.mode.value
        control.kill_switch = status.kill_switch
        control.connected = status.connected
        control.reconciled = status.reconciled
        control.blocked_reasons = status.blocked_reasons
        control.account = encode(status.account) if status.account else None
        control.positions = encode(status.positions)
        control.orders = encode(status.orders)
        control.day = {
            "start_equity": status.day_start_equity, "pnl": status.day_pnl, "loss_pct": status.day_loss_pct, "locked": status.day_locked,
        }
        control.last_error = status.last_error[:2000]
        control.gateway = status.gateway
        control.runner = self.runner_name
        control.heartbeat_at = timezone.now()
        control.save()
        # Floating P&L of open agent trades for the dashboard.
        for p in status.positions:
            Execution.objects.filter(position_ticket=p["ticket"], status__in=[ExecStatus.FILLED.value, ExecStatus.PARTIALLY_FILLED.value]).update(
                unrealized_pnl=round(p["profit"] + p.get("swap", 0.0), 2)
            )
        self._resolve_predictions()

    @staticmethod
    def _resolve_predictions() -> None:
        pending = Prediction.objects.filter(outcome__isnull=True).select_related("signal")[:50]
        for pred in pending:
            ex = Execution.objects.filter(signal_id=pred.signal.signal_id, status=ExecStatus.CLOSED.value).first()
            if ex is None or ex.realized_pnl is None:
                continue
            risk_money = (ex.risk_snapshot or {}).get("risk_money") or 0
            pred.outcome = 1 if ex.realized_pnl > 0 else 0
            pred.r_multiple = round(ex.realized_pnl / risk_money, 3) if risk_money else None
            pred.resolved_at = ex.closed_at
            pred.save()
