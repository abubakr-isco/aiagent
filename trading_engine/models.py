"""Plain data types shared by every engine layer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .constants import AccountTradeMode, OrderFilling, OrderTime, OrderType, TradeAction


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def order_type(self) -> OrderType:
        return OrderType.BUY if self is Side.BUY else OrderType.SELL


class AgentMode(str, Enum):
    ANALYSIS = "ANALYSIS"  # analyse only, never send orders
    DEMO = "DEMO"  # trade only on a verified demo account
    LIVE = "LIVE"  # real money, disabled by default


class AccountKind(str, Enum):
    DEMO = "DEMO"
    CONTEST = "CONTEST"
    REAL = "REAL"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_mt5(cls, value: Any) -> "AccountKind":
        mapping = {
            AccountTradeMode.DEMO: cls.DEMO,
            AccountTradeMode.CONTEST: cls.CONTEST,
            AccountTradeMode.REAL: cls.REAL,
        }
        try:
            return mapping[AccountTradeMode(int(value))]
        except (TypeError, ValueError, KeyError):
            return cls.UNKNOWN


@dataclass(frozen=True)
class TerminalStatus:
    connected: bool
    trade_allowed: bool
    name: str = ""
    build: int = 0
    ping_ms: float | None = None


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float | None
    leverage: int
    kind: AccountKind
    trade_allowed: bool
    trade_expert: bool
    company: str = ""
    name: str = ""

    @property
    def floating_pnl(self) -> float:
        return self.equity - self.balance


@dataclass(frozen=True)
class SymbolInfo:
    name: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_tick_value_loss: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    volume_limit: float = 0.0
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    trade_mode: int = 4  # SymbolTradeMode.FULL
    filling_mode: int = 1  # bitmask SYMBOL_FILLING_*
    currency_base: str = ""
    currency_profit: str = ""
    currency_margin: str = ""
    visible: bool = True
    description: str = ""


@dataclass(frozen=True)
class Tick:
    symbol: str
    time: datetime  # broker server time as reported by the terminal
    bid: float
    ask: float
    time_msc: int = 0
    last: float = 0.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    price_current: float
    profit: float
    swap: float
    magic: int
    comment: str
    time: datetime
    identifier: int = 0

    @property
    def has_stop_loss(self) -> bool:
        return self.sl is not None and self.sl > 0


@dataclass(frozen=True)
class PendingOrder:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    time_setup: datetime


@dataclass(frozen=True)
class Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    type: int  # DealType
    entry: int  # DealEntry
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    magic: int
    comment: str
    time: datetime

    @property
    def net(self) -> float:
        return self.profit + self.commission + self.swap + self.fee


@dataclass
class OrderRequest:
    action: TradeAction
    symbol: str
    volume: float
    type: OrderType
    price: float
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 10
    magic: int = 0
    comment: str = ""
    type_time: OrderTime = OrderTime.GTC
    type_filling: OrderFilling = OrderFilling.FOK
    position: int = 0
    order: int = 0

    def to_mt5(self) -> dict[str, Any]:
        req: dict[str, Any] = {
            "action": int(self.action),
            "symbol": self.symbol,
            "volume": float(self.volume),
            "type": int(self.type),
            "price": float(self.price),
            "sl": float(self.sl),
            "tp": float(self.tp),
            "deviation": int(self.deviation),
            "magic": int(self.magic),
            "comment": self.comment,
            "type_time": int(self.type_time),
            "type_filling": int(self.type_filling),
        }
        if self.position:
            req["position"] = int(self.position)
        if self.order:
            req["order"] = int(self.order)
        return req

    @classmethod
    def from_mt5(cls, data: dict[str, Any]) -> "OrderRequest":
        return cls(
            action=TradeAction(int(data["action"])),
            symbol=data.get("symbol", ""),
            volume=float(data.get("volume", 0.0)),
            type=OrderType(int(data.get("type", 0))),
            price=float(data.get("price", 0.0)),
            sl=float(data.get("sl", 0.0)),
            tp=float(data.get("tp", 0.0)),
            deviation=int(data.get("deviation", 10)),
            magic=int(data.get("magic", 0)),
            comment=data.get("comment", ""),
            type_time=OrderTime(int(data.get("type_time", 0))),
            type_filling=OrderFilling(int(data.get("type_filling", 0))),
            position=int(data.get("position", 0)),
            order=int(data.get("order", 0)),
        )


@dataclass(frozen=True)
class CheckResult:
    retcode: int
    comment: str
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0

    @property
    def ok(self) -> bool:
        # order_check() reports 0 ("Done") on success; some builds return 10009.
        return self.retcode in (0, 10009)


@dataclass(frozen=True)
class OrderResult:
    retcode: int
    comment: str
    deal: int = 0
    order: int = 0
    volume: float = 0.0
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    request_id: int = 0


@dataclass(frozen=True)
class Check:
    """One named rule evaluation, kept for transparency of every decision."""

    name: str
    passed: bool
    detail: str = ""
    mandatory: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    """A trade idea produced by a strategy. It is *not* an order."""

    strategy_id: str
    strategy_version: str
    symbol: str
    side: Side
    timeframe: str
    bar_time: datetime  # open time of the entry-timeframe bar that produced it
    entry: float
    stop_loss: float
    take_profit: float
    atr: float
    checks: list[Check] = field(default_factory=list)
    features: dict[str, float] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def signal_id(self) -> str:
        """Deterministic id: the same setup on the same bar always yields the same id."""
        raw = f"{self.strategy_id}|{self.strategy_version}|{self.symbol}|{self.side.value}|{self.timeframe}|{self.bar_time.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def rr(self) -> float:
        return self.reward_distance / self.risk_distance if self.risk_distance > 0 else 0.0

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "side": self.side.value,
            "timeframe": self.timeframe,
            "bar_time": self.bar_time.isoformat(),
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "atr": self.atr,
            "rr": round(self.rr, 3),
            "checks": [c.to_dict() for c in self.checks],
            "features": self.features,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class StrategyEvaluation:
    """Result of running a strategy on a snapshot: a signal or the reasons for none."""

    symbol: str
    strategy_id: str
    signal: Signal | None
    checks: list[Check]
    side_considered: Side | None = None

    @property
    def rejection_reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.mandatory and not c.passed]


def stable_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
