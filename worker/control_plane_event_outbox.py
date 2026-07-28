"""Durable, bounded, redacted event delivery to the hosted control plane."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4, uuid5

from control_plane.job_control_plane.protocol import ReasonCode as ControlPlaneReasonCode
from db.models import (
    ControlPlaneCommandReceipt,
    ControlPlaneEventOutbox,
    Submission,
    SubmissionCommand,
)

MAX_EVENT_PAYLOAD_BYTES = 4096
_REF_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPES = frozenset(
    {
        "command_accepted",
        "attempt_stage",
        "attempt_outcome",
        "evidence_recorded",
    }
)
_STAGES = frozenset(
    {
        "queued",
        "inspecting",
        "preparing",
        "ready",
        "committing",
        "verifying",
        "finished",
    }
)
_OUTCOMES = frozenset(
    {
        "confirmed_submitted",
        "already_applied",
        "needs_review",
        "unknown",
        "failed_before_commit",
        "draft_only",
        "operator_confirmed",
        "legacy_unverified",
    }
)
_EVIDENCE_TYPES = frozenset(
    {
        "employer_application_id",
        "schema_valid_receipt",
        "candidate_portal_record",
        "ats_visible_confirmation",
    }
)
_REMOTE_REASON_CODES = frozenset(item.value for item in ControlPlaneReasonCode)
_EVIDENCE_TYPE_MAP = {
    "employer_application_id": "employer_application_id",
    "api_receipt": "schema_valid_receipt",
    "candidate_portal_record": "candidate_portal_record",
    "visible_post_click_confirmation": "ats_visible_confirmation",
}
_EVENT_NAMESPACE = UUID("40895cca-f371-4ddb-bebf-f14cd8b52a3f")


class ControlPlaneEventError(ValueError):
    """A stable event-boundary rejection without payload echoing."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _naive(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    return _aware(timestamp).replace(tzinfo=None)


