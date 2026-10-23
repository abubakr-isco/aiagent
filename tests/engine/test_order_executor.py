from dataclasses import replace

import pytest

from trading_engine.config import ExecutionConfig
from trading_engine.constants import OrderType, TradeAction
from trading_engine.journal import ExecStatus
from trading_engine.models import OrderRequest, Side
from trading_engine.order_executor import OrderExecutor

from .conftest import approve


def deals_in(mock):
    return [d for d in mock.state.deals if d.entry == 0]


def test_successful_execution_is_verified_against_positions(mock, risk, executor, journal):
    d = approve(mock, risk)
    report = executor.execute(d.approval, "DEMO")
    assert report.ok, report.message
    rec = journal.get_execution(d.approval.proposal.signal_id)
    assert rec.status is ExecStatus.FILLED
    pos = mock.positions("EURUSD")[0]
    assert rec.position_ticket == pos.ticket
    assert pos.sl == pytest.approx(d.approval.proposal.stop_loss)
    assert pos.magic == executor.cfg.magic
    assert pos.comment == executor.client_key(rec.signal_id)
    assert len(mock.sent_requests) == 1


def test_same_signal_is_never_sent_twice(mock, risk, executor):
    d = approve(mock, risk)
    assert executor.execute(d.approval).ok
    d2 = risk.evaluate(d.approval.proposal, risk_ctx(mock, risk, reconciled=True))
    # Second approval is rejected by the risk manager (one position per symbol) ...
    assert not d2.approved
    # ... and even a still-valid first approval cannot be replayed.
    again = executor.execute(d.approval)
    assert again.duplicate
    assert len(mock.sent_requests) == 1


def risk_ctx(mock, risk, **kw):
    from .conftest import make_context
    return make_context(mock, risk, **kw)


def test_signal_found_on_broker_but_not_in_journal_is_not_resent(mock, risk, executor, journal):
    d = approve(mock, risk)
    assert executor.execute(d.approval).ok
    journal.executions.clear()  # simulate lost local state (e.g. crash before commit)
    again = executor.execute(d.approval)
    assert again.duplicate
    assert len(mock.sent_requests) == 1
    assert journal.get_execution(d.approval.proposal.signal_id).status is ExecStatus.FILLED


def test_rejects_unsigned_or_tampered_or_expired_approval(mock, risk, executor, clock):
    d = approve(mock, risk)
    forged = replace(d.approval, volume=5.0)
    assert executor.execute(forged).status is ExecStatus.REJECTED
    clock.advance(60)
    assert executor.execute(d.approval).status is ExecStatus.REJECTED
    assert mock.sent_requests == []


def test_timeout_after_execution_is_reconciled_not_resent(mock, risk, executor, journal):
    d = approve(mock, risk)
    mock.inject("timeout_executed")
    report = executor.execute(d.approval)
    assert report.ok, report.message
    assert len(mock.sent_requests) == 1
    assert len(mock.positions("EURUSD")) == 1


def test_timeout_before_execution_retries_once_after_verification(mock, risk, executor):
    d = approve(mock, risk)
    mock.inject("timeout_not_executed")
    report = executor.execute(d.approval)
    assert report.ok
    assert len(mock.sent_requests) == 2
    assert len(mock.positions("EURUSD")) == 1


def test_timeout_policy_never_does_not_retry(mock, risk, journal):
    ex = OrderExecutor(mock, journal, risk, ExecutionConfig(timeout_retry_policy="never", verify_attempts=2, verify_interval_s=0), sleep=lambda s: None)
    d = approve(mock, risk)
    mock.inject("timeout_not_executed")
    report = ex.execute(d.approval)
    assert report.status is ExecStatus.FAILED
    assert len(mock.sent_requests) == 1
    assert mock.positions() == []
    # FAILED-after-timeout does not block a fresh attempt for a new approval, but is counted.
    assert journal.get_execution(d.approval.proposal.signal_id).attempts == 1


def test_connection_lost_during_send_leaves_unknown_and_blocks(mock, risk, executor, journal):
    d = approve(mock, risk)

    def drop(_req):
        mock.set_connected(False)

    mock.on_send = drop
    mock.inject("timeout_executed")
    report = executor.execute(d.approval)
    assert report.status is ExecStatus.UNKNOWN
    rec = journal.get_execution(d.approval.proposal.signal_id)
    assert rec.status is ExecStatus.UNKNOWN
    assert rec.status.blocks_resubmit
    mock.on_send = None
    mock.set_connected(True)
    # Replay is blocked while the outcome is unknown.
    assert executor.execute(d.approval).duplicate
    assert len(mock.sent_requests) == 1


def test_none_result_is_treated_as_unknown(mock, risk, executor):
    d = approve(mock, risk)
    mock.inject("none_result")
    report = executor.execute(d.approval)
    # Verified absent -> one retry -> filled
    assert report.ok
    assert len(mock.sent_requests) == 2


def test_requote_retries_with_fresh_price(mock, risk, executor):
    d = approve(mock, risk)
    mock.inject("requote")
    assert executor.execute(d.approval).ok
    assert len(mock.sent_requests) == 2


def test_repeated_requotes_give_up(mock, risk, executor):
    d = approve(mock, risk)
    mock.inject("requote", count=10)
    report = executor.execute(d.approval)
    assert report.status is ExecStatus.REJECTED
    assert len(mock.sent_requests) == executor.cfg.max_price_retries + 1


@pytest.mark.parametrize("fault", ["reject", "no_money", "invalid_volume", "too_many_requests"])
def test_broker_rejections_are_not_retried(mock, risk, executor, journal, fault):
    d = approve(mock, risk)
    mock.inject(fault)
    report = executor.execute(d.approval)
    assert report.status is ExecStatus.REJECTED
    assert len(mock.sent_requests) == 1
    assert mock.positions() == []
    assert journal.get_execution(d.approval.proposal.signal_id).retcode is not None


