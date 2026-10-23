from __future__ import annotations

import itertools

import pytest

from trading_engine.config import ExecutionConfig, RiskConfig
from trading_engine.journal import InMemoryJournal
from trading_engine.mock_mt5 import MockMT5Gateway
from trading_engine.models import AccountKind, AgentMode, Side
from trading_engine.order_executor import OrderExecutor
from trading_engine.risk_manager import QuoteMonitor, RiskContext, RiskManager, TradeProposal


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mock() -> MockMT5Gateway:
    gw = MockMT5Gateway(seed=11)
    gw.state.commission_per_lot = 3.5
    return gw


@pytest.fixture
def journal() -> InMemoryJournal:
    return InMemoryJournal()


@pytest.fixture
def risk(mock, journal, clock) -> RiskManager:
    return RiskManager(RiskConfig(), journal, mock, QuoteMonitor(clock=clock), clock=clock)


@pytest.fixture
def executor(mock, journal, risk) -> OrderExecutor:
    return OrderExecutor(mock, journal, risk, ExecutionConfig(verify_attempts=3, verify_interval_s=0), sleep=lambda s: None)


_ids = itertools.count(1)


def make_proposal(mock: MockMT5Gateway, symbol: str = "EURUSD", side: Side = Side.BUY, sl_points: int = 200, rr: float = 2.0, signal_id: str | None = None) -> TradeProposal:
    tick = mock.tick(symbol)
    spec = mock.specs[symbol]
    entry = tick.ask if side is Side.BUY else tick.bid
    dist = sl_points * spec.point
    sl = entry - dist * side.sign
    tp = entry + dist * rr * side.sign
    return TradeProposal(
        signal_id=signal_id or f"sig{next(_ids):06d}", symbol=symbol, side=side, entry=entry,
        stop_loss=round(sl, spec.digits), take_profit=round(tp, spec.digits), strategy_id="trend_pullback", strategy_version="1.0.0",
    )


def make_context(mock: MockMT5Gateway, risk: RiskManager, symbol: str = "EURUSD", **overrides) -> RiskContext:
    tick = mock.tick(symbol)
    risk.quotes.observe(tick)
    ctx = dict(
        mode=AgentMode.DEMO, account=mock.account_info(), symbol=mock.symbol_info(symbol), tick=tick,
        positions=mock.positions(), connected=True, reconciled=True, kill_switch=False, quote_fresh=True,
    )
    ctx.update(overrides)
    return RiskContext(**ctx)


def approve(mock, risk, **kw):
    proposal = make_proposal(mock, **kw)
    decision = risk.evaluate(proposal, make_context(mock, risk, symbol=proposal.symbol))
    assert decision.approved, decision.violations
    return decision


__all__ = ["FakeClock", "make_proposal", "make_context", "approve", "AccountKind"]
