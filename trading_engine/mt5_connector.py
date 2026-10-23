"""Real MetaTrader 5 terminal gateway (requires Windows + the MetaTrader5 package).

Credentials are read from the environment (MT5_LOGIN / MT5_PASSWORD /
MT5_SERVER / MT5_PATH) and are never logged. The MetaTrader5 package keeps a
single global connection and is not thread-safe, so every call goes through a
lock.

Docs: https://www.mql5.com/en/docs/python_metatrader5
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .constants import Timeframe
from .gateway import RATES_COLUMNS, ConnectionLost, GatewayError, GatewayTimeout, empty_rates
from .models import (
    AccountInfo,
    AccountKind,
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

log = logging.getLogger(__name__)

# mt5.last_error() codes
RES_E_AUTH_FAILED = -6
RES_E_AUTO_TRADING_DISABLED = -8
_CONNECTION_ERRORS = {-10000, -10001, -10002, -10003, -10004}
_TIMEOUT_ERRORS = {-10005}


@dataclass(frozen=True)
class MT5Credentials:
    login: int | None
    password: str | None
    server: str | None
    path: str | None
    timeout_ms: int = 60_000

    @classmethod
    def from_env(cls) -> "MT5Credentials":
        login = os.environ.get("MT5_LOGIN")
        return cls(
            login=int(login) if login else None,
            password=os.environ.get("MT5_PASSWORD") or None,
            server=os.environ.get("MT5_SERVER") or None,
            path=os.environ.get("MT5_PATH") or None,
            timeout_ms=int(os.environ.get("MT5_TIMEOUT_MS", "60000")),
        )

    def __repr__(self) -> str:  # never leak the password through repr/logging
        return f"MT5Credentials(login={self.login}, server={self.server!r}, path={self.path!r}, password=***)"


def _ts(seconds: int | float) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


class MT5Connector:
    name = "mt5"

    def __init__(self, credentials: MT5Credentials | None = None, mt5_module: Any = None):
        self._creds = credentials or MT5Credentials.from_env()
        self._mt5 = mt5_module
        self._lock = threading.RLock()
        self._connected = False

    # ------------------------------------------------------------------ helpers
    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - platform dependent
                raise GatewayError(
                    "MetaTrader5 package is not installed. It is only available on Windows; "
                    "run bridge/mt5_bridge.py on the Windows host and use MT5_GATEWAY=remote."
                ) from exc
            self._mt5 = mt5
        return self._mt5

    def _raise_last_error(self, what: str) -> None:
        code, message = self.mt5.last_error()
        text = f"{what} failed: [{code}] {message}"
        if code in _TIMEOUT_ERRORS:
            raise GatewayTimeout(text)
        if code in _CONNECTION_ERRORS:
            self._connected = False
            raise ConnectionLost(text)
        raise GatewayError(text)

    def _call(self, what: str, fn_name: str, *args: Any, allow_none: bool = False, **kwargs: Any) -> Any:
        with self._lock:
            if not self._connected:
                raise ConnectionLost("not connected to MT5 terminal")
            result = getattr(self.mt5, fn_name)(*args, **kwargs)
            if result is None and not allow_none:
                self._raise_last_error(what)
            return result

    # --------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        with self._lock:
            kwargs: dict[str, Any] = {"timeout": self._creds.timeout_ms}
            if self._creds.path:
                kwargs["path"] = self._creds.path
            if self._creds.login:
                kwargs["login"] = self._creds.login
                kwargs["password"] = self._creds.password or ""
                kwargs["server"] = self._creds.server or ""
            ok = self.mt5.initialize(**kwargs)
            if not ok:
                code, message = self.mt5.last_error()
                self._connected = False
                if code == RES_E_AUTH_FAILED:
                    raise GatewayError(f"MT5 authorization failed for login {self._creds.login}")
                raise ConnectionLost(f"MT5 initialize failed: [{code}] {message}")
            self._connected = True
            log.info("Connected to MT5 terminal (login=%s, server=%s)", self._creds.login, self._creds.server)

    def disconnect(self) -> None:
        with self._lock:
            if self._mt5 is not None:
                self.mt5.shutdown()
            self._connected = False

    def terminal_status(self) -> TerminalStatus:
        with self._lock:
            if not self._connected:
                return TerminalStatus(connected=False, trade_allowed=False)
            info = self.mt5.terminal_info()
            if info is None:
                self._connected = False
                return TerminalStatus(connected=False, trade_allowed=False)
            return TerminalStatus(
                connected=bool(info.connected),
                trade_allowed=bool(info.trade_allowed),
                name=str(getattr(info, "name", "")),
                build=int(getattr(info, "build", 0)),
                ping_ms=float(getattr(info, "ping_last", 0)) / 1000.0 if getattr(info, "ping_last", None) else None,
            )

    # ------------------------------------------------------------------ account
    def account_info(self) -> AccountInfo:
        a = self._call("account_info", "account_info")
        return AccountInfo(
            login=int(a.login),
            server=str(a.server),
            currency=str(a.currency),
            balance=float(a.balance),
            equity=float(a.equity),
            margin=float(a.margin),
            margin_free=float(a.margin_free),
            margin_level=float(a.margin_level) if a.margin else None,
            leverage=int(a.leverage),
            kind=AccountKind.from_mt5(a.trade_mode),
            trade_allowed=bool(a.trade_allowed),
            trade_expert=bool(a.trade_expert),
            company=str(getattr(a, "company", "")),
            name=str(getattr(a, "name", "")),
        )

    # ------------------------------------------------------------------ symbols
    def symbols(self, group: str | None = None) -> list[str]:
        result = self._call("symbols_get", "symbols_get", group=group) if group else self._call("symbols_get", "symbols_get")
        return [s.name for s in result]

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        s = self._call("symbol_info", "symbol_info", symbol, allow_none=True)
        if s is None:
            return None
        return SymbolInfo(
            name=s.name,
            digits=int(s.digits),
            point=float(s.point),
            trade_tick_size=float(s.trade_tick_size),
            trade_tick_value=float(s.trade_tick_value),
            trade_tick_value_loss=float(getattr(s, "trade_tick_value_loss", s.trade_tick_value)),
            trade_contract_size=float(s.trade_contract_size),
            volume_min=float(s.volume_min),
            volume_max=float(s.volume_max),
            volume_step=float(s.volume_step),
            volume_limit=float(getattr(s, "volume_limit", 0.0)),
            trade_stops_level=int(s.trade_stops_level),
            trade_freeze_level=int(s.trade_freeze_level),
            trade_mode=int(s.trade_mode),
            filling_mode=int(s.filling_mode),
            currency_base=str(s.currency_base),
            currency_profit=str(s.currency_profit),
            currency_margin=str(s.currency_margin),
            visible=bool(s.visible),
            description=str(getattr(s, "description", "")),
        )

    def symbol_select(self, symbol: str) -> bool:
        return bool(self._call("symbol_select", "symbol_select", symbol, True, allow_none=True))

    def tick(self, symbol: str) -> Tick | None:
        t = self._call("symbol_info_tick", "symbol_info_tick", symbol, allow_none=True)
        if t is None:
            return None
        return Tick(
            symbol=symbol,
            time=_ts(t.time),
            bid=float(t.bid),
            ask=float(t.ask),
            time_msc=int(getattr(t, "time_msc", int(t.time) * 1000)),
            last=float(getattr(t, "last", 0.0)),
        )

    # -------------------------------------------------------------------- rates
    @staticmethod
    def _rates_frame(raw: Any) -> pd.DataFrame:
        if raw is None or len(raw) == 0:
            return empty_rates()
        df = pd.DataFrame(raw)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        for col in RATES_COLUMNS:
            if col not in df.columns:
                df[col] = 0
        return df[RATES_COLUMNS].reset_index(drop=True)

    def rates(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        # start_pos=1 skips the bar that is still forming.
        raw = self._call("copy_rates_from_pos", "copy_rates_from_pos", symbol, timeframe.mt5_value, 1, int(count))
        return self._rates_frame(raw)

    def rates_range(self, symbol: str, timeframe: Timeframe, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        raw = self._call("copy_rates_range", "copy_rates_range", symbol, timeframe.mt5_value, date_from, date_to)
        return self._rates_frame(raw)

    # ---------------------------------------------------------------- positions
    def positions(self, symbol: str | None = None) -> list[Position]:
        raw = self._call("positions_get", "positions_get", symbol=symbol) if symbol else self._call("positions_get", "positions_get")
        return [
            Position(
                ticket=int(p.ticket),
                symbol=p.symbol,
                side=Side.BUY if int(p.type) == 0 else Side.SELL,
                volume=float(p.volume),
                price_open=float(p.price_open),
                sl=float(p.sl),
                tp=float(p.tp),
                price_current=float(p.price_current),
                profit=float(p.profit),
                swap=float(p.swap),
                magic=int(p.magic),
                comment=str(p.comment),
                time=_ts(p.time),
                identifier=int(getattr(p, "identifier", p.ticket)),
            )
            for p in raw
        ]

    def orders(self, symbol: str | None = None) -> list[PendingOrder]:
        raw = self._call("orders_get", "orders_get", symbol=symbol) if symbol else self._call("orders_get", "orders_get")
        return [
            PendingOrder(
                ticket=int(o.ticket),
                symbol=o.symbol,
                type=int(o.type),
                volume=float(o.volume_current),
                price_open=float(o.price_open),
                sl=float(o.sl),
                tp=float(o.tp),
                magic=int(o.magic),
                comment=str(o.comment),
                time_setup=_ts(o.time_setup),
            )
            for o in raw
        ]

    def history_deals(self, date_from: datetime, date_to: datetime, position: int | None = None) -> list[Deal]:
        if position:
            raw = self._call("history_deals_get", "history_deals_get", position=int(position))
        else:
            raw = self._call("history_deals_get", "history_deals_get", date_from, date_to)
        return [
            Deal(
                ticket=int(d.ticket),
                order=int(d.order),
                position_id=int(d.position_id),
                symbol=d.symbol,
                type=int(d.type),
                entry=int(d.entry),
                volume=float(d.volume),
                price=float(d.price),
                profit=float(d.profit),
                commission=float(d.commission),
                swap=float(d.swap),
                fee=float(getattr(d, "fee", 0.0)),
                magic=int(d.magic),
                comment=str(d.comment),
                time=_ts(d.time),
            )
            for d in raw
        ]

    # ------------------------------------------------------------- calculators
    def calc_profit(self, side: Side, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        value = self._call(
            "order_calc_profit", "order_calc_profit", int(side.order_type), symbol, float(volume), float(price_open), float(price_close), allow_none=True
        )
        return float(value) if value is not None else None

    def calc_margin(self, side: Side, symbol: str, volume: float, price: float) -> float | None:
        value = self._call("order_calc_margin", "order_calc_margin", int(side.order_type), symbol, float(volume), float(price), allow_none=True)
        return float(value) if value is not None else None

    # ------------------------------------------------------------------ trading
    def order_check(self, request: OrderRequest) -> CheckResult:
        r = self._call("order_check", "order_check", request.to_mt5())
        return CheckResult(
            retcode=int(r.retcode),
            comment=str(r.comment),
            margin=float(getattr(r, "margin", 0.0)),
            margin_free=float(getattr(r, "margin_free", 0.0)),
            margin_level=float(getattr(r, "margin_level", 0.0)),
        )

    def order_send(self, request: OrderRequest) -> OrderResult | None:
        # A None result means the outcome is unknown; the executor reconciles.
        with self._lock:
            if not self._connected:
                raise ConnectionLost("not connected to MT5 terminal")
            r = self.mt5.order_send(request.to_mt5())
            if r is None:
                code, message = self.mt5.last_error()
                log.warning("order_send returned None: [%s] %s", code, message)
                if code in _CONNECTION_ERRORS:
                    self._connected = False
                return None
        return OrderResult(
            retcode=int(r.retcode),
            comment=str(r.comment),
            deal=int(r.deal),
            order=int(r.order),
            volume=float(r.volume),
            price=float(r.price),
            bid=float(r.bid),
            ask=float(r.ask),
            request_id=int(getattr(r, "request_id", 0)),
        )
