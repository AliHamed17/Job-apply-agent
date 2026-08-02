"""Add redacted runner operations summaries.

Revision ID: 0006_runner_operations_summary
Revises: 0005_kill_switch_commands
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_runner_operations_summary"
down_revision: str | None = "0005_kill_switch_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "control_runner_devices",
        sa.Column("operations_digest", sa.String(64), nullable=True),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column(
            "policy_status",
            sa.String(16),
            nullable=False,
            server_default="unavailable",
        ),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column("policy_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column("policy_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column(
            "policy_daily_remaining",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column(
            "policy_hourly_remaining",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column(
            "kill_switch_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column(
            "pipeline_counters_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column(
            "source_status_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "control_runner_devices",
        sa.Column(
            "adapter_status_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("control_runner_devices", "adapter_status_json")
    op.drop_column("control_runner_devices", "source_status_json")
    op.drop_column("control_runner_devices", "pipeline_counters_json")
    op.drop_column("control_runner_devices", "kill_switch_active")
    op.drop_column("control_runner_devices", "policy_hourly_remaining")
    op.drop_column("control_runner_devices", "policy_daily_remaining")
    op.drop_column("control_runner_devices", "policy_expires_at")
    op.drop_column("control_runner_devices", "policy_revision")
    op.drop_column("control_runner_devices", "policy_status")
    op.drop_column("control_runner_devices", "operations_digest")
