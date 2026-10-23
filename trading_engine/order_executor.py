"""Order execution with idempotency, pre-checks and post-trade verification.

Rules implemented here:

* Nothing is sent without a fresh, authentic :class:`RiskApproval`.
* Intent is journaled (PENDING_SUBMIT) before ``order_send``.
* A signal that already has a non-terminal or successful execution, or that
  is found on the broker side by its client key, is never sent again.
* A trade is considered open only after the position is observed in MT5.
* After a timeout / lost connection the account is inspected before any
  retry; if it cannot be inspected the record stays UNKNOWN and the agent
  blocks new trades until reconciliation succeeds.
* A position that ends up without a stop loss gets one immediately or is
  closed.
* All trade commands are serialised by one lock.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from .config import ExecutionConfig
from .constants import (
    RETCODES_PARTIAL,
    RETCODES_PRICE_RETRY,
    RETCODES_SUCCESS,
    RETCODES_UNKNOWN,
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    DealEntry,
    OrderFilling,
    OrderType,
    Retcode,
    TradeAction,
    describe_retcode,
)
from .gateway import ConnectionLost, GatewayError, GatewayTimeout
from .journal import ExecStatus, ExecutionRecord, Journal
from .models import OrderRequest, OrderResult, PendingOrder, Position, Side, SymbolInfo, utcnow
from .risk_manager import RiskApproval, RiskManager, floor_to_step

log = logging.getLogger(__name__)


@dataclass
class ExecutionReport:
    status: ExecStatus
    signal_id: str
    message: str
    record: ExecutionRecord | None = None
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (ExecStatus.FILLED, ExecStatus.PARTIALLY_FILLED)


@dataclass
class CommandResult:
    ok: bool
    message: str
    retcode: int | None = None
    unknown: bool = False


def choose_filling(sym: SymbolInfo) -> list[OrderFilling]:
    """Filling modes to try, in order, based on the symbol's allowed flags."""
    modes = []
    if sym.filling_mode & SYMBOL_FILLING_FOK:
        modes.append(OrderFilling.FOK)
    if sym.filling_mode & SYMBOL_FILLING_IOC:
        modes.append(OrderFilling.IOC)
    modes.append(OrderFilling.RETURN)
    return modes


