from django.conf import settings
from django.db import models


class BacktestRun(models.Model):
    KINDS = [("SINGLE", "Single backtest"), ("WALK_FORWARD", "Walk-forward")]
    SOURCES = [("csv", "CSV exported from MT5"), ("gateway", "History from connected terminal"), ("synthetic", "Synthetic (pipeline test only)")]
    STATUS = [(s, s) for s in ("PENDING", "RUNNING", "DONE", "FAILED")]

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="PENDING", db_index=True)
    kind = models.CharField(max_length=14, choices=KINDS, default="SINGLE")
    symbol = models.CharField(max_length=32)
    strategy_id = models.CharField(max_length=64, default="trend_pullback")
    strategy_version = models.CharField(max_length=20, blank=True)
    params = models.JSONField(default=dict, blank=True)
    param_grid = models.JSONField(default=list, blank=True)
    costs = models.JSONField(default=dict, blank=True)
    data_source = models.CharField(max_length=10, choices=SOURCES)
    data_path = models.CharField(max_length=300, blank=True)
    base_timeframe = models.CharField(max_length=4, default="M15")
    date_from = models.DateTimeField(null=True, blank=True)
    date_to = models.DateTimeField(null=True, blank=True)
    starting_equity = models.FloatField(default=10_000)
    metrics = models.JSONField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    validation_status = models.CharField(max_length=10, blank=True)
    reasons = models.JSONField(default=list, blank=True)
    synthetic = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
