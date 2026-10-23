"""Agent runner process: the only place where the trading loop runs.

The web API never talks to MT5 directly - it writes AgentCommand rows and
settings; this process applies them, runs cycles and reports status. A
PostgreSQL advisory lock guarantees a single runner (no parallel conflicting
trade commands across processes).
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any, Callable

from django.conf import settings
from django.db import connection
from django.utils import timezone as dj_tz

from apps.analytics.models import BacktestRun
from apps.audit.models import SystemEvent
from apps.market_data.models import SymbolConfig
from apps.risk.models import RiskSettings
from apps.strategies.models import StrategyConfig
from trading_engine.agent import AgentState, TradingAgent
from trading_engine.ai_decision_engine import AIDecisionEngine, PerformanceStats
from trading_engine.config import AgentConfig, ExecutionConfig
from trading_engine.models import AgentMode
from trading_engine.risk_manager import DailyLossTracker
from trading_engine.strategy_engine import TrendPullbackStrategy, build_strategy

from .journal import DjangoJournal
from .models import AgentCommand, AgentControl, ConnectionSettings
from .sink import DjangoSink

log = logging.getLogger(__name__)
ADVISORY_LOCK_KEY = 0x41495441  # "AITA"


class RunnerLockError(RuntimeError):
    pass


def build_gateway(conn: ConnectionSettings):
    if conn.gateway == "mt5":
        from trading_engine.mt5_connector import MT5Connector

        return MT5Connector()
    if conn.gateway == "remote":
        from trading_engine.remote_gateway import RemoteGateway

        return RemoteGateway(conn.bridge_url or None)
    from trading_engine.mock_mt5 import MockMT5Gateway

    return MockMT5Gateway()


def ensure_defaults() -> StrategyConfig:
    RiskSettings.load()
    ConnectionSettings.load()
    AgentControl.load()
    if not SymbolConfig.objects.exists():
        for name in ("EURUSD", "GBPUSD", "XAUUSD"):
            SymbolConfig.objects.create(name=name)
    strat, _ = StrategyConfig.objects.get_or_create(
        strategy_id=TrendPullbackStrategy.strategy_id,
        defaults={"version": TrendPullbackStrategy.version, "params": TrendPullbackStrategy().params.to_dict()},
    )
    return strat


def performance_context() -> dict[tuple[str, str], PerformanceStats]:
    out: dict[tuple[str, str], PerformanceStats] = {}
    for run in BacktestRun.objects.filter(status="DONE", kind="WALK_FORWARD", synthetic=False).order_by("created_at"):
        m = run.metrics or {}
        out[(run.strategy_id, run.symbol)] = PerformanceStats(
            source="BACKTEST_OOS", trades=int(m.get("trades") or 0), expectancy_r=float(m.get("expectancy_r") or 0.0),
            profit_factor=float(m.get("profit_factor") or 0.0), validated=run.validation_status == "PASSED",
        )
    return out


class AgentRunner:
    def __init__(self, gateway=None, *, sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic, advance_mock: bool = True):
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self.conn = ConnectionSettings.load()
        self.strategy_cfg = ensure_defaults()
        self.gateway = gateway or build_gateway(self.conn)
        self.journal = DjangoJournal()
        self.sink = DjangoSink(self.name)
        self.advance_mock = advance_mock and getattr(self.gateway, "name", "") == "mock"
        clock = None
        if getattr(self.gateway, "name", "") == "mock":
            clock = self.gateway.now  # simulated market time
        self.agent = TradingAgent(
            self.gateway, self.journal, self._strategy(), risk_cfg=RiskSettings.load().to_config(),
            exec_cfg=ExecutionConfig(), agent_cfg=self._agent_cfg(), ai=self._ai(), sink=self.sink, clock=clock,
            sleep=sleep, monotonic=monotonic,
        )
        self._versions = self._settings_versions()
        self._restored = False

    # ------------------------------------------------------------ settings
    def _symbols(self) -> tuple[str, ...]:
        return tuple(SymbolConfig.objects.filter(enabled=True).values_list("name", flat=True))

    def _agent_cfg(self) -> AgentConfig:
        conn = ConnectionSettings.load()
        return AgentConfig(
            symbols=self._symbols(), loop_interval_s=conn.loop_interval_s, bars_per_timeframe=conn.bars_per_timeframe,
            ai_min_quality=conn.ai_min_quality, allow_live_env=settings.ALLOW_LIVE_TRADING,
        )

    def _strategy(self):
        cfg = StrategyConfig.objects.get(pk=self.strategy_cfg.pk)
        return build_strategy(cfg.strategy_id, cfg.params)

    def _ai(self) -> AIDecisionEngine:
        return AIDecisionEngine(min_quality=ConnectionSettings.load().ai_min_quality, performance=performance_context())

    def _settings_versions(self) -> tuple[Any, ...]:
        return (
            RiskSettings.load().updated_at,
            StrategyConfig.objects.get(pk=self.strategy_cfg.pk).updated_at,
            ConnectionSettings.load().updated_at,
            tuple(SymbolConfig.objects.values_list("name", "enabled", "max_spread_points")),
            BacktestRun.objects.filter(status="DONE").count(),
        )

    def reload_if_changed(self) -> None:
        current = self._settings_versions()
        if current == self._versions:
            return
        self._versions = current
        cfg = RiskSettings.load().to_config()
        errors = cfg.validate()
        if errors:
            self.journal.log_event("ERROR", "risk", f"Rejected invalid risk settings: {errors}; keeping previous limits")
        else:
            self.agent.risk.cfg = cfg
            self.agent.risk.daily = DailyLossTracker(self.journal, cfg)
        self.agent.strategy = self._strategy()
        self.agent.cfg = self._agent_cfg()
        self.agent.ai = self._ai()
        conn = ConnectionSettings.load()
        if conn.gateway != self.conn.gateway or conn.bridge_url != self.conn.bridge_url:
            self.journal.log_event("WARNING", "connection", "Gateway settings changed; restart the runner to apply them")
        self.journal.log_event("INFO", "agent", "Settings reloaded", {"risk": cfg.to_dict(), "symbols": list(self.agent.cfg.symbols)})

    # -------------------------------------------------------------- restore
    def restore(self) -> None:
        """Safe restart: restore kill switch and mode, but never resume trading blindly."""
        control = AgentControl.load()
        agent = self.agent
        if control.kill_switch:
            agent.kill_switch = True
            agent.state = AgentState.EMERGENCY_STOP
            self.journal.log_event("CRITICAL", "agent", "Restarted with kill switch latched; trading stays blocked")
            self._restored = True
            return
        agent.ensure_connection()
        if control.mode == AgentMode.DEMO.value:
            ok, msg = agent.set_mode(AgentMode.DEMO)
            if not ok:
                self.journal.log_event("WARNING", "agent", f"Could not restore DEMO mode: {msg}; staying in ANALYSIS")
        elif control.mode == AgentMode.LIVE.value:
            if control.live_confirmed_at and settings.ALLOW_LIVE_TRADING and self._strategy_validated():
                ok, msg = agent.set_mode(AgentMode.LIVE, live_confirmed=True)
            else:
                ok, msg = False, "LIVE preconditions not met after restart"
            if not ok:
                self.journal.log_event("WARNING", "agent", f"Could not restore LIVE mode: {msg}; staying in ANALYSIS")
        # PAUSED still analyses and reconciles; new entries need an explicit START
        # unless AGENT_AUTO_RESUME is set and the agent was running before.
        resume = settings.AGENT_AUTO_RESUME and control.state == AgentState.RUNNING.value
        agent.state = AgentState.RUNNING if resume else AgentState.PAUSED
        self.journal.log_event("INFO", "agent", f"Runner {self.name} started: mode {agent.mode.value}, state {agent.state.value}; reconciliation runs before any trade")
        self._restored = True

    def _strategy_validated(self) -> bool:
        return StrategyConfig.objects.get(pk=self.strategy_cfg.pk).validation_status == "PASSED"

    # ------------------------------------------------------------- commands
    def process_commands(self) -> None:
        for cmd in AgentCommand.objects.filter(status="PENDING").order_by("created_at"):
            try:
                ok, message = self._apply(cmd)
            except Exception as exc:  # noqa: BLE001 - report every failure on the command itself
                ok, message = False, f"error: {exc}"
            cmd.status = "DONE" if ok else "FAILED"
            cmd.result = message
            cmd.processed_at = dj_tz.now()
            cmd.save()
            SystemEvent.objects.create(level="INFO" if ok else "WARNING", category="command", message=f"{cmd.command} by {cmd.requested_by}: {message}",
                                       data={"command_id": cmd.pk, "params": cmd.params})

    def _apply(self, cmd: AgentCommand) -> tuple[bool, str]:
        agent = self.agent
        control = AgentControl.load()
        if cmd.command == "start":
            return agent.start()
        if cmd.command == "pause":
            return agent.pause()
        if cmd.command == "stop":
            return agent.stop()
        if cmd.command == "emergency_stop":
            ok, msg = agent.emergency_stop(cmd.params.get("reason", f"requested by {cmd.requested_by}"))
            AgentControl.objects.filter(pk=1).update(kill_switch=True, state=agent.state.value)
            return ok, msg
        if cmd.command == "reset_emergency":
            ok, msg = agent.reset_emergency(str(cmd.requested_by))
            AgentControl.objects.filter(pk=1).update(kill_switch=False, state=agent.state.value)
            return ok, msg
        if cmd.command == "close_agent_positions":
            return agent.close_agent_positions(confirmed=bool(cmd.params.get("confirmed")))
        if cmd.command == "set_mode":
            mode = AgentMode(cmd.params["mode"])
            if mode is AgentMode.LIVE:
                if not self._strategy_validated():
                    return False, "LIVE refused: strategy has not PASSED out-of-sample validation"
                ok, msg = agent.set_mode(mode, live_confirmed=bool(cmd.params.get("live_confirmed")))
                if ok:
                    AgentControl.objects.filter(pk=1).update(live_confirmed_by=cmd.requested_by, live_confirmed_at=dj_tz.now(), mode=mode.value)
                return ok, msg
            ok, msg = agent.set_mode(mode)
            if ok:
                AgentControl.objects.filter(pk=1).update(mode=mode.value, live_confirmed_at=None, live_confirmed_by=None)
            return ok, msg
        return False, f"unknown command {cmd.command}"

    # ----------------------------------------------------------------- loop
    def step(self) -> None:
        if not self._restored:
            self.restore()
        self.process_commands()
        self.reload_if_changed()
        if self.advance_mock:
            self.gateway.advance(1)
        self.agent.run_cycle()

    def acquire_lock(self) -> None:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
            if not cur.fetchone()[0]:
                raise RunnerLockError("another agent runner holds the lock; refusing to start a second one")

    def release_lock(self) -> None:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])

    def run(self, max_cycles: int | None = None) -> None:
        self.acquire_lock()
        cycles = 0
        try:
            while max_cycles is None or cycles < max_cycles:
                started = time.monotonic()
                try:
                    self.step()
                except Exception as exc:  # noqa: BLE001 - keep the runner alive, report the error
                    log.exception("runner step failed")
                    self.journal.log_event("ERROR", "agent", f"Runner step failed: {exc!r}")
                cycles += 1
                elapsed = time.monotonic() - started
                time.sleep(max(0.5, self.agent.cfg.loop_interval_s - elapsed))
        finally:
            self.release_lock()
