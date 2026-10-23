from dataclasses import replace
from datetime import date

import pytest

from trading_engine.config import RiskConfig
from trading_engine.journal import DayState, ExecStatus, ExecutionRecord
from trading_engine.models import AccountKind, AgentMode, Side
from trading_engine.risk_manager import QuoteMonitor, RiskManager, floor_to_step

from .conftest import make_context, make_proposal


def names_failed(decision):
    return {c.name for c in decision.checks if not c.passed}


def test_floor_to_step_never_rounds_up():
    assert floor_to_step(0.129999, 0.01) == 0.12
    assert floor_to_step(0.13, 0.01) == 0.13
    assert floor_to_step(1.99, 0.1) == 1.9
    assert floor_to_step(7.9, 1) == 7


def test_position_size_from_stop_distance_eurusd(mock, risk):
    # 10_000 equity * 0.5% = 50 USD; 200 points SL on EURUSD = 200 USD per lot -> 0.25 lots
    proposal = make_proposal(mock, sl_points=200)
    d = risk.evaluate(proposal, make_context(mock, risk))
    assert d.approved, d.violations
    assert d.volume == pytest.approx(0.25)
    assert d.risk_money == pytest.approx(50.0, abs=0.5)
    assert d.risk_pct <= 0.5 + 1e-9
    assert d.approval is not None and d.approval.is_authentic()


def test_position_size_xauusd_and_usdjpy(mock, risk):
    # XAUUSD: contract 100, 300 points (=3.00 USD) -> 300 USD per lot -> 0.16 lots (floored)
    d = risk.evaluate(make_proposal(mock, symbol="XAUUSD", sl_points=300), make_context(mock, risk, symbol="XAUUSD"))
    assert d.approved, d.violations
    assert d.volume == pytest.approx(0.16)
    assert d.risk_money <= 50.0
    # USDJPY: profit in JPY converted to USD via price
    d = risk.evaluate(make_proposal(mock, symbol="USDJPY", sl_points=300), make_context(mock, risk, symbol="USDJPY"))
    assert d.approved, d.violations
    assert d.risk_money <= 50.0 and d.risk_money > 45.0


def test_volume_below_minimum_is_rejected_not_rounded_up(mock, journal, clock):
    small = RiskManager(RiskConfig(risk_per_trade_pct=0.01), journal, mock, QuoteMonitor(clock=clock), clock=clock)
    d = small.evaluate(make_proposal(mock, sl_points=2000), make_context(mock, small))
    assert not d.approved
    assert "volume_min" in names_failed(d)


def test_trade_without_stop_loss_is_rejected(mock, risk):
    p = replace(make_proposal(mock), stop_loss=0.0)
    d = risk.evaluate(p, make_context(mock, risk))
    assert not d.approved and "stop_loss_present" in names_failed(d)


def test_stop_on_wrong_side_rejected(mock, risk):
    p = make_proposal(mock, side=Side.BUY)
    p = replace(p, stop_loss=p.entry + 0.002)
    d = risk.evaluate(p, make_context(mock, risk))
    assert "stop_side" in names_failed(d)


def test_min_reward_to_risk(mock, risk):
    d = risk.evaluate(make_proposal(mock, rr=1.2), make_context(mock, risk))
    assert "risk_reward" in names_failed(d)


def test_max_open_positions(mock, risk):
    for sym in ("GBPUSD", "XAUUSD", "USDJPY"):
        spec = mock.specs[sym]
        bid = mock.tick(sym).bid
        mock.open_manual_position(sym, Side.BUY, 0.01, sl=round(bid - 500 * spec.point, spec.digits))
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "max_positions" in names_failed(d)


def test_one_position_per_symbol_blocks_grid(mock, risk):
    bid = mock.tick("EURUSD").bid
    mock.open_manual_position("EURUSD", Side.BUY, 0.01, sl=round(bid - 0.003, 5))
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "one_position_per_symbol" in names_failed(d)


