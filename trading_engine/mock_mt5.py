"""Deterministic MetaTrader 5 simulator.

Used by the test-suite and for development without a terminal. It mimics the
parts of MT5 the engine relies on (symbol specs, closed-bar rates, ticks,
order_check/order_send retcodes, positions, deals, SL/TP triggering) and allows
injecting faults: timeouts, requotes, rejections, disconnects, partial fills.

THIS IS NOT A REAL CONNECTION. Anything verified only against the mock must be
re-verified against a real demo terminal.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from .constants import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    DealEntry,
    DealType,
    OrderFilling,
    OrderType,
    Retcode,
    SymbolTradeMode,
    Timeframe,
    TradeAction,
)
from .data import resample, synthetic_ohlc
from .gateway import ConnectionLost, GatewayTimeout, empty_rates
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

DEFAULT_SPECS: dict[str, SymbolInfo] = {
    "EURUSD": SymbolInfo(
        name="EURUSD", digits=5, point=0.00001, trade_tick_size=0.00001, trade_tick_value=1.0, trade_tick_value_loss=1.0,
        trade_contract_size=100_000, volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_stops_level=10,
        filling_mode=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC, currency_base="EUR", currency_profit="USD", currency_margin="EUR",
    ),
    "GBPUSD": SymbolInfo(
        name="GBPUSD", digits=5, point=0.00001, trade_tick_size=0.00001, trade_tick_value=1.0, trade_tick_value_loss=1.0,
        trade_contract_size=100_000, volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_stops_level=10,
        filling_mode=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC, currency_base="GBP", currency_profit="USD", currency_margin="GBP",
    ),
    "XAUUSD": SymbolInfo(
        name="XAUUSD", digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=1.0, trade_tick_value_loss=1.0,
        trade_contract_size=100, volume_min=0.01, volume_max=50.0, volume_step=0.01, trade_stops_level=20,
        filling_mode=SYMBOL_FILLING_FOK, currency_base="XAU", currency_profit="USD", currency_margin="XAU",
    ),
    "USDJPY": SymbolInfo(
        name="USDJPY", digits=3, point=0.001, trade_tick_size=0.001, trade_tick_value=0.67, trade_tick_value_loss=0.67,
        trade_contract_size=100_000, volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_stops_level=10,
        filling_mode=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC, currency_base="USD", currency_profit="JPY", currency_margin="USD",
    ),
}

DEFAULT_PRICES = {"EURUSD": 1.1000, "GBPUSD": 1.2700, "XAUUSD": 2350.0, "USDJPY": 150.0}
DEFAULT_VOL = {"EURUSD": 0.00035, "GBPUSD": 0.0004, "XAUUSD": 0.0006, "USDJPY": 0.00035}


@dataclass
class Fault:
    """A one-shot failure applied to the next matching call."""

    kind: str  # timeout_executed | timeout_not_executed | none_result | requote | reject | no_money
    #            invalid_volume | partial | drop_sl | price_changed | check_fail | too_many_requests
    count: int = 1


@dataclass
class _Pos:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    time: datetime
    swap: float = 0.0


@dataclass
class MockState:
    balance: float = 10_000.0
    leverage: int = 100
    currency: str = "USD"
    kind: AccountKind = AccountKind.DEMO
    trade_allowed: bool = True
    trade_expert: bool = True
    terminal_trade_allowed: bool = True
    connected: bool = True
    commission_per_lot: float = 3.5  # per side, account currency
    slippage_points: int = 0
    positions: dict[int, _Pos] = field(default_factory=dict)
    orders: dict[int, PendingOrder] = field(default_factory=dict)
    deals: list[Deal] = field(default_factory=list)


class MockMT5Gateway:
    name = "mock"

    def __init__(
        self,
        data: dict[str, pd.DataFrame] | None = None,
        *,
        base_minutes: int = 5,
        specs: dict[str, SymbolInfo] | None = None,
        start_index: int = 3000,
        state: MockState | None = None,
        seed: int = 7,
    ):
        self.specs = dict(specs or DEFAULT_SPECS)
        self.base_minutes = base_minutes
        if data is None:
            data = {
                sym: synthetic_ohlc(
                    periods=12_000, minutes=base_minutes, base_price=DEFAULT_PRICES[sym], digits=self.specs[sym].digits,
                    volatility=DEFAULT_VOL[sym], spread_points=20 if sym == "XAUUSD" else 8, seed=seed + i,
                )
                for i, sym in enumerate(self.specs)
            }
        self.data = {k: v.reset_index(drop=True) for k, v in data.items()}
        self.cursor = {sym: min(start_index, len(df) - 1) for sym, df in self.data.items()}
        self.state = state or MockState()
        self.faults: deque[Fault] = deque()
        self.frozen_quotes: set[str] = set()
        self.spread_multiplier: dict[str, float] = {}
        self.sent_requests: list[OrderRequest] = []
        self.connect_calls = 0
        self.fail_connect = 0
        self._ids = itertools.count(1000)
        self._lock = threading.RLock()
        self._tick_seq = itertools.count(1)
        self._tick_msc: dict[str, int] = {}
        self.on_send: Callable[[OrderRequest], None] | None = None

    # ------------------------------------------------------------ test helpers
    def inject(self, kind: str, count: int = 1) -> None:
        self.faults.append(Fault(kind, count))

    def _take_fault(self, kinds: set[str]) -> str | None:
        if self.faults and self.faults[0].kind in kinds:
            fault = self.faults[0]
            fault.count -= 1
            if fault.count <= 0:
                self.faults.popleft()
            return fault.kind
        return None

    def set_connected(self, connected: bool) -> None:
        self.state.connected = connected

    def advance(self, bars: int = 1, symbol: str | None = None) -> None:
        """Move simulated time forward and trigger SL/TP on the traversed bars."""
        with self._lock:
            for _ in range(bars):
                for sym in [symbol] if symbol else list(self.data):
                    if self.cursor[sym] + 1 >= len(self.data[sym]):
                        continue
                    self.cursor[sym] += 1
                    self._process_stops(sym, self.data[sym].iloc[self.cursor[sym]])

    def now(self, symbol: str | None = None) -> datetime:
        sym = symbol or next(iter(self.data))
        bar = self.data[sym].iloc[self.cursor[sym]]
        return bar["time"].to_pydatetime() + timedelta(minutes=self.base_minutes)

    def _require_connection(self) -> None:
        if not self.state.connected:
            raise ConnectionLost("mock terminal disconnected")

    # --------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        self.connect_calls += 1
        if self.fail_connect > 0:
            self.fail_connect -= 1
            raise ConnectionLost("mock: connection refused")
        self.state.connected = True

    def disconnect(self) -> None:
        self.state.connected = False

    def terminal_status(self) -> TerminalStatus:
        return TerminalStatus(connected=self.state.connected, trade_allowed=self.state.terminal_trade_allowed, name="MockMT5", build=0)

    # ----------------------------------------------------------------- pricing
    def _bid(self, symbol: str) -> float:
        return float(self.data[symbol].iloc[self.cursor[symbol]]["close"])

    def _spread(self, symbol: str) -> float:
        spec = self.specs[symbol]
        points = float(self.data[symbol].iloc[self.cursor[symbol]]["spread"])
        return round(points * self.spread_multiplier.get(symbol, 1.0) * spec.point, spec.digits)

    def _ask(self, symbol: str) -> float:
        return round(self._bid(symbol) + self._spread(symbol), self.specs[symbol].digits)

    def _to_account(self, symbol: str, amount_profit_ccy: float, price: float) -> float:
        spec = self.specs[symbol]
        if spec.currency_profit == self.state.currency:
            return amount_profit_ccy
        if spec.currency_base == self.state.currency:
            return amount_profit_ccy / price
        return amount_profit_ccy * spec.trade_tick_value / (spec.trade_tick_size * spec.trade_contract_size)

    def _pos_profit(self, p: _Pos) -> float:
        close = self._bid(p.symbol) if p.side is Side.BUY else self._ask(p.symbol)
        return self.calc_profit(p.side, p.symbol, p.volume, p.price_open, close) or 0.0

    # ----------------------------------------------------------------- account
    def account_info(self) -> AccountInfo:
        self._require_connection()
        with self._lock:
            floating = sum(self._pos_profit(p) + p.swap for p in self.state.positions.values())
            margin = sum(self.calc_margin(p.side, p.symbol, p.volume, p.price_open) or 0.0 for p in self.state.positions.values())
            equity = self.state.balance + floating
            return AccountInfo(
                login=5_000_001, server="MockBroker-Demo" if self.state.kind is AccountKind.DEMO else "MockBroker-Live",
                currency=self.state.currency, balance=round(self.state.balance, 2), equity=round(equity, 2),
                margin=round(margin, 2), margin_free=round(equity - margin, 2),
                margin_level=round(equity / margin * 100, 2) if margin else None, leverage=self.state.leverage,
                kind=self.state.kind, trade_allowed=self.state.trade_allowed, trade_expert=self.state.trade_expert,
                company="Mock Broker Ltd", name="Mock Account",
            )

    def symbols(self, group: str | None = None) -> list[str]:
        self._require_connection()
        return sorted(self.specs)

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        self._require_connection()
        spec = self.specs.get(symbol)
        if spec is None:
            return None
        if spec.currency_base == self.state.currency and spec.currency_profit != self.state.currency:
            tv = spec.trade_tick_size * spec.trade_contract_size / self._bid(symbol)
            spec = replace(spec, trade_tick_value=round(tv, 6), trade_tick_value_loss=round(tv, 6))
        return spec

    def symbol_select(self, symbol: str) -> bool:
        self._require_connection()
        return symbol in self.specs

    def tick(self, symbol: str) -> Tick | None:
        self._require_connection()
        if symbol not in self.data:
            return None
        if symbol not in self.frozen_quotes or symbol not in self._tick_msc:
            self._tick_msc[symbol] = int(self.now(symbol).timestamp() * 1000) + next(self._tick_seq)
        msc = self._tick_msc[symbol]
        return Tick(symbol=symbol, time=datetime.fromtimestamp(msc / 1000, tz=timezone.utc), bid=self._bid(symbol), ask=self._ask(symbol), time_msc=msc)

    def rates(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        self._require_connection()
        if symbol not in self.data:
            return empty_rates()
        # Bar at cursor is treated as just closed: "now" is its close time.
        base = self.data[symbol].iloc[: self.cursor[symbol] + 1]
        if timeframe.minutes == self.base_minutes:
            out = base
        elif timeframe.minutes < self.base_minutes:
            raise ValueError(f"mock base timeframe is {self.base_minutes}m, cannot serve {timeframe.value}")
        else:
            window = base.iloc[-(count + 2) * (timeframe.minutes // self.base_minutes) - 1 :]
            out = resample(window, timeframe, self.base_minutes)
        return out.iloc[-count:].reset_index(drop=True)

    def rates_range(self, symbol: str, timeframe: Timeframe, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        self._require_connection()
        base = self.data[symbol].iloc[: self.cursor[symbol] + 1]
        base = base[(base["time"] >= pd.Timestamp(date_from)) & (base["time"] <= pd.Timestamp(date_to))]
        if timeframe.minutes == self.base_minutes:
            return base.reset_index(drop=True)
        return resample(base, timeframe, self.base_minutes)

    # --------------------------------------------------------------- positions
    def positions(self, symbol: str | None = None) -> list[Position]:
        self._require_connection()
        with self._lock:
            out = []
            for p in self.state.positions.values():
                if symbol and p.symbol != symbol:
                    continue
                current = self._bid(p.symbol) if p.side is Side.BUY else self._ask(p.symbol)
                out.append(
                    Position(
                        ticket=p.ticket, symbol=p.symbol, side=p.side, volume=p.volume, price_open=p.price_open, sl=p.sl, tp=p.tp,
                        price_current=current, profit=round(self._pos_profit(p), 2), swap=p.swap, magic=p.magic,
                        comment=p.comment, time=p.time, identifier=p.ticket,
                    )
                )
            return out

    def orders(self, symbol: str | None = None) -> list[PendingOrder]:
        self._require_connection()
        return [o for o in self.state.orders.values() if not symbol or o.symbol == symbol]

    def history_deals(self, date_from: datetime, date_to: datetime, position: int | None = None) -> list[Deal]:
        self._require_connection()
        if position:
            return [d for d in self.state.deals if d.position_id == position]
        return [d for d in self.state.deals if date_from <= d.time <= date_to]

    # ------------------------------------------------------------- calculators
    def calc_profit(self, side: Side, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        spec = self.specs.get(symbol)
        if spec is None:
            return None
        raw = (price_close - price_open) * side.sign * volume * spec.trade_contract_size
        return round(self._to_account(symbol, raw, price_close), 2)

    def calc_margin(self, side: Side, symbol: str, volume: float, price: float) -> float | None:
        spec = self.specs.get(symbol)
        if spec is None:
            return None
        notional_profit_ccy = volume * spec.trade_contract_size * price
        if spec.currency_base == self.state.currency:
            return round(volume * spec.trade_contract_size / self.state.leverage, 2)
        return round(self._to_account(symbol, notional_profit_ccy, price) / self.state.leverage, 2)

    # ----------------------------------------------------------------- trading
    def _validate(self, req: OrderRequest) -> tuple[int, str]:
        spec = self.specs.get(req.symbol)
        if spec is None:
            return Retcode.INVALID, "unknown symbol"
        if not (self.state.trade_allowed and self.state.trade_expert and self.state.terminal_trade_allowed):
            return Retcode.CLIENT_DISABLES_AT, "autotrading disabled"
        if spec.trade_mode == SymbolTradeMode.DISABLED:
            return Retcode.TRADE_DISABLED, "trade disabled"
        if req.action == TradeAction.SLTP:
            return (Retcode.OK_CHECK, "Done") if req.position in self.state.positions else (Retcode.POSITION_CLOSED, "position not found")
        if req.action == TradeAction.REMOVE:
            return (Retcode.OK_CHECK, "Done") if req.order in self.state.orders else (Retcode.INVALID_ORDER, "order not found")
        steps = req.volume / spec.volume_step
        if req.volume < spec.volume_min - 1e-9 or req.volume > spec.volume_max + 1e-9 or abs(steps - round(steps)) > 1e-6:
            return Retcode.INVALID_VOLUME, "invalid volume"
        allowed = {OrderFilling.RETURN}
        if spec.filling_mode & SYMBOL_FILLING_FOK:
            allowed.add(OrderFilling.FOK)
        if spec.filling_mode & SYMBOL_FILLING_IOC:
            allowed.add(OrderFilling.IOC)
        if req.action == TradeAction.DEAL and req.type_filling not in allowed:
            return Retcode.INVALID_FILL, "unsupported filling mode"
        if req.action == TradeAction.DEAL and req.position:
            pos = self.state.positions.get(req.position)
            if pos is None:
                return Retcode.POSITION_CLOSED, "position already closed"
            if req.volume > pos.volume + 1e-9:
                return Retcode.INVALID_CLOSE_VOLUME, "close volume exceeds position"
            return Retcode.OK_CHECK, "Done"
        if req.action == TradeAction.DEAL:
            side = Side.BUY if req.type == OrderType.BUY else Side.SELL
            price = self._ask(req.symbol) if side is Side.BUY else self._bid(req.symbol)
            min_dist = spec.trade_stops_level * spec.point
            if req.sl:
                if (side is Side.BUY and req.sl > price - min_dist) or (side is Side.SELL and req.sl < price + min_dist):
                    return Retcode.INVALID_STOPS, "invalid stops"
            if req.tp:
                if (side is Side.BUY and req.tp < price + min_dist) or (side is Side.SELL and req.tp > price - min_dist):
                    return Retcode.INVALID_STOPS, "invalid stops"
            margin = self.calc_margin(side, req.symbol, req.volume, price) or 0.0
            if margin > self.account_info().margin_free:
                return Retcode.NO_MONEY, "not enough money"
        return Retcode.OK_CHECK, "Done"

    def order_check(self, request: OrderRequest) -> CheckResult:
        self._require_connection()
        with self._lock:
            if self._take_fault({"check_fail"}):
                return CheckResult(retcode=Retcode.INVALID, comment="mock check failure")
            code, comment = self._validate(request)
            acct = self.account_info()
            return CheckResult(retcode=int(code), comment=comment, margin=acct.margin, margin_free=acct.margin_free, margin_level=acct.margin_level or 0.0)

    def order_send(self, request: OrderRequest) -> OrderResult | None:
        self._require_connection()
        with self._lock:
            self.sent_requests.append(request)
            if self.on_send:
                self.on_send(request)
            fault = self._take_fault(
                {"timeout_executed", "timeout_not_executed", "none_result", "requote", "reject", "no_money", "invalid_volume",
                 "price_changed", "too_many_requests", "partial", "drop_sl"}
            )
            bid, ask = self._bid(request.symbol), self._ask(request.symbol)
            if fault == "timeout_not_executed":
                raise GatewayTimeout("mock: request timed out before reaching server")
            if fault == "none_result":
                return None
            simple = {"requote": Retcode.REQUOTE, "reject": Retcode.REJECT, "no_money": Retcode.NO_MONEY,
                      "invalid_volume": Retcode.INVALID_VOLUME, "price_changed": Retcode.PRICE_CHANGED,
                      "too_many_requests": Retcode.TOO_MANY_REQUESTS}
            if fault in simple:
                return OrderResult(retcode=int(simple[fault]), comment=fault, bid=bid, ask=ask)
            code, comment = self._validate(request)
            if code != Retcode.OK_CHECK:
                return OrderResult(retcode=int(code), comment=comment, bid=bid, ask=ask)
            result = self._execute(request, partial=fault == "partial", drop_sl=fault == "drop_sl")
            if fault == "timeout_executed":
                raise GatewayTimeout("mock: request executed but response timed out")
            return result

    def _execute(self, req: OrderRequest, *, partial: bool, drop_sl: bool) -> OrderResult:
        spec = self.specs[req.symbol]
        bid, ask = self._bid(req.symbol), self._ask(req.symbol)
        now = self.now(req.symbol)
        if req.action == TradeAction.SLTP:
            pos = self.state.positions[req.position]
            pos.sl, pos.tp = req.sl, req.tp
            return OrderResult(retcode=int(Retcode.DONE), comment="Request executed", order=next(self._ids), bid=bid, ask=ask)
        if req.action == TradeAction.REMOVE:
            self.state.orders.pop(req.order)
            return OrderResult(retcode=int(Retcode.DONE), comment="Request executed", order=req.order, bid=bid, ask=ask)
        if req.action == TradeAction.PENDING:
            ticket = next(self._ids)
            self.state.orders[ticket] = PendingOrder(
                ticket=ticket, symbol=req.symbol, type=int(req.type), volume=req.volume, price_open=req.price, sl=req.sl,
                tp=req.tp, magic=req.magic, comment=req.comment[:31], time_setup=now,
            )
            return OrderResult(retcode=int(Retcode.PLACED), comment="Request placed", order=ticket, volume=req.volume, price=req.price, bid=bid, ask=ask)

        side = Side.BUY if req.type == OrderType.BUY else Side.SELL
        slip = self.state.slippage_points * spec.point
        price = round((ask + slip) if side is Side.BUY else (bid - slip), spec.digits)
        volume = req.volume
        retcode = Retcode.DONE
        if partial:
            volume = max(spec.volume_min, round(round(req.volume / 2 / spec.volume_step) * spec.volume_step, 8))
            retcode = Retcode.DONE_PARTIAL
        order_ticket = next(self._ids)
        deal_ticket = next(self._ids)
        commission = -round(self.state.commission_per_lot * volume, 2)

        if req.position:  # closing (fully or partially) an existing position
            pos = self.state.positions[req.position]
            volume = min(volume, pos.volume)
            profit = self.calc_profit(pos.side, pos.symbol, volume, pos.price_open, price) or 0.0
            self.state.balance += profit + commission
            self.state.deals.append(
                Deal(ticket=deal_ticket, order=order_ticket, position_id=pos.ticket, symbol=pos.symbol,
                     type=int(DealType.BUY if side is Side.BUY else DealType.SELL), entry=int(DealEntry.OUT), volume=volume,
                     price=price, profit=profit, commission=commission, swap=0.0, fee=0.0, magic=req.magic,
                     comment=req.comment[:31], time=now)
            )
            pos.volume = round(pos.volume - volume, 8)
            if pos.volume <= 1e-9:
                self.state.positions.pop(pos.ticket)
        else:
            self.state.balance += commission
            self.state.positions[order_ticket] = _Pos(
                ticket=order_ticket, symbol=req.symbol, side=side, volume=volume, price_open=price,
                sl=0.0 if drop_sl else req.sl, tp=req.tp, magic=req.magic, comment=req.comment[:31], time=now,
            )
            self.state.deals.append(
                Deal(ticket=deal_ticket, order=order_ticket, position_id=order_ticket, symbol=req.symbol,
                     type=int(DealType.BUY if side is Side.BUY else DealType.SELL), entry=int(DealEntry.IN), volume=volume,
                     price=price, profit=0.0, commission=commission, swap=0.0, fee=0.0, magic=req.magic,
                     comment=req.comment[:31], time=now)
            )
        return OrderResult(retcode=int(retcode), comment="Request executed", deal=deal_ticket, order=order_ticket,
                           volume=volume, price=price, bid=bid, ask=ask)

    def _process_stops(self, symbol: str, bar: pd.Series) -> None:
        spec = self.specs[symbol]
        spread = float(bar["spread"]) * spec.point
        for pos in [p for p in self.state.positions.values() if p.symbol == symbol]:
            exit_price, tag = None, ""
            if pos.side is Side.BUY:
                if pos.sl and bar["low"] <= pos.sl:
                    exit_price, tag = min(pos.sl, bar["open"]), "[sl]"  # a gap fills at the worse open
                elif pos.tp and bar["high"] >= pos.tp:
                    exit_price, tag = pos.tp, "[tp]"
            else:
                if pos.sl and bar["high"] + spread >= pos.sl:
                    exit_price, tag = max(pos.sl, bar["open"] + spread), "[sl]"
                elif pos.tp and bar["low"] + spread <= pos.tp:
                    exit_price, tag = pos.tp, "[tp]"
            if exit_price is None:
                continue
            profit = self.calc_profit(pos.side, symbol, pos.volume, pos.price_open, exit_price) or 0.0
            commission = -round(self.state.commission_per_lot * pos.volume, 2)
            self.state.balance += profit + commission
            self.state.deals.append(
                Deal(ticket=next(self._ids), order=next(self._ids), position_id=pos.ticket, symbol=symbol,
                     type=int(DealType.SELL if pos.side is Side.BUY else DealType.BUY), entry=int(DealEntry.OUT),
                     volume=pos.volume, price=round(exit_price, spec.digits), profit=profit, commission=commission,
                     swap=pos.swap, fee=0.0, magic=pos.magic, comment=tag,
                     time=bar["time"].to_pydatetime() + timedelta(minutes=self.base_minutes))
            )
            del self.state.positions[pos.ticket]

    # Manual (user) position helper for tests: opened outside the agent.
    def open_manual_position(self, symbol: str, side: Side, volume: float, sl: float = 0.0, tp: float = 0.0) -> int:
        req = OrderRequest(action=TradeAction.DEAL, symbol=symbol, volume=volume, type=side.order_type,
                           price=self._ask(symbol) if side is Side.BUY else self._bid(symbol), sl=sl, tp=tp, magic=0, comment="manual")
        result = self._execute(req, partial=False, drop_sl=False)
        return result.order
