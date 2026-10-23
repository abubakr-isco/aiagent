from django.conf import settings
from django.db import models

from trading_engine.config import RiskConfig

_DEFAULT = RiskConfig()


class RiskSettings(models.Model):
    """Singleton (pk=1). Values are starting points for DEMO tests, not 'safe' constants."""

    risk_per_trade_pct = models.FloatField(default=_DEFAULT.risk_per_trade_pct)
    max_total_open_risk_pct = models.FloatField(default=_DEFAULT.max_total_open_risk_pct)
    daily_loss_limit_pct = models.FloatField(default=_DEFAULT.daily_loss_limit_pct)
    daily_limit_includes_open_risk = models.BooleanField(default=_DEFAULT.daily_limit_includes_open_risk)
    max_open_positions = models.PositiveIntegerField(default=_DEFAULT.max_open_positions)
    min_rr = models.FloatField(default=_DEFAULT.min_rr)
    max_spread_atr_ratio = models.FloatField(default=_DEFAULT.max_spread_atr_ratio)
    spread_anomaly_factor = models.FloatField(default=_DEFAULT.spread_anomaly_factor)
    max_quote_age_s = models.FloatField(default=_DEFAULT.max_quote_age_s)
    max_margin_usage_pct = models.FloatField(default=_DEFAULT.max_margin_usage_pct)
    min_margin_level_pct = models.FloatField(default=_DEFAULT.min_margin_level_pct)
    risk_calc_tolerance_pct = models.FloatField(default=_DEFAULT.risk_calc_tolerance_pct)
    block_if_foreign_position_without_sl = models.BooleanField(default=_DEFAULT.block_if_foreign_position_without_sl)
    trading_day_utc_offset_hours = models.IntegerField(default=_DEFAULT.trading_day_utc_offset_hours)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    FIELDS = [
        "risk_per_trade_pct", "max_total_open_risk_pct", "daily_loss_limit_pct", "daily_limit_includes_open_risk",
        "max_open_positions", "min_rr", "max_spread_atr_ratio", "spread_anomaly_factor", "max_quote_age_s",
        "max_margin_usage_pct", "min_margin_level_pct", "risk_calc_tolerance_pct", "block_if_foreign_position_without_sl",
        "trading_day_utc_offset_hours",
    ]

    @classmethod
    def load(cls) -> "RiskSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}

    def to_config(self) -> RiskConfig:
        from apps.market_data.models import SymbolConfig

        caps = {s.name: s.max_spread_points for s in SymbolConfig.objects.filter(max_spread_points__isnull=False)}
        return RiskConfig(**self.as_dict(), max_spread_points=caps)


class DailyRiskState(models.Model):
    day = models.DateField(unique=True)
    start_equity = models.FloatField()
    locked = models.BooleanField(default=False)
    locked_reason = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-day"]
