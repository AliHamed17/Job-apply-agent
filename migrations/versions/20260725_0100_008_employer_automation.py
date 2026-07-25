"""Add explicit approval provenance and durable application audit events.

Revision ID: 008_employer_automation
Revises: 007_discovery_runs
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008_employer_automation"
down_revision: str | None = "007_discovery_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("approval_source", sa.String(32), nullable=True))

    op.create_table(
        "application_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_application_events_application_id",
        "application_events",
        ["application_id"],
    )
    op.create_index(
        "ix_application_events_type",
        "application_events",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_application_events_type", table_name="application_events")
    op.drop_index(
        "ix_application_events_application_id",
        table_name="application_events",
    )
    op.drop_table("application_events")
    with op.batch_alter_table("applications") as batch:
        batch.drop_column("approval_source")
