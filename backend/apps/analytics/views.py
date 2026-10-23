from rest_framework import mixins, serializers, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import OPERATOR, IsViewer, has_role
from apps.audit.services import audit
from trading_engine.backtester import CostModel

from .models import BacktestRun
from .services import performance_report, safe_data_path, start_backtest


class BacktestListSerializer(serializers.ModelSerializer):
    class Meta:
        model = BacktestRun
        exclude = ["result"]


class BacktestDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = BacktestRun
        fields = "__all__"


class BacktestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BacktestRun
        fields = ["kind", "symbol", "strategy_id", "params", "param_grid", "costs", "data_source", "data_path", "base_timeframe",
                  "date_from", "date_to", "starting_equity"]

    def validate_costs(self, value):
        try:
            CostModel(**value)
        except TypeError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate(self, attrs):
        if attrs.get("data_source") == "csv":
            try:
                safe_data_path(attrs.get("data_path", ""))
            except ValueError as exc:
                raise serializers.ValidationError({"data_path": str(exc)}) from exc
        if len(attrs.get("param_grid") or []) > 12:
            raise serializers.ValidationError({"param_grid": "at most 12 parameter sets (limits overfitting and runtime)"})
        return attrs


class BacktestViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsViewer]
    queryset = BacktestRun.objects.all()

    def get_serializer_class(self):
        if self.action == "create":
            return BacktestCreateSerializer
        return BacktestDetailSerializer if self.action == "retrieve" else BacktestListSerializer

    def create(self, request, *args, **kwargs):
        if not has_role(request.user, OPERATOR):
            return Response({"detail": "operator role required"}, status=status.HTTP_403_FORBIDDEN)
        ser = BacktestCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        run = ser.save(created_by=request.user)
        audit(request, "backtest.create", target=f"BacktestRun:{run.pk}", after=ser.data)
        start_backtest(run)
        run.refresh_from_db()
        return Response(BacktestListSerializer(run).data, status=status.HTTP_201_CREATED)


class ReportView(APIView):
    permission_classes = [IsViewer]

    def get(self, request):
        source = request.query_params.get("source", "DEMO").upper()
        if source not in ("BACKTEST", "DEMO", "LIVE"):
            return Response({"detail": "source must be BACKTEST, DEMO or LIVE"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(performance_report(source, request.query_params.get("group_by")))
