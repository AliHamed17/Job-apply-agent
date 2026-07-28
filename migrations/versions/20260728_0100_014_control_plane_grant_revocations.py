"""Add durable outbound projection state for review-grant revocations.

Revision ID: 014_control_plane_grant_revocations
Revises: 013_vercel_local_control_plane
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014_control_plane_grant_revocations"
down_revision: str | None = "013_vercel_local_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REVOCATION_STATE = (
    "revocation_state IN ('none', 'pending', 'claimed', 'delivered', 'expired') "
    "AND revocation_attempts >= 0"
)

_REVOCATION_METADATA = (
    "(revocation_state = 'none' "
    "AND revocation_available_at IS NULL "
    "AND revocation_claimed_at IS NULL "
    "AND revocation_claimed_by IS NULL "
    "AND revocation_claim_token IS NULL "
    "AND revocation_sent_at IS NULL) "
    "OR (revocation_state = 'pending' "
    "AND revocation_available_at IS NOT NULL "
    "AND revocation_claimed_at IS NULL "
    "AND revocation_claimed_by IS NULL "
    "AND revocation_claim_token IS NULL "
    "AND revocation_sent_at IS NULL) "
    "OR (revocation_state = 'claimed' "
    "AND revocation_available_at IS NOT NULL "
    "AND revocation_claimed_at IS NOT NULL "
    "AND revocation_claimed_by IS NOT NULL "
    "AND revocation_claim_token IS NOT NULL "
    "AND revocation_sent_at IS NULL) "
    "OR (revocation_state = 'delivered' "
    "AND revocation_available_at IS NOT NULL "
    "AND revocation_claimed_at IS NULL "
    "AND revocation_claimed_by IS NULL "
    "AND revocation_claim_token IS NULL "
    "AND revocation_sent_at IS NOT NULL) "
    "OR (revocation_state = 'expired' "
    "AND revocation_available_at IS NOT NULL "
    "AND revocation_claimed_at IS NULL "
    "AND revocation_claimed_by IS NULL "
    "AND revocation_claim_token IS NULL "
    "AND revocation_sent_at IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("control_plane_review_grants") as batch:
        batch.add_column(
            sa.Column(
                "revocation_state",
                sa.String(length=16),
                nullable=False,
                server_default="none",
            )
        )
        batch.add_column(sa.Column("revocation_available_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("revocation_claimed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("revocation_claimed_by", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("revocation_claim_token", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "revocation_attempts",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("revocation_sent_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column("last_revocation_error_code", sa.String(length=64), nullable=True)
        )
        batch.create_check_constraint(
            "ck_control_plane_review_grants_revocation_state",
            _REVOCATION_STATE,
        )
        batch.create_check_constraint(
            "ck_control_plane_review_grants_revocation_metadata",
            _REVOCATION_METADATA,
        )
        batch.create_index(
            "ix_control_plane_review_grants_revocation",
            ["revocation_state", "revocation_available_at"],
        )
    op.execute(
        sa.text(
            "UPDATE control_plane_review_grants "
            "SET revocation_state = CASE "
            "WHEN consumed_at IS NULL AND revoked_at < expires_at "
            "THEN 'pending' ELSE 'expired' END, "
            "revocation_available_at = revoked_at "
            "WHERE revoked_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("control_plane_review_grants") as batch:
        batch.drop_index("ix_control_plane_review_grants_revocation")
        batch.drop_constraint(
            "ck_control_plane_review_grants_revocation_metadata",
            type_="check",
        )
        batch.drop_constraint(
            "ck_control_plane_review_grants_revocation_state",
            type_="check",
        )
        batch.drop_column("last_revocation_error_code")
        batch.drop_column("revocation_sent_at")
        batch.drop_column("revocation_attempts")
        batch.drop_column("revocation_claim_token")
        batch.drop_column("revocation_claimed_by")
        batch.drop_column("revocation_claimed_at")
        batch.drop_column("revocation_available_at")
        batch.drop_column("revocation_state")