class OrderExecutor:
    def __init__(
        self,
        gateway,
        journal: Journal,
        risk: RiskManager,
        cfg: ExecutionConfig | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.gateway = gateway
        self.journal = journal
        self.risk = risk
        self.cfg = cfg or ExecutionConfig()
        self._sleep = sleep
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- helpers
    def client_key(self, signal_id: str) -> str:
        return f"{self.cfg.comment_prefix}:{signal_id[:12]}"  # MT5 comments are limited to 31 chars

    def _owned(self, comment: str, key: str) -> bool:
        return comment == key or comment.startswith(key)

    def _find_position(self, symbol: str, key: str, order_ticket: int | None = None) -> Position | None:
        for p in self.gateway.positions(symbol):
            if order_ticket and (p.identifier == order_ticket or p.ticket == order_ticket):
                return p
            if p.magic == self.cfg.magic and self._owned(p.comment, key):
                return p
        return None

    def _find_entry_deals(self, key: str, order_ticket: int | None = None):
        now = utcnow()
        deals = self.gateway.history_deals(now - timedelta(days=3), now + timedelta(days=1))
        return [
            d for d in deals
            if d.entry == DealEntry.IN and ((order_ticket and d.order == order_ticket) or (d.magic == self.cfg.magic and self._owned(d.comment, key)))
        ]

    def _broker_state(self, symbol: str, key: str, order_ticket: int | None = None) -> tuple[str, Position | None]:
        """'open' | 'closed' | 'absent' | 'unknown' for the order identified by ``key``."""
        try:
            pos = self._find_position(symbol, key, order_ticket)
            if pos is not None:
                return "open", pos
            if self._find_entry_deals(key, order_ticket):
                return "closed", None
            return "absent", None
        except GatewayError as exc:
            log.warning("broker state unavailable: %s", exc)
            return "unknown", None

    def _verify(self, symbol: str, key: str, order_ticket: int | None = None) -> tuple[str, Position | None]:
        state, pos = "unknown", None
        for attempt in range(self.cfg.verify_attempts):
            state, pos = self._broker_state(symbol, key, order_ticket)
            if state in ("open", "closed"):
                return state, pos
            if attempt + 1 < self.cfg.verify_attempts:
                self._sleep(self.cfg.verify_interval_s)
        return state, pos

    def _event(self, level: str, message: str, **data) -> None:
        self.journal.log_event(level, "execution", message, data)
        getattr(log, "critical" if level == "CRITICAL" else level.lower(), log.info)(message)

    def _mark_filled(self, rec: ExecutionRecord, pos: Position, partial: bool) -> None:
        rec.status = ExecStatus.PARTIALLY_FILLED if partial or pos.volume + 1e-9 < rec.requested_volume else ExecStatus.FILLED
        rec.position_ticket = pos.ticket
        rec.filled_volume = pos.volume
        rec.fill_price = pos.price_open
        rec.opened_at = pos.time
        rec.sl, rec.tp = pos.sl or rec.sl, pos.tp or rec.tp
        self.journal.save_execution(rec)

    # ----------------------------------------------------------------- execute
    def execute(self, approval: RiskApproval, account_kind: str = "") -> ExecutionReport:
        with self._lock:
            return self._execute(approval, account_kind)

    def _execute(self, approval: RiskApproval, account_kind: str) -> ExecutionReport:
        p = approval.proposal
        valid, why = self.risk.approval_valid(approval)
        if not valid:
            self._event("ERROR", f"Refused to execute {p.signal_id}: {why}", signal_id=p.signal_id)
            return ExecutionReport(ExecStatus.REJECTED, p.signal_id, why)

        key = self.client_key(p.signal_id)
        existing = self.journal.get_execution(p.signal_id)
        if existing and existing.status.blocks_resubmit:
            return ExecutionReport(existing.status, p.signal_id, f"duplicate signal, existing status {existing.status.value}", existing, duplicate=True)
        if existing and existing.attempts >= self.cfg.max_price_retries + 2:
            return ExecutionReport(existing.status, p.signal_id, "attempt budget for this signal exhausted", existing, duplicate=True)

        state, pos = self._broker_state(p.symbol, key)
        if state == "unknown":
            return ExecutionReport(ExecStatus.REJECTED, p.signal_id, "cannot read account state; not sending")
        if state in ("open", "closed"):
            rec = existing or self._new_record(approval, key, account_kind)
            if pos is not None:
                self._mark_filled(rec, pos, partial=False)
            else:
                rec.status = ExecStatus.CLOSED
                self.journal.save_execution(rec)
            self._event("WARNING", f"Signal {p.signal_id} already present on broker ({state}); not resending", signal_id=p.signal_id)
            return ExecutionReport(rec.status, p.signal_id, f"already on broker ({state})", rec, duplicate=True)

        rec = self._new_record(approval, key, account_kind)
        if existing:
            rec.attempts = existing.attempts
        self.journal.save_execution(rec)  # write-ahead intent

        sym = self.gateway.symbol_info(p.symbol)
        if sym is None:
            return self._reject(rec, "symbol info unavailable")
        fillings = choose_filling(sym)
        filling_idx = 0
        price_retries = 0
        timeout_retries = 0

        while True:
            tick = self.gateway.tick(p.symbol)
            if tick is None or not tick.is_valid():
                return self._reject(rec, "no valid quote")
            price = tick.ask if p.side is Side.BUY else tick.bid
            drift_points = abs(price - approval.entry_price) / sym.point
            if drift_points > self.cfg.max_slippage_points:
                return self._reject(rec, f"price moved {drift_points:.0f} points since risk approval (max {self.cfg.max_slippage_points}); re-evaluate")
            request = OrderRequest(
                action=TradeAction.DEAL, symbol=p.symbol, volume=approval.volume, type=p.side.order_type,
                price=round(price, sym.digits), sl=round(p.stop_loss, sym.digits), tp=round(p.take_profit, sym.digits),
                deviation=self.cfg.deviation_points, magic=self.cfg.magic, comment=key, type_filling=fillings[filling_idx],
            )
            try:
                check = self.gateway.order_check(request)
            except GatewayError as exc:
                return self._reject(rec, f"order_check failed: {exc}")
            if not check.ok:
                if check.retcode == Retcode.INVALID_FILL and filling_idx + 1 < len(fillings):
                    filling_idx += 1
                    continue
                return self._reject(rec, f"order_check: {describe_retcode(check.retcode)} ({check.comment})", check.retcode)

            rec.attempts += 1
            rec.status = ExecStatus.SUBMITTED
            self.journal.save_execution(rec)
            result: OrderResult | None
            try:
                result = self.gateway.order_send(request)
            except (GatewayTimeout, ConnectionLost) as exc:
                self._event("WARNING", f"order_send outcome unknown for {p.signal_id}: {exc}", signal_id=p.signal_id)
                result = None

            if result is None or result.retcode in RETCODES_UNKNOWN:
                rec.status = ExecStatus.UNKNOWN
                rec.retcode = result.retcode if result else None
                rec.message = "outcome unknown, verifying account state"
                self.journal.save_execution(rec)
                state, pos = self._verify(p.symbol, key, result.order if result else None)
                if state == "open" and pos is not None:
                    self._mark_filled(rec, pos, partial=False)
                    self._event("INFO", f"Verified fill after unknown outcome: {p.symbol} {p.side.value} {pos.volume} @ {pos.price_open}", signal_id=p.signal_id, ticket=pos.ticket)
                    return self._ensure_stop(rec, pos, sym)
                if state == "closed":
                    rec.status = ExecStatus.CLOSED
                    rec.message = "filled and already closed (found in deal history)"
                    self.journal.save_execution(rec)
                    return ExecutionReport(rec.status, p.signal_id, rec.message, rec)
                if state == "unknown":
                    self._event("CRITICAL", f"Cannot verify order {p.signal_id} after timeout; trading blocked until reconciliation", signal_id=p.signal_id)
                    return ExecutionReport(ExecStatus.UNKNOWN, p.signal_id, "unverified - reconciliation required", rec)
                # Verified absent.
                if self.cfg.timeout_retry_policy == "after_verification" and timeout_retries < 1:
                    timeout_retries += 1
                    self._event("WARNING", f"Order {p.signal_id} verified as not executed; retrying once", signal_id=p.signal_id)
                    continue
                rec.status = ExecStatus.FAILED
                rec.message = "not executed (verified after timeout)"
                self.journal.save_execution(rec)
                return ExecutionReport(rec.status, p.signal_id, rec.message, rec)

            rec.retcode = result.retcode
            rec.order_ticket = result.order or None
            rec.deal_ticket = result.deal or None
            if result.retcode in RETCODES_PRICE_RETRY and price_retries < self.cfg.max_price_retries:
                price_retries += 1
                self._event("INFO", f"{describe_retcode(result.retcode)} for {p.signal_id}, retry {price_retries} with fresh quote", signal_id=p.signal_id)
                self._sleep(self.cfg.verify_interval_s)
                continue
            if result.retcode == Retcode.INVALID_FILL and filling_idx + 1 < len(fillings):
                filling_idx += 1
                continue
            if result.retcode in RETCODES_SUCCESS or result.retcode in RETCODES_PARTIAL:
                partial = result.retcode in RETCODES_PARTIAL
                state, pos = self._verify(p.symbol, key, result.order)
                if state == "open" and pos is not None:
                    self._mark_filled(rec, pos, partial)
                    slip = (pos.price_open - approval.entry_price) * p.side.sign / sym.point
                    self._event("INFO", f"Opened {p.symbol} {p.side.value} {pos.volume} @ {pos.price_open} (slippage {slip:+.0f} pts), ticket {pos.ticket}", signal_id=p.signal_id, ticket=pos.ticket, partial=partial)
                    return self._ensure_stop(rec, pos, sym)
                if state == "closed":
                    rec.status = ExecStatus.CLOSED
                    rec.message = "position closed immediately after fill"
                    self.journal.save_execution(rec)
                    return ExecutionReport(rec.status, p.signal_id, rec.message, rec)
                rec.status = ExecStatus.UNKNOWN
                rec.message = f"broker reported {describe_retcode(result.retcode)} but position not found"
                self.journal.save_execution(rec)
                self._event("CRITICAL", f"{rec.message} ({p.signal_id})", signal_id=p.signal_id, order=result.order)
                return ExecutionReport(rec.status, p.signal_id, rec.message, rec)
            return self._reject(rec, f"order_send: {describe_retcode(result.retcode)} ({result.comment})", result.retcode)

    def _new_record(self, approval: RiskApproval, key: str, account_kind: str) -> ExecutionRecord:
        p = approval.proposal
        return ExecutionRecord(
            signal_id=p.signal_id, client_key=key, symbol=p.symbol, side=p.side.value, requested_volume=approval.volume,
            requested_price=approval.entry_price, sl=p.stop_loss, tp=p.take_profit, status=ExecStatus.PENDING_SUBMIT,
            strategy_id=p.strategy_id, strategy_version=p.strategy_version,
            risk_snapshot={"risk_money": approval.risk_money, "risk_pct": approval.risk_pct, "volume": approval.volume,
                           "config": self.risk.cfg.to_dict()},
            account_kind=account_kind,
        )

    def _reject(self, rec: ExecutionRecord, message: str, retcode: int | None = None) -> ExecutionReport:
        rec.status = ExecStatus.REJECTED
        rec.message = message
        rec.retcode = retcode
        self.journal.save_execution(rec)
        self._event("WARNING", f"Order for {rec.signal_id} rejected: {message}", signal_id=rec.signal_id, retcode=retcode)
        return ExecutionReport(rec.status, rec.signal_id, message, rec)

    def _ensure_stop(self, rec: ExecutionRecord, pos: Position, sym: SymbolInfo) -> ExecutionReport:
        if pos.has_stop_loss:
            return ExecutionReport(rec.status, rec.signal_id, "filled and verified", rec)
        self._event("CRITICAL", f"Position {pos.ticket} opened WITHOUT stop loss; setting it now", ticket=pos.ticket)
        res = self._modify(pos, rec.sl, rec.tp, sym)
        if res.ok:
            fresh = self._find_position(pos.symbol, rec.client_key, pos.ticket)
            if fresh is not None and fresh.has_stop_loss:
                return ExecutionReport(rec.status, rec.signal_id, "filled; stop loss attached after fill", rec)
        self._event("CRITICAL", f"Could not attach stop loss to {pos.ticket}; closing position", ticket=pos.ticket)
        close = self._close(pos, None, "no stop loss could be set")
        rec.status = ExecStatus.CLOSED if close.ok else ExecStatus.UNKNOWN
        rec.close_reason = "safety: stop loss missing"
        self.journal.save_execution(rec)
        return ExecutionReport(rec.status, rec.signal_id, "position closed because SL could not be set" if close.ok else "SL missing and close failed - manual action required", rec)

    # ------------------------------------------------------- position commands
    def modify_sltp(self, pos: Position, sl: float, tp: float) -> CommandResult:
        with self._lock:
            sym = self.gateway.symbol_info(pos.symbol)
            if sym is None:
                return CommandResult(False, "symbol info unavailable")
            if not sl or (pos.side is Side.BUY and sl >= pos.price_current) or (pos.side is Side.SELL and sl <= pos.price_current):
                return CommandResult(False, f"refusing SL {sl}: missing or on wrong side of current price {pos.price_current}")
            return self._modify(pos, sl, tp, sym)

    def _modify(self, pos: Position, sl: float, tp: float, sym: SymbolInfo) -> CommandResult:
        req = OrderRequest(action=TradeAction.SLTP, symbol=pos.symbol, volume=pos.volume, type=pos.side.order_type, price=0.0,
                           sl=round(sl, sym.digits), tp=round(tp, sym.digits), magic=pos.magic, position=pos.ticket)
        try:
            result = self.gateway.order_send(req)
        except GatewayError as exc:
            return CommandResult(False, f"modify failed: {exc}", unknown=True)
        if result is None:
            return CommandResult(False, "modify outcome unknown", unknown=True)
        ok = result.retcode in RETCODES_SUCCESS
        return CommandResult(ok, describe_retcode(result.retcode), result.retcode)

    def close_position(self, pos: Position, volume: float | None = None, reason: str = "") -> CommandResult:
        with self._lock:
            return self._close(pos, volume, reason)

    def _close(self, pos: Position, volume: float | None, reason: str) -> CommandResult:
        sym = self.gateway.symbol_info(pos.symbol)
        if sym is None:
            return CommandResult(False, "symbol info unavailable")
        vol = pos.volume if volume is None else floor_to_step(volume, sym.volume_step)
        if volume is not None:
            remaining = round(pos.volume - vol, 8)
            if vol < sym.volume_min or (0 < remaining < sym.volume_min):
                return CommandResult(False, f"partial close {volume} invalid: min {sym.volume_min}, step {sym.volume_step}, remaining {remaining}")
            if vol > pos.volume + 1e-9:
                return CommandResult(False, f"close volume {vol} exceeds position volume {pos.volume}")
        try:
            if not any(p.ticket == pos.ticket for p in self.gateway.positions(pos.symbol)):
                return CommandResult(True, "already closed")
        except GatewayError as exc:
            return CommandResult(False, f"cannot read positions before close: {exc}")
        fillings = choose_filling(sym)
        for filling in fillings:
            tick = self.gateway.tick(pos.symbol)
            if tick is None or not tick.is_valid():
                return CommandResult(False, "no valid quote for close")
            close_side = pos.side.opposite
            req = OrderRequest(
                action=TradeAction.DEAL, symbol=pos.symbol, volume=vol, type=close_side.order_type,
                price=tick.bid if pos.side is Side.BUY else tick.ask, deviation=self.cfg.deviation_points,
                magic=pos.magic or self.cfg.magic, comment=f"{self.cfg.comment_prefix}:close", type_filling=filling, position=pos.ticket,
            )
            before = pos.volume
            try:
                result = self.gateway.order_send(req)
            except GatewayError as exc:
                result = None
                self._event("WARNING", f"close of {pos.ticket} outcome unknown: {exc}", ticket=pos.ticket)
            if result is not None and result.retcode == Retcode.INVALID_FILL:
                continue
            # Verify by reading the position back, whatever the broker said.
            for attempt in range(self.cfg.verify_attempts):
                try:
                    current = next((p for p in self.gateway.positions(pos.symbol) if p.ticket == pos.ticket), None)
                except GatewayError:
                    current = pos
                    if attempt + 1 == self.cfg.verify_attempts:
                        return CommandResult(False, "close unverified (no connection)", unknown=True)
                    self._sleep(self.cfg.verify_interval_s)
                    continue
                closed_vol = before - (current.volume if current else 0.0)
                if closed_vol + 1e-9 >= vol:
                    self._event("INFO", f"Closed {vol} of {pos.ticket} ({reason})", ticket=pos.ticket, reason=reason)
                    return CommandResult(True, "closed" if current is None else f"partially closed, {current.volume} remains", result.retcode if result else None)
                if result is not None and result.retcode not in RETCODES_SUCCESS | RETCODES_PARTIAL | RETCODES_UNKNOWN:
                    return CommandResult(False, f"close rejected: {describe_retcode(result.retcode)}", result.retcode)
                if attempt + 1 < self.cfg.verify_attempts:
                    self._sleep(self.cfg.verify_interval_s)
            return CommandResult(False, "close not confirmed", result.retcode if result else None, unknown=result is None)
        return CommandResult(False, "no supported filling mode for close")

    def cancel_order(self, order: PendingOrder) -> CommandResult:
        with self._lock:
            if order.magic != self.cfg.magic:
                return CommandResult(False, f"order {order.ticket} does not belong to the agent")
            req = OrderRequest(action=TradeAction.REMOVE, symbol=order.symbol, volume=order.volume, type=OrderType(order.type), price=0.0, order=order.ticket)
            try:
                result = self.gateway.order_send(req)
            except GatewayError as exc:
                return CommandResult(False, f"cancel failed: {exc}", unknown=True)
            if result is None:
                return CommandResult(False, "cancel outcome unknown", unknown=True)
            return CommandResult(result.retcode in RETCODES_SUCCESS, describe_retcode(result.retcode), result.retcode)
