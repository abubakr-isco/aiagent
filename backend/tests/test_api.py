import pytest
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.execution.models import AgentCommand, AgentControl
from apps.risk.models import RiskSettings
from apps.strategies.models import StrategyConfig

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


def test_health_is_public_everything_else_requires_auth():
    c = APIClient()
    assert c.get("/api/health/").status_code == 200
    for url in ("/api/status/", "/api/signals/", "/api/risk/settings/", "/api/events/", "/api/trades/", "/api/connection/"):
        assert c.get(url).status_code == 401, url


def test_login_returns_token_and_is_audited(users):
    c = APIClient()
    r = c.post("/api/auth/login/", {"username": "operator", "password": PASSWORD}, format="json")
    assert r.status_code == 200 and r.data["role"] == "operator"
    c.credentials(HTTP_AUTHORIZATION=f"Token {r.data['token']}")
    assert c.get("/api/auth/me/").data["username"] == "operator"
    assert c.post("/api/auth/login/", {"username": "operator", "password": "wrong"}, format="json").status_code == 401
    assert AuditLog.objects.filter(action="login").exists() and AuditLog.objects.filter(action="login_failed").exists()


def test_user_without_role_cannot_log_in_or_read(users, client_for):
    assert APIClient().post("/api/auth/login/", {"username": "norole", "password": PASSWORD}, format="json").status_code == 403
    assert client_for("norole").get("/api/status/").status_code == 403


def test_role_matrix_for_commands(client_for):
    assert client_for("viewer").post("/api/control/command/", {"command": "start"}, format="json").status_code == 403
    r = client_for("operator").post("/api/control/command/", {"command": "start"}, format="json")
    assert r.status_code == 202
    assert AgentCommand.objects.get(pk=r.data["id"]).status == "PENDING"
    assert client_for("operator").post("/api/control/command/", {"command": "reset_emergency"}, format="json").status_code == 403
    assert client_for("admin").post("/api/control/command/", {"command": "reset_emergency"}, format="json").status_code == 202
    assert client_for("operator").post("/api/control/command/", {"command": "rm -rf"}, format="json").status_code == 400
    assert AuditLog.objects.filter(action="command.start").count() == 1


def test_close_all_requires_admin_and_typed_confirmation(client_for):
    assert client_for("admin").post("/api/control/command/", {"command": "close_agent_positions"}, format="json").status_code == 400
    r = client_for("admin").post("/api/control/command/", {"command": "close_agent_positions", "confirm": "CLOSE AGENT POSITIONS"}, format="json")
    assert r.status_code == 202 and r.data["params"] == {"confirmed": True}


def test_risk_settings_validation_and_audit(client_for):
    c = client_for("admin")
    assert c.get("/api/risk/settings/").data["risk_per_trade_pct"] == 0.5
    r = c.put("/api/risk/settings/", {"risk_per_trade_pct": 5}, format="json")
    assert r.status_code == 400
    r = c.put("/api/risk/settings/", {"max_total_open_risk_pct": 0.1}, format="json")
    assert r.status_code == 400  # below per-trade risk
    r = c.put("/api/risk/settings/", {"risk_per_trade_pct": 0.25}, format="json")
    assert r.status_code == 200 and RiskSettings.load().risk_per_trade_pct == 0.25
    log = AuditLog.objects.get(action="risk.update")
    assert log.before["risk_per_trade_pct"] == 0.5 and log.after["risk_per_trade_pct"] == 0.25 and log.actor_name == "admin"
    assert client_for("operator").put("/api/risk/settings/", {"risk_per_trade_pct": 0.3}, format="json").status_code == 403


def test_strategy_param_change_resets_validation(client_for):
    from apps.execution.runner import ensure_defaults

    s = ensure_defaults()
    StrategyConfig.objects.filter(pk=s.pk).update(validation_status="PASSED")
    c = client_for("admin")
    assert c.patch(f"/api/strategies/{s.pk}/", {"params": {"martingale": True}}, format="json").status_code == 400
    r = c.patch(f"/api/strategies/{s.pk}/", {"params": {"rr_target": 2.5}}, format="json")
    assert r.status_code == 200, r.data
    s.refresh_from_db()
    assert s.params["rr_target"] == 2.5 and s.validation_status == "NOT_VALIDATED"


def test_live_mode_refused_without_every_precondition(client_for, settings):
    c = client_for("admin")
    r = c.post("/api/control/mode/", {"mode": "LIVE", "confirm": "ENABLE LIVE TRADING", "password": PASSWORD}, format="json")
    assert r.status_code == 400 and any("ALLOW_LIVE_TRADING" in p for p in r.data["problems"])
    settings.ALLOW_LIVE_TRADING = True
    r = c.post("/api/control/mode/", {"mode": "LIVE", "confirm": "yes", "password": "bad"}, format="json")
    problems = " ".join(r.data["problems"])
    assert "confirmation phrase" in problems and "password" in problems and "strategy" in problems and "REAL" in problems
    assert not AgentCommand.objects.filter(command="set_mode").exists()
    assert AuditLog.objects.filter(action="mode.live_refused").count() == 2
    assert client_for("operator").post("/api/control/mode/", {"mode": "DEMO"}, format="json").status_code == 403


