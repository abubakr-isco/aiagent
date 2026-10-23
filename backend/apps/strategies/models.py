from django.conf import settings
from django.db import models


class StrategyConfig(models.Model):
    VALIDATION = [("NOT_VALIDATED", "Not validated"), ("PASSED", "Passed"), ("FAILED", "Failed")]

    strategy_id = models.CharField(max_length=64, unique=True)
    version = models.CharField(max_length=20)
    params = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)
    validation_status = models.CharField(max_length=16, choices=VALIDATION, default="NOT_VALIDATED")
    validation_run = models.ForeignKey("analytics.BacktestRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.strategy_id} {self.version}"


class Signal(models.Model):
    signal_id = models.CharField(max_length=40, unique=True)
    strategy_id = models.CharField(max_length=64, db_index=True)
    strategy_version = models.CharField(max_length=20)
    symbol = models.CharField(max_length=32, db_index=True)
    side = models.CharField(max_length=4)
    timeframe = models.CharField(max_length=4)
    bar_time = models.DateTimeField()
    entry = models.FloatField()
    stop_loss = models.FloatField()
    take_profit = models.FloatField()
    rr = models.FloatField()
    atr = models.FloatField()
    checks = models.JSONField(default=list)
    features = models.JSONField(default=dict)
    ai = models.JSONField(default=dict)
    ai_score = models.FloatField(null=True)
    ai_approved = models.BooleanField(default=False)
    regime = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, db_index=True)
    note = models.TextField(blank=True)
    risk = models.JSONField(null=True, blank=True)  # full RiskDecision incl. config snapshot
    volume = models.FloatField(null=True)
    risk_money = models.FloatField(null=True)
    risk_pct = models.FloatField(null=True)
    mode = models.CharField(max_length=10)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class Prediction(models.Model):
    """AI score at decision time and the realised outcome, for calibration."""

    signal = models.OneToOneField(Signal, on_delete=models.CASCADE, related_name="prediction")
    score = models.FloatField()
    model_version = models.CharField(max_length=40)
    predicted_at = models.DateTimeField(auto_now_add=True)
    outcome = models.IntegerField(null=True, blank=True)  # 1 win, 0 loss
    r_multiple = models.FloatField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
