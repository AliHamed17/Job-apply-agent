"""Initial schema — all MVP tables.

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000 UTC
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("whatsapp_message_id", sa.String(length=255), nullable=False),
        sa.Column("sender_phone", sa.String(length=50), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("correlation_id", sa.String(length=20), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("whatsapp_message_id"),
    )
    op.create_index("ix_messages_whatsapp_id", "messages", ["whatsapp_message_id"])

    op.create_table(
        "extracted_urls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "fetched", "failed", "blocked", name="urlstatus"),
            nullable=False,
        ),
        sa.Column("fetch_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extracted_urls_hash", "extracted_urls", ["url_hash"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("extracted_url_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("company", sa.String(length=300), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("employment_type", sa.String(length=100), nullable=True),
        sa.Column("seniority", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("requirements", sa.Text(), nullable=True),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("date_posted", sa.String(length=50), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("apply_url_hash", sa.String(length=64), nullable=True),
        sa.Column("job_signature", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "extracted", "scored", "skipped", "draft",
                "approved", "submitted", "failed",
                name="jobstatus",
            ),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["extracted_url_id"], ["extracted_urls.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_apply_url_hash", "jobs", ["apply_url_hash"])
    op.create_index("ix_jobs_signature", "jobs", ["job_signature"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("cover_letter", sa.Text(), nullable=True),
        sa.Column("recruiter_message", sa.Text(), nullable=True),
        sa.Column("qa_answers", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "extracted", "scored", "skipped", "draft",
                "approved", "submitted", "failed",
                name="jobstatus",
            ),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("submitter_name", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "success", "failed", "draft_only", name="submissionstatus"),
            nullable=False,
        ),
        sa.Column("confirmation_url", sa.Text(), nullable=True),
        sa.Column("confirmation_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id"),
    )

    op.create_table(
        "user_profile_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_yaml", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("user_profile_versions")
    op.drop_table("submissions")
    op.drop_table("applications")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_signature", table_name="jobs")
    op.drop_index("ix_jobs_apply_url_hash", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_extracted_urls_hash", table_name="extracted_urls")
    op.drop_table("extracted_urls")
    op.drop_index("ix_messages_whatsapp_id", table_name="messages")
    op.drop_table("messages")
