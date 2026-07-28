"""Add the singleton invalid-login throttle and bound operator audit history.

Revision ID: 0003_login_throttle
Revises: 0002_review_grant_revocations
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision = "0003_login_throttle"
down_revision = "0002_review_grant_revocations"
branch_labels = None
depends_on = None

_AUDIT_HARD_CAP = 5_000
_AUDIT_RETENTION = timedelta(days=30)
_LOGIN_DENIAL_LIMIT = 8
_LOGIN_THROTTLE_ID = "operator_login"


def upgrade() -> None:
    op.create_table(
        "control_login_throttle",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("denial_count", sa.Integer(), nullable=False),
        sa.Column("denial_audited_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"denial_count >= 0 AND denial_count <= {_LOGIN_DENIAL_LIMIT}",
            name=op.f("ck_control_login_throttle_count"),
        ),
        sa.CheckConstraint(
            f"id = '{_LOGIN_THROTTLE_ID}'",
            name=op.f("ck_control_login_throttle_singleton"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_login_throttle")),
    )
    now = datetime.now(UTC)
    op.execute(
        sa.insert(
            sa.table(
                "control_login_throttle",
                sa.column("id", sa.String()),
                sa.column("window_started_at", sa.DateTime(timezone=True)),
                sa.column("denial_count", sa.Integer()),
                sa.column("denial_audited_at", sa.DateTime(timezone=True)),
            )
        ).values(
            id=_LOGIN_THROTTLE_ID,
            window_started_at=now,
            denial_count=0,
            denial_audited_at=None,
        )
    )

    op.create_index(
        op.f("ix_control_operator_audit_created_at"),
        "control_operator_audit",
        ["created_at"],
        unique=False,
    )
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM control_operator_audit WHERE created_at < :retention_cutoff"),
        {"retention_cutoff": now - _AUDIT_RETENTION},
    )
    op.execute(
        sa.text(
            "DELETE FROM control_operator_audit "
            "WHERE id NOT IN ("
            "SELECT id FROM control_operator_audit "
            "ORDER BY created_at DESC, id DESC "
            f"LIMIT {_AUDIT_HARD_CAP}"
            ")"
        )
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_control_operator_audit_created_at"),
        table_name="control_operator_audit",
    )
    op.drop_table("control_login_throttle")
