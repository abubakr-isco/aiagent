"""JSON encoding of engine types for the Windows bridge protocol."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd

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


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, pd.DataFrame):
        df = value.copy()
        if "time" in df:
            df["time"] = pd.to_datetime(df["time"], utc=True).map(lambda t: t.isoformat())
        return df.to_dict(orient="list")
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def decode_account(d: dict) -> AccountInfo:
    return AccountInfo(**{**d, "kind": AccountKind(d["kind"])})


def decode_symbol(d: dict | None) -> SymbolInfo | None:
    return SymbolInfo(**d) if d else None


def decode_tick(d: dict | None) -> Tick | None:
    return Tick(**{**d, "time": _dt(d["time"])}) if d else None


def decode_position(d: dict) -> Position:
    return Position(**{**d, "side": Side(d["side"]), "time": _dt(d["time"])})


def decode_order(d: dict) -> PendingOrder:
    return PendingOrder(**{**d, "time_setup": _dt(d["time_setup"])})


def decode_deal(d: dict) -> Deal:
    return Deal(**{**d, "time": _dt(d["time"])})


def decode_rates(d: dict) -> pd.DataFrame:
    df = pd.DataFrame(d)
    if "time" in df:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def decode_request(d: dict) -> OrderRequest:
    return OrderRequest.from_mt5(d)


DECODERS = {
    "terminal_status": lambda d: TerminalStatus(**d),
    "account_info": decode_account,
    "symbols": list,
    "symbol_info": decode_symbol,
    "symbol_select": bool,
    "tick": decode_tick,
    "rates": decode_rates,
    "rates_range": decode_rates,
    "positions": lambda rows: [decode_position(r) for r in rows],
    "orders": lambda rows: [decode_order(r) for r in rows],
    "history_deals": lambda rows: [decode_deal(r) for r in rows],
    "calc_profit": lambda v: None if v is None else float(v),
    "calc_margin": lambda v: None if v is None else float(v),
    "order_check": lambda d: CheckResult(**d),
    "order_send": lambda d: None if d is None else OrderResult(**d),
    "connect": lambda v: None,
    "disconnect": lambda v: None,
}
