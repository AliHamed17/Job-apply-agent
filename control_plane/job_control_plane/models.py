"""SQLAlchemy models containing only redacted control-plane metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _sha256_check_sql(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


class RunnerDevice(Base):
    __tablename__ = "control_runner_devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    public_key_b64: Mapped[str] = mapped_column(String(43), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    boot_id: Mapped[str | None] = mapped_column(String(36))
    release_digest: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(16))

    review_grants: Mapped[list[ReviewGrant]] = relationship(back_populates="device")
    commands: Mapped[list[SubmissionCommand]] = relationship(back_populates="device")
    kill_commands: Mapped[list[ControlKillSwitchCommand]] = relationship(back_populates="device")


class ReviewGrant(Base):
    __tablename__ = "control_review_grants"
    __table_args__ = (
        Index(
            "ix_control_review_grants_eligibility",
            "revoked_at",
            "consumed_at",
            "expires_at",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revocation_envelope_digest IS NULL) OR "
            "(revoked_at IS NOT NULL AND revocation_envelope_digest IS NOT NULL "
            f"AND {_sha256_check_sql('revocation_envelope_digest')})",
            name="ck_control_review_grants_revocation_evidence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("control_runner_devices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    application_ref: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    application_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter: Mapped[str] = mapped_column(String(24), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    form_fingerprint_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_envelope_digest: Mapped[str | None] = mapped_column(String(64))

    device: Mapped[RunnerDevice] = relationship(back_populates="review_grants")
    command: Mapped[SubmissionCommand | None] = relationship(
        back_populates="grant",
        uselist=False,
    )


class SubmissionCommand(Base):
    __tablename__ = "control_submission_commands"
    __table_args__ = (
        UniqueConstraint("grant_id", name="uq_control_submission_commands_grant_id"),
        Index("ix_control_submission_commands_poll", "device_id", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    grant_id: Mapped[str] = mapped_column(
        ForeignKey("control_review_grants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("control_runner_devices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    application_ref: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    application_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter: Mapped[str] = mapped_column(String(24), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    form_fingerprint_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    client_idempotency_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    signed_envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_status: Mapped[str | None] = mapped_column(String(16))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    grant: Mapped[ReviewGrant] = relationship(back_populates="command")
    device: Mapped[RunnerDevice] = relationship(back_populates="commands")
    events: Mapped[list[RunnerEvent]] = relationship(back_populates="command")


class ControlKillSwitchCommand(Base):
    """Signed, redacted, activation-only emergency-stop command."""

    __tablename__ = "control_kill_switch_commands"
    __table_args__ = (
        Index(
            "ix_control_kill_switch_commands_poll",
            "device_id",
            "status",
            "expires_at",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'acknowledged', 'rejected', 'expired')",
            name="ck_control_kill_switch_commands_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("control_runner_devices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    client_idempotency_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    signed_envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_status: Mapped[str | None] = mapped_column(String(16))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    device: Mapped[RunnerDevice] = relationship(back_populates="kill_commands")


class RunnerNonce(Base):
    __tablename__ = "control_runner_nonces"
    __table_args__ = (
        UniqueConstraint(
            "device_id",
            "purpose",
            "nonce",
            name="uq_control_runner_nonces_device_purpose_nonce",
        ),
        Index("ix_control_runner_nonces_expiry", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("control_runner_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    nonce: Mapped[str] = mapped_column(String(36), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunnerEvent(Base):
    __tablename__ = "control_runner_events"
    __table_args__ = (
        UniqueConstraint(
            "command_id",
            "sequence",
            name="uq_control_runner_events_command_sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("control_runner_devices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    command_id: Mapped[str] = mapped_column(
        ForeignKey("control_submission_commands.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(40))
    evidence_type: Mapped[str | None] = mapped_column(String(40))
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    command: Mapped[SubmissionCommand] = relationship(back_populates="events")


class OperatorSession(Base):
    __tablename__ = "control_operator_sessions"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    session_token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperatorAudit(Base):
    __tablename__ = "control_operator_audit"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(24))
    target_id: Mapped[str | None] = mapped_column(String(36))
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )


__all__ = [
    "ControlKillSwitchCommand",
    "OperatorAudit",
    "OperatorSession",
    "ReviewGrant",
    "RunnerDevice",
    "RunnerEvent",
    "RunnerNonce",
    "SubmissionCommand",
]
