from datetime import datetime, timezone

import pytest

from trading_engine.agent import AgentState, NullSink, SignalStatus, TradingAgent
from trading_engine.config import AgentConfig, ExecutionConfig, StrategyParams
from trading_engine.constants import OrderType, TradeAction
from trading_engine.journal import ExecStatus, ExecutionRecord, InMemoryJournal
from trading_engine.market_analyzer import MarketSnapshot
from trading_engine.mock_mt5 import MockMT5Gateway
from trading_engine.models import AccountKind, AgentMode, Check, OrderRequest, Side, Signal, StrategyEvaluation
from trading_engine.strategy_engine import Strategy

from .conftest import FakeClock, approve


class AlwaysSignal(Strategy):
    """Emits one BUY signal per symbol per bar - exercises the pipeline deterministically."""

    strategy_id = "stub"
    version = "0.0.1"

    def __init__(self, sl_points: int = 200, exit_reason: str | None = None):
        super().__init__(StrategyParams())
        self.sl_points = sl_points
        self._exit = exit_reason

    def evaluate(self, snapshot: MarketSnapshot, *, spread, now_utc):
        point = snapshot.point
        entry = snapshot.ask
        sl = round(entry - self.sl_points * point, 5)
        tp = round(entry + 2 * self.sl_points * point, 5)
        bar_time = snapshot.frame("M15").bar_time if snapshot.frame("M15") else datetime(2024, 1, 1, tzinfo=timezone.utc)
        sig = Signal(self.strategy_id, self.version, snapshot.symbol, Side.BUY, "M15", bar_time, entry, sl, tp, 0.001,
                     checks=[Check("stub", True, "always")], features={"h1_adx": 40, "h1_slope_atr": 1.5, "h1_structure_aligned": 1, "h4_aligned": 1, "m15_rsi_centered": 7, "spread_risk_ratio": 0.0, "m15_atr_rank": 0.5})
        return StrategyEvaluation(snapshot.symbol, self.strategy_id, sig, sig.checks, Side.BUY)

    def exit_reason(self, side, snapshot, bars_held):
        return self._exit


def make_agent(mock=None, strategy=None, symbols=("EURUSD",), allow_live=False, **kw):
    mock = mock or MockMT5Gateway(seed=3)
    journal = InMemoryJournal()
    from trading_engine.ai_decision_engine import AIDecisionEngine, Regime
    ai = AIDecisionEngine(min_quality=0.0, allowed_regimes=(Regime.TREND, Regime.RANGE, Regime.HIGH_VOLATILITY, Regime.UNKNOWN))
    agent = TradingAgent(
        mock, journal, strategy or AlwaysSignal(), ai=ai,
        agent_cfg=AgentConfig(symbols=symbols, bars_per_timeframe=400, allow_live_env=allow_live),
        exec_cfg=ExecutionConfig(verify_attempts=2, verify_interval_s=0), sleep=lambda s: None, monotonic=FakeClock(), **kw,
    )
    return agent, mock, journal


def test_analysis_mode_never_sends_orders():
    agent, mock, _ = make_agent()
    agent.start()
    report = agent.run_cycle()
    assert report.signals == 1
    assert mock.sent_requests == []
    assert agent.sink.signals[-1][2] is SignalStatus.ANALYSIS_ONLY


def test_demo_mode_trades_and_duplicate_signal_is_not_resent():
    agent, mock, journal = make_agent()
    assert agent.set_mode(AgentMode.DEMO)[0]
    agent.start()
    r1 = agent.run_cycle()
    assert r1.executed == 1, agent.sink.signals[-1][5]
    r2 = agent.run_cycle()  # same bar -> same signal id
    assert r2.executed == 0
    assert agent.sink.signals[-1][2] is SignalStatus.DUPLICATE
    assert len([r for r in mock.sent_requests if r.action == TradeAction.DEAL]) == 1


def test_demo_mode_refused_on_real_account():
    mock = MockMT5Gateway(seed=3)
    mock.state.kind = AccountKind.REAL
    agent, _, _ = make_agent(mock)
    ok, msg = agent.set_mode(AgentMode.DEMO)
    assert not ok and "demo" in msg.lower()


def test_account_type_change_after_mode_switch_blocks_trading():
    agent, mock, _ = make_agent()
    agent.set_mode(AgentMode.DEMO)
    agent.start()
    mock.state.kind = AccountKind.REAL  # terminal re-logged into a real account
    agent.run_cycle()
    assert mock.sent_requests == []
    assert any("DEMO mode but account is REAL" in r for r in agent.blocked_reasons())


