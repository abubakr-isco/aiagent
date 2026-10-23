from __future__ import annotations

from datetime import datetime, timezone

import pytest
from django.contrib.auth.models import Group, User
from rest_framework.test import APIClient

from trading_engine.ai_decision_engine import AIDecisionEngine, Regime
from trading_engine.config import StrategyParams
from trading_engine.mock_mt5 import MockMT5Gateway
from trading_engine.models import Check, Side, Signal, StrategyEvaluation
from trading_engine.strategy_engine import Strategy

PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture(autouse=True)
def _sync_backtests(settings):
    settings.BACKTEST_SYNC = True


@pytest.fixture
def roles(db):
    return {name: Group.objects.get_or_create(name=name)[0] for name in ("viewer", "operator", "admin")}


def _user(roles, name, role):
    u = User.objects.create_user(name, password=PASSWORD)
    if role:
        u.groups.add(roles[role])
    return u


@pytest.fixture
def users(roles):
    return {r: _user(roles, r, r) for r in ("viewer", "operator", "admin")} | {"norole": _user(roles, "norole", None)}


@pytest.fixture
def client_for(users):
    def make(role: str) -> APIClient:
        c = APIClient()
        c.force_authenticate(users[role])
        return c

    return make


class AlwaysSignal(Strategy):
    """Pipeline stub: one BUY per symbol per bar with a valid SL/TP."""

    strategy_id = "trend_pullback"
    version = "test"

    def __init__(self):
        super().__init__(StrategyParams())

    def evaluate(self, snapshot, *, spread, now_utc):
        entry = snapshot.ask
        point = snapshot.point
        f = snapshot.frame("M15")
        sig = Signal(self.strategy_id, self.version, snapshot.symbol, Side.BUY, "M15",
                     f.bar_time if f else datetime(2024, 1, 1, tzinfo=timezone.utc), entry,
                     round(entry - 200 * point, 5), round(entry + 400 * point, 5), 0.001, checks=[Check("stub", True, "test")],
                     features={"h1_adx": 40, "h1_slope_atr": 1.5, "h1_structure_aligned": 1, "h4_aligned": 1, "m15_rsi_centered": 7,
                               "spread_risk_ratio": 0.0, "m15_atr_rank": 0.5})
        return StrategyEvaluation(snapshot.symbol, self.strategy_id, sig, sig.checks, Side.BUY)

    def exit_reason(self, side, snapshot, bars_held):
        return None


@pytest.fixture
def runner(db):
    from apps.execution.runner import AgentRunner
    from apps.market_data.models import SymbolConfig

    SymbolConfig.objects.create(name="EURUSD")
    mock = MockMT5Gateway(seed=2)
    r = AgentRunner(gateway=mock, sleep=lambda s: None, advance_mock=False)
    r.agent.strategy = AlwaysSignal()
    r.agent.ai = AIDecisionEngine(min_quality=0.0, allowed_regimes=(Regime.TREND, Regime.RANGE, Regime.HIGH_VOLATILITY, Regime.UNKNOWN))
    r.agent.executor.cfg = r.agent.executor.cfg.__class__(verify_attempts=2, verify_interval_s=0)
    r._versions = r._settings_versions()
    return r
