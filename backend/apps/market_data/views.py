from rest_framework import serializers, viewsets

from apps.accounts.permissions import IsViewer, ReadViewerWriteAdmin
from apps.audit.services import audit

from .models import MarketSnapshot, SymbolConfig


class SnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketSnapshot
        fields = ["symbol", "time", "bid", "ask", "data", "evaluation", "updated_at"]


class SnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsViewer]
    serializer_class = SnapshotSerializer
    queryset = MarketSnapshot.objects.all()
    lookup_field = "symbol"


class SymbolSerializer(serializers.ModelSerializer):
    class Meta:
        model = SymbolConfig
        fields = ["id", "name", "enabled", "max_spread_points", "notes"]


class SymbolViewSet(viewsets.ModelViewSet):
    permission_classes = [ReadViewerWriteAdmin]
    serializer_class = SymbolSerializer
    queryset = SymbolConfig.objects.all()

    def perform_create(self, serializer):
        obj = serializer.save()
        audit(self.request, "symbol.create", target=obj.name, after=serializer.data)

    def perform_update(self, serializer):
        before = SymbolSerializer(serializer.instance).data
        obj = serializer.save()
        audit(self.request, "symbol.update", target=obj.name, before=before, after=serializer.data)

    def perform_destroy(self, instance):
        audit(self.request, "symbol.delete", target=instance.name, before=SymbolSerializer(instance).data)
        instance.delete()
