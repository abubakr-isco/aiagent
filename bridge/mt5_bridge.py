"""MT5 bridge: exposes the local MetaTrader 5 terminal to the agent over HTTP(S).

Run on the Windows machine/VPS where MT5 is installed and logged in::

    set MT5_LOGIN=...  & set MT5_PASSWORD=...  & set MT5_SERVER=...
    set BRIDGE_TOKEN=<random 32+ chars>
    set BRIDGE_ALLOW_REAL=false
    python -m uvicorn bridge.mt5_bridge:app --host 127.0.0.1 --port 8765 ^
        --ssl-keyfile key.pem --ssl-certfile cert.pem

Security model:

* Every request needs ``Authorization: Bearer <BRIDGE_TOKEN>`` (constant-time compare).
* Optional client IP allow-list (``BRIDGE_ALLOWED_IPS``, comma separated).
* Binds to localhost by default; expose it only through TLS, a VPN
  (WireGuard/Tailscale) or an SSH tunnel - never directly to the internet.
* Independent safety net: unless ``BRIDGE_ALLOW_REAL=true`` the bridge refuses
  ``order_send`` when the logged-in account is not a DEMO account, whatever
  the caller says.
* Only an explicit allow-list of gateway methods is callable.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from trading_engine import codec
from trading_engine.constants import Timeframe
from trading_engine.gateway import ConnectionLost, GatewayError, GatewayTimeout
from trading_engine.models import AccountKind, OrderRequest, Side

log = logging.getLogger("mt5_bridge")

ALLOWED_METHODS = {
    "connect", "disconnect", "terminal_status", "account_info", "symbols", "symbol_info", "symbol_select", "tick", "rates",
    "rates_range", "positions", "orders", "history_deals", "calc_profit", "calc_margin", "order_check", "order_send",
}


def _parse_dt(v: Any) -> datetime:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def create_app(gateway=None, *, token: str | None = None, allow_real: bool | None = None, allowed_ips: set[str] | None = None, rate_per_minute: int = 600) -> FastAPI:
    token = token if token is not None else os.environ.get("BRIDGE_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("BRIDGE_TOKEN must be set and at least 32 characters long")
    allow_real = allow_real if allow_real is not None else os.environ.get("BRIDGE_ALLOW_REAL", "false").lower() == "true"
    if allowed_ips is None:
        raw = os.environ.get("BRIDGE_ALLOWED_IPS", "")
        allowed_ips = {ip.strip() for ip in raw.split(",") if ip.strip()}
    if gateway is None:
        from trading_engine.mt5_connector import MT5Connector

        gateway = MT5Connector()
    lock = threading.Lock()
    hits: deque[float] = deque()

    app = FastAPI(title="MT5 Bridge", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client_ip = request.client.host if request.client else ""
        if allowed_ips and client_ip not in allowed_ips:
            return JSONResponse({"ok": False, "error": "ip not allowed"}, status_code=403)
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            log.warning("rejected unauthenticated request from %s", client_ip)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        now = time.monotonic()
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= rate_per_minute:
            return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
        hits.append(now)
        return await call_next(request)

    def call(method: str, args: dict[str, Any]) -> Any:
        if method in ("rates", "rates_range"):
            args["timeframe"] = Timeframe(args["timeframe"])
        for key in ("date_from", "date_to"):
            if key in args:
                args[key] = _parse_dt(args[key])
        if "side" in args:
            args["side"] = Side(args["side"])
        if "request" in args:
            args["request"] = OrderRequest.from_mt5(args["request"])
        if method == "order_send" and not allow_real:
            acct = gateway.account_info()
            if acct.kind is not AccountKind.DEMO:
                raise PermissionError(f"bridge is DEMO-only (BRIDGE_ALLOW_REAL=false) but account is {acct.kind.value}")
        if method in ("symbols", "positions", "orders"):
            args = {k: v for k, v in args.items() if v is not None}
        return getattr(gateway, method)(**args)

    @app.post("/rpc/{method}")
    async def rpc(method: str, request: Request):
        if method not in ALLOWED_METHODS:
            raise HTTPException(status_code=404, detail="unknown method")
        args = await request.json() if await request.body() else {}
        try:
            with lock:  # MT5 package is not thread-safe
                result = call(method, dict(args))
            if method in ("order_send", "order_check"):
                log.info("%s %s -> %s", method, {k: args.get("request", {}).get(k) for k in ("symbol", "type", "volume", "magic", "comment")}, getattr(result, "retcode", None))
            return {"ok": True, "result": codec.encode(result)}
        except PermissionError as exc:
            log.error("blocked: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc), "error_type": "PermissionError"}, status_code=403)
        except (ConnectionLost, GatewayTimeout, GatewayError) as exc:
            return JSONResponse({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, status_code=502)

    @app.get("/health")
    async def health():
        status = gateway.terminal_status()
        return {"ok": True, "connected": status.connected, "trade_allowed": status.trade_allowed, "allow_real": allow_real}

    return app


app = create_app() if os.environ.get("BRIDGE_AUTOCREATE", "1") == "1" and os.environ.get("BRIDGE_TOKEN") else None
