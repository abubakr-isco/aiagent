"""Deterministic Risk Manager - the mandatory gate before every order.

The RiskManager is the only component that can create a valid
:class:`RiskApproval`. Approvals are HMAC-signed with a per-process secret and
expire after ``approval_ttl_s``; the OrderExecutor refuses anything that is not
a fresh, untampered approval for exactly the proposal it is asked to execute.

If any input needed to compute risk is missing or inconsistent, the trade is
rejected - the manager never "assumes" a value.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import RiskConfig
from .constants import SymbolTradeMode
from .journal import DayState, Journal
from .models import AccountInfo, AccountKind, AgentMode, Check, Position, Side, SymbolInfo, Tick

_PROCESS_SECRET = secrets.token_bytes(32)


@dataclass(frozen=True)
class TradeProposal:
    signal_id: str
    symbol: str
    side: Side
    entry: float
    stop_loss: float
    take_profit: float
    strategy_id: str
    strategy_version: str


@dataclass(frozen=True)
class RiskApproval:
    proposal: TradeProposal
    volume: float
    risk_money: float
    risk_pct: float
    entry_price: float  # price the approval was computed at
    issued_monotonic: float
    signature: str

    def payload(self) -> bytes:
        p = self.proposal
        return (
            f"{p.signal_id}|{p.symbol}|{p.side.value}|{p.stop_loss:.10f}|{p.take_profit:.10f}|"
            f"{self.volume:.8f}|{self.risk_money:.6f}|{self.entry_price:.10f}|{self.issued_monotonic:.6f}"
        ).encode()

    def is_authentic(self) -> bool:
        expected = hmac.new(_PROCESS_SECRET, self.payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)


@dataclass
class RiskDecision:
    approved: bool
    checks: list[Check]
    approval: RiskApproval | None = None
    volume: float = 0.0
    risk_money: float = 0.0
    risk_pct: float = 0.0
    loss_per_lot: float | None = None
    open_risk_pct: float = 0.0
    day_loss_pct: float = 0.0
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def violations(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "volume": self.volume,
            "risk_money": round(self.risk_money, 2),
            "risk_pct": round(self.risk_pct, 4),
            "loss_per_lot": self.loss_per_lot,
            "open_risk_pct": round(self.open_risk_pct, 4),
            "day_loss_pct": round(self.day_loss_pct, 4),
            "checks": [c.to_dict() for c in self.checks],
            "config": self.config,
        }


@dataclass
class RiskContext:
    mode: AgentMode
    account: AccountInfo | None
    symbol: SymbolInfo | None
    tick: Tick | None
    positions: list[Position]
    connected: bool
    reconciled: bool
    kill_switch: bool
    quote_fresh: bool
    live_enabled: bool = False
    entry_atr: float | None = None
    agent_symbols_with_positions: set[str] = field(default_factory=set)


def floor_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return 0.0
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(volume / step + 1e-9) * step, decimals)


class QuoteMonitor:
    """Tracks quote freshness by local monotonic time and detects spread anomalies.

    Broker tick timestamps are in server time (unknown offset), so freshness is
    measured as "how long since this symbol's tick last changed", locally.
    """

    def __init__(self, window: int = 200, clock=time.monotonic):
        self._last: dict[str, tuple[int, float]] = {}
        self._spreads: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._clock = clock

    def observe(self, tick: Tick) -> None:
        key = tick.time_msc or int(tick.time.timestamp() * 1000)
        prev = self._last.get(tick.symbol)
        if prev is None or prev[0] != key:
            self._last[tick.symbol] = (key, self._clock())
        if tick.is_valid():
            self._spreads[tick.symbol].append(tick.spread)

    def age_s(self, symbol: str) -> float | None:
        prev = self._last.get(symbol)
        return None if prev is None else self._clock() - prev[1]

    def is_fresh(self, symbol: str, max_age_s: float) -> bool:
        age = self.age_s(symbol)
        return age is not None and age <= max_age_s

    def median_spread(self, symbol: str) -> float | None:
        values = self._spreads.get(symbol)
        if not values or len(values) < 20:
            return None
        return statistics.median(values)


class DailyLossTracker:
    """Day-start equity and the daily lock, persisted so a restart cannot reset them."""

    def __init__(self, journal: Journal, cfg: RiskConfig):
        self.journal = journal
        self.cfg = cfg

    def trading_day(self, now: datetime | None = None) -> date:
        now = now or datetime.now(timezone.utc)
        return (now.astimezone(timezone.utc) + timedelta(hours=self.cfg.trading_day_utc_offset_hours)).date()

    def state(self, account: AccountInfo, now: datetime | None = None, realized_today: float | None = None) -> DayState:
        day = self.trading_day(now)
        st = self.journal.load_day_state(day)
        if st is None:
            # First sight of this day. If we restarted mid-day, back out today's
            # realised P&L (incl. commission/swap) so the limit is not reset.
            start = account.equity if realized_today is None else account.balance - realized_today
            st = DayState(day=day, start_equity=round(start, 2))
            self.journal.save_day_state(st)
            self.journal.log_event("INFO", "risk", f"trading day {day} start equity {st.start_equity}", {"equity": account.equity})
        return st

    def update(self, account: AccountInfo, now: datetime | None = None, realized_today: float | None = None) -> DayState:
        st = self.state(account, now, realized_today)
        loss_pct = self.loss_pct(st, account)
        if not st.locked and loss_pct >= self.cfg.daily_loss_limit_pct:
            st.locked = True
            st.locked_reason = f"daily loss {loss_pct:.2f}% >= limit {self.cfg.daily_loss_limit_pct}%"
            self.journal.save_day_state(st)
            self.journal.log_event("CRITICAL", "risk", f"Daily loss limit hit: {st.locked_reason}. New trades blocked until next trading day.")
        return st

    @staticmethod
    def loss_pct(st: DayState, account: AccountInfo) -> float:
        # Equity already includes floating P&L, commissions and swaps.
        if st.start_equity <= 0:
            return 100.0
        return max(0.0, (st.start_equity - account.equity) / st.start_equity * 100)


class RiskManager:
    def __init__(self, cfg: RiskConfig, journal: Journal, gateway, quotes: QuoteMonitor | None = None, clock=time.monotonic):
        errors = cfg.validate()
        if errors:
            raise ValueError("invalid risk config: " + "; ".join(errors))
        self.cfg = cfg
        self.journal = journal
        self.gateway = gateway
        self.quotes = quotes or QuoteMonitor()
        self.daily = DailyLossTracker(journal, cfg)
        self._clock = clock

    # ----------------------------------------------------------------- helpers
    def loss_per_lot(self, side: Side, sym: SymbolInfo, entry: float, sl: float) -> tuple[float | None, str]:
        """Money lost per 1.0 lot if SL is hit, cross-checked by two methods."""
        calc = None
        try:
            value = self.gateway.calc_profit(side, sym.name, 1.0, entry, sl)
            calc = abs(value) if value is not None else None
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot compute"
            return None, f"order_calc_profit failed: {exc}"
        tick_based = None
        tick_value = sym.trade_tick_value_loss or sym.trade_tick_value
        if sym.trade_tick_size > 0 and tick_value > 0:
            tick_based = abs(entry - sl) / sym.trade_tick_size * tick_value
        if calc is None and tick_based is None:
            return None, "neither order_calc_profit nor tick value available"
        if calc is not None and tick_based is not None and calc > 0:
            diff = abs(calc - tick_based) / calc * 100
            if diff > self.cfg.risk_calc_tolerance_pct:
                return None, f"risk calculations disagree: calc_profit {calc:.2f} vs tick value {tick_based:.2f} ({diff:.1f}%)"
        value = calc if calc is not None else tick_based
        if not value or value <= 0:
            return None, "computed loss per lot is zero"
        return value, f"loss/lot {value:.2f} (calc_profit {calc if calc is None else round(calc, 2)}, tick {tick_based if tick_based is None else round(tick_based, 2)})"

    def position_risk(self, pos: Position, from_current: bool = False) -> float | None:
        """Money at risk for an open position (None = undefined, e.g. no SL)."""
        if not pos.has_stop_loss:
            return None
        ref = pos.price_current if from_current else pos.price_open
        try:
            pnl_at_sl = self.gateway.calc_profit(pos.side, pos.symbol, pos.volume, ref, pos.sl)
        except Exception:  # noqa: BLE001
            return None
        if pnl_at_sl is None:
            return None
        return max(0.0, -pnl_at_sl)

    def _sign(self, proposal: TradeProposal, volume: float, risk_money: float, risk_pct: float, entry: float) -> RiskApproval:
        draft = RiskApproval(proposal, volume, risk_money, risk_pct, entry, self._clock(), "")
        sig = hmac.new(_PROCESS_SECRET, draft.payload(), hashlib.sha256).hexdigest()
        return RiskApproval(proposal, volume, risk_money, risk_pct, entry, draft.issued_monotonic, sig)

    def approval_valid(self, approval: RiskApproval) -> tuple[bool, str]:
        if not approval.is_authentic():
            return False, "approval signature invalid"
        age = self._clock() - approval.issued_monotonic
        if age > self.cfg.approval_ttl_s:
            return False, f"approval expired ({age:.1f}s > {self.cfg.approval_ttl_s}s)"
        return True, "ok"

    # ---------------------------------------------------------------- evaluate
    def evaluate(self, proposal: TradeProposal, ctx: RiskContext, *, realized_today: float | None = None, now: datetime | None = None) -> RiskDecision:
        cfg = self.cfg
        checks: list[Check] = []

        def check(name: str, ok: bool, detail: str) -> bool:
            checks.append(Check(name, bool(ok), detail))
            return bool(ok)

        decision = RiskDecision(approved=False, checks=checks, config=cfg.to_dict())

        check("kill_switch", not ctx.kill_switch, "kill switch engaged" if ctx.kill_switch else "off")
        check("connection", ctx.connected, "connected" if ctx.connected else "terminal/trade server not connected")
        check("reconciled", ctx.reconciled, "positions reconciled" if ctx.reconciled else "account state not reconciled since (re)connect")
        acct, sym, tick = ctx.account, ctx.symbol, ctx.tick
        if acct is None or sym is None or tick is None:
            check("inputs", False, f"missing {'account' if acct is None else 'symbol info' if sym is None else 'tick'}")
            return decision

        # --- mode / account type
        if ctx.mode is AgentMode.ANALYSIS:
            check("mode", False, "ANALYSIS mode never trades")
        elif ctx.mode is AgentMode.DEMO:
            check("mode", acct.kind is AccountKind.DEMO, f"DEMO mode requires a demo account, terminal reports {acct.kind.value}")
        else:
            check("mode", acct.kind is AccountKind.REAL and ctx.live_enabled, f"LIVE requires REAL account and explicit enablement (account={acct.kind.value}, enabled={ctx.live_enabled})")
        check("trading_permissions", acct.trade_allowed and acct.trade_expert, f"account trade_allowed={acct.trade_allowed}, expert={acct.trade_expert}")
        check("symbol_trade_mode", sym.trade_mode == SymbolTradeMode.FULL or (sym.trade_mode == SymbolTradeMode.LONGONLY and proposal.side is Side.BUY) or (sym.trade_mode == SymbolTradeMode.SHORTONLY and proposal.side is Side.SELL), f"symbol trade mode {sym.trade_mode}")

        # --- quotes
        quote_ok = tick.is_valid()
        check("quote_valid", quote_ok, f"bid {tick.bid} ask {tick.ask}")
        check("quote_fresh", ctx.quote_fresh, "quote updated recently" if ctx.quote_fresh else f"no quote change within {cfg.max_quote_age_s}s")
        if not quote_ok:
            return decision
        spread = tick.spread
        spread_points = spread / sym.point if sym.point else float("inf")
        cap = cfg.max_spread_points.get(sym.name)
        if cap is not None:
            check("spread_cap", spread_points <= cap, f"spread {spread_points:.0f} pts (cap {cap})")
        median = self.quotes.median_spread(sym.name)
        if median:
            check("spread_anomaly", spread <= cfg.spread_anomaly_factor * median, f"spread {spread_points:.0f} pts vs median {median / sym.point:.0f} pts")
        if ctx.entry_atr:
            check("spread_vs_atr", spread <= cfg.max_spread_atr_ratio * ctx.entry_atr, f"spread {spread / ctx.entry_atr:.2f} ATR (max {cfg.max_spread_atr_ratio})")

        # --- stops geometry at the actual execution price
        side = proposal.side
        entry = tick.ask if side is Side.BUY else tick.bid
        sl, tp = proposal.stop_loss, proposal.take_profit
        if not check("stop_loss_present", sl > 0, "stop loss is mandatory"):
            return decision
        risk_dist = (entry - sl) * side.sign
        reward_dist = (tp - entry) * side.sign
        if not check("stop_side", risk_dist > 0, f"SL {sl} on the {'wrong' if risk_dist <= 0 else 'correct'} side of entry {entry}"):
            return decision
        check("take_profit_side", tp > 0 and reward_dist > 0, f"TP {tp} vs entry {entry}")
        min_stop = max(sym.trade_stops_level, sym.trade_freeze_level) * sym.point
        check("stops_level", risk_dist >= min_stop and reward_dist >= min_stop, f"SL/TP distance {risk_dist:.5f}/{reward_dist:.5f} vs broker minimum {min_stop:.5f}")
        rr = reward_dist / risk_dist if risk_dist > 0 else 0.0
        check("risk_reward", rr >= cfg.min_rr, f"RR {rr:.2f} at execution price (min {cfg.min_rr})")

        # --- portfolio
        positions = ctx.positions
        check("max_positions", len(positions) < cfg.max_open_positions, f"{len(positions)} open (max {cfg.max_open_positions})")
        same_symbol = [p for p in positions if p.symbol == sym.name]
        check("one_position_per_symbol", len(same_symbol) < cfg.max_positions_per_symbol, f"{len(same_symbol)} open on {sym.name} (no grids/averaging)")
        risks = [self.position_risk(p) for p in positions]
        undefined = [p.ticket for p, r in zip(positions, risks) if r is None]
        if undefined and cfg.block_if_foreign_position_without_sl:
            check("open_risk_defined", False, f"positions without SL or uncomputable risk: {undefined}")
        open_risk = sum(r for r in risks if r is not None)
        remaining = [self.position_risk(p, from_current=True) for p in positions]
        remaining_risk = sum(r for r in remaining if r is not None)

        # --- daily loss
        equity = acct.equity
        if not check("equity_positive", equity > 0, f"equity {equity}"):
            return decision
        day = self.daily.update(acct, now, realized_today=realized_today)
        day_loss_pct = self.daily.loss_pct(day, acct)
        decision.day_loss_pct = day_loss_pct
        check("daily_loss_lock", not day.locked, day.locked_reason or f"day loss {day_loss_pct:.2f}% (limit {cfg.daily_loss_limit_pct}%)")

        # --- position size
        lpl, lpl_detail = self.loss_per_lot(side, sym, entry, sl)
        decision.loss_per_lot = lpl
        if not check("risk_computable", lpl is not None, lpl_detail):
            return decision
        risk_budget = equity * cfg.risk_per_trade_pct / 100
        raw_volume = risk_budget / lpl
        max_vol = min(sym.volume_max, sym.volume_limit) if sym.volume_limit > 0 else sym.volume_max
        volume = floor_to_step(min(raw_volume, max_vol), sym.volume_step)
        if not check("volume_min", volume >= sym.volume_min - 1e-12, f"sized volume {raw_volume:.4f} -> {volume} (min {sym.volume_min}, step {sym.volume_step}); never rounded up"):
            return decision
        risk_money = volume * lpl
        risk_pct = risk_money / equity * 100
        check("risk_per_trade", risk_money <= risk_budget * 1.0001, f"risk {risk_money:.2f} = {risk_pct:.3f}% (max {cfg.risk_per_trade_pct}%)")

        open_risk_pct = (open_risk + risk_money) / equity * 100
        decision.open_risk_pct = open_risk_pct
        check("total_open_risk", open_risk_pct <= cfg.max_total_open_risk_pct + 1e-9, f"open risk after trade {open_risk_pct:.3f}% (max {cfg.max_total_open_risk_pct}%)")
        if cfg.daily_limit_includes_open_risk:
            worst = day.start_equity - equity + remaining_risk + risk_money
            worst_pct = worst / day.start_equity * 100 if day.start_equity > 0 else 100
            check("daily_worst_case", worst_pct <= cfg.daily_loss_limit_pct + 1e-9, f"if every stop is hit today's loss would be {worst_pct:.2f}% (limit {cfg.daily_loss_limit_pct}%)")

        # No increase of risk after a loss (anti-martingale guard).
        last = self.journal.last_closed_execution()
        # Sizing is fixed-fractional of *current* equity and never rounds up, so
        # risk money shrinks after a loss; this also blocks raising the
        # configured percentage right after a losing trade.
        if last is not None and (last.realized_pnl or 0) < 0:
            prev_pct = float(last.risk_snapshot.get("config", {}).get("risk_per_trade_pct", cfg.risk_per_trade_pct))
            check("no_increase_after_loss", cfg.risk_per_trade_pct <= prev_pct + 1e-9, f"configured risk {cfg.risk_per_trade_pct}% vs {prev_pct}% on the previous losing trade")

        # --- margin
        try:
            margin = self.gateway.calc_margin(side, sym.name, volume, entry)
        except Exception as exc:  # noqa: BLE001
            margin = None
            check("margin_computable", False, f"order_calc_margin failed: {exc}")
        if margin is None:
            check("margin_computable", False, "order_calc_margin returned nothing")
        else:
            used_after = acct.margin + margin
            check("free_margin", margin < acct.margin_free, f"required {margin:.2f}, free {acct.margin_free:.2f}")
            check("margin_usage", used_after / equity * 100 <= cfg.max_margin_usage_pct, f"margin after trade {used_after / equity * 100:.1f}% of equity (max {cfg.max_margin_usage_pct}%)")
            level_after = equity / used_after * 100 if used_after > 0 else float("inf")
            check("margin_level", level_after >= cfg.min_margin_level_pct, f"margin level after trade {level_after:.0f}% (min {cfg.min_margin_level_pct}%)")

        decision.volume = volume
        decision.risk_money = risk_money
        decision.risk_pct = risk_pct
        decision.approved = all(c.passed for c in checks)
        if decision.approved:
            decision.approval = self._sign(proposal, volume, risk_money, risk_pct, entry)
        return decision