def test_total_open_risk_limit(mock, risk):
    # Two existing positions each risking ~0.7% -> a new 0.5% trade exceeds 1.5%
    for sym in ("GBPUSD", "USDJPY"):
        spec = mock.specs[sym]
        bid = mock.tick(sym).bid
        mock.open_manual_position(sym, Side.BUY, 0.35, sl=round(bid - 200 * spec.point, spec.digits))
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "total_open_risk" in names_failed(d)


def test_foreign_position_without_sl_blocks_new_trades(mock, risk):
    mock.open_manual_position("GBPUSD", Side.BUY, 0.01)
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "open_risk_defined" in names_failed(d)


@pytest.mark.parametrize(
    "override,failed",
    [
        ({"kill_switch": True}, "kill_switch"),
        ({"connected": False}, "connection"),
        ({"reconciled": False}, "reconciled"),
        ({"quote_fresh": False}, "quote_fresh"),
        ({"mode": AgentMode.ANALYSIS}, "mode"),
    ],
)
def test_global_blocks(mock, risk, override, failed):
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk, **override))
    assert not d.approved and failed in names_failed(d)


def test_demo_mode_requires_demo_account(mock, risk):
    mock.state.kind = AccountKind.REAL
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "mode" in names_failed(d)


def test_live_mode_requires_real_account_and_enablement(mock, risk):
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk, mode=AgentMode.LIVE, live_enabled=True))
    assert "mode" in names_failed(d)  # account is DEMO
    mock.state.kind = AccountKind.REAL
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk, mode=AgentMode.LIVE, live_enabled=False))
    assert "mode" in names_failed(d)
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk, mode=AgentMode.LIVE, live_enabled=True))
    assert d.approved, d.violations


def test_invalid_quotes_rejected(mock, risk):
    ctx = make_context(mock, risk)
    bad = replace(ctx.tick, bid=1.2, ask=1.1)
    d = risk.evaluate(make_proposal(mock), replace(ctx, tick=bad))
    assert "quote_valid" in names_failed(d)
    zero = replace(ctx.tick, bid=0.0, ask=0.0)
    d = risk.evaluate(make_proposal(mock), replace(ctx, tick=zero))
    assert "quote_valid" in names_failed(d)


def test_stale_quote_detection(clock, mock):
    q = QuoteMonitor(clock=clock)
    t = mock.tick("EURUSD")
    q.observe(t)
    assert q.is_fresh("EURUSD", 30)
    clock.advance(31)
    q.observe(t)  # same tick again: no change
    assert not q.is_fresh("EURUSD", 30)
    q.observe(mock.tick("EURUSD"))  # new tick
    assert q.is_fresh("EURUSD", 30)
    assert not q.is_fresh("UNKNOWN", 30)


def test_spread_anomaly(mock, risk):
    for _ in range(30):
        risk.quotes.observe(mock.tick("EURUSD"))
    mock.spread_multiplier["EURUSD"] = 10
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "spread_anomaly" in names_failed(d)


def test_daily_loss_limit_locks_until_next_day(mock, journal, risk):
    acct = mock.account_info()
    today = risk.daily.trading_day()
    journal.save_day_state(DayState(day=today, start_equity=acct.equity + 250))  # already -2.4%
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "daily_loss_lock" in names_failed(d)
    st = journal.load_day_state(today)
    assert st.locked
    # Even if equity recovers the lock stays for the rest of the day.
    journal.save_day_state(replace(st, start_equity=acct.equity))
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "daily_loss_lock" in names_failed(d)
    # A new trading day starts unlocked.
    assert journal.load_day_state(date(2000, 1, 1)) is None


