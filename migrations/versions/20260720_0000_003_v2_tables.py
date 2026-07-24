"""Add answer_cache + outbound_contacts tables and v2 columns (Phase 3).

Revision ID: 003_v2_tables
Revises: 002
Create Date: 2026-07-20 00:00:00.000000 UTC
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_v2_tables"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("question_hash"),
    )
    op.create_index("ix_answer_cache_hash", "answer_cache", ["question_hash"])

    op.create_table(
        "outbound_contacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("contact_hash", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("last_contacted_at", sa.DateTime(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_hash"),
    )
    op.create_index("ix_outbound_contact_hash", "outbound_contacts", ["contact_hash"])

    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("discovery_source", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("easy_apply", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
        batch.alter_column("extracted_url_id", existing_type=sa.Integer(), nullable=True)

    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("submission_channel", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("needs_review_reason", sa.Text(), nullable=True))

    # jobs.status / applications.status are backed by a native Postgres enum
    # (created as "jobstatus" in migration 001). SQLite has no real enum type
    # (columns are permissive VARCHAR), so this only matters on Postgres —
    # without it, persisting JobStatus.NEEDS_REVIEW there raises
    # InvalidTextRepresentation the first time a required Easy Apply field
    # can't be answered.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'needs_review'")


def downgrade() -> None:
    # Note: Postgres has no "ALTER TYPE ... DROP VALUE" — an added enum label
    # can't be cleanly removed short of rebuilding the type. 'needs_review'
    # is left in place on downgrade; this is a standard, accepted limitation
    # of Postgres enum migrations and does not affect the column/table drops
    # below.
    with op.batch_alter_table("applications") as batch:
        batch.drop_column("needs_review_reason")
        batch.drop_column("submission_channel")

    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("extracted_url_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("expires_at")
        batch.drop_column("easy_apply")
        batch.drop_column("discovery_source")

    op.drop_index("ix_outbound_contact_hash", table_name="outbound_contacts")
    op.drop_table("outbound_contacts")

    op.drop_index("ix_answer_cache_hash", table_name="answer_cache")
    op.drop_table("answer_cache")
