"""Broker access abstraction.

Three implementations exist:

* :class:`trading_engine.mt5_connector.MT5Connector` - the real terminal via the
  ``MetaTrader5`` package (Windows only).
* :class:`trading_engine.remote_gateway.RemoteGateway` - HTTP client for the
  Windows bridge (``bridge/mt5_bridge.py``) when the engine runs elsewhere.
* :class:`trading_engine.mock_mt5.MockMT5Gateway` - deterministic simulator used
  by tests and for development without a terminal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from .constants import Timeframe
from .models import (
    AccountInfo,
    CheckResult,
    Deal,
    OrderRequest,
    OrderResult,
    PendingOrder,
    Position,
    Side,
    SymbolInfo,
    TerminalStatus,
    Tick,
)

RATES_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


class GatewayError(Exception):
    """Any failure talking to the terminal."""


class ConnectionLost(GatewayError):
    """Terminal or trade server is not reachable."""


class GatewayTimeout(GatewayError):
    """Call did not complete in time. The outcome of a trade request is UNKNOWN."""


@runtime_checkable
class MT5Gateway(Protocol):
    name: str

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def terminal_status(self) -> TerminalStatus: ...

    def account_info(self) -> AccountInfo: ...

    def symbols(self, group: str | None = None) -> list[str]: ...

    def symbol_info(self, symbol: str) -> SymbolInfo | None: ...

    def symbol_select(self, symbol: str) -> bool: ...

    def tick(self, symbol: str) -> Tick | None: ...

    def rates(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        """Return the last ``count`` *closed* bars, oldest first, columns RATES_COLUMNS.

        ``time`` is a tz-aware UTC-labelled timestamp of the bar open in broker
        server time (MT5 convention). The currently forming bar is excluded.
        """
        ...

    def rates_range(self, symbol: str, timeframe: Timeframe, date_from: datetime, date_to: datetime) -> pd.DataFrame: ...

    def positions(self, symbol: str | None = None) -> list[Position]: ...

    def orders(self, symbol: str | None = None) -> list[PendingOrder]: ...

    def history_deals(self, date_from: datetime, date_to: datetime, position: int | None = None) -> list[Deal]: ...

    def calc_profit(self, side: Side, symbol: str, volume: float, price_open: float, price_close: float) -> float | None: ...

    def calc_margin(self, side: Side, symbol: str, volume: float, price: float) -> float | None: ...

    def order_check(self, request: OrderRequest) -> CheckResult: ...

    def order_send(self, request: OrderRequest) -> OrderResult | None: ...


def empty_rates() -> pd.DataFrame:
    return pd.DataFrame(columns=RATES_COLUMNS)
