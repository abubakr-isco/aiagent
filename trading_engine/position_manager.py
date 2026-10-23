"""Reconciliation of the journal with the real MT5 state, and exit management."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .constants import DealEntry
from .gateway import GatewayError
from .journal import ExecStatus, ExecutionRecord, Journal
from .market_analyzer import MarketSnapshot
from .models import Position, Side, utcnow
from .order_executor import OrderExecutor
from .strategy_engine import Strategy

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    ok: bool
    messages: list[str] = field(default_factory=list)
    orphans: list[int] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    foreign_without_sl: list[int] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)

    @property
    def critical(self) -> bool:
        return bool(self.duplicates)


class PositionManager:
    def __init__(self, gateway, journal: Journal, executor: OrderExecutor):
        self.gateway = gateway
        self.journal = journal
        self.executor = executor

    @property
    def magic(self) -> int:
        return self.executor.cfg.magic

    def realized_today(self, day_start_utc: datetime) -> float | None:
        try:
            deals = self.gateway.history_deals(day_start_utc, utcnow() + timedelta(days=1))
        except GatewayError:
            return None
        return sum(d.net for d in deals if d.entry in (DealEntry.OUT, DealEntry.INOUT, DealEntry.OUT_BY) or d.commission)

    def _close_details(self, rec: ExecutionRecord) -> None:
        if not rec.position_ticket:
            return
        deals = self.gateway.history_deals(utcnow() - timedelta(days=30), utcnow() + timedelta(days=1), position=rec.position_ticket)
        if not deals:
            return
        exits = [d for d in deals if d.entry in (DealEntry.OUT, DealEntry.OUT_BY, DealEntry.INOUT)]
        rec.realized_pnl = round(sum(d.net for d in deals), 2)
        rec.commission = round(sum(d.commission + d.fee for d in deals), 2)
        rec.swap = round(sum(d.swap for d in deals), 2)
        if exits:
            last = max(exits, key=lambda d: d.time)
            total = sum(d.volume for d in exits)
            rec.exit_price = sum(d.price * d.volume for d in exits) / total if total else last.price
            rec.closed_at = last.time
            if not rec.close_reason:
                rec.close_reason = {"[sl]": "stop loss", "[tp]": "take profit"}.get(last.comment[:4], last.comment or "closed")

    def reconcile(self) -> ReconcileReport:
        report = ReconcileReport(ok=True)
        try:
            positions = self.gateway.positions()
            report.positions = positions
            by_ticket = {p.ticket: p for p in positions}

            for rec in self.journal.unresolved_executions():
                state, pos = self.executor._broker_state(rec.symbol, rec.client_key, rec.order_ticket)
                if state == "unknown":
                    report.ok = False
                    report.messages.append(f"{rec.signal_id}: broker state unknown")
                    continue
                if state == "open" and pos is not None:
                    self.executor._mark_filled(rec, pos, partial=False)
                    report.messages.append(f"{rec.signal_id}: recovered as open position {pos.ticket}")
                elif state == "closed":
                    rec.status = ExecStatus.CLOSED
                    self._close_details(rec)
                    self.journal.save_execution(rec)
                    report.messages.append(f"{rec.signal_id}: recovered as already closed")
                else:
                    rec.status = ExecStatus.FAILED
                    rec.message = "not executed (verified during reconciliation)"
                    self.journal.save_execution(rec)
                    report.messages.append(f"{rec.signal_id}: verified not executed")

            known: set[int] = set()
            for rec in self.journal.open_executions():
                pos = by_ticket.get(rec.position_ticket or -1)
                if pos is None:
                    rec.status = ExecStatus.CLOSED
                    self._close_details(rec)
                    self.journal.save_execution(rec)
                    report.closed.append(rec.signal_id)
                    self.journal.log_event("INFO", "positions", f"Position {rec.position_ticket} {rec.symbol} closed: {rec.close_reason}, P&L {rec.realized_pnl}", {"signal_id": rec.signal_id})
                    continue
                known.add(pos.ticket)
                if abs(pos.volume - rec.filled_volume) > 1e-9:
                    rec.filled_volume = pos.volume
                    self.journal.save_execution(rec)
                if not pos.has_stop_loss:
                    res = self.executor.modify_sltp(pos, rec.sl, rec.tp)
                    report.messages.append(f"position {pos.ticket} had no SL; restore -> {res.message}")
                    if not res.ok:
                        self.executor.close_position(pos, reason="stop loss missing and could not be restored")

            keys: dict[str, list[int]] = {}
            for pos in positions:
                if pos.magic != self.magic:
                    if not pos.has_stop_loss:
                        report.foreign_without_sl.append(pos.ticket)
                    continue
                keys.setdefault(pos.comment, []).append(pos.ticket)
                if pos.ticket not in known:
                    report.orphans.append(pos.ticket)
                    self.journal.log_event("WARNING", "positions", f"Agent position {pos.ticket} {pos.symbol} not in journal (orphan); adopted", {"comment": pos.comment})
                    orphan = ExecutionRecord(
                        signal_id=f"orphan-{pos.ticket}", client_key=pos.comment, symbol=pos.symbol, side=pos.side.value,
                        requested_volume=pos.volume, requested_price=pos.price_open, sl=pos.sl, tp=pos.tp, status=ExecStatus.FILLED,
                        position_ticket=pos.ticket, filled_volume=pos.volume, fill_price=pos.price_open, opened_at=pos.time,
                        message="adopted during reconciliation",
                    )
                    self.journal.save_execution(orphan)
                    if not pos.has_stop_loss:
                        self.executor.close_position(pos, reason="orphan agent position without stop loss")
            for comment, tickets in keys.items():
                if comment and len(tickets) > 1:
                    report.duplicates.append(comment)
                    self.journal.log_event("CRITICAL", "positions", f"Duplicate agent positions with key {comment}: {tickets}", {})
        except GatewayError as exc:
            report.ok = False
            report.messages.append(f"reconciliation failed: {exc}")
        if report.critical:
            report.ok = False
        return report

    def manage(self, strategy: Strategy, snapshots: dict[str, MarketSnapshot], now: datetime | None = None) -> list[str]:
        """Apply strategy exit rules and optional breakeven to the agent's open positions."""
        now = now or datetime.now(timezone.utc)
        actions: list[str] = []
        entry_minutes = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}.get(strategy.params.entry_tf, 15)
        try:
            positions = {p.ticket: p for p in self.gateway.positions()}
        except GatewayError as exc:
            return [f"manage skipped: {exc}"]
        for rec in self.journal.open_executions():
            pos = positions.get(rec.position_ticket or -1)
            snap = snapshots.get(rec.symbol)
            if pos is None or snap is None:
                continue
            opened = rec.opened_at or pos.time
            bars_held = int((now - opened).total_seconds() // (entry_minutes * 60)) if opened else 0
            reason = strategy.exit_reason(pos.side, snap, bars_held)
            if reason:
                res = self.executor.close_position(pos, reason=reason)
                rec.close_reason = reason
                self.journal.save_execution(rec)
                actions.append(f"close {pos.ticket}: {reason} -> {res.message}")
                continue
            be = strategy.params.breakeven_at_r
            if be > 0 and rec.fill_price and rec.risk_snapshot:
                initial_risk = abs(rec.fill_price - rec.sl) if rec.sl else 0
                if initial_risk > 0:
                    gain = (pos.price_current - pos.price_open) * pos.side.sign
                    at_be = (pos.sl - pos.price_open) * pos.side.sign >= 0
                    if gain >= be * initial_risk and not at_be:
                        res = self.executor.modify_sltp(pos, pos.price_open, pos.tp)
                        actions.append(f"breakeven {pos.ticket} -> {res.message}")
        return actions
