"""Durable privacy-safe application lifecycle audit helpers."""

from __future__ import annotations

import json
from typing import Any

from db.models import ApplicationEvent

_ALLOWED_DETAIL_KEYS = {
    "application_revision",
    "approval_source",
    "attempt_number",
    "external_action_queued",
    "external_action_started",
    "field_id_hash",
    "form_plan_id",
    "platform",
    "profile_version",
    "reason_code",
    "reusable",
    "selected_cv_id",
    "state",
}
_ALLOWED_ACTORS = {
    "operator",
    "batch_operator",
    "whatsapp_operator",
    # worker/autopilot_inspection.py records this actor. Without it here, an
    # unattended send was silently relabelled "system" and became
    # indistinguishable from routine worker activity in the audit trail.
    "qualified_autopilot",
    "worker",
    "system",
}


def redacted_event_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Keep bounded structural values and discard personal/free-text data."""
    output: dict[str, Any] = {}
    for key, value in (details or {}).items():
        if key not in _ALLOWED_DETAIL_KEYS or value is None:
            continue
        if isinstance(value, (bool, int, float)):
            output[key] = value
        elif isinstance(value, str):
            output[key] = value[:128]
    return output


def record_application_event(
    db,
    application_id: int,
    event_type: str,
    *,
    actor: str = "system",
    details: dict[str, Any] | None = None,
) -> ApplicationEvent:
    """Append a lifecycle event to the current transaction."""
    event = ApplicationEvent(
        application_id=application_id,
        event_type=event_type[:64],
        actor=actor if actor in _ALLOWED_ACTORS else "system",
        details=json.dumps(
            redacted_event_details(details),
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    db.add(event)
    return event
