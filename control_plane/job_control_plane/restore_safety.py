"""Fail-closed quarantine for a restored redacted control-plane database."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import OperatorSession, RunnerDevice, SubmissionCommand

_UNDELIVERED_COMMAND_STATES = frozenset({"queued", "claimed"})


def _aware_utc(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ControlPlaneRestoreSummary:
    """Redacted mutation counts plus the mandatory rotation instruction."""

    runner_devices_deactivated: int = 0
    operator_sessions_revoked: int = 0
    undelivered_commands_rejected: int = 0
    identity_rotation_required: bool = True

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def quarantine_restored_control_plane(
    db: Session,
    *,
    now: datetime | None = None,
) -> ControlPlaneRestoreSummary:
    """Disable restored authority without creating events or evidence.

    ``queued`` and ``claimed`` commands are rejected because the restored
    control plane cannot prove current delivery authority. Acknowledged,
    running, rejected, and finished command history is preserved. Deactivating
    every restored device makes all old signatures unusable; recovery requires
    a new device UUID and Ed25519 key pair.
    """

    timestamp = _aware_utc(now)
    devices_deactivated = 0
    sessions_revoked = 0
    commands_rejected = 0

    device_query = select(RunnerDevice)
    session_query = select(OperatorSession)
    command_query = select(SubmissionCommand)
    if db.get_bind().dialect.name == "postgresql":
        device_query = device_query.with_for_update()
        session_query = session_query.with_for_update()
        command_query = command_query.with_for_update()

    for device in db.scalars(device_query):
        if device.active:
            device.active = False
            devices_deactivated += 1

    for session in db.scalars(session_query):
        if session.revoked_at is None:
            session.revoked_at = timestamp
            sessions_revoked += 1

    for command in db.scalars(command_query):
        if command.status not in _UNDELIVERED_COMMAND_STATES:
            continue
        command.status = "rejected"
        command.claim_lease_expires_at = None
        command.finished_at = command.finished_at or timestamp
        commands_rejected += 1

    return ControlPlaneRestoreSummary(
        runner_devices_deactivated=devices_deactivated,
        operator_sessions_revoked=sessions_revoked,
        undelivered_commands_rejected=commands_rejected,
    )


__all__ = [
    "ControlPlaneRestoreSummary",
    "quarantine_restored_control_plane",
]
