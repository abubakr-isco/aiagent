"""Integration: engine -> RemoteGateway -> HTTP bridge -> (mock) terminal."""

import pytest
from fastapi.testclient import TestClient

from bridge.mt5_bridge import create_app
from trading_engine.config import ExecutionConfig, RiskConfig
from trading_engine.constants import Timeframe
from trading_engine.gateway import GatewayError
from trading_engine.journal import ExecStatus, InMemoryJournal
from trading_engine.mock_mt5 import MockMT5Gateway
from trading_engine.models import AccountKind, AgentMode
from trading_engine.order_executor import OrderExecutor
from trading_engine.remote_gateway import RemoteGateway
from trading_engine.risk_manager import QuoteMonitor, RiskContext, RiskManager

from .conftest import FakeClock, make_proposal

TOKEN = "t" * 40


def remote_for(app, token=TOKEN) -> RemoteGateway:
    gw = RemoteGateway("http://bridge", token)
    gw._client = TestClient(app, base_url="http://bridge", headers={"Authorization": f"Bearer {token}"})
    return gw


@pytest.fixture
def mock():
    return MockMT5Gateway(seed=4)


def test_bridge_requires_long_token(mock):
    with pytest.raises(RuntimeError):
        create_app(mock, token="short")


def test_bridge_rejects_bad_token(mock):
    gw = remote_for(create_app(mock, token=TOKEN), token="x" * 40)
    with pytest.raises(GatewayError, match="authentication"):
        gw.account_info()


def test_bridge_ip_allow_list(mock):
    gw = remote_for(create_app(mock, token=TOKEN, allowed_ips={"10.0.0.5"}))
    with pytest.raises(GatewayError):
        gw.account_info()


def test_roundtrip_types(mock):
    gw = remote_for(create_app(mock, token=TOKEN))
    acct = gw.account_info()
    assert acct.kind is AccountKind.DEMO and acct.equity == mock.account_info().equity
    assert gw.symbol_info("EURUSD") == mock.symbol_info("EURUSD")
    assert gw.symbol_info("NOPE") is None
    df = gw.rates("EURUSD", Timeframe.H1, 50)
    ref = mock.rates("EURUSD", Timeframe.H1, 50)
    assert len(df) == 50 and df["close"].tolist() == ref["close"].tolist()
    assert str(df["time"].dtype).startswith("datetime64")
    assert gw.tick("EURUSD").bid == mock.tick("EURUSD").bid


def test_connection_errors_propagate_as_types(mock):
    from trading_engine.gateway import ConnectionLost
    gw = remote_for(create_app(mock, token=TOKEN))
    mock.set_connected(False)
    with pytest.raises(ConnectionLost):
        gw.positions()


def test_full_execution_through_bridge(mock):
    gw = remote_for(create_app(mock, token=TOKEN))
    journal = InMemoryJournal()
    clock = FakeClock()
    risk = RiskManager(RiskConfig(), journal, gw, QuoteMonitor(clock=clock), clock=clock)
    ex = OrderExecutor(gw, journal, risk, ExecutionConfig(verify_attempts=2, verify_interval_s=0), sleep=lambda s: None)
    p = make_proposal(mock)
    tick = gw.tick("EURUSD")
    risk.quotes.observe(tick)
    ctx = RiskContext(mode=AgentMode.DEMO, account=gw.account_info(), symbol=gw.symbol_info("EURUSD"), tick=tick, positions=gw.positions(),
                      connected=True, reconciled=True, kill_switch=False, quote_fresh=True)
    d = risk.evaluate(p, ctx)
    assert d.approved, d.violations
    report = ex.execute(d.approval)
    assert report.status is ExecStatus.FILLED
    assert len(mock.positions()) == 1
    assert gw.positions()[0].ticket == mock.positions()[0].ticket


def test_bridge_is_demo_only_by_default(mock):
    mock.state.kind = AccountKind.REAL
    gw = remote_for(create_app(mock, token=TOKEN))
    from trading_engine.models import OrderRequest
    from trading_engine.constants import OrderType, TradeAction
    req = OrderRequest(action=TradeAction.DEAL, symbol="EURUSD", volume=0.01, type=OrderType.BUY, price=mock.tick("EURUSD").ask)
    with pytest.raises(GatewayError, match="DEMO-only"):
        gw.order_send(req)
    assert mock.positions() == []


def test_unknown_method_not_exposed(mock):
    client = TestClient(create_app(mock, token=TOKEN), headers={"Authorization": f"Bearer {TOKEN}"})
    assert client.post("/rpc/_execute", json={}).status_code == 404
    assert client.post("/rpc/open_manual_position", json={}).status_code == 404
