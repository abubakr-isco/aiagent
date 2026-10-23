"""MetaTrader 5 constant values.

Values mirror the MetaTrader5 Python package so the engine (and the mock
terminal) can run on machines where that package is not installable
(macOS/Linux). Source: https://www.mql5.com/en/docs/constants
"""

from __future__ import annotations

from enum import Enum, IntEnum


class Timeframe(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        return _TF_MINUTES[self]

    @property
    def mt5_value(self) -> int:
        return _TF_MT5[self]

    @property
    def pandas_rule(self) -> str:
        return f"{self.minutes}min"


_TF_MINUTES = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
}

# mt5.TIMEFRAME_* values
_TF_MT5 = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 16385,
    Timeframe.H4: 16388,
    Timeframe.D1: 16408,
}


class TradeAction(IntEnum):
    DEAL = 1
    PENDING = 5
    SLTP = 6
    MODIFY = 7
    REMOVE = 8
    CLOSE_BY = 10


class OrderType(IntEnum):
    BUY = 0
    SELL = 1
    BUY_LIMIT = 2
    SELL_LIMIT = 3
    BUY_STOP = 4
    SELL_STOP = 5
    BUY_STOP_LIMIT = 6
    SELL_STOP_LIMIT = 7
    CLOSE_BY = 8


class OrderFilling(IntEnum):
    FOK = 0
    IOC = 1
    RETURN = 2


class OrderTime(IntEnum):
    GTC = 0
    DAY = 1
    SPECIFIED = 2
    SPECIFIED_DAY = 3


# symbol_info().filling_mode is a bitmask of these flags
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


class SymbolTradeMode(IntEnum):
    DISABLED = 0
    LONGONLY = 1
    SHORTONLY = 2
    CLOSEONLY = 3
    FULL = 4


class AccountTradeMode(IntEnum):
    DEMO = 0
    CONTEST = 1
    REAL = 2


class DealEntry(IntEnum):
    IN = 0
    OUT = 1
    INOUT = 2
    OUT_BY = 3


class DealType(IntEnum):
    BUY = 0
    SELL = 1
    BALANCE = 2
    CREDIT = 3
    CHARGE = 4
    CORRECTION = 5
    BONUS = 6
    COMMISSION = 7


class Retcode(IntEnum):
    """mt5.TRADE_RETCODE_* values."""

    OK_CHECK = 0  # order_check() success
    REQUOTE = 10004
    REJECT = 10006
    CANCEL = 10007
    PLACED = 10008
    DONE = 10009
    DONE_PARTIAL = 10010
    ERROR = 10011
    TIMEOUT = 10012
    INVALID = 10013
    INVALID_VOLUME = 10014
    INVALID_PRICE = 10015
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    MARKET_CLOSED = 10018
    NO_MONEY = 10019
    PRICE_CHANGED = 10020
    PRICE_OFF = 10021
    INVALID_EXPIRATION = 10022
    ORDER_CHANGED = 10023
    TOO_MANY_REQUESTS = 10024
    NO_CHANGES = 10025
    SERVER_DISABLES_AT = 10026
    CLIENT_DISABLES_AT = 10027
    LOCKED = 10028
    FROZEN = 10029
    INVALID_FILL = 10030
    CONNECTION = 10031
    ONLY_REAL = 10032
    LIMIT_ORDERS = 10033
    LIMIT_VOLUME = 10034
    INVALID_ORDER = 10035
    POSITION_CLOSED = 10036
    INVALID_CLOSE_VOLUME = 10038
    CLOSE_ORDER_EXIST = 10039
    LIMIT_POSITIONS = 10040
    REJECT_CANCEL = 10041
    LONG_ONLY = 10042
    SHORT_ONLY = 10043
    CLOSE_ONLY = 10044
    FIFO_CLOSE = 10045


# Outcome classes used by the executor.
RETCODES_SUCCESS = {Retcode.DONE, Retcode.PLACED}
RETCODES_PARTIAL = {Retcode.DONE_PARTIAL}
# Price moved: safe to retry with a fresh quote (nothing was executed).
RETCODES_PRICE_RETRY = {Retcode.REQUOTE, Retcode.PRICE_CHANGED, Retcode.PRICE_OFF}
# Outcome unknown: the request may or may not have been executed.
RETCODES_UNKNOWN = {Retcode.TIMEOUT, Retcode.CONNECTION, Retcode.ERROR}
RETCODE_DESCRIPTIONS = {
    Retcode.OK_CHECK: "check passed",
    Retcode.REQUOTE: "requote",
    Retcode.REJECT: "request rejected",
    Retcode.CANCEL: "request canceled by trader",
    Retcode.PLACED: "order placed",
    Retcode.DONE: "request completed",
    Retcode.DONE_PARTIAL: "only part of the request was completed",
    Retcode.ERROR: "request processing error",
    Retcode.TIMEOUT: "request canceled by timeout",
    Retcode.INVALID: "invalid request",
    Retcode.INVALID_VOLUME: "invalid volume",
    Retcode.INVALID_PRICE: "invalid price",
    Retcode.INVALID_STOPS: "invalid stops",
    Retcode.TRADE_DISABLED: "trade is disabled",
    Retcode.MARKET_CLOSED: "market is closed",
    Retcode.NO_MONEY: "not enough money",
    Retcode.PRICE_CHANGED: "prices changed",
    Retcode.PRICE_OFF: "no quotes to process the request",
    Retcode.TOO_MANY_REQUESTS: "too frequent requests",
    Retcode.LOCKED: "request locked for processing",
    Retcode.FROZEN: "order or position frozen",
    Retcode.INVALID_FILL: "invalid order filling type",
    Retcode.CONNECTION: "no connection with the trade server",
    Retcode.ONLY_REAL: "operation allowed only for live accounts",
    Retcode.LIMIT_ORDERS: "pending orders limit reached",
    Retcode.LIMIT_VOLUME: "volume limit for symbol reached",
    Retcode.POSITION_CLOSED: "position already closed",
    Retcode.INVALID_CLOSE_VOLUME: "close volume exceeds position volume",
    Retcode.LIMIT_POSITIONS: "open positions limit reached",
    Retcode.LONG_ONLY: "only long positions allowed",
    Retcode.SHORT_ONLY: "only short positions allowed",
    Retcode.CLOSE_ONLY: "only position closing allowed",
    Retcode.FIFO_CLOSE: "FIFO rule: close older position first",
}


def describe_retcode(code: int | None) -> str:
    if code is None:
        return "no result"
    try:
        return RETCODE_DESCRIPTIONS.get(Retcode(code), Retcode(code).name.lower())
    except ValueError:
        return f"unknown retcode {code}"