def test_order_check_failure_prevents_send(mock, risk, executor):
    d = approve(mock, risk)
    mock.inject("check_fail")
    assert executor.execute(d.approval).status is ExecStatus.REJECTED
    assert mock.sent_requests == []


def test_price_drift_beyond_tolerance_requires_reevaluation(mock, risk, executor):
    d = approve(mock, risk)
    mock.advance(30)  # price moves away
    report = executor.execute(d.approval)
    if abs(mock.tick("EURUSD").ask - d.approval.entry_price) / 0.00001 > executor.cfg.max_slippage_points:
        assert report.status is ExecStatus.REJECTED
        assert "re-evaluate" in report.message
    else:  # pragma: no cover - depends on synthetic path
        assert report.ok


def test_partial_fill_recorded_with_actual_volume(mock, risk, executor, journal):
    d = approve(mock, risk)
    mock.inject("partial")
    report = executor.execute(d.approval)
    assert report.status is ExecStatus.PARTIALLY_FILLED
    rec = journal.get_execution(d.approval.proposal.signal_id)
    assert rec.filled_volume == pytest.approx(mock.positions()[0].volume)
    assert rec.filled_volume < d.approval.volume


def test_missing_stop_loss_after_fill_is_repaired(mock, risk, executor):
    d = approve(mock, risk)
    mock.inject("drop_sl")
    report = executor.execute(d.approval)
    assert report.ok
    assert mock.positions()[0].sl == pytest.approx(d.approval.proposal.stop_loss)


def test_missing_stop_loss_that_cannot_be_repaired_closes_position(mock, risk, executor, monkeypatch):
    d = approve(mock, risk)
    mock.inject("drop_sl")
    original = mock.order_send

    def no_sltp(req):
        if req.action == TradeAction.SLTP:
            from trading_engine.models import OrderResult
            return OrderResult(retcode=10006, comment="reject")
        return original(req)

    monkeypatch.setattr(mock, "order_send", no_sltp)
    report = executor.execute(d.approval)
    assert report.status is ExecStatus.CLOSED
    assert mock.positions() == []


def test_close_and_partial_close(mock, risk, executor):
    d = approve(mock, risk, sl_points=100)  # 0.5 lots
    assert executor.execute(d.approval).ok
    pos = mock.positions()[0]
    res = executor.close_position(pos, volume=0.2, reason="test partial")
    assert res.ok, res.message
    assert mock.positions()[0].volume == pytest.approx(pos.volume - 0.2)
    res = executor.close_position(mock.positions()[0], reason="test full")
    assert res.ok and mock.positions() == []


def test_invalid_partial_close_volumes(mock, risk, executor):
    mock.specs["EURUSD"] = replace(mock.specs["EURUSD"], volume_min=0.1)
    d = approve(mock, risk, sl_points=100)  # 0.5 lots
    executor.execute(d.approval)
    pos = mock.positions()[0]
    assert not executor.close_position(pos, volume=0.05).ok  # below min
    assert not executor.close_position(pos, volume=0.45).ok  # remainder 0.05 below min
    assert not executor.close_position(pos, volume=pos.volume + 1).ok
    assert mock.positions()[0].volume == pytest.approx(0.5)
    # A non step-aligned request is floored to the step (never closes more than asked).
    assert executor.close_position(pos, volume=0.255).ok
    assert mock.positions()[0].volume == pytest.approx(0.25)


def test_close_of_already_closed_position(mock, risk, executor):
    d = approve(mock, risk)
    executor.execute(d.approval)
    pos = mock.positions()[0]
    assert executor.close_position(pos).ok
    sent = len(mock.sent_requests)
    res = executor.close_position(pos)
    assert res.ok and res.message == "already closed"
    assert len(mock.sent_requests) == sent  # nothing sent for a closed position


def test_cancel_only_agent_orders(mock, executor):
    agent = mock.order_send(OrderRequest(action=TradeAction.PENDING, symbol="EURUSD", volume=0.1, type=OrderType.BUY_LIMIT, price=1.0, magic=executor.cfg.magic))
    user = mock.order_send(OrderRequest(action=TradeAction.PENDING, symbol="EURUSD", volume=0.1, type=OrderType.BUY_LIMIT, price=1.0, magic=0))
    orders = {o.ticket: o for o in mock.orders()}
    assert executor.cancel_order(orders[agent.order]).ok
    assert not executor.cancel_order(orders[user.order]).ok
    assert [o.ticket for o in mock.orders()] == [user.order]


def test_modify_refuses_removing_stop(mock, risk, executor):
    d = approve(mock, risk)
    executor.execute(d.approval)
    pos = mock.positions()[0]
    assert not executor.modify_sltp(pos, 0.0, pos.tp).ok
    assert not executor.modify_sltp(pos, pos.price_current + 0.01, pos.tp).ok  # BUY SL above price


def test_concurrent_execution_of_same_signal_sends_once(mock, risk, executor):
    import threading

    d = approve(mock, risk)
    results = []
    threads = [threading.Thread(target=lambda: results.append(executor.execute(d.approval))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.ok and not r.duplicate for r in results) == 1
    assert len(mock.sent_requests) == 1
    assert len(mock.positions()) == 1


def test_sell_execution(mock, risk, executor):
    d = approve(mock, risk, side=Side.SELL)
    assert executor.execute(d.approval).ok
    pos = mock.positions()[0]
    assert pos.side is Side.SELL and pos.sl > pos.price_open
