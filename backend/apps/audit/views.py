from rest_framework import serializers, viewsets

from apps.accounts.permissions import IsAdmin, IsViewer

from .models import AuditLog, SystemEvent


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemEvent
        fields = "__all__"


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsViewer]
    serializer_class = EventSerializer

    def get_queryset(self):
        qs = SystemEvent.objects.all()
        q = self.request.query_params
        if q.get("level"):
            qs = qs.filter(level__in=q["level"].split(","))
        if q.get("category"):
            qs = qs.filter(category=q["category"])
        if q.get("search"):
            qs = qs.filter(message__icontains=q["search"])
        return qs


class AuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = "__all__"


class AuditViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAdmin]
    serializer_class = AuditSerializer
    queryset = AuditLog.objects.all()