def test_live_mode_gates():
    agent, mock, _ = make_agent()
    ok, msg = agent.set_mode(AgentMode.LIVE, live_confirmed=True)
    assert not ok and "ALLOW_LIVE_TRADING" in msg
    agent, mock, _ = make_agent(allow_live=True)
    ok, msg = agent.set_mode(AgentMode.LIVE, live_confirmed=False)
    assert not ok and "confirmation" in msg
    ok, msg = agent.set_mode(AgentMode.LIVE, live_confirmed=True)
    assert not ok and "REAL" in msg  # account is demo
    mock.state.kind = AccountKind.UNKNOWN
    ok, _ = agent.set_mode(AgentMode.LIVE, live_confirmed=True)
    assert not ok  # unknown account type -> LIVE forbidden
    mock.state.kind = AccountKind.REAL
    ok, _ = agent.set_mode(AgentMode.LIVE, live_confirmed=True)
    assert ok
    assert agent.state is AgentState.STOPPED  # mode change never auto-starts


def test_mode_change_pauses_running_agent():
    agent, mock, _ = make_agent()
    agent.start()
    agent.set_mode(AgentMode.DEMO)
    assert agent.state is AgentState.PAUSED


def test_paused_agent_does_not_open_trades():
    agent, mock, _ = make_agent()
    agent.set_mode(AgentMode.DEMO)
    agent.pause()
    agent.run_cycle()
    assert mock.sent_requests == []
    assert agent.sink.signals[-1][2] is SignalStatus.BLOCKED


def test_emergency_stop_blocks_and_cancels_only_agent_orders_without_closing_positions():
    agent, mock, _ = make_agent()
    agent.set_mode(AgentMode.DEMO)
    agent.start()
    agent.run_cycle()
    assert len(mock.positions()) == 1
    magic = agent.executor.cfg.magic
    mock.order_send(OrderRequest(action=TradeAction.PENDING, symbol="EURUSD", volume=0.1, type=OrderType.BUY_LIMIT, price=1.0, magic=magic))
    mock.order_send(OrderRequest(action=TradeAction.PENDING, symbol="EURUSD", volume=0.1, type=OrderType.BUY_LIMIT, price=1.0, magic=0))
    ok, msg = agent.emergency_stop("test")
    assert ok and "cancelled 1" in msg
    assert [o.magic for o in mock.orders()] == [0]
    assert len(mock.positions()) == 1  # positions are NOT closed automatically
    assert agent.start()[0] is False
    mock.advance(4)
    agent.run_cycle()
    assert len([r for r in mock.sent_requests if r.action == TradeAction.DEAL]) == 1
    assert not agent.close_agent_positions(confirmed=False)[0]
    assert agent.close_agent_positions(confirmed=True)[0]
    assert mock.positions() == []
    agent.reset_emergency("tester")
    assert agent.state is AgentState.PAUSED and not agent.kill_switch


def test_connection_loss_blocks_and_reconnect_requires_reconciliation():
    agent, mock, journal = make_agent()
    agent.set_mode(AgentMode.DEMO)
    agent.start()
    agent.run_cycle()
    assert agent.reconciled
    mock.set_connected(False)
    mock.fail_connect = 1
    r = agent.run_cycle()
    assert r.skipped == "no connection"
    assert not agent.connected and not agent.reconciled
    assert "no connection" in agent.blocked_reasons()
    agent._next_reconnect = 0
    agent.run_cycle()  # reconnects, reconciles
    assert agent.connected and agent.reconciled
    assert any("Reconnected" in e["message"] for e in journal.events)


def test_daily_loss_limit_blocks_new_trades():
    agent, mock, journal = make_agent()
    agent.set_mode(AgentMode.DEMO)
    agent.start()
    from trading_engine.journal import DayState
    day = agent.risk.daily.trading_day(agent.clock())
    journal.save_day_state(DayState(day=day, start_equity=mock.account_info().equity * 1.03))
    agent.run_cycle()
    assert mock.sent_requests == []
    status, _, note = agent.sink.signals[-1][2], None, agent.sink.signals[-1][5]
    assert status is SignalStatus.RISK_REJECTED and "daily_loss_lock" in note
    assert agent.status().day_locked


