"""MT5Connector against a fake ``MetaTrader5`` module.

This verifies field mapping and error handling only. It is NOT a substitute
for testing against a real terminal (see TESTING.md).
"""

from types import SimpleNamespace

import numpy as np
import pytest

from trading_engine.constants import Timeframe
from trading_engine.gateway import ConnectionLost, GatewayError, GatewayTimeout
from trading_engine.models import AccountKind, OrderRequest, Side
from trading_engine.constants import OrderType, TradeAction
from trading_engine.mt5_connector import MT5Connector, MT5Credentials


class FakeMT5:
    def __init__(self):
        self.error = (1, "Success")
        self.init_ok = True
        self.calls = []
        self.send_result = SimpleNamespace(retcode=10009, comment="done", deal=11, order=22, volume=0.1, price=1.1, bid=1.1, ask=1.1001, request_id=1)

    def initialize(self, **kw):
        self.calls.append(("initialize", kw))
        return self.init_ok

    def shutdown(self):
        pass

    def last_error(self):
        return self.error

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=True, name="MT5", build=4000, ping_last=12000)

    def account_info(self):
        return SimpleNamespace(login=1, server="Broker-Demo", currency="USD", balance=1000.0, equity=990.0, margin=10.0, margin_free=980.0,
                               margin_level=9900.0, leverage=100, trade_mode=0, trade_allowed=True, trade_expert=True, company="B", name="N")

    def copy_rates_from_pos(self, symbol, tf, start, count):
        self.calls.append(("copy_rates_from_pos", symbol, tf, start, count))
        dt = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])
        return np.array([(1704153600, 1.1, 1.2, 1.0, 1.15, 10, 5, 0)], dtype=dt)

    def order_send(self, req):
        self.calls.append(("order_send", req))
        return self.send_result

    def positions_get(self, **kw):
        return None


def test_connect_passes_credentials_but_repr_hides_password():
    fake = FakeMT5()
    creds = MT5Credentials(login=123, password="s3cret", server="Broker-Demo", path=None)
    c = MT5Connector(creds, mt5_module=fake)
    c.connect()
    kw = fake.calls[0][1]
    assert kw["login"] == 123 and kw["password"] == "s3cret"
    assert "s3cret" not in repr(creds)


def test_connect_failure_raises_connection_lost():
    fake = FakeMT5()
    fake.init_ok = False
    fake.error = (-10003, "IPC initialize failed")
    with pytest.raises(ConnectionLost):
        MT5Connector(MT5Credentials(None, None, None, None), mt5_module=fake).connect()


def test_calls_before_connect_are_refused():
    c = MT5Connector(MT5Credentials(None, None, None, None), mt5_module=FakeMT5())
    with pytest.raises(ConnectionLost):
        c.account_info()


def test_account_mapping_and_rates_skip_forming_bar():
    fake = FakeMT5()
    c = MT5Connector(MT5Credentials(None, None, None, None), mt5_module=fake)
    c.connect()
    a = c.account_info()
    assert a.kind is AccountKind.DEMO and a.equity == 990.0
    df = c.rates("EURUSD", Timeframe.H1, 100)
    assert fake.calls[-1] == ("copy_rates_from_pos", "EURUSD", 16385, 1, 100)
    assert str(df["time"].iloc[0]) == "2024-01-02 00:00:00+00:00"


def test_none_results_map_to_typed_errors():
    fake = FakeMT5()
    c = MT5Connector(MT5Credentials(None, None, None, None), mt5_module=fake)
    c.connect()
    fake.error = (-10005, "timeout")
    with pytest.raises(GatewayTimeout):
        c.positions()
    fake.error = (-10004, "no connection")
    with pytest.raises(ConnectionLost):
        c.positions()
    c.connect()
    fake.error = (-2, "invalid params")
    with pytest.raises(GatewayError):
        c.positions()


def test_order_send_none_returns_none_for_reconciliation():
    fake = FakeMT5()
    c = MT5Connector(MT5Credentials(None, None, None, None), mt5_module=fake)
    c.connect()
    req = OrderRequest(action=TradeAction.DEAL, symbol="EURUSD", volume=0.1, type=OrderType.BUY, price=1.1, sl=1.09, tp=1.12, magic=7, comment="AIA:x")
    assert c.order_send(req).retcode == 10009
    sent = fake.calls[-1][1]
    assert sent["action"] == 1 and sent["type"] == 0 and sent["sl"] == 1.09 and sent["magic"] == 7
    fake.send_result = None
    fake.error = (-10005, "timeout")
    assert c.order_send(req) is None


def test_account_kind_mapping():
    assert AccountKind.from_mt5(0) is AccountKind.DEMO
    assert AccountKind.from_mt5(2) is AccountKind.REAL
    assert AccountKind.from_mt5(1) is AccountKind.CONTEST
    assert AccountKind.from_mt5(None) is AccountKind.UNKNOWN
    assert AccountKind.from_mt5(99) is AccountKind.UNKNOWN
