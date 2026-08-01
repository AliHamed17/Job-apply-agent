"""Shared transaction fence for every qualified-autopilot authority mutation."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

AUTOMATION_AUTHORITY_FENCE_ID = 5_354_025_376_604_503_901


def lock_automation_authority_fence(db: Session) -> None:
    """Serialize mutable authority state with irreversible-action admission."""

    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": AUTOMATION_AUTHORITY_FENCE_ID},
        )


__all__ = ["AUTOMATION_AUTHORITY_FENCE_ID", "lock_automation_authority_fence"]
