"""HTTP client for the Windows MT5 bridge (``bridge/mt5_bridge.py``).

Use when Django/the agent run on a machine without MT5 (macOS/Linux/Docker).
Transport errors map to the same exception types as the local connector so
the executor's unknown-outcome handling works identically.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from . import codec
from .constants import Timeframe
from .gateway import ConnectionLost, GatewayError, GatewayTimeout
from .models import OrderRequest, Side


class RemoteGateway:
    name = "remote"

    def __init__(self, base_url: str | None = None, token: str | None = None, *, timeout_s: float = 15.0, verify: bool | str = True, transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or os.environ.get("MT5_BRIDGE_URL", "")).rstrip("/")
        token = token or os.environ.get("MT5_BRIDGE_TOKEN", "")
        if not self.base_url or not token:
            raise GatewayError("MT5_BRIDGE_URL and MT5_BRIDGE_TOKEN must be set for the remote gateway")
        ca = os.environ.get("MT5_BRIDGE_CA_FILE")
        self._client = httpx.Client(
            base_url=self.base_url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout_s,
            verify=ca or verify, transport=transport,
        )

    def _rpc(self, method: str, **kwargs: Any) -> Any:
        try:
            resp = self._client.post(f"/rpc/{method}", json=codec.encode(kwargs))
        except httpx.TimeoutException as exc:
            raise GatewayTimeout(f"bridge timeout on {method}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionLost(f"bridge unreachable on {method}: {exc}") from exc
        if resp.status_code == 401:
            raise GatewayError("bridge refused authentication (401)")
        try:
            body = resp.json()
        except ValueError:
            raise GatewayError(f"bridge returned non-JSON response ({resp.status_code})") from None
        if resp.status_code != 200 or not body.get("ok"):
            kind = body.get("error_type", "GatewayError")
            message = body.get("error", resp.text)
            raise {"ConnectionLost": ConnectionLost, "GatewayTimeout": GatewayTimeout}.get(kind, GatewayError)(message)
        return codec.DECODERS[method](body.get("result"))

    def connect(self) -> None:
        self._rpc("connect")

    def disconnect(self) -> None:
        self._rpc("disconnect")

    def terminal_status(self):
        return self._rpc("terminal_status")

    def account_info(self):
        return self._rpc("account_info")

    def symbols(self, group: str | None = None):
        return self._rpc("symbols", group=group)

    def symbol_info(self, symbol: str):
        return self._rpc("symbol_info", symbol=symbol)

    def symbol_select(self, symbol: str):
        return self._rpc("symbol_select", symbol=symbol)

    def tick(self, symbol: str):
        return self._rpc("tick", symbol=symbol)

    def rates(self, symbol: str, timeframe: Timeframe, count: int):
        return self._rpc("rates", symbol=symbol, timeframe=timeframe.value, count=count)

    def rates_range(self, symbol: str, timeframe: Timeframe, date_from: datetime, date_to: datetime):
        return self._rpc("rates_range", symbol=symbol, timeframe=timeframe.value, date_from=date_from, date_to=date_to)

    def positions(self, symbol: str | None = None):
        return self._rpc("positions", symbol=symbol)

    def orders(self, symbol: str | None = None):
        return self._rpc("orders", symbol=symbol)

    def history_deals(self, date_from: datetime, date_to: datetime, position: int | None = None):
        return self._rpc("history_deals", date_from=date_from, date_to=date_to, position=position)

    def calc_profit(self, side: Side, symbol: str, volume: float, price_open: float, price_close: float):
        return self._rpc("calc_profit", side=side.value, symbol=symbol, volume=volume, price_open=price_open, price_close=price_close)

    def calc_margin(self, side: Side, symbol: str, volume: float, price: float):
        return self._rpc("calc_margin", side=side.value, symbol=symbol, volume=volume, price=price)

    def order_check(self, request: OrderRequest):
        return self._rpc("order_check", request=request.to_mt5())

    def order_send(self, request: OrderRequest):
        return self._rpc("order_send", request=request.to_mt5())
