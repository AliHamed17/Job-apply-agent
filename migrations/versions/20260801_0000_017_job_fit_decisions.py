"""Add immutable calibrated job-fit decisions.

Revision ID: 017_job_fit_decisions
Revises: 016_discovery_mesh
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "017_job_fit_decisions"
down_revision: str | None = "016_discovery_mesh"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def upgrade() -> None:
    op.create_table(
        "job_fit_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column("job_digest", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("routing_config_digest", sa.String(64), nullable=False),
        sa.Column("cv_manifest_digest", sa.String(64), nullable=False),
        sa.Column("selected_cv_id", sa.String(255), nullable=True),
        sa.Column("selected_cv_hash", sa.String(64), nullable=True),
        sa.Column("routing_confidence", sa.Float(), nullable=False),
        sa.Column("routing_margin", sa.Float(), nullable=False),
        sa.Column("routing_fallback_reason", sa.String(64), nullable=True),
        sa.Column("fit_score", sa.Float(), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column(
            "quality_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("hard_exclusions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("uncertainty_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("unsupported_skills_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("thresholds_json", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("model_identity", sa.String(64), nullable=False),
        sa.Column("qualification_digest", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "job_id",
            "decision_digest",
            name="uq_job_fit_decisions_job_digest",
        ),
        sa.UniqueConstraint(
            "id",
            "job_id",
            name="uq_job_fit_decisions_id_job",
        ),
        sa.CheckConstraint(
            f"{_sha256_check('decision_digest')} "
            f"AND {_sha256_check('job_digest')} "
            f"AND {_sha256_check('routing_config_digest')} "
            f"AND {_sha256_check('cv_manifest_digest')}",
            name="ck_job_fit_decisions_required_digests",
        ),
        sa.CheckConstraint(
            "selected_cv_hash IS NULL OR " + _sha256_check("selected_cv_hash"),
            name="ck_job_fit_decisions_selected_cv_hash",
        ),
        sa.CheckConstraint(
            "qualification_digest IS NULL OR " + _sha256_check("qualification_digest"),
            name="ck_job_fit_decisions_qualification_digest",
        ),
        sa.CheckConstraint(
            "routing_confidence >= 0 AND routing_confidence <= 1 "
            "AND routing_margin >= 0 AND routing_margin <= 1 "
            "AND fit_score >= 0 AND fit_score <= 100",
            name="ck_job_fit_decisions_metrics",
        ),
        sa.CheckConstraint(
            "disposition IN ('excluded', 'needs_review', 'eligible')",
            name="ck_job_fit_decisions_disposition",
        ),
        sa.CheckConstraint(
            "quality_eligible = false OR "
            "(disposition = 'eligible' AND selected_cv_id IS NOT NULL "
            "AND selected_cv_hash IS NOT NULL AND qualification_digest IS NOT NULL)",
            name="ck_job_fit_decisions_eligibility",
        ),
        sa.CheckConstraint(
            "profile_version IS NULL OR profile_version > 0",
            name="ck_job_fit_decisions_profile_version",
        ),
    )
    op.create_index(
        "ix_job_fit_decisions_job_created",
        "job_fit_decisions",
        ["job_id", "created_at", "id"],
    )
    op.create_index(
        "ix_job_fit_decisions_disposition_created",
        "job_fit_decisions",
        ["disposition", "quality_eligible", "created_at"],
    )

    with op.batch_alter_table("applications") as batch_op:
        batch_op.add_column(sa.Column("cv_routing_margin", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("job_fit_decision_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_applications_job_fit_decision",
            "job_fit_decisions",
            ["job_fit_decision_id", "job_id"],
            ["id", "job_id"],
        )
        batch_op.create_check_constraint(
            "ck_applications_cv_routing_margin",
            "cv_routing_margin IS NULL OR (cv_routing_margin >= 0 AND cv_routing_margin <= 1)",
        )


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_constraint("ck_applications_cv_routing_margin", type_="check")
        batch_op.drop_constraint("fk_applications_job_fit_decision", type_="foreignkey")
        batch_op.drop_column("job_fit_decision_id")
        batch_op.drop_column("cv_routing_margin")

    op.drop_index(
        "ix_job_fit_decisions_disposition_created",
        table_name="job_fit_decisions",
    )
    op.drop_index("ix_job_fit_decisions_job_created", table_name="job_fit_decisions")
    op.drop_table("job_fit_decisions")
