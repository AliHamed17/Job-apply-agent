"""Add cover_letter_feedback table (Phase 10 feedback loop).

Revision ID: 002
Revises: 001
Create Date: 2024-02-01 00:00:00.000000 UTC
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cover_letter_feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("corrected_text", sa.Text(), nullable=False),
        sa.Column("feedback_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cover_letter_feedback_app_id",
        "cover_letter_feedback",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_cover_letter_feedback_app_id", table_name="cover_letter_feedback")
    op.drop_table("cover_letter_feedback")
