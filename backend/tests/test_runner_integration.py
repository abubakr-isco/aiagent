"""Runner + Django journal + mock MT5: the full loop with real PostgreSQL persistence."""

import psycopg
import pytest
from django.db import connection

from apps.audit.logging import redact
from apps.audit.models import SystemEvent
from apps.execution.journal import DjangoJournal
from apps.execution.models import AgentCommand, AgentControl, Execution
from apps.execution.runner import ADVISORY_LOCK_KEY, AgentRunner
from apps.risk.models import DailyRiskState
from apps.strategies.models import Prediction, Signal
from trading_engine.agent import AgentState
from trading_engine.journal import DayState, ExecStatus, ExecutionRecord
from trading_engine.models import AccountKind, AgentMode

pytestmark = pytest.mark.django_db(transaction=True)


def command(name, **params):
    return AgentCommand.objects.create(command=name, params=params)


def test_restart_starts_paused_and_analysis_runs(runner):
    runner.step()
    control = AgentControl.load()
    assert control.state == "PAUSED" and control.mode == "ANALYSIS"
    assert control.connected and control.reconciled and control.gateway == "mock"
    sig = Signal.objects.get()
    assert sig.status == "ANALYSIS_ONLY"  # default mode never trades
    assert Execution.objects.count() == 0


def test_demo_trading_flow_persists_everything(runner, client_for):
    runner.step()
    command("set_mode", mode="DEMO")
    command("start")
    runner.step()
    runner.step()
    control = AgentControl.load()
    assert control.mode == "DEMO" and control.state == "RUNNING", control.blocked_reasons
    ex = Execution.objects.get()
    assert ex.status == "FILLED" and ex.account_kind == "DEMO" and ex.position_ticket
    assert ex.risk_snapshot["config"]["risk_per_trade_pct"] == 0.5
    assert Signal.objects.get(signal_id=ex.signal_id).status == "EXECUTED"
    assert Prediction.objects.filter(signal__signal_id=ex.signal_id).exists()
    assert len(control.positions) == 1 and control.positions[0]["agent"]
    assert [c.status for c in AgentCommand.objects.order_by("created_at")] == ["DONE", "DONE"]
    # Repeated cycles on the same bar do not duplicate the order.
    runner.step()
    assert Execution.objects.count() == 1
    assert len(runner.gateway.positions()) == 1
    status = client_for("viewer").get("/api/status/").data
    assert status["runner_alive"] and status["is_mock"] and status["mode"] == "DEMO"
    assert status["recent_trades"][0]["signal_id"] == ex.signal_id


def test_broker_side_close_is_booked_with_pnl_and_prediction_resolved(runner):
    runner.step()
    command("set_mode", mode="DEMO")
    command("start")
    runner.step()
    ex = Execution.objects.get()
    pos = runner.gateway.state.positions[ex.position_ticket]
    pos.sl = runner.gateway.tick("EURUSD").bid + 1  # make SL hit on the next bar
    runner.gateway.advance(1)
    runner.step()
    ex.refresh_from_db()
    assert ex.status == "CLOSED" and ex.realized_pnl is not None and ex.closed_at and ex.close_reason == "stop loss"
    pred = Prediction.objects.get(signal__signal_id=ex.signal_id)
    assert pred.outcome == (1 if ex.realized_pnl > 0 else 0)


def test_emergency_stop_survives_restart(runner):
    runner.step()
    command("emergency_stop", reason="test")
    runner.step()
    assert AgentControl.load().kill_switch
    fresh = AgentRunner(gateway=runner.gateway, sleep=lambda s: None, advance_mock=False)
    fresh.step()
    assert fresh.agent.kill_switch and fresh.agent.state is AgentState.EMERGENCY_STOP
    command("start")
    fresh.step()
    assert AgentCommand.objects.filter(command="start").latest("created_at").status == "FAILED"


def test_demo_mode_refused_for_real_account(runner):
    runner.gateway.state.kind = AccountKind.REAL
    command("set_mode", mode="DEMO")
    runner.step()
    cmd = AgentCommand.objects.get(command="set_mode")
    assert cmd.status == "FAILED" and "demo" in cmd.result.lower()
    assert runner.agent.mode is AgentMode.ANALYSIS


def test_live_command_refused_without_validation(runner, settings):
    settings.ALLOW_LIVE_TRADING = True
    runner.gateway.state.kind = AccountKind.REAL
    command("set_mode", mode="LIVE", live_confirmed=True)
    runner.step()
    cmd = AgentCommand.objects.get(command="set_mode")
    assert cmd.status == "FAILED" and "validation" in cmd.result
    assert runner.agent.mode is AgentMode.ANALYSIS


def test_risk_settings_change_is_hot_reloaded(runner):
    from apps.risk.models import RiskSettings

    rs = RiskSettings.load()
    rs.risk_per_trade_pct = 0.25
    rs.save()
    runner.step()
    assert runner.agent.risk.cfg.risk_per_trade_pct == 0.25


def test_daily_state_persists_across_journal_instances(db):
    from datetime import date

    j = DjangoJournal()
    j.save_day_state(DayState(day=date(2026, 1, 5), start_equity=10_000, locked=True, locked_reason="limit"))
    st = DjangoJournal().load_day_state(date(2026, 1, 5))
    assert st.locked and st.start_equity == 10_000
    assert DailyRiskState.objects.count() == 1


def test_journal_roundtrip_and_redaction(db, monkeypatch):
    monkeypatch.setenv("MT5_PASSWORD", "hunter22secret")
    j = DjangoJournal()
    rec = ExecutionRecord(signal_id="s1", client_key="AIA:s1", symbol="EURUSD", side="BUY", requested_volume=0.1, requested_price=1.1,
                          sl=1.09, tp=1.12, status=ExecStatus.PENDING_SUBMIT, message="login failed password=hunter22secret")
    j.save_execution(rec)
    back = j.get_execution("s1")
    assert back.status is ExecStatus.PENDING_SUBMIT and "hunter22secret" not in back.message
    assert [r.signal_id for r in j.unresolved_executions()] == ["s1"]
    j.log_event("ERROR", "connection", "auth with token=abc123def failed for hunter22secret")
    msg = SystemEvent.objects.get().message
    assert "abc123def" not in msg and "hunter22secret" not in msg


def test_redact_patterns():
    assert "abc.def.ghi" not in redact("Authorization: Bearer abc.def.ghi")
    assert "s3cr3t" not in redact('{"password": "s3cr3t"}')


def test_single_runner_lock(runner):
    runner.acquire_lock()
    try:
        params = connection.get_connection_params()
        params.pop("cursor_factory", None)
        params.pop("context", None)
        with psycopg.connect(**{k: v for k, v in params.items() if k in ("dbname", "user", "password", "host", "port")}, autocommit=True) as other:
            assert other.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY]).fetchone()[0] is False
    finally:
        runner.release_lock()
