"""Create the isolated redacted control-plane schema.

Revision ID: 0001_control_plane
Revises:
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_control_plane"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_runner_devices",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("public_key_b64", sa.String(43), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("boot_id", sa.String(36), nullable=True),
        sa.Column("release_digest", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_runner_devices")),
        sa.UniqueConstraint(
            "public_key_b64",
            name=op.f("uq_control_runner_devices_public_key_b64"),
        ),
    )
    op.create_table(
        "control_operator_sessions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("session_token_digest", sa.String(64), nullable=False),
        sa.Column("csrf_token_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_operator_sessions")),
        sa.UniqueConstraint(
            "session_token_digest",
            name=op.f("uq_control_operator_sessions_session_token_digest"),
        ),
    )
    op.create_index(
        op.f("ix_control_operator_sessions_expires_at"),
        "control_operator_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "control_operator_audit",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(24), nullable=True),
        sa.Column("target_id", sa.String(36), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_operator_audit")),
    )
    op.create_index(
        op.f("ix_control_operator_audit_action"),
        "control_operator_audit",
        ["action"],
        unique=False,
    )
    op.create_table(
        "control_review_grants",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("application_ref", sa.String(36), nullable=False),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(24), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("form_fingerprint_digest", sa.String(64), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["control_runner_devices.id"],
            name=op.f("fk_control_review_grants_device_id_control_runner_devices"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_review_grants")),
    )
    op.create_index(
        op.f("ix_control_review_grants_application_ref"),
        "control_review_grants",
        ["application_ref"],
        unique=False,
    )
    op.create_index(
        op.f("ix_control_review_grants_device_id"),
        "control_review_grants",
        ["device_id"],
        unique=False,
    )
    op.create_index(
        "ix_control_review_grants_eligibility",
        "control_review_grants",
        ["consumed_at", "expires_at"],
        unique=False,
    )
    op.create_table(
        "control_submission_commands",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("grant_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("application_ref", sa.String(36), nullable=False),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(24), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("form_fingerprint_digest", sa.String(64), nullable=False),
        sa.Column("client_idempotency_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("signed_envelope_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_count", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_status", sa.String(16), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["control_runner_devices.id"],
            name=op.f("fk_control_submission_commands_device_id_control_runner_devices"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["control_review_grants.id"],
            name=op.f("fk_control_submission_commands_grant_id_control_review_grants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_submission_commands")),
        sa.UniqueConstraint(
            "client_idempotency_digest",
            name=op.f("uq_control_submission_commands_client_idempotency_digest"),
        ),
        sa.UniqueConstraint(
            "grant_id",
            name="uq_control_submission_commands_grant_id",
        ),
    )
    op.create_index(
        op.f("ix_control_submission_commands_application_ref"),
        "control_submission_commands",
        ["application_ref"],
        unique=False,
    )
    op.create_index(
        op.f("ix_control_submission_commands_device_id"),
        "control_submission_commands",
        ["device_id"],
        unique=False,
    )
    op.create_index(
        "ix_control_submission_commands_poll",
        "control_submission_commands",
        ["device_id", "status", "expires_at"],
        unique=False,
    )
    op.create_table(
        "control_runner_nonces",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("nonce", sa.String(36), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["control_runner_devices.id"],
            name=op.f("fk_control_runner_nonces_device_id_control_runner_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_runner_nonces")),
        sa.UniqueConstraint(
            "device_id",
            "purpose",
            "nonce",
            name="uq_control_runner_nonces_device_purpose_nonce",
        ),
    )
    op.create_index(
        "ix_control_runner_nonces_expiry",
        "control_runner_nonces",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "control_runner_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("command_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("evidence_type", sa.String(40), nullable=True),
        sa.Column("evidence_digest", sa.String(64), nullable=True),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["control_submission_commands.id"],
            name=op.f("fk_control_runner_events_command_id_control_submission_commands"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["control_runner_devices.id"],
            name=op.f("fk_control_runner_events_device_id_control_runner_devices"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_runner_events")),
        sa.UniqueConstraint(
            "command_id",
            "sequence",
            name="uq_control_runner_events_command_sequence",
        ),
    )
    op.create_index(
        op.f("ix_control_runner_events_command_id"),
        "control_runner_events",
        ["command_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_control_runner_events_device_id"),
        "control_runner_events",
        ["device_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_control_runner_events_device_id"),
        table_name="control_runner_events",
    )
    op.drop_index(
        op.f("ix_control_runner_events_command_id"),
        table_name="control_runner_events",
    )
    op.drop_table("control_runner_events")
    op.drop_index("ix_control_runner_nonces_expiry", table_name="control_runner_nonces")
    op.drop_table("control_runner_nonces")
    op.drop_index(
        "ix_control_submission_commands_poll",
        table_name="control_submission_commands",
    )
    op.drop_index(
        op.f("ix_control_submission_commands_device_id"),
        table_name="control_submission_commands",
    )
    op.drop_index(
        op.f("ix_control_submission_commands_application_ref"),
        table_name="control_submission_commands",
    )
    op.drop_table("control_submission_commands")
    op.drop_index(
        "ix_control_review_grants_eligibility",
        table_name="control_review_grants",
    )
    op.drop_index(
        op.f("ix_control_review_grants_device_id"),
        table_name="control_review_grants",
    )
    op.drop_index(
        op.f("ix_control_review_grants_application_ref"),
        table_name="control_review_grants",
    )
    op.drop_table("control_review_grants")
    op.drop_index(
        op.f("ix_control_operator_audit_action"),
        table_name="control_operator_audit",
    )
    op.drop_table("control_operator_audit")
    op.drop_index(
        op.f("ix_control_operator_sessions_expires_at"),
        table_name="control_operator_sessions",
    )
    op.drop_table("control_operator_sessions")
    op.drop_table("control_runner_devices")
