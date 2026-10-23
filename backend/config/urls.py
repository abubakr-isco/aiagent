from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.accounts.views import LoginView, LogoutView, MeView
from apps.analytics.views import BacktestViewSet, ReportView
from apps.audit.views import AuditViewSet, EventViewSet
from apps.execution.views import (
    CommandHistoryViewSet,
    CommandView,
    ConnectionView,
    HealthView,
    ModeView,
    PositionsView,
    StatusView,
    TradeViewSet,
)
from apps.market_data.views import SnapshotViewSet, SymbolViewSet
from apps.risk.views import RiskSettingsView, RiskStateView
from apps.strategies.views import PredictionQualityViewSet, SignalViewSet, StrategyViewSet

router = DefaultRouter()
router.register("signals", SignalViewSet, basename="signal")
router.register("trades", TradeViewSet, basename="trade")
router.register("strategies", StrategyViewSet, basename="strategy")
router.register("market/snapshots", SnapshotViewSet, basename="snapshot")
router.register("market/symbols", SymbolViewSet, basename="symbol")
router.register("backtests", BacktestViewSet, basename="backtest")
router.register("events", EventViewSet, basename="event")
router.register("audit", AuditViewSet, basename="audit")
router.register("control/history", CommandHistoryViewSet, basename="command-history")
router.register("ai/quality", PredictionQualityViewSet, basename="ai-quality")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view()),
    path("api/auth/login/", LoginView.as_view()),
    path("api/auth/logout/", LogoutView.as_view()),
    path("api/auth/me/", MeView.as_view()),
    path("api/status/", StatusView.as_view()),
    path("api/control/command/", CommandView.as_view()),
    path("api/control/mode/", ModeView.as_view()),
    path("api/positions/", PositionsView.as_view()),
    path("api/risk/settings/", RiskSettingsView.as_view()),
    path("api/risk/state/", RiskStateView.as_view()),
    path("api/connection/", ConnectionView.as_view()),
    path("api/analytics/report/", ReportView.as_view()),
    path("api/", include(router.urls)),
]
