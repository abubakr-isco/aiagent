from __future__ import annotations

from typing import Any

from .logging import redact
from .models import AuditLog


def client_ip(request) -> str | None:
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR")


def audit(request, action: str, target: str = "", before: Any = None, after: Any = None, **details: Any) -> AuditLog:
    user = getattr(request, "user", None) if request is not None else None
    authenticated = bool(user and user.is_authenticated)
    return AuditLog.objects.create(
        actor=user if authenticated else None,
        actor_name=user.get_username() if authenticated else "system",
        action=action,
        target=target,
        before=before,
        after=after,
        ip=client_ip(request),
        details={k: (redact(v) if isinstance(v, str) else v) for k, v in details.items()},
    )
