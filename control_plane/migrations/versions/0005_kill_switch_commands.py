"""Add signed activation-only kill-switch commands.

Revision ID: 0005_kill_switch_commands
Revises: 0004_remove_login_throttle
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_kill_switch_commands"
down_revision: str | None = "0004_remove_login_throttle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "control_kill_switch_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(36),
            sa.ForeignKey("control_runner_devices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("runner_boot_id", sa.String(36), nullable=False),
        sa.Column("client_idempotency_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("signed_envelope_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_status", sa.String(16), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'acknowledged', 'rejected', 'expired')",
            name="ck_control_kill_switch_commands_status",
        ),
    )
    op.create_index(
        "ix_control_kill_switch_commands_device_id",
        "control_kill_switch_commands",
        ["device_id"],
    )
    op.create_index(
        "ix_control_kill_switch_commands_poll",
        "control_kill_switch_commands",
        ["device_id", "runner_boot_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_kill_switch_commands_poll",
        table_name="control_kill_switch_commands",
    )
    op.drop_index(
        "ix_control_kill_switch_commands_device_id",
        table_name="control_kill_switch_commands",
    )
    op.drop_table("control_kill_switch_commands")
