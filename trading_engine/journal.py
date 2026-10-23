"""Persistence protocol used by the engine, plus an in-memory implementation.

The backend provides a Django/PostgreSQL implementation
(``backend/apps/execution/journal.py``). The engine writes intent *before*
sending an order (write-ahead) so a crash between send and confirmation is
recoverable by reconciliation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Any, Protocol

from .models import utcnow


class ExecStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"  # intent recorded, request not yet confirmed sent
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    UNKNOWN = "UNKNOWN"  # timeout / connection loss - must be reconciled
    REJECTED = "REJECTED"  # broker or pre-check refused; nothing executed
    FAILED = "FAILED"  # verified not executed after an unknown outcome
    CLOSED = "CLOSED"

    @property
    def blocks_resubmit(self) -> bool:
        return self in {
            ExecStatus.PENDING_SUBMIT,
            ExecStatus.SUBMITTED,
            ExecStatus.FILLED,
            ExecStatus.PARTIALLY_FILLED,
            ExecStatus.UNKNOWN,
            ExecStatus.CLOSED,
        }


@dataclass
class ExecutionRecord:
    signal_id: str
    client_key: str  # short key placed in the MT5 order comment
    symbol: str
    side: str
    requested_volume: float
    requested_price: float
    sl: float
    tp: float
    status: ExecStatus
    strategy_id: str = ""
    strategy_version: str = ""
    risk_snapshot: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    retcode: int | None = None
    message: str = ""
    order_ticket: int | None = None
    deal_ticket: int | None = None
    position_ticket: int | None = None
    filled_volume: float = 0.0
    fill_price: float | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None
    commission: float = 0.0
    swap: float = 0.0
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str = ""
    account_kind: str = ""
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class DayState:
    day: date
    start_equity: float
    locked: bool = False
    locked_reason: str = ""


class Journal(Protocol):
    def get_execution(self, signal_id: str) -> ExecutionRecord | None: ...

    def save_execution(self, record: ExecutionRecord) -> None: ...

    def open_executions(self) -> list[ExecutionRecord]: ...

    def unresolved_executions(self) -> list[ExecutionRecord]: ...

    def log_event(self, level: str, category: str, message: str, data: dict[str, Any] | None = None) -> None: ...

    def load_day_state(self, day: date) -> DayState | None: ...

    def save_day_state(self, state: DayState) -> None: ...

    def last_closed_execution(self) -> ExecutionRecord | None: ...


class InMemoryJournal:
    def __init__(self) -> None:
        self.executions: dict[str, ExecutionRecord] = {}
        self.events: list[dict[str, Any]] = []
        self.days: dict[date, DayState] = {}
        self._lock = threading.RLock()

    def get_execution(self, signal_id: str) -> ExecutionRecord | None:
        with self._lock:
            rec = self.executions.get(signal_id)
            return replace(rec) if rec else None

    def save_execution(self, record: ExecutionRecord) -> None:
        with self._lock:
            record.updated_at = utcnow()
            self.executions[record.signal_id] = replace(record)

    def open_executions(self) -> list[ExecutionRecord]:
        with self._lock:
            return [replace(r) for r in self.executions.values() if r.status in (ExecStatus.FILLED, ExecStatus.PARTIALLY_FILLED)]

    def unresolved_executions(self) -> list[ExecutionRecord]:
        with self._lock:
            return [replace(r) for r in self.executions.values() if r.status in (ExecStatus.PENDING_SUBMIT, ExecStatus.SUBMITTED, ExecStatus.UNKNOWN)]

    def log_event(self, level: str, category: str, message: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.events.append({"time": utcnow(), "level": level, "category": category, "message": message, "data": data or {}})

    def load_day_state(self, day: date) -> DayState | None:
        return self.days.get(day)

    def save_day_state(self, state: DayState) -> None:
        self.days[state.day] = replace(state)

    def last_closed_execution(self) -> ExecutionRecord | None:
        closed = [r for r in self.executions.values() if r.status is ExecStatus.CLOSED and r.closed_at]
        return replace(max(closed, key=lambda r: r.closed_at)) if closed else None
