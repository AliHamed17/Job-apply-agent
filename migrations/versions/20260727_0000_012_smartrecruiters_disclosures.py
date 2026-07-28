"""Persist bounded candidate-form disclosures for operator review.

Revision ID: 012_smartrecruiters_disclosures
Revises: 011_workday_browser_v2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012_smartrecruiters_disclosures"
down_revision: str | None = "011_workday_browser_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("form_plans") as batch:
        batch.add_column(
            sa.Column(
                "disclosures_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("form_plans") as batch:
        batch.drop_column("disclosures_json")
