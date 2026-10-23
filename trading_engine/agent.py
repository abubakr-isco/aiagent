"""Trading agent orchestration: modes, lifecycle, kill switch, trading cycle.

State machine::

    STOPPED --start--> RUNNING --pause--> PAUSED --start--> RUNNING
       ^                  |                  |
       +------stop--------+------stop--------+
    any --emergency_stop--> EMERGENCY_STOP --reset_emergency--> PAUSED

* STOPPED: nothing runs.
* PAUSED: analysis and protective position management continue, no new entries.
* RUNNING: new entries allowed if mode/account/risk allow.
* EMERGENCY_STOP: kill switch latched; agent's pending orders are cancelled;
  positions are NOT closed automatically (explicit, confirmed command only).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Protocol

from .ai_decision_engine import AIDecision, AIDecisionEngine
from .config import AgentConfig, ExecutionConfig, RiskConfig
from .constants import Timeframe
from .gateway import ConnectionLost, GatewayError
from .journal import ExecStatus, Journal
from .market_analyzer import MarketAnalyzer, MarketSnapshot
from .models import AccountInfo, AccountKind, AgentMode, Signal, StrategyEvaluation, utcnow
from .order_executor import ExecutionReport, OrderExecutor
from .position_manager import PositionManager, ReconcileReport
from .risk_manager import QuoteMonitor, RiskContext, RiskDecision, RiskManager, TradeProposal
from .strategy_engine import Strategy

log = logging.getLogger(__name__)

ANALYSIS_TIMEFRAMES = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)


class AgentState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class SignalStatus(str, Enum):
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    BLOCKED = "BLOCKED"
    AI_REJECTED = "AI_REJECTED"
    RISK_REJECTED = "RISK_REJECTED"
    EXECUTED = "EXECUTED"
    EXEC_REJECTED = "EXEC_REJECTED"
    DUPLICATE = "DUPLICATE"
    UNKNOWN = "UNKNOWN"


class AgentSink(Protocol):
    """Receives everything the agent produces (the backend persists it)."""

    def on_snapshot(self, snapshot: MarketSnapshot) -> None: ...

    def on_evaluation(self, evaluation: StrategyEvaluation) -> None: ...

    def on_signal(self, signal: Signal, ai: AIDecision, status: SignalStatus, risk: RiskDecision | None, execution: ExecutionReport | None, note: str) -> None: ...

    def on_status(self, status: "AgentStatus") -> None: ...


class NullSink:
    def __init__(self) -> None:
        self.signals: list[tuple[Signal, AIDecision, SignalStatus, RiskDecision | None, ExecutionReport | None, str]] = []
        self.snapshots: dict[str, MarketSnapshot] = {}
        self.status: AgentStatus | None = None

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.snapshots[snapshot.symbol] = snapshot

    def on_evaluation(self, evaluation: StrategyEvaluation) -> None:
        pass

    def on_signal(self, signal, ai, status, risk, execution, note) -> None:
        self.signals.append((signal, ai, status, risk, execution, note))

    def on_status(self, status: "AgentStatus") -> None:
        self.status = status


@dataclass
class AgentStatus:
    state: AgentState
    mode: AgentMode
    connected: bool
    reconciled: bool
    kill_switch: bool
    blocked_reasons: list[str]
    account: AccountInfo | None
    day_start_equity: float | None
    day_pnl: float | None
    day_loss_pct: float | None
    day_locked: bool
    positions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    last_cycle_at: datetime | None = None
    last_error: str = ""
    gateway: str = ""


@dataclass
class CycleReport:
    started_at: datetime
    signals: int = 0
    executed: int = 0
    errors: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    skipped: str = ""


class TradingAgent:
    def __init__(
        self,
        gateway,
        journal: Journal,
        strategy: Strategy,
        *,
        risk_cfg: RiskConfig | None = None,
        exec_cfg: ExecutionConfig | None = None,
        agent_cfg: AgentConfig | None = None,
        ai: AIDecisionEngine | None = None,
        analyzer: MarketAnalyzer | None = None,
        sink: AgentSink | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.gateway = gateway
        self.journal = journal
        self.strategy = strategy
        self.cfg = agent_cfg or AgentConfig()
        self.analyzer = analyzer or MarketAnalyzer()
        self.ai = ai or AIDecisionEngine(min_quality=self.cfg.ai_min_quality)
        self.quotes = QuoteMonitor(clock=monotonic)
        self.risk = RiskManager(risk_cfg or RiskConfig(), journal, gateway, self.quotes, clock=monotonic)
        self.executor = OrderExecutor(gateway, journal, self.risk, exec_cfg, sleep=sleep)
        self.positions = PositionManager(gateway, journal, self.executor)
        self.sink = sink or NullSink()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._lock = threading.RLock()

        self.state = AgentState.STOPPED
        self.mode = AgentMode.ANALYSIS
        self.kill_switch = False
        self.live_confirmed = False
        self.connected = False
        self.reconciled = False
        self.last_error = ""
        self.last_reconcile: ReconcileReport | None = None
        self.account: AccountInfo | None = None
        self._reconnect_attempt = 0
        self._next_reconnect = 0.0
        self.last_cycle_at: datetime | None = None
        self.snapshots: dict[str, MarketSnapshot] = {}

    # --------------------------------------------------------------- controls
    def _event(self, level: str, message: str, **data: Any) -> None:
        self.journal.log_event(level, "agent", message, data)
        log.log(logging.getLevelName(level) if level != "CRITICAL" else logging.CRITICAL, message)

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.state is AgentState.EMERGENCY_STOP:
                return False, "emergency stop is latched; reset it explicitly first"
            self.state = AgentState.RUNNING
            self._event("INFO", f"Agent started in {self.mode.value} mode")
            return True, "running"

    def pause(self) -> tuple[bool, str]:
        with self._lock:
            if self.state is AgentState.EMERGENCY_STOP:
                return False, "emergency stop is latched"
            self.state = AgentState.PAUSED
            self._event("INFO", "Agent paused: no new entries")
            return True, "paused"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if self.state is not AgentState.EMERGENCY_STOP:
                self.state = AgentState.STOPPED
            self._event("INFO", "Agent stopped")
            return True, self.state.value.lower()

    def emergency_stop(self, reason: str = "manual") -> tuple[bool, str]:
        with self._lock:
            self.kill_switch = True
            self.state = AgentState.EMERGENCY_STOP
            self._event("CRITICAL", f"EMERGENCY STOP: {reason}. New trades blocked.")
            cancelled, failed = 0, 0
            try:
                for order in self.gateway.orders():
                    if order.magic != self.executor.cfg.magic:
                        continue
                    res = self.executor.cancel_order(order)
                    cancelled += int(res.ok)
                    failed += int(not res.ok)
            except GatewayError as exc:
                self._event("ERROR", f"Could not cancel pending orders during emergency stop: {exc}")
                return True, f"kill switch engaged; pending orders NOT verified cancelled ({exc})"
            msg = f"kill switch engaged; cancelled {cancelled} agent pending orders" + (f", {failed} failed" if failed else "")
            self._event("CRITICAL", msg)
            return True, msg

    def reset_emergency(self, actor: str = "") -> tuple[bool, str]:
        with self._lock:
            self.kill_switch = False
            self.state = AgentState.PAUSED
            self.reconciled = False
            self._event("WARNING", f"Emergency stop reset by {actor or 'unknown'}; agent PAUSED, reconciliation required")
            return True, "reset; agent paused"

    def close_agent_positions(self, confirmed: bool, reason: str = "manual close-all") -> tuple[bool, str]:
        """Close only the agent's positions, and only with explicit confirmation."""
        if not confirmed:
            return False, "confirmation required: closing positions may realise a loss"
        with self._lock:
            results = []
            for pos in self.gateway.positions():
                if pos.magic == self.executor.cfg.magic:
                    res = self.executor.close_position(pos, reason=reason)
                    results.append(f"{pos.ticket}: {res.message}")
            self._event("WARNING", f"Close agent positions: {results or 'none open'}")
            self.reconciled = False
            return True, "; ".join(results) or "no agent positions"

    def set_mode(self, mode: AgentMode, *, live_confirmed: bool = False) -> tuple[bool, str]:
        with self._lock:
            if mode is AgentMode.LIVE:
                if not self.cfg.allow_live_env:
                    return False, "LIVE is disabled on this deployment (ALLOW_LIVE_TRADING is not set)"
                if not live_confirmed:
                    return False, "LIVE requires explicit confirmation"
                acct = self._safe_account()
                if acct is None or acct.kind is not AccountKind.REAL:
                    return False, f"LIVE requires a verified REAL account (terminal reports {acct.kind.value if acct else 'nothing'})"
                self.live_confirmed = True
            elif mode is AgentMode.DEMO:
                acct = self._safe_account()
                if acct is None or acct.kind is not AccountKind.DEMO:
                    return False, f"DEMO requires a verified demo account (terminal reports {acct.kind.value if acct else 'nothing'})"
                self.live_confirmed = False
            else:
                self.live_confirmed = False
            previous = self.mode
            self.mode = mode
            if mode is not previous:
                self.state = AgentState.PAUSED if self.state is AgentState.RUNNING else self.state
            self._event("WARNING" if mode is AgentMode.LIVE else "INFO", f"Mode changed {previous.value} -> {mode.value}; agent paused until started again")
            return True, mode.value

    def _safe_account(self) -> AccountInfo | None:
        try:
            return self.gateway.account_info()
        except GatewayError:
            return None

    # ------------------------------------------------------------- connection
    def ensure_connection(self) -> bool:
        try:
            status = self.gateway.terminal_status()
            if status.connected:
                if not self.connected:
                    self._event("INFO", "Terminal connected")
                self.connected = True
                self._reconnect_attempt = 0
                return True
        except GatewayError:
            pass
        if self.connected:
            self._event("ERROR", "Connection to terminal lost; new trades blocked")
        self.connected = False
        self.reconciled = False
        now = self._monotonic()
        if now < self._next_reconnect:
            return False
        try:
            self.gateway.connect()
            status = self.gateway.terminal_status()
            self.connected = bool(status.connected)
        except GatewayError as exc:
            self.last_error = str(exc)
            self.connected = False
        if not self.connected:
            backoff = self.cfg.reconnect_backoff_s[min(self._reconnect_attempt, len(self.cfg.reconnect_backoff_s) - 1)]
            self._reconnect_attempt += 1
            self._next_reconnect = now + backoff
            self._event("WARNING", f"Reconnect attempt {self._reconnect_attempt} failed; next in {backoff}s")
        else:
            self._reconnect_attempt = 0
            self._event("INFO", "Reconnected to terminal; reconciliation pending")
        return self.connected

    # ------------------------------------------------------------------ cycle
    def blocked_reasons(self) -> list[str]:
        reasons = []
        if self.mode is AgentMode.ANALYSIS:
            reasons.append("ANALYSIS mode")
        if self.state is not AgentState.RUNNING:
            reasons.append(f"agent {self.state.value}")
        if self.kill_switch:
            reasons.append("kill switch")
        if not self.connected:
            reasons.append("no connection")
        if not self.reconciled:
            reasons.append("not reconciled")
        acct = self.account
        if acct is not None:
            if self.mode is AgentMode.DEMO and acct.kind is not AccountKind.DEMO:
                reasons.append(f"DEMO mode but account is {acct.kind.value}")
            if self.mode is AgentMode.LIVE and (acct.kind is not AccountKind.REAL or not self.live_confirmed or not self.cfg.allow_live_env):
                reasons.append("LIVE preconditions not met")
        return reasons

    def run_cycle(self) -> CycleReport:
        with self._lock:
            return self._run_cycle()

    def _run_cycle(self) -> CycleReport:
        report = CycleReport(started_at=utcnow())
        if self.state is AgentState.STOPPED:
            report.skipped = "stopped"
            self._publish_status()
            return report
        if not self.ensure_connection():
            report.skipped = "no connection"
            self._publish_status()
            return report
        try:
            # Reconcile every cycle: positions closed broker-side (SL/TP, user)
            # are booked, and any mismatch blocks new trades immediately.
            was_reconciled = self.reconciled
            rec = self.positions.reconcile()
            self.last_reconcile = rec
            self.reconciled = rec.ok
            for m in rec.messages:
                self._event("INFO" if rec.ok else "WARNING", f"reconcile: {m}")
            if rec.critical:
                self.emergency_stop(f"reconciliation found duplicate positions {rec.duplicates}")
            elif rec.ok and not was_reconciled:
                self._event("INFO", f"Reconciled: {len(rec.positions)} positions on account, {len(rec.orphans)} orphans adopted")
            self.account = self.gateway.account_info()
            now = self.clock()
            day_start = datetime.combine(self.risk.daily.trading_day(now), datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=self.risk.cfg.trading_day_utc_offset_hours)
            realized = self.positions.realized_today(day_start)
            self.risk.daily.update(self.account, now, realized)

            self.snapshots = self._collect_snapshots(report)
            if self.mode is not AgentMode.ANALYSIS and self.state in (AgentState.RUNNING, AgentState.PAUSED) and self.reconciled:
                report.actions += self.positions.manage(self.strategy, self.snapshots, now)
            for symbol, snap in self.snapshots.items():
                try:
                    self._evaluate_symbol(symbol, snap, now, realized, report)
                except ConnectionLost as exc:
                    raise exc
                except GatewayError as exc:
                    report.errors.append(f"{symbol}: {exc}")
        except ConnectionLost as exc:
            self.connected = False
            self.reconciled = False
            self.last_error = str(exc)
            report.errors.append(str(exc))
            self._event("ERROR", f"Connection lost during cycle: {exc}")
        except GatewayError as exc:
            self.last_error = str(exc)
            report.errors.append(str(exc))
            self._event("ERROR", f"Gateway error during cycle: {exc}")
        self.last_cycle_at = utcnow()
        self._publish_status()
        return report

    def _collect_snapshots(self, report: CycleReport) -> dict[str, MarketSnapshot]:
        out: dict[str, MarketSnapshot] = {}
        for symbol in self.cfg.symbols:
            info = self.gateway.symbol_info(symbol)
            if info is None:
                report.errors.append(f"{symbol}: not offered by broker (check symbol name/suffix)")
                continue
            if not info.visible:
                self.gateway.symbol_select(symbol)
            tick = self.gateway.tick(symbol)
            if tick is None or not tick.is_valid():
                report.errors.append(f"{symbol}: no valid quote")
                self._event("WARNING", f"{symbol}: invalid or missing quote {tick}")
                continue
            self.quotes.observe(tick)
            frames = {tf: self.gateway.rates(symbol, tf, self.cfg.bars_per_timeframe) for tf in ANALYSIS_TIMEFRAMES}
            snap = self.analyzer.snapshot(symbol, frames, tick=tick, point=info.point, now=self.clock())
            out[symbol] = snap
            self.sink.on_snapshot(snap)
        return out

    def _evaluate_symbol(self, symbol: str, snap: MarketSnapshot, now: datetime, realized: float | None, report: CycleReport) -> None:
        evaluation = self.strategy.evaluate(snap, spread=snap.spread, now_utc=now)
        self.sink.on_evaluation(evaluation)
        signal = evaluation.signal
        if signal is None:
            return
        report.signals += 1
        ai = self.ai.evaluate(signal, snap)
        blocked = self.blocked_reasons()
        if self.mode is AgentMode.ANALYSIS:
            self.sink.on_signal(signal, ai, SignalStatus.ANALYSIS_ONLY, None, None, "analysis mode")
            return
        existing = self.journal.get_execution(signal.signal_id)
        if existing and existing.status.blocks_resubmit:
            self.sink.on_signal(signal, ai, SignalStatus.DUPLICATE, None, None, f"already {existing.status.value}")
            return
        if blocked:
            self.sink.on_signal(signal, ai, SignalStatus.BLOCKED, None, None, "; ".join(blocked))
            return
        if not ai.approve:
            self.sink.on_signal(signal, ai, SignalStatus.AI_REJECTED, None, None, "; ".join(ai.reasons_against))
            return
        info = self.gateway.symbol_info(symbol)
        tick = self.gateway.tick(symbol)
        if tick is not None:
            self.quotes.observe(tick)
        ctx = RiskContext(
            mode=self.mode, account=self.gateway.account_info(), symbol=info, tick=tick, positions=self.gateway.positions(),
            connected=self.connected, reconciled=self.reconciled, kill_switch=self.kill_switch,
            quote_fresh=self.quotes.is_fresh(symbol, self.risk.cfg.max_quote_age_s),
            live_enabled=self.live_confirmed and self.cfg.allow_live_env,
            entry_atr=snap.frame(self.strategy.params.entry_tf).atr if snap.frame(self.strategy.params.entry_tf) else None,
        )
        proposal = TradeProposal(signal.signal_id, symbol, signal.side, signal.entry, signal.stop_loss, signal.take_profit, signal.strategy_id, signal.strategy_version)
        decision = self.risk.evaluate(proposal, ctx, realized_today=realized, now=now)
        if not decision.approved or decision.approval is None:
            self.sink.on_signal(signal, ai, SignalStatus.RISK_REJECTED, decision, None, "; ".join(decision.violations))
            return
        execution = self.executor.execute(decision.approval, account_kind=ctx.account.kind.value if ctx.account else "")
        if execution.status is ExecStatus.UNKNOWN:
            self.reconciled = False
        status = (
            SignalStatus.EXECUTED if execution.ok or execution.status is ExecStatus.CLOSED
            else SignalStatus.DUPLICATE if execution.duplicate
            else SignalStatus.UNKNOWN if execution.status is ExecStatus.UNKNOWN
            else SignalStatus.EXEC_REJECTED
        )
        if execution.ok:
            report.executed += 1
        self.sink.on_signal(signal, ai, status, decision, execution, execution.message)

    def status(self) -> AgentStatus:
        acct = self.account
        day = None
        if acct is not None:
            day = self.journal.load_day_state(self.risk.daily.trading_day(self.clock()))
        positions, orders = [], []
        if self.connected:
            try:
                positions = [
                    {"ticket": p.ticket, "symbol": p.symbol, "side": p.side.value, "volume": p.volume, "price_open": p.price_open,
                     "price_current": p.price_current, "sl": p.sl, "tp": p.tp, "profit": p.profit, "swap": p.swap,
                     "magic": p.magic, "comment": p.comment, "agent": p.magic == self.executor.cfg.magic, "time": p.time.isoformat()}
                    for p in self.gateway.positions()
                ]
                orders = [
                    {"ticket": o.ticket, "symbol": o.symbol, "type": o.type, "volume": o.volume, "price": o.price_open,
                     "sl": o.sl, "tp": o.tp, "magic": o.magic, "agent": o.magic == self.executor.cfg.magic}
                    for o in self.gateway.orders()
                ]
            except GatewayError:
                pass
        return AgentStatus(
            state=self.state, mode=self.mode, connected=self.connected, reconciled=self.reconciled, kill_switch=self.kill_switch,
            blocked_reasons=self.blocked_reasons(), account=acct,
            day_start_equity=day.start_equity if day else None,
            day_pnl=round(acct.equity - day.start_equity, 2) if (day and acct) else None,
            day_loss_pct=self.risk.daily.loss_pct(day, acct) if (day and acct) else None,
            day_locked=bool(day and day.locked), positions=positions, orders=orders,
            last_cycle_at=self.last_cycle_at, last_error=self.last_error, gateway=getattr(self.gateway, "name", ""),
        )

    def _publish_status(self) -> None:
        try:
            self.sink.on_status(self.status())
        except Exception as exc:  # noqa: BLE001 - status publishing must never break trading
            log.warning("status publish failed: %s", exc)

    def run_forever(self, should_continue: Callable[[], bool] = lambda: True, on_tick: Callable[["TradingAgent"], None] | None = None) -> None:
        while should_continue():
            started = self._monotonic()
            if on_tick:
                on_tick(self)
            try:
                self.run_cycle()
            except Exception as exc:  # noqa: BLE001 - loop must survive unexpected errors
                self.last_error = repr(exc)
                self._event("ERROR", f"Unhandled error in cycle: {exc!r}")
                log.exception("cycle failed")
            elapsed = self._monotonic() - started
            time.sleep(max(0.5, self.cfg.loop_interval_s - elapsed))
