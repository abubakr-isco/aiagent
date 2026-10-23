from django.db import models


class SymbolConfig(models.Model):
    """Broker symbol names differ (EURUSD, EURUSD.m, EURUSDpro...) - configure the exact name."""

    name = models.CharField(max_length=32, unique=True)
    enabled = models.BooleanField(default=True)
    max_spread_points = models.PositiveIntegerField(null=True, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class MarketSnapshot(models.Model):
    """Latest multi-timeframe analysis per symbol (overwritten every cycle)."""

    symbol = models.CharField(max_length=32, unique=True)
    time = models.DateTimeField()
    bid = models.FloatField(null=True)
    ask = models.FloatField(null=True)
    data = models.JSONField(default=dict)
    evaluation = models.JSONField(default=dict)  # last strategy evaluation incl. rejection reasons
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol"]