def test_exit_rule_closes_agent_position():
    agent, mock, journal = make_agent(strategy=AlwaysSignal(exit_reason="trend flipped"))
    agent.set_mode(AgentMode.DEMO)
    agent.start()
    agent.run_cycle()
    assert len(mock.positions()) == 1
    report = agent.run_cycle()  # manage() closes it; the same-bar signal is a duplicate
    assert mock.positions() == []
    assert any("trend flipped" in a for a in report.actions)
    agent.run_cycle()  # next cycle's reconciliation books the close with broker P&L
    closed = [r for r in journal.executions.values() if r.status is ExecStatus.CLOSED]
    assert closed and closed[0].close_reason == "trend flipped" and closed[0].realized_pnl is not None


def test_reconcile_detects_closed_orphan_and_unresolved(mock, risk, executor, journal):
    from trading_engine.position_manager import PositionManager
    pm = PositionManager(mock, journal, executor)
    d = approve(mock, risk)
    executor.execute(d.approval)
    rec = journal.get_execution(d.approval.proposal.signal_id)
    # Position is closed on the broker side by SL
    mock.positions()
    pos = mock.positions()[0]
    mock.state.positions[pos.ticket].sl = mock.tick("EURUSD").bid + 0.5  # force SL hit on next bar
    mock.advance(1)
    # Orphan: an agent-magic position that the journal doesn't know
    orphan_req = OrderRequest(action=TradeAction.DEAL, symbol="GBPUSD", volume=0.01, type=OrderType.BUY, price=mock.tick("GBPUSD").ask,
                              sl=round(mock.tick("GBPUSD").bid - 0.005, 5), magic=executor.cfg.magic, comment="AIA:orphan1")
    mock.order_send(orphan_req)
    # Unresolved: crashed between journal write and send
    journal.save_execution(ExecutionRecord(signal_id="crashed", client_key="AIA:crashed", symbol="EURUSD", side="BUY",
                                           requested_volume=0.1, requested_price=1.1, sl=1.0, tp=1.2, status=ExecStatus.PENDING_SUBMIT))
    rep = pm.reconcile()
    assert rep.ok, rep.messages
    closed = journal.get_execution(rec.signal_id)
    assert closed.status is ExecStatus.CLOSED and closed.realized_pnl is not None and closed.exit_price
    assert rep.orphans and journal.get_execution(f"orphan-{rep.orphans[0]}") is not None
    assert journal.get_execution("crashed").status is ExecStatus.FAILED


def test_reconcile_flags_duplicates_as_critical(mock, executor, journal):
    from trading_engine.position_manager import PositionManager
    for _ in range(2):
        mock.order_send(OrderRequest(action=TradeAction.DEAL, symbol="EURUSD", volume=0.01, type=OrderType.BUY, price=mock.tick("EURUSD").ask,
                                     sl=round(mock.tick("EURUSD").bid - 0.005, 5), magic=executor.cfg.magic, comment="AIA:dup"))
    rep = PositionManager(mock, journal, executor).reconcile()
    assert rep.critical and not rep.ok


def test_duplicate_positions_trigger_emergency_stop():
    agent, mock, _ = make_agent()
    for _ in range(2):
        mock.order_send(OrderRequest(action=TradeAction.DEAL, symbol="EURUSD", volume=0.01, type=OrderType.BUY, price=mock.tick("EURUSD").ask,
                                     sl=round(mock.tick("EURUSD").bid - 0.005, 5), magic=agent.executor.cfg.magic, comment="AIA:dup"))
    agent.start()
    agent.run_cycle()
    assert agent.kill_switch and agent.state is AgentState.EMERGENCY_STOP


def test_reconcile_restores_missing_sl_on_agent_position(mock, risk, executor, journal):
    from trading_engine.position_manager import PositionManager
    d = approve(mock, risk)
    executor.execute(d.approval)
    pos = mock.positions()[0]
    mock.state.positions[pos.ticket].sl = 0.0
    PositionManager(mock, journal, executor).reconcile()
    assert mock.positions()[0].sl == pytest.approx(d.approval.proposal.stop_loss)


def test_unknown_symbol_reported_not_crashing():
    agent, mock, _ = make_agent(symbols=("EURUSD.m",))
    agent.start()
    r = agent.run_cycle()
    assert any("not offered by broker" in e for e in r.errors)


def test_status_contains_dashboard_fields():
    agent, mock, _ = make_agent()
    agent.set_mode(AgentMode.DEMO)
    agent.start()
    agent.run_cycle()
    st = agent.sink.status
    assert st.connected and st.account.equity > 0
    assert st.positions and st.positions[0]["agent"] is True
    assert st.day_start_equity is not None
