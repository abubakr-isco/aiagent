from django.contrib import admin

from .models import AuditLog, SystemEvent


class ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnly):
    list_display = ("time", "actor_name", "action", "target", "ip")
    list_filter = ("action",)


@admin.register(SystemEvent)
class SystemEventAdmin(ReadOnly):
    list_display = ("time", "level", "category", "message")
    list_filter = ("level", "category")
