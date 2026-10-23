"""PostgreSQL implementation of the engine's Journal protocol."""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db import transaction

from apps.audit.logging import redact
from apps.audit.models import SystemEvent
from apps.risk.models import DailyRiskState
from trading_engine.journal import DayState, ExecStatus, ExecutionRecord

from .models import Execution

_FIELDS = [
    "client_key", "symbol", "side", "requested_volume", "requested_price", "sl", "tp", "strategy_id", "strategy_version",
    "risk_snapshot", "attempts", "retcode", "message", "order_ticket", "deal_ticket", "position_ticket", "filled_volume",
    "fill_price", "exit_price", "realized_pnl", "commission", "swap", "opened_at", "closed_at", "close_reason", "account_kind",
]


def to_record(row: Execution) -> ExecutionRecord:
    return ExecutionRecord(signal_id=row.signal_id, status=ExecStatus(row.status), **{f: getattr(row, f) for f in _FIELDS})


class DjangoJournal:
    def get_execution(self, signal_id: str) -> ExecutionRecord | None:
        row = Execution.objects.filter(signal_id=signal_id).first()
        return to_record(row) if row else None

    def save_execution(self, record: ExecutionRecord) -> None:
        values = {f: getattr(record, f) for f in _FIELDS}
        values["status"] = record.status.value
        values["message"] = redact(values["message"] or "")
        # Commit immediately: the write-ahead intent must survive a crash.
        with transaction.atomic():
            Execution.objects.update_or_create(signal_id=record.signal_id, defaults=values)

    def open_executions(self) -> list[ExecutionRecord]:
        rows = Execution.objects.filter(status__in=[ExecStatus.FILLED.value, ExecStatus.PARTIALLY_FILLED.value])
        return [to_record(r) for r in rows]

    def unresolved_executions(self) -> list[ExecutionRecord]:
        rows = Execution.objects.filter(status__in=[ExecStatus.PENDING_SUBMIT.value, ExecStatus.SUBMITTED.value, ExecStatus.UNKNOWN.value])
        return [to_record(r) for r in rows]

    def log_event(self, level: str, category: str, message: str, data: dict[str, Any] | None = None) -> None:
        SystemEvent.objects.create(level=level, category=category, message=redact(message), data=_jsonable(data or {}))

    def load_day_state(self, day: date) -> DayState | None:
        row = DailyRiskState.objects.filter(day=day).first()
        return DayState(day=row.day, start_equity=row.start_equity, locked=row.locked, locked_reason=row.locked_reason) if row else None

    def save_day_state(self, state: DayState) -> None:
        DailyRiskState.objects.update_or_create(
            day=state.day, defaults={"start_equity": state.start_equity, "locked": state.locked, "locked_reason": state.locked_reason}
        )

    def last_closed_execution(self) -> ExecutionRecord | None:
        row = Execution.objects.filter(status=ExecStatus.CLOSED.value, closed_at__isnull=False).order_by("-closed_at").first()
        return to_record(row) if row else None


def _jsonable(value: Any) -> Any:
    from trading_engine.codec import encode

    return encode(value)
