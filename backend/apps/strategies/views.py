from dataclasses import fields

from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response

from apps.accounts.permissions import IsViewer, ReadViewerWriteAdmin
from apps.audit.services import audit
from trading_engine.config import StrategyParams

from .models import Prediction, Signal, StrategyConfig

_PARAM_NAMES = {f.name for f in fields(StrategyParams)}


class StrategySerializer(serializers.ModelSerializer):
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = StrategyConfig
        fields = ["id", "strategy_id", "version", "params", "enabled", "validation_status", "validation_run", "updated_by", "updated_at"]
        read_only_fields = ["strategy_id", "version", "validation_status", "validation_run"]

    def validate_params(self, value):
        unknown = set(value) - _PARAM_NAMES
        if unknown:
            raise serializers.ValidationError(f"unknown parameters: {sorted(unknown)}")
        merged = {**(self.instance.params if self.instance else {}), **value}
        try:
            p = StrategyParams.from_dict(merged)
        except TypeError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        if p.rr_target < 1.0 or p.max_sl_atr <= p.min_sl_atr or p.pullback_ema not in ("fast", "mid"):
            raise serializers.ValidationError("inconsistent parameters (rr_target >= 1, max_sl_atr > min_sl_atr, pullback_ema fast|mid)")
        return p.to_dict()


class StrategyViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    permission_classes = [ReadViewerWriteAdmin]
    serializer_class = StrategySerializer
    queryset = StrategyConfig.objects.all()

    def perform_update(self, serializer):
        before = {"params": serializer.instance.params, "enabled": serializer.instance.enabled}
        changed_params = "params" in serializer.validated_data and serializer.validated_data["params"] != serializer.instance.params
        extra = {"validation_status": "NOT_VALIDATED", "validation_run": None} if changed_params else {}
        obj = serializer.save(updated_by=self.request.user, **extra)
        audit(self.request, "strategy.update", target=obj.strategy_id, before=before, after={"params": obj.params, "enabled": obj.enabled},
              note="parameter change resets validation" if changed_params else "")


class SignalSerializer(serializers.ModelSerializer):
    outcome = serializers.SerializerMethodField()

    class Meta:
        model = Signal
        fields = "__all__"

    def get_outcome(self, obj):
        try:
            p = obj.prediction
        except Prediction.DoesNotExist:
            return None
        return {"score": p.score, "outcome": p.outcome, "r_multiple": p.r_multiple}


class SignalViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsViewer]
    serializer_class = SignalSerializer
    lookup_field = "signal_id"

    def get_queryset(self):
        qs = Signal.objects.all()
        q = self.request.query_params
        for key in ("symbol", "status", "side", "mode"):
            if q.get(key):
                qs = qs.filter(**{key: q[key]})
        return qs


class PredictionQualityViewSet(viewsets.ViewSet):
    permission_classes = [IsViewer]

    def list(self, request):
        from trading_engine.ai_decision_engine import brier_score

        resolved = list(Prediction.objects.filter(outcome__isnull=False).values_list("score", "outcome"))
        buckets: dict[str, list[int]] = {}
        for score, outcome in resolved:
            key = f"{int(score * 10) / 10:.1f}"
            buckets.setdefault(key, []).append(outcome)
        return Response({
            "resolved": len(resolved),
            "pending": Prediction.objects.filter(outcome__isnull=True).count(),
            "brier_score": brier_score([s for s, _ in resolved], [o for _, o in resolved]),
            "calibration": {k: {"n": len(v), "win_rate": sum(v) / len(v)} for k, v in sorted(buckets.items())},
        })
