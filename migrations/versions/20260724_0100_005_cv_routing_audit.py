"""Add application CV routing and outcome audit fields.

Revision ID: 005_cv_routing_audit
Revises: 004_submission_attempts
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_cv_routing_audit"
down_revision: str | None = "004_submission_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    sa.Column("selected_cv_id", sa.String(255), nullable=True),
    sa.Column("profile_version", sa.Integer(), nullable=True),
    sa.Column("cv_routing_confidence", sa.Float(), nullable=True),
    sa.Column("cv_routing_evidence", sa.Text(), nullable=True),
    sa.Column("cv_routing_fallback_reason", sa.String(64), nullable=True),
    sa.Column("cv_override_id", sa.String(255), nullable=True),
    sa.Column("outcome", sa.String(32), nullable=True),
    sa.Column("outcome_note", sa.Text(), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        for column in _COLUMNS:
            batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        for column in reversed(_COLUMNS):
            batch.drop_column(column.name)
