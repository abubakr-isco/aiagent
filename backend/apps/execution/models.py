from django.conf import settings
from django.db import models


class Singleton(models.Model):
    class Meta:
        abstract = True

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ConnectionSettings(Singleton):
    """Non-secret connection parameters. Credentials live only in environment variables."""

    GATEWAYS = [("mock", "Mock MT5 (simulation)"), ("mt5", "Local MT5 terminal (Windows)"), ("remote", "Remote MT5 bridge")]

    gateway = models.CharField(max_length=10, choices=GATEWAYS, default="mock")
    bridge_url = models.CharField(max_length=200, blank=True)
    loop_interval_s = models.FloatField(default=15.0)
    bars_per_timeframe = models.PositiveIntegerField(default=600)
    ai_min_quality = models.FloatField(default=0.55)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)


class AgentControl(Singleton):
    """Desired state written by the API, actual state reported by the runner."""

    MODES = [("ANALYSIS", "Analysis"), ("DEMO", "Demo"), ("LIVE", "Live")]

    # reported by the runner
    mode = models.CharField(max_length=10, choices=MODES, default="ANALYSIS")
    state = models.CharField(max_length=20, default="STOPPED")
    kill_switch = models.BooleanField(default=False)
    live_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    live_confirmed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    connected = models.BooleanField(default=False)
    reconciled = models.BooleanField(default=False)
    blocked_reasons = models.JSONField(default=list)
    account = models.JSONField(null=True, blank=True)
    positions = models.JSONField(default=list)
    orders = models.JSONField(default=list)
    day = models.JSONField(default=dict)
    last_error = models.TextField(blank=True)
    gateway = models.CharField(max_length=20, blank=True)
    runner = models.CharField(max_length=100, blank=True)


class AgentCommand(models.Model):
    COMMANDS = [(c, c) for c in ("start", "pause", "stop", "emergency_stop", "reset_emergency", "set_mode", "close_agent_positions")]
    STATUS = [(s, s) for s in ("PENDING", "DONE", "FAILED")]

    command = models.CharField(max_length=32, choices=COMMANDS)
    params = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS, default="PENDING", db_index=True)
    result = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Execution(models.Model):
    """Order intent, broker result and the resulting trade (one per signal)."""

    signal_id = models.CharField(max_length=40, unique=True)
    client_key = models.CharField(max_length=40, db_index=True)
    symbol = models.CharField(max_length=32, db_index=True)
    side = models.CharField(max_length=4)
    requested_volume = models.FloatField()
    requested_price = models.FloatField()
    sl = models.FloatField()
    tp = models.FloatField()
    status = models.CharField(max_length=20, db_index=True)
    strategy_id = models.CharField(max_length=64, blank=True)
    strategy_version = models.CharField(max_length=20, blank=True)
    risk_snapshot = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    retcode = models.IntegerField(null=True, blank=True)
    message = models.TextField(blank=True)
    order_ticket = models.BigIntegerField(null=True, blank=True)
    deal_ticket = models.BigIntegerField(null=True, blank=True)
    position_ticket = models.BigIntegerField(null=True, blank=True, db_index=True)
    filled_volume = models.FloatField(default=0.0)
    fill_price = models.FloatField(null=True, blank=True)
    exit_price = models.FloatField(null=True, blank=True)
    realized_pnl = models.FloatField(null=True, blank=True)
    unrealized_pnl = models.FloatField(null=True, blank=True)
    commission = models.FloatField(default=0.0)
    swap = models.FloatField(default=0.0)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=200, blank=True)
    account_kind = models.CharField(max_length=10, blank=True, db_index=True)  # DEMO / REAL -> report separation
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def source(self) -> str:
        return "LIVE" if self.account_kind == "REAL" else "DEMO"
