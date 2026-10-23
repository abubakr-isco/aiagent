from django.contrib import admin

from .models import AgentCommand, AgentControl, ConnectionSettings, Execution


@admin.register(Execution)
class ExecutionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "symbol", "side", "status", "filled_volume", "fill_price", "realized_pnl", "account_kind")
    list_filter = ("status", "symbol", "account_kind")
    search_fields = ("signal_id", "client_key")

    def has_change_permission(self, request, obj=None):
        return False  # trade records are written by the agent only


@admin.register(AgentCommand)
class AgentCommandAdmin(admin.ModelAdmin):
    list_display = ("created_at", "command", "status", "requested_by", "result")

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(AgentControl)
admin.site.register(ConnectionSettings)
