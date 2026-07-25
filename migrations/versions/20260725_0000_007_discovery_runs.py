"""Add durable discovery provider run status.

Revision ID: 007_discovery_runs
Revises: 006_browser_qualification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007_discovery_runs"
down_revision: str | None = "006_browser_qualification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_discovery_runs_source_finished",
        "discovery_runs",
        ["source", "finished_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_discovery_runs_source_finished", table_name="discovery_runs")
    op.drop_table("discovery_runs")