def _bounded_token(value: str | None, *, reason: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not _TOKEN_RE.fullmatch(normalized):
        raise ControlPlaneEventError(reason)
    return normalized


@dataclass(frozen=True, slots=True, repr=False)
class RedactedControlPlaneEvent:
    """Allowlisted event shape; arbitrary metadata is intentionally impossible."""

    event_id: str
    command_id: str
    sequence: int
    stage: str
    occurred_at: datetime
    cycle: int = 0
    outcome: str | None = None
    reason_code: str | None = None
    evidence_type: str | None = None
    evidence_digest: str | None = None

    def __post_init__(self) -> None:
        if not _REF_RE.fullmatch(self.event_id):
            raise ControlPlaneEventError("EVENT_REF_INVALID")
        if not _REF_RE.fullmatch(self.command_id):
            raise ControlPlaneEventError("EVENT_COMMAND_REF_INVALID")
        if not 1 <= self.sequence <= 2_147_483_647 or self.cycle < 0:
            raise ControlPlaneEventError("EVENT_SEQUENCE_INVALID")
        if self.stage not in _STAGES:
            raise ControlPlaneEventError("EVENT_STAGE_INVALID")
        if self.outcome is not None and self.outcome not in _OUTCOMES:
            raise ControlPlaneEventError("EVENT_OUTCOME_INVALID")
        if self.evidence_type is not None and self.evidence_type not in _EVIDENCE_TYPES:
            raise ControlPlaneEventError("EVENT_EVIDENCE_TYPE_INVALID")
        if self.evidence_digest is not None and not _SHA256_RE.fullmatch(self.evidence_digest):
            raise ControlPlaneEventError("EVENT_EVIDENCE_DIGEST_INVALID")
        if self.reason_code is not None and self.reason_code not in _REMOTE_REASON_CODES:
            raise ControlPlaneEventError("EVENT_REASON_INVALID")
        if (self.stage == "finished") != (self.outcome is not None):
            raise ControlPlaneEventError("EVENT_TERMINAL_STATE_INVALID")
        if (self.evidence_type is None) != (self.evidence_digest is None):
            raise ControlPlaneEventError("EVENT_EVIDENCE_INCOMPLETE")
        if self.outcome == "confirmed_submitted" and self.evidence_type is None:
            raise ControlPlaneEventError("EVENT_EVIDENCE_REQUIRED")
        if self.outcome not in {None, "confirmed_submitted"} and self.evidence_type is not None:
            raise ControlPlaneEventError("EVENT_EVIDENCE_NOT_ALLOWED")

    def __repr__(self) -> str:
        return (
            "RedactedControlPlaneEvent("
            f"event_id={self.event_id!r}, command_id={self.command_id!r}, "
            f"stage={self.stage!r})"
        )

    def to_wire(self) -> dict[str, object]:
        values: dict[str, object] = {
            "event_id": self.event_id,
            "command_id": self.command_id,
            "sequence": self.sequence,
            "stage": self.stage,
            "occurred_at": _aware(self.occurred_at).isoformat(),
        }
        for field in (
            "outcome",
            "reason_code",
            "evidence_type",
            "evidence_digest",
        ):
            value = getattr(self, field)
            if value is not None:
                values[field] = value
        return values


def new_event_ref() -> str:
    return str(uuid4())


def transition_event_ref(command_id: str, sequence: int) -> str:
    return str(uuid5(_EVENT_NAMESPACE, f"{command_id}:{sequence}"))


def _local_event_type(event: RedactedControlPlaneEvent) -> str:
    if event.evidence_type is not None:
        return "evidence_recorded"
    if event.outcome is not None:
        return "attempt_outcome"
    if event.stage == "queued":
        return "command_accepted"
    return "attempt_stage"


def canonical_event_payload(event: RedactedControlPlaneEvent) -> bytes:
    payload = json.dumps(
        event.to_wire(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > MAX_EVENT_PAYLOAD_BYTES:
        raise ControlPlaneEventError("EVENT_PAYLOAD_TOO_LARGE")
    return payload


def enqueue_control_plane_event(
    db,
    event: RedactedControlPlaneEvent,
) -> ControlPlaneEventOutbox:
    payload = canonical_event_payload(event)
    existing = (
        db.query(ControlPlaneEventOutbox)
        .filter(ControlPlaneEventOutbox.event_ref == event.event_id)
        .one_or_none()
    )
    payload_digest = hashlib.sha256(payload).hexdigest()
    if existing is not None:
        if not secrets.compare_digest(existing.payload_digest, payload_digest):
            raise ControlPlaneEventError("EVENT_ID_CONFLICT")
        return existing
    row = ControlPlaneEventOutbox(
        event_ref=event.event_id,
        remote_command_ref=event.command_id,
        sequence=event.sequence,
        cycle=event.cycle,
        event_type=_local_event_type(event),
        payload_json=payload.decode("ascii"),
        payload_digest=payload_digest,
        state="pending",
        available_at=_naive(event.occurred_at),
    )
    db.add(row)
    db.flush()
    return row


def _decode_row(row: ControlPlaneEventOutbox) -> dict[str, object]:
    raw = row.payload_json.encode("ascii", errors="strict")
    payload_digest = cast(str, row.payload_digest)
    if (
        len(raw) > MAX_EVENT_PAYLOAD_BYTES
        or not _SHA256_RE.fullmatch(payload_digest)
        or not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), payload_digest)
    ):
        raise ControlPlaneEventError("EVENT_PAYLOAD_CORRUPT")
    try:
        decoded = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError) as exc:
        raise ControlPlaneEventError("EVENT_PAYLOAD_CORRUPT") from exc
    if not isinstance(decoded, dict):
        raise ControlPlaneEventError("EVENT_PAYLOAD_CORRUPT")
    # Revalidation through the typed allowlist prevents a database mutation
    # from turning the outbox into a private-data exfiltration channel.
    try:
        rebuilt = RedactedControlPlaneEvent(
            event_id=str(decoded.pop("event_id")),
            command_id=str(decoded.pop("command_id")),
            sequence=int(decoded.pop("sequence")),
            stage=str(decoded.pop("stage")),
            occurred_at=datetime.fromisoformat(str(decoded.pop("occurred_at"))),
            cycle=row.cycle,
            **decoded,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlPlaneEventError("EVENT_PAYLOAD_CORRUPT") from exc
    return rebuilt.to_wire()


def enqueue_control_plane_attempt_transition(
    db,
    *,
    attempt: Submission,
    command: SubmissionCommand,
    occurred_at: datetime,
    use_attempt_reason: bool = True,
) -> ControlPlaneEventOutbox | None:
    """Mirror one authoritative local transition when it came from Vercel."""

    receipt_query = db.query(ControlPlaneCommandReceipt).filter(
        ControlPlaneCommandReceipt.client_idempotency_key == command.idempotency_key
    )
    if db.bind.dialect.name == "postgresql":
        receipt_query = receipt_query.with_for_update()
    receipt = receipt_query.one_or_none()
    if receipt is None:
        return None

    outcome = str(attempt.outcome) if attempt.outcome is not None else None
    reason_code = None
    if use_attempt_reason and outcome != "confirmed_submitted" and attempt.reason_code:
        candidate = str(attempt.reason_code)
        reason_code = (
            candidate
            if candidate in _REMOTE_REASON_CODES
            else ControlPlaneReasonCode.INTERNAL_ERROR.value
        )
    evidence_type = None
    evidence_digest = None
    if outcome == "confirmed_submitted":
        evidence_type = _EVIDENCE_TYPE_MAP.get(str(attempt.verification_kind or ""))
        evidence_digest = str(attempt.evidence_digest or "")
        if evidence_type is None or not _SHA256_RE.fullmatch(evidence_digest):
            raise ControlPlaneEventError("EVENT_EVIDENCE_REQUIRED")

    last_query = (
        db.query(ControlPlaneEventOutbox)
        .filter(ControlPlaneEventOutbox.remote_command_ref == receipt.remote_command_ref)
        .order_by(ControlPlaneEventOutbox.sequence.desc())
    )
    if db.bind.dialect.name == "postgresql":
        last_query = last_query.with_for_update()
    last = last_query.first()
    if last is not None:
        last_payload = _decode_row(last)
        same_transition = (
            last_payload.get("stage") == attempt.stage
            and last_payload.get("outcome") == outcome
            and last_payload.get("reason_code") == reason_code
            and last_payload.get("evidence_type") == evidence_type
            and last_payload.get("evidence_digest") == evidence_digest
        )
        if same_transition:
            return last
        sequence = last.sequence + 1
        prior_stage = str(last_payload.get("stage") or "")
        cycle = last.cycle + (
            1
            if attempt.stage == "queued" and prior_stage in {"inspecting", "preparing", "ready"}
            else 0
        )
    else:
        if attempt.stage != "queued":
            raise ControlPlaneEventError("EVENT_INITIAL_STAGE_INVALID")
        sequence = 1
        cycle = 0

    return enqueue_control_plane_event(
        db,
        RedactedControlPlaneEvent(
            event_id=transition_event_ref(receipt.remote_command_ref, sequence),
            command_id=receipt.remote_command_ref,
            sequence=sequence,
            cycle=cycle,
            stage=attempt.stage,
            outcome=outcome,
            reason_code=reason_code,
            evidence_type=evidence_type,
            evidence_digest=evidence_digest,
            occurred_at=occurred_at,
        ),
    )


def claim_control_plane_event(
    db,
    *,
    runner_id: str,
    now: datetime | None = None,
) -> tuple[int, str] | None:
    """Claim one due event and recover claims abandoned by crashed runners."""

    timestamp = _naive(now)
    runner_token = _bounded_token(runner_id, reason="RUNNER_ID_INVALID")
    stale_before = timestamp - timedelta(seconds=30)
    stale_query = (
        db.query(ControlPlaneEventOutbox)
        .filter(
            ControlPlaneEventOutbox.state == "claimed",
            ControlPlaneEventOutbox.claimed_at <= stale_before,
        )
        .order_by(ControlPlaneEventOutbox.claimed_at, ControlPlaneEventOutbox.id)
        .limit(100)
    )
    if db.bind.dialect.name == "postgresql":
        stale_query = stale_query.with_for_update(skip_locked=True)
    for stale in stale_query.all():
        stale.state = "pending"
        stale.claimed_at = None
        stale.claimed_by = None
        stale.claim_token = None
        stale.available_at = min(stale.available_at, timestamp)
        stale.last_error_code = "CONTROL_PLANE_CLAIM_EXPIRED"
    db.flush()

    query = (
        db.query(ControlPlaneEventOutbox)
        .filter(
            ControlPlaneEventOutbox.state == "pending",
            ControlPlaneEventOutbox.available_at <= timestamp,
        )
        .order_by(ControlPlaneEventOutbox.available_at, ControlPlaneEventOutbox.id)
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    row = query.first()
    if row is None:
        return None
    claim_token = secrets.token_hex(32)
    row.state = "claimed"
    row.claimed_at = timestamp
    row.claimed_by = runner_token
    row.claim_token = claim_token
    row.delivery_count += 1
    db.commit()
    return row.id, claim_token


def _claimed_row(db, row_id: int, claim_token: str) -> ControlPlaneEventOutbox:
    query = db.query(ControlPlaneEventOutbox).filter(
        ControlPlaneEventOutbox.id == row_id,
        ControlPlaneEventOutbox.state == "claimed",
        ControlPlaneEventOutbox.claim_token == claim_token,
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise ControlPlaneEventError("EVENT_CLAIM_LOST")
    return row


async def deliver_claimed_control_plane_event(
    db,
    *,
    row_id: int,
    claim_token: str,
    signer: Callable[[str, Mapping[str, object]], Mapping[str, object]],
    sender: Callable[[Mapping[str, object]], Awaitable[None]],
    now: datetime | None = None,
) -> None:
    """Sign and deliver one redacted row without logging its envelope."""

    row = _claimed_row(db, row_id, claim_token)
    try:
        payload = _decode_row(row)
        envelope = signer("runner_event", payload)
        await sender(envelope)
    except Exception:
        db.rollback()
        row = _claimed_row(db, row_id, claim_token)
        row.state = "pending"
        row.claimed_at = None
        row.claimed_by = None
        row.claim_token = None
        row.available_at = _naive(now) + timedelta(seconds=10)
        row.last_error_code = "CONTROL_PLANE_DELIVERY_FAILED"
        db.commit()
        raise

    row.state = "sent"
    row.sent_at = _naive(now)
    row.claimed_at = None
    row.claimed_by = None
    row.claim_token = None
    row.last_error_code = None
    db.commit()
