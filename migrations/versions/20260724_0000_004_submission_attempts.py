"""Make submission history immutable and retry-safe.

Revision ID: 004_submission_attempts
Revises: 003_v2_tables
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_submission_attempts"
down_revision: str | None = "003_v2_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE submissionstatus ADD VALUE IF NOT EXISTS 'running'")
        op.execute("ALTER TYPE submissionstatus ADD VALUE IF NOT EXISTS 'unknown'")
        op.drop_constraint(
            "submissions_application_id_key",
            "submissions",
            type_="unique",
        )

    naming = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(
        "submissions", naming_convention=naming, recreate=recreate
    ) as batch:
        if bind.dialect.name == "sqlite":
            batch.drop_constraint("uq_submissions_application_id", type_="unique")
        batch.add_column(sa.Column("attempt_number", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(36), nullable=True))
        batch.add_column(sa.Column("reason_code", sa.String(64), nullable=True))
        batch.add_column(sa.Column("diagnostic_details", sa.Text(), nullable=True))
        batch.add_column(sa.Column("selected_cv_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("profile_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("reconciled_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("reconciliation_note", sa.Text(), nullable=True))

    submissions = sa.table(
        "submissions",
        sa.column("id", sa.Integer()),
        sa.column("attempt_number", sa.Integer()),
        sa.column("idempotency_key", sa.String()),
    )
    rows = bind.execute(sa.select(submissions.c.id)).all()
    for row in rows:
        bind.execute(
            submissions.update()
            .where(submissions.c.id == row.id)
            .values(attempt_number=1, idempotency_key=str(uuid.uuid4()))
        )

    with op.batch_alter_table("submissions", recreate=recreate) as batch:
        batch.alter_column("attempt_number", nullable=False)
        batch.alter_column("idempotency_key", nullable=False)
        batch.create_unique_constraint(
            "uq_submissions_application_attempt",
            ["application_id", "attempt_number"],
        )
        batch.create_unique_constraint(
            "uq_submissions_idempotency_key", ["idempotency_key"]
        )
        batch.create_index("ix_submissions_application_id", ["application_id"])
        batch.create_index("ix_submissions_status", ["status"])


def downgrade() -> None:
    # Multiple attempts cannot be represented by v2. Keep only the latest
    # attempt per application before restoring the legacy uniqueness rule.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM submissions WHERE id NOT IN "
            "(SELECT MAX(id) FROM submissions GROUP BY application_id)"
        )
    )
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("submissions", recreate=recreate) as batch:
        batch.drop_index("ix_submissions_status")
        batch.drop_index("ix_submissions_application_id")
        batch.drop_constraint(
            "uq_submissions_idempotency_key", type_="unique"
        )
        batch.drop_constraint(
            "uq_submissions_application_attempt", type_="unique"
        )
        for name in (
            "reconciliation_note",
            "reconciled_at",
            "finished_at",
            "started_at",
            "profile_version",
            "selected_cv_id",
            "diagnostic_details",
            "reason_code",
            "idempotency_key",
            "attempt_number",
        ):
            batch.drop_column(name)
        batch.create_unique_constraint(
            (
                "submissions_application_id_key"
                if bind.dialect.name == "postgresql"
                else "uq_submissions_application_id"
            ),
            ["application_id"],
        )