def test_live_mode_request_when_all_preconditions_hold(client_for, settings):
    from apps.execution.runner import ensure_defaults

    settings.ALLOW_LIVE_TRADING = True
    s = ensure_defaults()
    StrategyConfig.objects.filter(pk=s.pk).update(validation_status="PASSED")
    AgentControl.objects.filter(pk=1).update(account={"kind": "REAL"})
    r = client_for("admin").post("/api/control/mode/", {"mode": "LIVE", "confirm": "ENABLE LIVE TRADING", "password": PASSWORD}, format="json")
    assert r.status_code == 202
    assert r.data["params"] == {"mode": "LIVE", "live_confirmed": True}


def test_connection_settings_never_expose_secrets(client_for, monkeypatch):
    monkeypatch.setenv("MT5_PASSWORD", "super-secret-pass")
    r = client_for("viewer").get("/api/connection/")
    assert r.status_code == 200
    assert r.data["secrets_configured"]["MT5_PASSWORD"] is True
    assert "super-secret-pass" not in str(r.data)
    assert client_for("viewer").put("/api/connection/", {"gateway": "mt5"}, format="json").status_code == 403
    bad = client_for("admin").put("/api/connection/", {"bridge_url": "http://1.2.3.4:8765"}, format="json")
    assert bad.status_code == 400
    ok = client_for("admin").put("/api/connection/", {"bridge_url": "https://vps.example:8765", "gateway": "remote"}, format="json")
    assert ok.status_code == 200 and AuditLog.objects.filter(action="connection.update").exists()


def test_backtest_api_synthetic_is_flagged_and_does_not_validate(client_for):
    from apps.execution.runner import ensure_defaults

    s = ensure_defaults()
    assert client_for("viewer").post("/api/backtests/", {}, format="json").status_code == 403
    r = client_for("operator").post("/api/backtests/", {"kind": "WALK_FORWARD", "symbol": "EURUSD", "data_source": "synthetic",
                                                          "base_timeframe": "M15", "params": {"_synthetic_bars": 9000},
                                                          "param_grid": [{"rr_target": 2.0}]}, format="json")
    assert r.status_code == 201, r.data
    detail = client_for("viewer").get(f"/api/backtests/{r.data['id']}/").data
    assert detail["status"] == "DONE", detail["error"]
    assert detail["synthetic"] and detail["validation_status"] == "FAILED"
    assert any("synthetic" in x for x in detail["reasons"])
    s.refresh_from_db()
    assert s.validation_status == "NOT_VALIDATED"  # synthetic runs never validate a strategy


def test_backtest_csv_path_traversal_rejected(client_for, settings, tmp_path):
    settings.DATA_DIR = tmp_path
    r = client_for("operator").post("/api/backtests/", {"symbol": "EURUSD", "data_source": "csv", "data_path": "../../etc/passwd"}, format="json")
    assert r.status_code == 400 and "data_path" in r.data


def test_backtest_from_mt5_csv(client_for, settings, tmp_path):
    from trading_engine.data import synthetic_ohlc

    df = synthetic_ohlc(periods=6000, minutes=15, seed=9, volatility=0.0007, trend_strength=0.00008)
    lines = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    for row in df.itertuples():
        lines.append(f"{row.time:%Y.%m.%d}\t{row.time:%H:%M:%S}\t{row.open}\t{row.high}\t{row.low}\t{row.close}\t{row.tick_volume}\t0\t{row.spread}")
    (tmp_path / "EURUSD_M15.csv").write_text("\n".join(lines))
    settings.DATA_DIR = tmp_path
    r = client_for("operator").post("/api/backtests/", {"symbol": "EURUSD", "data_source": "csv", "data_path": "EURUSD_M15.csv"}, format="json")
    assert r.status_code == 201
    run = client_for("viewer").get(f"/api/backtests/{r.data['id']}/").data
    assert run["status"] == "DONE", run["error"]
    assert run["metrics"]["trades"] >= 0
    assert "commission" in run["result"]["costs"]


def test_events_and_audit_permissions(client_for):
    from apps.audit.models import SystemEvent

    SystemEvent.objects.create(level="ERROR", category="execution", message="boom")
    SystemEvent.objects.create(level="INFO", category="agent", message="hello")
    r = client_for("viewer").get("/api/events/?level=ERROR")
    assert r.data["count"] == 1 and r.data["results"][0]["message"] == "boom"
    assert client_for("viewer").get("/api/audit/").status_code == 403
    assert client_for("admin").get("/api/audit/").status_code == 200
