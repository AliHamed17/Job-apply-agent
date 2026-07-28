"""Add the private local control-plane bridge persistence boundary.

Revision ID: 013_vercel_local_control_plane
Revises: 012_smartrecruiters_disclosures
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013_vercel_local_control_plane"
down_revision: str | None = "012_smartrecruiters_disclosures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_check_sql(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def upgrade() -> None:
    op.create_table(
        "control_plane_application_refs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("remote_ref", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_projected_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "length(remote_ref) BETWEEN 32 AND 64",
            name="ck_control_plane_application_refs_bounded",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id",
            name="uq_control_plane_application_refs_application_id",
        ),
        sa.UniqueConstraint(
            "remote_ref",
            name="uq_control_plane_application_refs_remote_ref",
        ),
    )

    op.create_table(
        "control_plane_review_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("grant_ref", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("application_ref_id", sa.Integer(), nullable=False),
        sa.Column("form_plan_id", sa.Integer(), nullable=False),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column("job_url_hash", sa.String(length=64), nullable=False),
        sa.Column("form_plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cv_hash", sa.String(length=64), nullable=False),
        sa.Column("adapter_name", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("selector_version", sa.String(length=64), nullable=False),
        sa.Column("runner_release", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_command_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "projection_state",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "projection_available_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("projection_claimed_at", sa.DateTime(), nullable=True),
        sa.Column("projection_claimed_by", sa.String(length=64), nullable=True),
        sa.Column("projection_claim_token", sa.String(length=64), nullable=True),
        sa.Column(
            "projection_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("projected_at", sa.DateTime(), nullable=True),
        sa.Column("last_projection_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "length(grant_ref) BETWEEN 32 AND 64 "
            "AND application_revision > 0 "
            "AND length(trim(adapter_name)) BETWEEN 1 AND 64 "
            "AND length(trim(adapter_version)) BETWEEN 1 AND 32 "
            "AND length(trim(selector_version)) BETWEEN 1 AND 64 "
            "AND length(trim(runner_release)) BETWEEN 1 AND 64",
            name="ck_control_plane_review_grants_metadata",
        ),
        sa.CheckConstraint(
            f"{_sha256_check_sql('job_url_hash')} "
            f"AND {_sha256_check_sql('form_plan_fingerprint')} "
            f"AND {_sha256_check_sql('cv_hash')}",
            name="ck_control_plane_review_grants_digests",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_control_plane_review_grants_lifetime",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND consumed_command_ref IS NULL) OR "
            "(consumed_at IS NOT NULL AND consumed_command_ref IS NOT NULL "
            "AND length(trim(consumed_command_ref)) BETWEEN 1 AND 64)",
            name="ck_control_plane_review_grants_consumption",
        ),
        sa.CheckConstraint(
            "projection_state IN ('pending', 'claimed', 'projected') AND projection_attempts >= 0",
            name="ck_control_plane_review_grants_projection_state",
        ),
        sa.CheckConstraint(
            "(projection_state = 'pending' "
            "AND projection_claimed_at IS NULL "
            "AND projection_claimed_by IS NULL "
            "AND projection_claim_token IS NULL "
            "AND projected_at IS NULL) "
            "OR (projection_state = 'claimed' "
            "AND projection_claimed_at IS NOT NULL "
            "AND projection_claimed_by IS NOT NULL "
            "AND projection_claim_token IS NOT NULL "
            "AND projected_at IS NULL) "
            "OR (projection_state = 'projected' "
            "AND projection_claimed_at IS NULL "
            "AND projection_claimed_by IS NULL "
            "AND projection_claim_token IS NULL "
            "AND projected_at IS NOT NULL)",
            name="ck_control_plane_review_grants_projection_metadata",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.ForeignKeyConstraint(
            ["application_ref_id"],
            ["control_plane_application_refs.id"],
        ),
        sa.ForeignKeyConstraint(["form_plan_id"], ["form_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "grant_ref",
            name="uq_control_plane_review_grants_grant_ref",
        ),
    )
    op.create_index(
        "ix_control_plane_review_grants_application",
        "control_plane_review_grants",
        ["application_id", "issued_at"],
    )
    op.create_index(
        "ix_control_plane_review_grants_expiry",
        "control_plane_review_grants",
        ["expires_at"],
    )
    op.create_index(
        "ix_control_plane_review_grants_projection",
        "control_plane_review_grants",
        ["projection_state", "projection_available_at"],
    )

    op.create_table(
        "control_plane_command_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("remote_command_ref", sa.String(length=64), nullable=False),
        sa.Column("remote_attempt_ref", sa.String(length=64), nullable=False),
        sa.Column("review_grant_id", sa.Integer(), nullable=False),
        sa.Column("delivery_nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("envelope_digest", sa.String(length=64), nullable=False),
        sa.Column("client_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(remote_command_ref) BETWEEN 16 AND 64 "
            "AND length(remote_attempt_ref) BETWEEN 32 AND 64 "
            "AND length(client_idempotency_key) BETWEEN 16 AND 128",
            name="ck_control_plane_command_receipts_metadata",
        ),
        sa.CheckConstraint(
            f"{_sha256_check_sql('delivery_nonce_hash')} "
            f"AND {_sha256_check_sql('envelope_digest')}",
            name="ck_control_plane_command_receipts_digests",
        ),
        sa.ForeignKeyConstraint(
            ["review_grant_id"],
            ["control_plane_review_grants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "remote_command_ref",
            name="uq_control_plane_command_receipts_command_ref",
        ),
        sa.UniqueConstraint(
            "remote_attempt_ref",
            name="uq_control_plane_command_receipts_attempt_ref",
        ),
        sa.UniqueConstraint(
            "review_grant_id",
            name="uq_control_plane_command_receipts_review_grant",
        ),
        sa.UniqueConstraint(
            "delivery_nonce_hash",
            name="uq_control_plane_command_receipts_delivery_nonce",
        ),
        sa.UniqueConstraint(
            "client_idempotency_key",
            name="uq_control_plane_command_receipts_idempotency",
        ),
    )

    op.create_table(
        "control_plane_event_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_ref", sa.String(length=64), nullable=False),
        sa.Column("remote_command_ref", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "cycle",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column(
            "delivery_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(event_ref) BETWEEN 32 AND 64 "
            "AND length(remote_command_ref) BETWEEN 16 AND 64 "
            "AND sequence > 0 AND cycle >= 0 "
            "AND length(trim(event_type)) BETWEEN 1 AND 64 "
            "AND length(payload_json) BETWEEN 2 AND 4096 "
            "AND delivery_count >= 0",
            name="ck_control_plane_event_outbox_metadata",
        ),
        sa.CheckConstraint(
            _sha256_check_sql("payload_digest"),
            name="ck_control_plane_event_outbox_digest",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'sent')",
            name="ck_control_plane_event_outbox_state",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL AND sent_at IS NULL) "
            "OR (state = 'claimed' AND claimed_at IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claim_token IS NOT NULL "
            "AND sent_at IS NULL) "
            "OR (state = 'sent' AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL "
            "AND sent_at IS NOT NULL)",
            name="ck_control_plane_event_outbox_state_metadata",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_ref",
            name="uq_control_plane_event_outbox_event_ref",
        ),
        sa.UniqueConstraint(
            "remote_command_ref",
            "sequence",
            name="uq_control_plane_event_outbox_command_sequence",
        ),
    )
    op.create_index(
        "ix_control_plane_event_outbox_state_available",
        "control_plane_event_outbox",
        ["state", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_plane_event_outbox_state_available",
        table_name="control_plane_event_outbox",
    )
    op.drop_table("control_plane_event_outbox")
    op.drop_table("control_plane_command_receipts")
    op.drop_index(
        "ix_control_plane_review_grants_projection",
        table_name="control_plane_review_grants",
    )
    op.drop_index(
        "ix_control_plane_review_grants_expiry",
        table_name="control_plane_review_grants",
    )
    op.drop_index(
        "ix_control_plane_review_grants_application",
        table_name="control_plane_review_grants",
    )
    op.drop_table("control_plane_review_grants")
    op.drop_table("control_plane_application_refs")
