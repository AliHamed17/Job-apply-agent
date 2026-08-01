"""Transactional control-plane services and replay-safe protocol handling."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .config import Settings
from .crypto import (
    private_key_from_base64url,
    public_key_from_base64url,
    sign_envelope,
    verify_envelope,
)
from .db import EXPECTED_SCHEMA_REVISION
from .models import (
    ControlKillSwitchCommand,
    OperatorAudit,
    ReviewGrant,
    RunnerDevice,
    RunnerEvent,
    RunnerNonce,
    SubmissionCommand,
)
from .protocol import (
    CONTROL_AUDIENCE,
    RUNNER_AUDIENCE,
    AdapterCode,
    AttemptOutcome,
    AttemptStage,
    CommandAckEnvelope,
    CommandAckStatus,
    CommandPollEnvelope,
    ControlCommandEnvelope,
    ControlCommandPayload,
    EnvelopePurpose,
    HeartbeatEnvelope,
    KillSwitchCommandEnvelope,
    KillSwitchCommandPayload,
    ReviewGrantEnvelope,
    ReviewGrantRevocationEnvelope,
    RunnerEventEnvelope,
    SignedEnvelope,
    StrictProtocolModel,
    canonical_envelope_bytes,
)


class ControlPlaneError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class CommandCreation:
    command: SubmissionCommand
    duplicate: bool


@dataclass(frozen=True, slots=True)
class KillCommandCreation:
    command: ControlKillSwitchCommand
    duplicate: bool


@dataclass(frozen=True, slots=True)
class Receipt:
    identifier: str
    duplicate: bool


OPERATOR_AUDIT_RETENTION = timedelta(days=30)
OPERATOR_AUDIT_HARD_CAP = 5_000
_OPERATOR_AUDIT_ADVISORY_LOCK = 5_354_025_376_604_503_895


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_model_digest(model: StrictProtocolModel) -> str:
    raw = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(raw)


def require_current_schema(db: Session) -> None:
    try:
        revisions = tuple(db.scalars(text("SELECT version_num FROM alembic_version")).all())
    except SQLAlchemyError as exc:
        raise ControlPlaneError("SCHEMA_NOT_CURRENT", status_code=503) from exc
    if revisions != (EXPECTED_SCHEMA_REVISION,):
        raise ControlPlaneError("SCHEMA_NOT_CURRENT", status_code=503)


def _idempotent_command(
    prior: SubmissionCommand | None,
    *,
    grant_id: UUID,
    application_ref: UUID,
    application_revision: int,
    form_fingerprint_digest: str,
) -> CommandCreation | None:
    if prior is None:
        return None
    if (
        prior.grant_id != str(grant_id)
        or prior.application_ref != str(application_ref)
        or prior.application_revision != application_revision
        or prior.form_fingerprint_digest != form_fingerprint_digest
    ):
        raise ControlPlaneError("IDEMPOTENCY_CONFLICT")
    return CommandCreation(command=prior, duplicate=True)


def audit(
    db: Session,
    *,
    action: str,
    result: str,
    request_digest: str,
    target_type: str | None = None,
    target_id: str | None = None,
    now: datetime | None = None,
) -> None:
    created_at = as_utc(now or utc_now())
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _OPERATOR_AUDIT_ADVISORY_LOCK},
        )
    db.add(
        OperatorAudit(
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            request_digest=request_digest,
            created_at=created_at,
        )
    )
    db.flush()
    db.execute(
        delete(OperatorAudit).where(
            OperatorAudit.created_at < created_at - OPERATOR_AUDIT_RETENTION
        )
    )
    retained_ids = (
        select(OperatorAudit.id)
        .order_by(OperatorAudit.created_at.desc(), OperatorAudit.id.desc())
        .limit(OPERATOR_AUDIT_HARD_CAP)
    )
    db.execute(delete(OperatorAudit).where(OperatorAudit.id.not_in(retained_ids)))


def _configured_device(db: Session, settings: Settings, *, now: datetime) -> RunnerDevice:
    identifier = str(settings.runner_device_id)
    row = db.get(RunnerDevice, identifier)
    if row is None:
        row = RunnerDevice(
            id=identifier,
            public_key_b64=settings.runner_verify_public_key,
            active=True,
            created_at=now,
        )
        db.add(row)
        db.flush()
    if not row.active:
        raise ControlPlaneError("RUNNER_DISABLED", status_code=401)
    if row.public_key_b64 != settings.runner_verify_public_key:
        raise ControlPlaneError("RUNNER_KEY_MISMATCH", status_code=401)
    return row


def verify_runner_envelope(
    db: Session,
    settings: Settings,
    envelope: SignedEnvelope[StrictProtocolModel],
    *,
    expected_purpose: EnvelopePurpose,
    now: datetime | None = None,
) -> RunnerDevice:
    checked_at = now or utc_now()
    if envelope.key_id != settings.runner_device_id:
        raise ControlPlaneError("RUNNER_UNKNOWN", status_code=401)
    try:
        verify_envelope(
            envelope,
            public_key_from_base64url(settings.runner_verify_public_key),
            expected_purpose=expected_purpose,
            expected_audience=CONTROL_AUDIENCE,
            now=checked_at,
        )
    except ValueError as exc:
        raise ControlPlaneError("RUNNER_SIGNATURE_INVALID", status_code=401) from exc

    require_current_schema(db)
    device = _configured_device(db, settings, now=checked_at)
    nonce = RunnerNonce(
        device_id=device.id,
        purpose=envelope.purpose.value,
        nonce=str(envelope.nonce),
        issued_at=envelope.issued_at,
        expires_at=envelope.expires_at,
        seen_at=checked_at,
    )
    try:
        with db.begin_nested():
            db.execute(
                delete(RunnerNonce).where(
                    RunnerNonce.expires_at < checked_at - timedelta(minutes=10)
                )
            )
            db.add(nonce)
            db.flush()
    except IntegrityError as exc:
        raise ControlPlaneError("RUNNER_REPLAYED", status_code=409) from exc
    # Authentication nonces are security state, not business state. Persist the
    # one-use nonce before downstream validation so a valid-but-denied envelope
    # can never be replayed after the business transaction rolls back.
    db.commit()
    return device


def receive_heartbeat(
    db: Session,
    settings: Settings,
    envelope: HeartbeatEnvelope,
    *,
    now: datetime | None = None,
) -> Receipt:
    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_HEARTBEAT,
        now=checked_at,
    )
    device.boot_id = str(envelope.payload.boot_id)
    device.release_digest = envelope.payload.release_digest
    device.status = envelope.payload.status.value
    device.last_seen_at = checked_at
    return Receipt(identifier=device.id, duplicate=False)


def receive_review_grant(
    db: Session,
    settings: Settings,
    envelope: ReviewGrantEnvelope,
    *,
    now: datetime | None = None,
) -> Receipt:
    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_REVIEW_GRANT,
        now=checked_at,
    )
    payload = envelope.payload
    reviewed_at = as_utc(payload.reviewed_at)
    if reviewed_at > as_utc(envelope.issued_at) + timedelta(seconds=30):
        raise ControlPlaneError("REVIEW_GRANT_TIME_INVALID")
    if reviewed_at < as_utc(envelope.issued_at) - timedelta(minutes=30):
        raise ControlPlaneError("REVIEW_GRANT_STALE")

    identifier = str(payload.grant_id)
    expected_binding = (
        device.id,
        str(payload.application_ref),
        payload.application_revision,
        payload.adapter.value,
        payload.adapter_version,
        payload.form_fingerprint_digest,
        reviewed_at,
        as_utc(envelope.expires_at),
    )
    existing = db.scalar(select(ReviewGrant).where(ReviewGrant.id == identifier).with_for_update())
    if existing is not None:
        actual_binding = (
            existing.device_id,
            existing.application_ref,
            existing.application_revision,
            existing.adapter,
            existing.adapter_version,
            existing.form_fingerprint_digest,
            as_utc(existing.reviewed_at),
            as_utc(existing.expires_at),
        )
        if actual_binding != expected_binding:
            raise ControlPlaneError("REVIEW_GRANT_CONFLICT")
        return Receipt(identifier=identifier, duplicate=True)

    envelope_digest = sha256_bytes(canonical_envelope_bytes(envelope))
    try:
        with db.begin_nested():
            db.add(
                ReviewGrant(
                    id=identifier,
                    device_id=device.id,
                    application_ref=str(payload.application_ref),
                    application_revision=payload.application_revision,
                    adapter=payload.adapter.value,
                    adapter_version=payload.adapter_version,
                    form_fingerprint_digest=payload.form_fingerprint_digest,
                    envelope_digest=envelope_digest,
                    reviewed_at=reviewed_at,
                    expires_at=envelope.expires_at,
                    created_at=checked_at,
                )
            )
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(ReviewGrant).where(ReviewGrant.id == identifier).with_for_update()
        )
        if existing is None:
            raise ControlPlaneError("REVIEW_GRANT_CONFLICT") from None
        actual_binding = (
            existing.device_id,
            existing.application_ref,
            existing.application_revision,
            existing.adapter,
            existing.adapter_version,
            existing.form_fingerprint_digest,
            as_utc(existing.reviewed_at),
            as_utc(existing.expires_at),
        )
        if actual_binding != expected_binding:
            raise ControlPlaneError("REVIEW_GRANT_CONFLICT") from None
        return Receipt(identifier=identifier, duplicate=True)
    return Receipt(identifier=identifier, duplicate=False)


def receive_review_grant_revocation(
    db: Session,
    settings: Settings,
    envelope: ReviewGrantRevocationEnvelope,
    *,
    now: datetime | None = None,
) -> Receipt:
    """Apply a signed tombstone that cannot be undone by delayed projection."""

    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_REVIEW_GRANT_REVOCATION,
        now=checked_at,
    )
    payload = envelope.payload
    reviewed_at = as_utc(payload.reviewed_at)
    grant_expires_at = as_utc(payload.grant_expires_at)
    revoked_at = as_utc(payload.revoked_at)
    if revoked_at > as_utc(envelope.issued_at) + timedelta(seconds=30):
        raise ControlPlaneError("REVIEW_GRANT_REVOCATION_TIME_INVALID")

    identifier = str(payload.grant_id)
    expected_binding = (
        device.id,
        str(payload.application_ref),
        payload.application_revision,
        payload.adapter.value,
        payload.adapter_version,
        payload.form_fingerprint_digest,
        reviewed_at,
        grant_expires_at,
    )
    envelope_digest = sha256_bytes(canonical_envelope_bytes(envelope))
    existing = db.scalar(select(ReviewGrant).where(ReviewGrant.id == identifier).with_for_update())
    if existing is None:
        try:
            with db.begin_nested():
                db.add(
                    ReviewGrant(
                        id=identifier,
                        device_id=device.id,
                        application_ref=str(payload.application_ref),
                        application_revision=payload.application_revision,
                        adapter=payload.adapter.value,
                        adapter_version=payload.adapter_version,
                        form_fingerprint_digest=payload.form_fingerprint_digest,
                        envelope_digest=envelope_digest,
                        reviewed_at=reviewed_at,
                        expires_at=grant_expires_at,
                        created_at=checked_at,
                        revoked_at=revoked_at,
                        revocation_envelope_digest=envelope_digest,
                    )
                )
                db.flush()
        except IntegrityError:
            existing = db.scalar(
                select(ReviewGrant).where(ReviewGrant.id == identifier).with_for_update()
            )
            if existing is None:
                raise ControlPlaneError("REVIEW_GRANT_REVOCATION_CONFLICT") from None
        else:
            return Receipt(identifier=identifier, duplicate=False)

    actual_binding = (
        existing.device_id,
        existing.application_ref,
        existing.application_revision,
        existing.adapter,
        existing.adapter_version,
        existing.form_fingerprint_digest,
        as_utc(existing.reviewed_at),
        as_utc(existing.expires_at),
    )
    if actual_binding != expected_binding:
        raise ControlPlaneError("REVIEW_GRANT_REVOCATION_CONFLICT")
    if existing.revoked_at is not None:
        if as_utc(existing.revoked_at) != revoked_at:
            raise ControlPlaneError("REVIEW_GRANT_REVOCATION_CONFLICT")
        _cancel_unadmitted_revoked_command(existing, checked_at=checked_at)
        return Receipt(identifier=identifier, duplicate=True)
    existing.revoked_at = revoked_at
    existing.revocation_envelope_digest = envelope_digest
    _cancel_unadmitted_revoked_command(existing, checked_at=checked_at)
    return Receipt(identifier=identifier, duplicate=False)


def _cancel_unadmitted_revoked_command(
    grant: ReviewGrant,
    *,
    checked_at: datetime,
) -> None:
    """Prevent an unacknowledged stale command from leaving the cloud queue."""

    command = grant.command
    if (
        command is None
        or command.acknowledged_at is not None
        or command.status not in {"queued", "claimed"}
    ):
        return
    command.status = "rejected"
    command.claim_lease_expires_at = None
    command.finished_at = checked_at


def create_command(
    db: Session,
    settings: Settings,
    *,
    grant_id: UUID,
    application_ref: UUID,
    application_revision: int,
    form_fingerprint_digest: str,
    client_idempotency_key: UUID,
    now: datetime | None = None,
) -> CommandCreation:
    checked_at = now or utc_now()
    if not settings.dispatch_allowed:
        raise ControlPlaneError("DISPATCH_DISABLED", status_code=403)

    idempotency_digest = sha256_bytes(str(client_idempotency_key).encode("ascii"))
    prior = db.scalar(
        select(SubmissionCommand).where(
            SubmissionCommand.client_idempotency_digest == idempotency_digest
        )
    )
    replay = _idempotent_command(
        prior,
        grant_id=grant_id,
        application_ref=application_ref,
        application_revision=application_revision,
        form_fingerprint_digest=form_fingerprint_digest,
    )
    if replay is not None:
        return replay

    grant = db.scalar(select(ReviewGrant).where(ReviewGrant.id == str(grant_id)).with_for_update())
    if grant is None:
        raise ControlPlaneError("REVIEW_GRANT_NOT_FOUND", status_code=404)
    # A concurrent request may have committed while this transaction waited
    # for the one-use grant lock. Recheck the idempotency key after acquiring
    # the lock so the loser receives the original command, never a false
    # consumed-grant failure or a second command.
    prior = db.scalar(
        select(SubmissionCommand).where(
            SubmissionCommand.client_idempotency_digest == idempotency_digest
        )
    )
    replay = _idempotent_command(
        prior,
        grant_id=grant_id,
        application_ref=application_ref,
        application_revision=application_revision,
        form_fingerprint_digest=form_fingerprint_digest,
    )
    if replay is not None:
        return replay
    if grant.revoked_at is not None:
        raise ControlPlaneError("REVIEW_GRANT_REVOKED")
    if (
        grant.application_ref != str(application_ref)
        or grant.application_revision != application_revision
        or grant.form_fingerprint_digest != form_fingerprint_digest
    ):
        raise ControlPlaneError("REVIEW_GRANT_BINDING_MISMATCH")
    if as_utc(grant.expires_at) <= checked_at:
        raise ControlPlaneError("REVIEW_GRANT_EXPIRED")
    if grant.consumed_at is not None or grant.command is not None:
        raise ControlPlaneError("REVIEW_GRANT_CONSUMED")

    device = db.scalar(
        select(RunnerDevice).where(RunnerDevice.id == grant.device_id).with_for_update()
    )
    if device is None or not device.active:
        raise ControlPlaneError("RUNNER_OFFLINE")
    if (
        device.last_seen_at is None
        or checked_at - as_utc(device.last_seen_at)
        > timedelta(seconds=settings.runner_offline_seconds)
        or device.status != "ready"
        or not device.boot_id
    ):
        raise ControlPlaneError("RUNNER_OFFLINE")

    command_id = uuid4()
    expires_at = min(as_utc(grant.expires_at), checked_at + timedelta(minutes=5))
    unsigned = ControlCommandEnvelope(
        key_id=settings.control_signing_key_id,
        purpose=EnvelopePurpose.CONTROL_COMMAND,
        audience=RUNNER_AUDIENCE,
        issued_at=checked_at,
        expires_at=expires_at,
        nonce=uuid4(),
        payload=ControlCommandPayload(
            command_id=command_id,
            grant_id=grant_id,
            application_ref=application_ref,
            application_revision=application_revision,
            adapter=AdapterCode(grant.adapter),
            adapter_version=grant.adapter_version,
            form_fingerprint_digest=form_fingerprint_digest,
        ),
    )
    signed = ControlCommandEnvelope.model_validate(
        sign_envelope(
            unsigned,
            private_key_from_base64url(settings.control_signing_private_key),
        ).model_dump()
    )
    command = SubmissionCommand(
        id=str(command_id),
        grant_id=grant.id,
        device_id=device.id,
        application_ref=grant.application_ref,
        application_revision=grant.application_revision,
        adapter=grant.adapter,
        adapter_version=grant.adapter_version,
        form_fingerprint_digest=grant.form_fingerprint_digest,
        client_idempotency_digest=idempotency_digest,
        status="queued",
        signed_envelope_json=canonical_envelope_bytes(signed).decode("utf-8"),
        created_at=checked_at,
        expires_at=expires_at,
    )
    try:
        with db.begin_nested():
            grant.consumed_at = checked_at
            db.add(command)
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ControlPlaneError("COMMAND_CONFLICT") from exc
    return CommandCreation(command=command, duplicate=False)


def create_kill_switch_command(
    db: Session,
    settings: Settings,
    *,
    client_idempotency_key: UUID,
    now: datetime | None = None,
) -> KillCommandCreation:
    """Mint one five-minute activation-only emergency-stop command."""

    checked_at = now or utc_now()
    idempotency_digest = sha256_bytes(str(client_idempotency_key).encode("ascii"))
    prior = db.scalar(
        select(ControlKillSwitchCommand).where(
            ControlKillSwitchCommand.client_idempotency_digest == idempotency_digest
        )
    )
    if prior is not None:
        return KillCommandCreation(command=prior, duplicate=True)
    device = db.scalar(
        select(RunnerDevice)
        .where(RunnerDevice.id == str(settings.runner_device_id))
        .with_for_update()
    )
    if device is None or not device.active:
        raise ControlPlaneError("RUNNER_DISABLED")
    try:
        runner_boot_id = UUID(str(device.boot_id))
    except (TypeError, ValueError) as exc:
        raise ControlPlaneError("RUNNER_OFFLINE") from exc

    command_id = uuid4()
    expires_at = checked_at + timedelta(minutes=5)
    unsigned = KillSwitchCommandEnvelope(
        key_id=settings.control_signing_key_id,
        purpose=EnvelopePurpose.CONTROL_KILL_COMMAND,
        audience=RUNNER_AUDIENCE,
        issued_at=checked_at,
        expires_at=expires_at,
        nonce=uuid4(),
        payload=KillSwitchCommandPayload(
            command_id=command_id,
            boot_id=runner_boot_id,
        ),
    )
    signed = KillSwitchCommandEnvelope.model_validate(
        sign_envelope(
            unsigned,
            private_key_from_base64url(settings.control_signing_private_key),
        ).model_dump()
    )
    command = ControlKillSwitchCommand(
        id=str(command_id),
        device_id=device.id,
        runner_boot_id=str(runner_boot_id),
        client_idempotency_digest=idempotency_digest,
        status="queued",
        signed_envelope_json=canonical_envelope_bytes(signed).decode("utf-8"),
        created_at=checked_at,
        expires_at=expires_at,
    )
    try:
        with db.begin_nested():
            db.add(command)
            db.flush()
    except IntegrityError:
        replay = db.scalar(
            select(ControlKillSwitchCommand).where(
                ControlKillSwitchCommand.client_idempotency_digest == idempotency_digest
            )
        )
        if replay is None:
            raise ControlPlaneError("KILL_SWITCH_COMMAND_CONFLICT") from None
        return KillCommandCreation(command=replay, duplicate=True)
    return KillCommandCreation(command=command, duplicate=False)


def poll_kill_switch_command(
    db: Session,
    settings: Settings,
    envelope: CommandPollEnvelope,
    *,
    now: datetime | None = None,
) -> list[KillSwitchCommandEnvelope]:
    """Deliver emergency stops before ordinary application commands."""

    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_COMMAND_POLL,
        now=checked_at,
    )
    if device.boot_id != str(envelope.payload.boot_id):
        raise ControlPlaneError("RUNNER_BOOT_MISMATCH")
    if (
        not device.active
        or device.last_seen_at is None
        or checked_at - as_utc(device.last_seen_at)
        > timedelta(seconds=settings.runner_offline_seconds)
    ):
        raise ControlPlaneError("RUNNER_OFFLINE")
    command = db.scalar(
        select(ControlKillSwitchCommand)
        .where(
            ControlKillSwitchCommand.device_id == device.id,
            ControlKillSwitchCommand.runner_boot_id == str(envelope.payload.boot_id),
            or_(
                ControlKillSwitchCommand.status == "queued",
                and_(
                    ControlKillSwitchCommand.status == "claimed",
                    ControlKillSwitchCommand.acknowledged_at.is_(None),
                    ControlKillSwitchCommand.claim_lease_expires_at <= checked_at,
                ),
            ),
            ControlKillSwitchCommand.expires_at > checked_at,
        )
        .order_by(ControlKillSwitchCommand.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if command is None:
        return []
    try:
        signed = KillSwitchCommandEnvelope.model_validate_json(command.signed_envelope_json)
    except ValueError as exc:
        raise ControlPlaneError("KILL_SWITCH_COMMAND_INVALID") from exc
    if (
        command.runner_boot_id != device.boot_id
        or str(signed.payload.boot_id) != command.runner_boot_id
    ):
        raise ControlPlaneError("KILL_SWITCH_COMMAND_BINDING_MISMATCH")
    command.status = "claimed"
    if command.claimed_at is None:
        command.claimed_at = checked_at
    command.claim_lease_expires_at = checked_at + timedelta(seconds=15)
    command.delivery_count += 1
    return [signed]


def acknowledge_kill_switch_command(
    db: Session,
    settings: Settings,
    envelope: CommandAckEnvelope,
    *,
    path_command_id: UUID,
    now: datetime | None = None,
) -> Receipt:
    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_COMMAND_ACK,
        now=checked_at,
    )
    if envelope.payload.command_id != path_command_id:
        raise ControlPlaneError("KILL_SWITCH_COMMAND_BINDING_MISMATCH")
    command = db.scalar(
        select(ControlKillSwitchCommand)
        .where(ControlKillSwitchCommand.id == str(path_command_id))
        .with_for_update()
    )
    if command is None or command.device_id != device.id:
        raise ControlPlaneError("KILL_SWITCH_COMMAND_NOT_FOUND", status_code=404)
    if command.runner_boot_id != device.boot_id:
        raise ControlPlaneError("RUNNER_BOOT_MISMATCH")
    acknowledgement = envelope.payload.ack_status.value
    if command.acknowledged_at is not None:
        if command.ack_status != acknowledgement:
            raise ControlPlaneError("KILL_SWITCH_ACK_CONFLICT")
        return Receipt(identifier=command.id, duplicate=True)
    if (
        command.status != "claimed"
        or command.claimed_at is None
        or as_utc(command.claimed_at) >= as_utc(command.expires_at)
    ):
        raise ControlPlaneError("KILL_SWITCH_COMMAND_NOT_CLAIMED")
    command.ack_status = acknowledgement
    command.acknowledged_at = checked_at
    command.claim_lease_expires_at = None
    command.finished_at = checked_at
    command.status = (
        "acknowledged" if envelope.payload.ack_status is CommandAckStatus.RECEIVED else "rejected"
    )
    return Receipt(identifier=command.id, duplicate=False)


def poll_command(
    db: Session,
    settings: Settings,
    envelope: CommandPollEnvelope,
    *,
    now: datetime | None = None,
) -> list[ControlCommandEnvelope]:
    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_COMMAND_POLL,
        now=checked_at,
    )
    if device.boot_id != str(envelope.payload.boot_id):
        raise ControlPlaneError("RUNNER_BOOT_MISMATCH")
    if (
        device.status != "ready"
        or device.last_seen_at is None
        or checked_at - as_utc(device.last_seen_at)
        > timedelta(seconds=settings.runner_offline_seconds)
    ):
        raise ControlPlaneError("RUNNER_OFFLINE")

    command = db.scalar(
        select(SubmissionCommand)
        .where(
            SubmissionCommand.device_id == device.id,
            or_(
                SubmissionCommand.status == "queued",
                and_(
                    SubmissionCommand.status == "claimed",
                    SubmissionCommand.acknowledged_at.is_(None),
                    SubmissionCommand.claim_lease_expires_at <= checked_at,
                ),
            ),
            SubmissionCommand.expires_at > checked_at,
        )
        .order_by(SubmissionCommand.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if command is None:
        return []
    command.status = "claimed"
    if command.claimed_at is None:
        command.claimed_at = checked_at
    command.claim_lease_expires_at = checked_at + timedelta(seconds=15)
    command.delivery_count += 1
    parsed = json.loads(command.signed_envelope_json)
    return [ControlCommandEnvelope.model_validate(parsed)]


def acknowledge_command(
    db: Session,
    settings: Settings,
    envelope: CommandAckEnvelope,
    *,
    path_command_id: UUID,
    now: datetime | None = None,
) -> Receipt:
    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_COMMAND_ACK,
        now=checked_at,
    )
    if envelope.payload.command_id != path_command_id:
        raise ControlPlaneError("COMMAND_BINDING_MISMATCH")
    command = db.scalar(
        select(SubmissionCommand)
        .where(SubmissionCommand.id == str(path_command_id))
        .with_for_update()
    )
    if command is None or command.device_id != device.id:
        raise ControlPlaneError("COMMAND_NOT_FOUND", status_code=404)
    ack = envelope.payload.ack_status.value
    if command.acknowledged_at is not None:
        if command.ack_status != ack:
            raise ControlPlaneError("COMMAND_ACK_CONFLICT")
        return Receipt(identifier=command.id, duplicate=True)
    if command.status == "rejected" and command.grant.revoked_at is not None:
        if envelope.payload.ack_status is not CommandAckStatus.REJECTED:
            raise ControlPlaneError("COMMAND_ACK_CONFLICT")
        command.ack_status = ack
        command.acknowledged_at = checked_at
        return Receipt(identifier=command.id, duplicate=False)
    if command.status != "claimed":
        raise ControlPlaneError("COMMAND_NOT_CLAIMED")
    if not _command_was_claimed_before_expiry(command):
        raise ControlPlaneError("COMMAND_CLAIM_INVALID")
    command.ack_status = ack
    command.acknowledged_at = checked_at
    command.claim_lease_expires_at = None
    if envelope.payload.ack_status is CommandAckStatus.RECEIVED:
        command.status = "acknowledged"
    else:
        command.status = "rejected"
        command.finished_at = checked_at
    return Receipt(identifier=command.id, duplicate=False)


_STAGE_ORDER = {stage.value: index for index, stage in enumerate(AttemptStage)}


def _command_was_claimed_before_expiry(command: SubmissionCommand) -> bool:
    """Prove the cloud released this command while its authority was live."""

    return bool(
        command.claimed_at is not None and as_utc(command.claimed_at) < as_utc(command.expires_at)
    )


def receive_runner_event(
    db: Session,
    settings: Settings,
    envelope: RunnerEventEnvelope,
    *,
    now: datetime | None = None,
) -> Receipt:
    checked_at = now or utc_now()
    device = verify_runner_envelope(
        db,
        settings,
        envelope,
        expected_purpose=EnvelopePurpose.RUNNER_EVENT,
        now=checked_at,
    )
    payload = envelope.payload
    payload_digest = canonical_model_digest(payload)
    identifier = str(payload.event_id)
    existing = db.get(RunnerEvent, identifier)
    if existing is not None:
        if existing.payload_digest != payload_digest:
            raise ControlPlaneError("EVENT_ID_CONFLICT")
        return Receipt(identifier=identifier, duplicate=True)

    command = db.scalar(
        select(SubmissionCommand)
        .where(SubmissionCommand.id == str(payload.command_id))
        .with_for_update()
    )
    if command is None or command.device_id != device.id:
        raise ControlPlaneError("COMMAND_NOT_FOUND", status_code=404)
    occurred_at = as_utc(payload.occurred_at)
    if command.acknowledged_at is None or command.ack_status != CommandAckStatus.RECEIVED.value:
        recovers_lost_ack = bool(
            command.status == "claimed"
            and command.claimed_at is not None
            and _command_was_claimed_before_expiry(command)
            and payload.sequence == 1
            and payload.stage is AttemptStage.QUEUED
            and occurred_at >= as_utc(command.claimed_at) - timedelta(seconds=30)
            and occurred_at <= as_utc(command.expires_at)
        )
        if not recovers_lost_ack:
            raise ControlPlaneError("COMMAND_NOT_ACKNOWLEDGED")
        # The durable local QUEUED event is created in the same transaction as
        # local admission. Its signed, pre-expiry timestamp is therefore a
        # stronger recovery receipt than a fresh post-expiry ACK. This changes
        # reporting state only; it cannot create or re-run local work.
        command.ack_status = CommandAckStatus.RECEIVED.value
        command.acknowledged_at = checked_at
        command.claim_lease_expires_at = None
    if occurred_at > as_utc(envelope.issued_at) + timedelta(seconds=30) or occurred_at < as_utc(
        command.created_at
    ) - timedelta(seconds=30):
        raise ControlPlaneError("EVENT_TIME_INVALID")

    same_sequence = db.scalar(
        select(RunnerEvent).where(
            RunnerEvent.command_id == command.id,
            RunnerEvent.sequence == payload.sequence,
        )
    )
    if same_sequence is not None:
        raise ControlPlaneError("EVENT_SEQUENCE_CONFLICT")
    previous = db.scalar(
        select(RunnerEvent)
        .where(RunnerEvent.command_id == command.id)
        .order_by(RunnerEvent.sequence.desc())
        .limit(1)
    )
    if command.finished_at is not None:
        safe_reconciliation = bool(
            previous
            and previous.outcome
            in {
                AttemptOutcome.UNKNOWN.value,
                AttemptOutcome.LEGACY_UNVERIFIED.value,
            }
            and payload.stage is AttemptStage.FINISHED
            and payload.outcome
            in {
                AttemptOutcome.OPERATOR_CONFIRMED,
                AttemptOutcome.FAILED_BEFORE_COMMIT,
            }
            and payload.evidence_type is None
        )
        if not safe_reconciliation:
            raise ControlPlaneError("COMMAND_ALREADY_FINISHED")
    expected_sequence = 1 if previous is None else previous.sequence + 1
    if payload.sequence != expected_sequence:
        raise ControlPlaneError("EVENT_SEQUENCE_INVALID")
    if previous is None and payload.stage is not AttemptStage.QUEUED:
        raise ControlPlaneError("EVENT_INITIAL_STAGE_INVALID")
    if previous is not None and _STAGE_ORDER[payload.stage.value] < _STAGE_ORDER[previous.stage]:
        safe_precommit_reset = payload.stage is AttemptStage.QUEUED and previous.stage in {
            AttemptStage.INSPECTING.value,
            AttemptStage.PREPARING.value,
            AttemptStage.READY.value,
        }
        if not safe_precommit_reset:
            raise ControlPlaneError("EVENT_STAGE_REGRESSION")

    event = RunnerEvent(
        id=identifier,
        device_id=device.id,
        command_id=command.id,
        sequence=payload.sequence,
        stage=payload.stage.value,
        outcome=payload.outcome.value if payload.outcome else None,
        reason_code=payload.reason_code.value if payload.reason_code else None,
        evidence_type=payload.evidence_type.value if payload.evidence_type else None,
        evidence_digest=payload.evidence_digest,
        payload_digest=payload_digest,
        envelope_digest=sha256_bytes(canonical_envelope_bytes(envelope)),
        occurred_at=occurred_at,
        received_at=checked_at,
    )
    db.add(event)
    if payload.stage is AttemptStage.FINISHED:
        command.status = "finished"
        if command.finished_at is None:
            command.finished_at = checked_at
    else:
        command.status = "running"
    return Receipt(identifier=identifier, duplicate=False)


__all__ = [
    "CommandCreation",
    "ControlPlaneError",
    "KillCommandCreation",
    "OPERATOR_AUDIT_HARD_CAP",
    "OPERATOR_AUDIT_RETENTION",
    "Receipt",
    "acknowledge_command",
    "acknowledge_kill_switch_command",
    "as_utc",
    "audit",
    "canonical_model_digest",
    "create_command",
    "create_kill_switch_command",
    "poll_command",
    "poll_kill_switch_command",
    "receive_heartbeat",
    "receive_review_grant",
    "receive_review_grant_revocation",
    "receive_runner_event",
    "require_current_schema",
    "sha256_bytes",
    "utc_now",
    "verify_runner_envelope",
]
