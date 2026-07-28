"""Remove the legacy database-backed invalid-login throttle.

Revision ID: 0004_remove_login_throttle
Revises: 0003_login_throttle
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0004_remove_login_throttle"
down_revision = "0003_login_throttle"
branch_labels = None
depends_on = None

_LOGIN_DENIAL_LIMIT = 8
_LOGIN_THROTTLE_ID = "operator_login"


def upgrade() -> None:
    op.drop_table("control_login_throttle")


def downgrade() -> None:
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
            window_started_at=datetime.now(UTC),
            denial_count=0,
            denial_audited_at=None,
        )
    )
