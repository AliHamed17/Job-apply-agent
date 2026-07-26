"""Redacted browser qualification traces."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SELECTOR_VERSION = "linkedin-easy-apply-v1"
_ALLOWED_KEYS = {
    "event",
    "selector_version",
    "step",
    "field_types",
    "resolver_sources",
    "terminal_reason",
    "timestamp",
}


@dataclass
class RedactedTrace:
    selector_version: str = SELECTOR_VERSION
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, **details: Any) -> None:
        payload = {
            "event": event,
            "selector_version": self.selector_version,
            "timestamp": datetime.now(UTC).isoformat(),
            **details,
        }
        self.events.append({key: value for key, value in payload.items() if key in _ALLOWED_KEYS})

    def write_report(self, path: str | Path, qualified: bool) -> None:
        terminal = next(
            (
                event.get("terminal_reason")
                for event in reversed(self.events)
                if event.get("terminal_reason")
            ),
            "UNKNOWN",
        )
        report = {
            "qualified": qualified,
            "selector_version": self.selector_version,
            "terminal_reason": terminal,
            "events": self.events,
            "privacy": (
                "No field answers, CV text, cookies, page content, names, URLs, "
                "emails, or phone numbers are retained."
            ),
        }
        Path(path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
