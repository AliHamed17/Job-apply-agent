"""Add privacy-safe browser qualification records.

Revision ID: 006_browser_qualification
Revises: 005_cv_routing_audit
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_browser_qualification"
down_revision: str | None = "005_cv_routing_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_qualification_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("selector_version", sa.String(64), nullable=False),
        sa.Column("terminal_reason", sa.String(64), nullable=False),
        sa.Column("qualified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trace_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_browser_qualification_selector_reason",
        "browser_qualification_runs",
        ["selector_version", "terminal_reason"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_browser_qualification_selector_reason",
        table_name="browser_qualification_runs",
    )
    op.drop_table("browser_qualification_runs")
