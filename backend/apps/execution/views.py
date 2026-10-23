from __future__ import annotations

import os
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import ADMIN, IsAdmin, IsOperator, IsViewer, ReadViewerWriteAdmin, has_role
from apps.audit.models import SystemEvent
from apps.audit.services import audit
from apps.strategies.models import Signal, StrategyConfig

from .models import AgentCommand, AgentControl, ConnectionSettings, Execution

CLOSE_ALL_PHRASE = "CLOSE AGENT POSITIONS"


class ExecutionSerializer(serializers.ModelSerializer):
    source = serializers.CharField(read_only=True)

    class Meta:
        model = Execution
        fields = "__all__"


class CommandSerializer(serializers.ModelSerializer):
    requested_by = serializers.StringRelatedField()

    class Meta:
        model = AgentCommand
        fields = ["id", "command", "params", "requested_by", "created_at", "status", "result", "processed_at"]


def runner_alive(control: AgentControl) -> bool:
    if control.heartbeat_at is None:
        return False
    interval = ConnectionSettings.load().loop_interval_s
    return timezone.now() - control.heartbeat_at < timedelta(seconds=max(30, interval * 4))


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        return Response({"ok": True})


class StatusView(APIView):
    permission_classes = [IsViewer]

    def get(self, request):
        c = AgentControl.load()
        alive = runner_alive(c)
        strat = StrategyConfig.objects.first()
        recent_signals = Signal.objects.order_by("-created_at")[:10]
        recent_trades = Execution.objects.exclude(status__in=["REJECTED", "FAILED"]).order_by("-created_at")[:10]
        events = SystemEvent.objects.order_by("-time")[:20]
        account = c.account or {}
        day = c.day or {}
        drawdown_pct = None
        if account.get("balance"):
            drawdown_pct = round(max(0.0, (account["balance"] - account["equity"]) / account["balance"] * 100), 3)
        return Response({
            "runner_alive": alive,
            "heartbeat_at": c.heartbeat_at,
            "connected": c.connected and alive,
            "reconciled": c.reconciled,
            "gateway": c.gateway,
            "is_mock": c.gateway == "mock",
            "mode": c.mode,
            "state": c.state,
            "kill_switch": c.kill_switch,
            "blocked_reasons": c.blocked_reasons if alive else ["agent runner not running"],
            "account": account,
            "day": day,
            "floating_drawdown_pct": drawdown_pct,
            "positions": c.positions,
            "orders": c.orders,
            "last_error": c.last_error,
            "allow_live_env": settings.ALLOW_LIVE_TRADING,
            "live_confirmed_at": c.live_confirmed_at,
            "strategy": {"id": strat.strategy_id, "version": strat.version, "validation_status": strat.validation_status} if strat else None,
            "recent_signals": [
                {"signal_id": s.signal_id, "symbol": s.symbol, "side": s.side, "status": s.status, "entry": s.entry,
                 "stop_loss": s.stop_loss, "take_profit": s.take_profit, "created_at": s.created_at, "ai_score": s.ai_score}
                for s in recent_signals
            ],
            "recent_trades": ExecutionSerializer(recent_trades, many=True).data,
            "events": [{"time": e.time, "level": e.level, "category": e.category, "message": e.message} for e in events],
        })


class CommandView(APIView):
    """Queue a control command for the runner. Every command is audited."""

    permission_classes = [IsOperator]
    OPERATOR_COMMANDS = {"start", "pause", "stop", "emergency_stop"}
    ADMIN_COMMANDS = {"reset_emergency", "close_agent_positions"}

    def get(self, request):
        return Response(CommandSerializer(AgentCommand.objects.all()[:50], many=True).data)

    def post(self, request):
        command = str(request.data.get("command", ""))
        params: dict = {}
        if command not in self.OPERATOR_COMMANDS | self.ADMIN_COMMANDS:
            return Response({"detail": f"unknown command {command!r}"}, status=status.HTTP_400_BAD_REQUEST)
        if command in self.ADMIN_COMMANDS and not has_role(request.user, ADMIN):
            return Response({"detail": "admin role required"}, status=status.HTTP_403_FORBIDDEN)
        if command == "emergency_stop":
            params["reason"] = str(request.data.get("reason", "dashboard emergency stop"))[:200]
        if command == "close_agent_positions":
            if request.data.get("confirm") != CLOSE_ALL_PHRASE:
                return Response({"detail": f"type '{CLOSE_ALL_PHRASE}' to confirm; closing may realise losses"}, status=status.HTTP_400_BAD_REQUEST)
            params["confirmed"] = True
        cmd = AgentCommand.objects.create(command=command, params=params, requested_by=request.user)
        audit(request, f"command.{command}", target=f"AgentCommand:{cmd.pk}", after=params)
        return Response(CommandSerializer(cmd).data, status=status.HTTP_202_ACCEPTED)


