"""Add replay-safe review-grant revocation tombstones.

Revision ID: 0002_review_grant_revocations
Revises: 0001_control_plane
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_review_grant_revocations"
down_revision = "0001_control_plane"
branch_labels = None
depends_on = None


def _sha256_check_sql(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def upgrade() -> None:
    with op.batch_alter_table("control_review_grants") as batch:
        batch.drop_index("ix_control_review_grants_eligibility")
        batch.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("revocation_envelope_digest", sa.String(length=64), nullable=True)
        )
        batch.create_check_constraint(
            "ck_control_review_grants_revocation_evidence",
            "(revoked_at IS NULL AND revocation_envelope_digest IS NULL) OR "
            "(revoked_at IS NOT NULL AND revocation_envelope_digest IS NOT NULL "
            f"AND {_sha256_check_sql('revocation_envelope_digest')})",
        )
        batch.create_index(
            "ix_control_review_grants_eligibility",
            ["revoked_at", "consumed_at", "expires_at"],
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE control_review_grants "
            "SET consumed_at = revoked_at "
            "WHERE revoked_at IS NOT NULL AND consumed_at IS NULL"
        )
    )
    with op.batch_alter_table("control_review_grants") as batch:
        batch.drop_index("ix_control_review_grants_eligibility")
        batch.drop_constraint(
            "ck_control_review_grants_revocation_evidence",
            type_="check",
        )
        batch.drop_column("revocation_envelope_digest")
        batch.drop_column("revoked_at")
        batch.create_index(
            "ix_control_review_grants_eligibility",
            ["consumed_at", "expires_at"],
        )
