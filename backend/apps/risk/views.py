from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsViewer, ReadViewerWriteAdmin
from apps.audit.services import audit
from trading_engine.config import RiskConfig

from .models import DailyRiskState, RiskSettings


class RiskSettingsSerializer(serializers.ModelSerializer):
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = RiskSettings
        fields = RiskSettings.FIELDS + ["updated_by", "updated_at"]

    def validate(self, attrs):
        merged = {**self.instance.as_dict(), **attrs} if self.instance else attrs
        errors = RiskConfig(**merged).validate()
        if errors:
            raise serializers.ValidationError({"risk": errors})
        return attrs


class RiskSettingsView(APIView):
    """Limits are only changed by an admin through this audited endpoint - never by the AI."""

    permission_classes = [ReadViewerWriteAdmin]

    def get(self, request):
        return Response(RiskSettingsSerializer(RiskSettings.load()).data)

    def put(self, request):
        obj = RiskSettings.load()
        before = obj.as_dict()
        ser = RiskSettingsSerializer(obj, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save(updated_by=request.user)
        audit(request, "risk.update", target="RiskSettings", before=before, after=obj.as_dict())
        return Response(ser.data)


class RiskStateView(APIView):
    permission_classes = [IsViewer]

    def get(self, request):
        days = DailyRiskState.objects.all()[:30]
        return Response([{"day": d.day, "start_equity": d.start_equity, "locked": d.locked, "locked_reason": d.locked_reason} for d in days])