class ModeView(APIView):
    """Mode changes are admin-only; LIVE needs env flag, password, phrase, validated strategy and a REAL account."""

    permission_classes = [IsAdmin]

    def post(self, request):
        mode = str(request.data.get("mode", "")).upper()
        if mode not in ("ANALYSIS", "DEMO", "LIVE"):
            return Response({"detail": "mode must be ANALYSIS, DEMO or LIVE"}, status=status.HTTP_400_BAD_REQUEST)
        params = {"mode": mode}
        if mode == "LIVE":
            problems = []
            if not settings.ALLOW_LIVE_TRADING:
                problems.append("ALLOW_LIVE_TRADING is not enabled on the server")
            if request.data.get("confirm") != settings.LIVE_CONFIRMATION_PHRASE:
                problems.append(f"confirmation phrase must be exactly '{settings.LIVE_CONFIRMATION_PHRASE}'")
            if authenticate(request, username=request.user.username, password=str(request.data.get("password", ""))) is None:
                problems.append("password re-authentication failed")
            strat = StrategyConfig.objects.first()
            if strat is None or strat.validation_status != "PASSED":
                problems.append("strategy has not PASSED walk-forward validation on real data")
            kind = (AgentControl.load().account or {}).get("kind")
            if kind != "REAL":
                problems.append(f"terminal reports account type {kind or 'UNKNOWN'}, LIVE requires REAL")
            if problems:
                audit(request, "mode.live_refused", target="AgentControl", details_problems=problems)
                return Response({"detail": "LIVE refused", "problems": problems}, status=status.HTTP_400_BAD_REQUEST)
            params["live_confirmed"] = True
        before = AgentControl.load().mode
        cmd = AgentCommand.objects.create(command="set_mode", params=params, requested_by=request.user)
        audit(request, "mode.change_requested", target="AgentControl", before={"mode": before}, after=params)
        return Response(CommandSerializer(cmd).data, status=status.HTTP_202_ACCEPTED)


class PositionsView(APIView):
    permission_classes = [IsViewer]

    def get(self, request):
        c = AgentControl.load()
        return Response({"positions": c.positions, "orders": c.orders, "as_of": c.heartbeat_at, "runner_alive": runner_alive(c)})


class TradeViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsViewer]
    serializer_class = ExecutionSerializer

    def get_queryset(self):
        qs = Execution.objects.all()
        q = self.request.query_params
        if q.get("source") == "LIVE":
            qs = qs.filter(account_kind="REAL")
        elif q.get("source") == "DEMO":
            qs = qs.exclude(account_kind="REAL")
        if q.get("status"):
            qs = qs.filter(status=q["status"])
        if q.get("symbol"):
            qs = qs.filter(symbol=q["symbol"])
        return qs


class ConnectionSettingsSerializer(serializers.ModelSerializer):
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = ConnectionSettings
        fields = ["gateway", "bridge_url", "loop_interval_s", "bars_per_timeframe", "ai_min_quality", "updated_by", "updated_at"]

    def validate_loop_interval_s(self, v):
        if not 2 <= v <= 3600:
            raise serializers.ValidationError("must be between 2 and 3600 seconds")
        return v

    def validate_ai_min_quality(self, v):
        if not 0 <= v <= 1:
            raise serializers.ValidationError("must be in [0, 1]")
        return v

    def validate_bridge_url(self, v):
        if v and not v.startswith("https://") and not v.startswith("http://127.0.0.1") and not v.startswith("http://localhost"):
            raise serializers.ValidationError("bridge must use https:// unless it is on localhost (tunnel)")
        return v


class ConnectionView(APIView):
    permission_classes = [ReadViewerWriteAdmin]

    def _payload(self):
        c = AgentControl.load()
        return {
            "settings": ConnectionSettingsSerializer(ConnectionSettings.load()).data,
            "status": {"runner_alive": runner_alive(c), "connected": c.connected, "gateway": c.gateway, "account": c.account,
                       "heartbeat_at": c.heartbeat_at, "last_error": c.last_error},
            # Only whether secrets are configured - never their values.
            "secrets_configured": {
                "MT5_LOGIN": bool(os.environ.get("MT5_LOGIN")), "MT5_PASSWORD": bool(os.environ.get("MT5_PASSWORD")),
                "MT5_SERVER": bool(os.environ.get("MT5_SERVER")), "MT5_BRIDGE_TOKEN": bool(os.environ.get("MT5_BRIDGE_TOKEN")),
            },
            "restart_required_for": ["gateway", "bridge_url"],
        }

    def get(self, request):
        return Response(self._payload())

    def put(self, request):
        obj = ConnectionSettings.load()
        before = ConnectionSettingsSerializer(obj).data
        ser = ConnectionSettingsSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        audit(request, "connection.update", target="ConnectionSettings", before=before, after=ser.data)
        return Response(self._payload())


class CommandHistoryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsViewer]
    serializer_class = CommandSerializer
    queryset = AgentCommand.objects.all()
