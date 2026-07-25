"""System event audit buffer and logger."""

from __future__ import annotations

from datetime import datetime, timezone

_AUDIT_LOG_BUFFER: list[dict] = []


def record_audit_event(event_name: str, level: str = "info", details: dict | None = None) -> dict:
    """Record structured event in memory audit buffer."""
    item = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_name": event_name,
        "level": level,
        "details": details or {},
    }
    _AUDIT_LOG_BUFFER.append(item)

    if len(_AUDIT_LOG_BUFFER) > 200:
        _AUDIT_LOG_BUFFER.pop(0)

    return item


def get_audit_logs(level: str | None = None, limit: int = 50) -> list[dict]:
    """Retrieve audit events from memory buffer."""
    logs = _AUDIT_LOG_BUFFER
    if level:
        logs = [l for l in logs if l["level"].lower() == level.lower()]
    return list(reversed(logs))[:limit]
