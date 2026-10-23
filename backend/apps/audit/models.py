from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Who changed what. Written for every trading command and settings change."""

    time = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    actor_name = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=64, db_index=True)
    target = models.CharField(max_length=128, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-time"]


class SystemEvent(models.Model):
    """Agent journal: connection events, executions, risk events, errors."""

    LEVELS = [(x, x) for x in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")]

    time = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=10, choices=LEVELS, db_index=True)
    category = models.CharField(max_length=32, db_index=True)
    message = models.TextField()
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-time"]
