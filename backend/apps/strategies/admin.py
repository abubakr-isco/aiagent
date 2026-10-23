from django.contrib import admin

from .models import Signal, StrategyConfig

admin.site.register(StrategyConfig)


@admin.register(Signal)
class SignalAdmin(admin.ModelAdmin):
    list_display = ("created_at", "symbol", "side", "status", "entry", "stop_loss", "take_profit", "ai_score")
    list_filter = ("status", "symbol", "side")
