"""Log filter that removes secrets from every log record."""

from __future__ import annotations

import logging
import os
import re

_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|token|authorization)(\"?\s*[:=]\s*\"?)([^\s,\"'}]+)"),
    re.compile(r"(?i)(bearer\s+)([a-z0-9._\-]+)"),
]
_SECRET_ENV = ("MT5_PASSWORD", "MT5_BRIDGE_TOKEN", "BRIDGE_TOKEN", "DJANGO_SECRET_KEY", "POSTGRES_PASSWORD")


def redact(text: str) -> str:
    for name in _SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}***", text)  # bearer first, before the key=value pass
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return text


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True