def test_daily_loss_includes_floating_loss(mock, journal, risk):
    # Daily loss uses equity, so an open losing position counts.
    acct = mock.account_info()
    today = risk.daily.trading_day()
    journal.save_day_state(DayState(day=today, start_equity=acct.equity))
    spec = mock.specs["GBPUSD"]
    bid = mock.tick("GBPUSD").bid
    mock.open_manual_position("GBPUSD", Side.BUY, 1.0, sl=round(bid - 0.02, 5))
    mock.state.balance -= 250  # simulate realised loss + commission today
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert d.day_loss_pct > 2.0
    assert "daily_loss_lock" in names_failed(d)


def test_daily_worst_case_blocks_trade_that_could_breach_limit(mock, journal, risk):
    acct = mock.account_info()
    today = risk.daily.trading_day()
    journal.save_day_state(DayState(day=today, start_equity=round(acct.equity / (1 - 0.017), 2)))  # down 1.7%
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "daily_worst_case" in names_failed(d)
    assert "daily_loss_lock" not in names_failed(d)


def test_restart_does_not_reset_day_start(mock, journal, risk):
    acct = mock.account_info()
    st = risk.daily.state(acct, realized_today=-150.0)
    assert st.start_equity == pytest.approx(acct.balance + 150.0)


def test_risk_calc_disagreement_rejects(mock, risk):
    spec = mock.specs["EURUSD"]
    mock.specs["EURUSD"] = replace(spec, trade_tick_value=2.0, trade_tick_value_loss=2.0)
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "risk_computable" in names_failed(d)


def test_risk_not_computable_when_calc_fails(mock, risk, monkeypatch):
    monkeypatch.setattr(mock, "calc_profit", lambda *a, **k: None)
    spec = mock.specs["EURUSD"]
    mock.specs["EURUSD"] = replace(spec, trade_tick_value=0.0, trade_tick_value_loss=0.0)
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    assert "risk_computable" in names_failed(d)


def test_no_risk_increase_after_loss(mock, journal, clock):
    from datetime import datetime, timezone
    journal.save_execution(ExecutionRecord(
        signal_id="old", client_key="AIA:old", symbol="EURUSD", side="BUY", requested_volume=0.25, requested_price=1.1,
        sl=1.09, tp=1.12, status=ExecStatus.CLOSED, realized_pnl=-50.0, closed_at=datetime.now(timezone.utc),
        risk_snapshot={"risk_pct": 0.5, "config": {"risk_per_trade_pct": 0.5}},
    ))
    bigger = RiskManager(RiskConfig(risk_per_trade_pct=1.0), journal, mock, QuoteMonitor(clock=clock), clock=clock)
    d = bigger.evaluate(make_proposal(mock), make_context(mock, bigger))
    assert "no_increase_after_loss" in names_failed(d)
    same = RiskManager(RiskConfig(), journal, mock, QuoteMonitor(clock=clock), clock=clock)
    assert same.evaluate(make_proposal(mock), make_context(mock, same)).approved


def test_margin_limits(mock, journal, clock):
    mock.state.leverage = 1
    rm = RiskManager(RiskConfig(), journal, mock, QuoteMonitor(clock=clock), clock=clock)
    d = rm.evaluate(make_proposal(mock), make_context(mock, rm))
    assert {"free_margin", "margin_usage"} & names_failed(d)


def test_invalid_config_rejected(journal, mock):
    with pytest.raises(ValueError):
        RiskManager(RiskConfig(max_positions_per_symbol=3), journal, mock)
    with pytest.raises(ValueError):
        RiskManager(RiskConfig(risk_per_trade_pct=5), journal, mock)


def test_approval_expires_and_is_tamper_proof(mock, risk, clock):
    d = risk.evaluate(make_proposal(mock), make_context(mock, risk))
    ok, _ = risk.approval_valid(d.approval)
    assert ok
    tampered = replace(d.approval, volume=d.approval.volume * 10)
    assert not risk.approval_valid(tampered)[0]
    clock.advance(risk.cfg.approval_ttl_s + 1)
    assert not risk.approval_valid(d.approval)[0]
