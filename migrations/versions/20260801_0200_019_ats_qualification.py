"""Add strict ATS qualification and one-use canary authority.

Revision ID: 019_ats_qualification
Revises: 018_signed_autopilot_policy
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019_ats_qualification"
down_revision: str | None = "018_signed_autopilot_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def upgrade() -> None:
    op.create_table(
        "adapter_qualification_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("qualification_tier", sa.String(32), nullable=False),
        sa.Column("adapter_name", sa.String(64), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("selector_version", sa.String(64), nullable=False),
        sa.Column("execution_contract_version", sa.String(32), nullable=False),
        sa.Column("form_fingerprint", sa.String(64), nullable=False),
        sa.Column("form_contract_digest", sa.String(64), nullable=False),
        sa.Column("fixture_digest", sa.String(64), nullable=False),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column(
            "form_plan_id",
            sa.Integer(),
            sa.ForeignKey("form_plans.id"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.Integer(), nullable=True),
        sa.Column("job_url_hash", sa.String(64), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("runner_release", sa.String(64), nullable=False),
        sa.Column("qualified_at", sa.DateTime(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        sa.Column("invalidation_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "qualification_tier IN ('dry_run_qualified', 'live_canary_qualified')",
            name="ck_adapter_qualification_records_tier",
        ),
        sa.CheckConstraint(
            "length(trim(adapter_name)) BETWEEN 1 AND 64 "
            "AND length(trim(adapter_version)) BETWEEN 1 AND 32 "
            "AND length(trim(selector_version)) BETWEEN 1 AND 64 "
            "AND length(trim(execution_contract_version)) BETWEEN 1 AND 32 "
            "AND length(trim(runner_release)) BETWEEN 1 AND 64 "
            "AND application_revision > 0",
            name="ck_adapter_qualification_records_metadata",
        ),
        sa.CheckConstraint(
            f"{_sha256_check('form_fingerprint')} "
            f"AND {_sha256_check('form_contract_digest')} "
            f"AND {_sha256_check('fixture_digest')} "
            f"AND {_sha256_check('job_url_hash')} "
            f"AND {_sha256_check('evidence_digest')}",
            name="ck_adapter_qualification_records_digests",
        ),
        sa.CheckConstraint(
            "(qualification_tier = 'dry_run_qualified' AND attempt_id IS NULL) OR "
            "(qualification_tier = 'live_canary_qualified' AND attempt_id IS NOT NULL)",
            name="ck_adapter_qualification_records_attempt",
        ),
        sa.CheckConstraint(
            "(invalidated_at IS NULL AND invalidation_reason IS NULL) OR "
            "(invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL "
            "AND length(trim(invalidation_reason)) BETWEEN 2 AND 64)",
            name="ck_adapter_qualification_records_invalidation",
        ),
        sa.UniqueConstraint(
            "qualification_tier",
            "application_id",
            "application_revision",
            "form_plan_id",
            "adapter_name",
            "adapter_version",
            "selector_version",
            "form_fingerprint",
            "runner_release",
            name="uq_adapter_qualification_records_observation",
        ),
        sa.UniqueConstraint(
            "attempt_id",
            name="uq_adapter_qualification_records_attempt",
        ),
    )
    op.create_index(
        "ix_adapter_qualification_records_effective",
        "adapter_qualification_records",
        [
            "adapter_name",
            "adapter_version",
            "selector_version",
            "qualification_tier",
            "form_contract_digest",
            "runner_release",
            "invalidated_at",
        ],
    )
    op.create_index(
        "ix_adapter_qualification_records_application",
        "adapter_qualification_records",
        ["application_id", "application_revision", "form_plan_id"],
    )

    op.create_table(
        "qualification_canary_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("authorization_digest", sa.String(64), nullable=False),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column(
            "form_plan_id",
            sa.Integer(),
            sa.ForeignKey("form_plans.id"),
            nullable=False,
        ),
        sa.Column(
            "dry_run_qualification_id",
            sa.Integer(),
            sa.ForeignKey("adapter_qualification_records.id"),
            nullable=False,
        ),
        sa.Column("adapter_name", sa.String(64), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("selector_version", sa.String(64), nullable=False),
        sa.Column("execution_contract_version", sa.String(32), nullable=False),
        sa.Column("form_fingerprint", sa.String(64), nullable=False),
        sa.Column("form_contract_digest", sa.String(64), nullable=False),
        sa.Column("selected_cv_hash", sa.String(64), nullable=False),
        sa.Column("job_url_hash", sa.String(64), nullable=False),
        sa.Column("runner_release", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revocation_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "id",
            "authorization_digest",
            name="uq_qualification_canary_authorizations_id_digest",
        ),
        sa.UniqueConstraint(
            "authorization_digest",
            name="uq_qualification_canary_authorizations_digest",
        ),
        sa.UniqueConstraint(
            "nonce_hash",
            name="uq_qualification_canary_authorizations_nonce",
        ),
        sa.CheckConstraint(
            "application_revision > 0 AND expires_at > issued_at "
            "AND length(trim(adapter_name)) BETWEEN 1 AND 64 "
            "AND length(trim(adapter_version)) BETWEEN 1 AND 32 "
            "AND length(trim(selector_version)) BETWEEN 1 AND 64 "
            "AND length(trim(execution_contract_version)) BETWEEN 1 AND 32 "
            "AND length(trim(runner_release)) BETWEEN 1 AND 64",
            name="ck_qualification_canary_authorizations_metadata",
        ),
        sa.CheckConstraint(
            f"{_sha256_check('authorization_digest')} "
            f"AND {_sha256_check('nonce_hash')} "
            f"AND {_sha256_check('form_fingerprint')} "
            f"AND {_sha256_check('form_contract_digest')} "
            f"AND {_sha256_check('selected_cv_hash')} "
            f"AND {_sha256_check('job_url_hash')}",
            name="ck_qualification_canary_authorizations_digests",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND revoked_at IS NULL AND revocation_reason IS NULL) OR "
            "(consumed_at IS NOT NULL AND revoked_at IS NULL AND revocation_reason IS NULL) OR "
            "(consumed_at IS NULL AND revoked_at IS NOT NULL "
            "AND revocation_reason IS NOT NULL "
            "AND length(trim(revocation_reason)) BETWEEN 2 AND 64)",
            name="ck_qualification_canary_authorizations_state",
        ),
    )
    op.create_index(
        "ix_qualification_canary_authorizations_application",
        "qualification_canary_authorizations",
        ["application_id", "application_revision", "form_plan_id"],
    )
    op.create_index(
        "ix_qualification_canary_authorizations_expiry",
        "qualification_canary_authorizations",
        ["expires_at", "consumed_at", "revoked_at"],
    )

    with op.batch_alter_table("submissions") as batch_op:
        batch_op.drop_constraint("ck_submissions_automation_authority", type_="check")
        batch_op.drop_constraint("ck_submissions_authority_kind", type_="check")
        batch_op.add_column(
            sa.Column("qualification_canary_authorization_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("qualification_canary_authorization_digest", sa.String(64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_submissions_authority_kind",
            "authority_kind IN ('explicit_operator', 'control_plane', "
            "'qualified_autopilot', 'qualification_canary', 'legacy')",
        )
        batch_op.create_check_constraint(
            "ck_submissions_automation_authority",
            "(authority_kind = 'qualified_autopilot' "
            "AND automation_policy_decision_id IS NOT NULL "
            "AND automation_policy_decision_digest IS NOT NULL "
            "AND qualification_canary_authorization_id IS NULL "
            "AND qualification_canary_authorization_digest IS NULL) OR "
            "(authority_kind = 'qualification_canary' "
            "AND automation_policy_decision_id IS NULL "
            "AND automation_policy_decision_digest IS NULL "
            "AND qualification_canary_authorization_id IS NOT NULL "
            "AND qualification_canary_authorization_digest IS NOT NULL) OR "
            "(authority_kind NOT IN ('qualified_autopilot', 'qualification_canary') "
            "AND automation_policy_decision_id IS NULL "
            "AND automation_policy_decision_digest IS NULL "
            "AND qualification_canary_authorization_id IS NULL "
            "AND qualification_canary_authorization_digest IS NULL)",
        )
        batch_op.create_foreign_key(
            "fk_submissions_qualification_canary_authorization",
            "qualification_canary_authorizations",
            [
                "qualification_canary_authorization_id",
                "qualification_canary_authorization_digest",
            ],
            ["id", "authorization_digest"],
        )
        batch_op.create_unique_constraint(
            "uq_submissions_qualification_canary_authorization",
            ["qualification_canary_authorization_id"],
        )

    with op.batch_alter_table("adapter_qualification_records") as batch_op:
        batch_op.create_foreign_key(
            "fk_adapter_qualification_records_attempt",
            "submissions",
            ["attempt_id"],
            ["id"],
        )

    with op.batch_alter_table("final_submit_permits") as batch_op:
        batch_op.drop_constraint("ck_final_submit_permits_automation_authority", type_="check")
        batch_op.drop_constraint("ck_final_submit_permits_authority_kind", type_="check")
        batch_op.add_column(
            sa.Column("qualification_canary_authorization_digest", sa.String(64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_final_submit_permits_authority_kind",
            "authority_kind IN ('explicit_operator', 'control_plane', "
            "'qualified_autopilot', 'qualification_canary', 'legacy')",
        )
        batch_op.create_check_constraint(
            "ck_final_submit_permits_automation_authority",
            "(authority_kind = 'qualified_autopilot' "
            "AND automation_policy_decision_digest IS NOT NULL "
            "AND qualification_canary_authorization_digest IS NULL) OR "
            "(authority_kind = 'qualification_canary' "
            "AND automation_policy_decision_digest IS NULL "
            "AND qualification_canary_authorization_digest IS NOT NULL) OR "
            "(authority_kind NOT IN ('qualified_autopilot', 'qualification_canary') "
            "AND automation_policy_decision_digest IS NULL "
            "AND qualification_canary_authorization_digest IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_final_submit_permits_canary_digest",
            "qualification_canary_authorization_digest IS NULL OR "
            + _sha256_check("qualification_canary_authorization_digest"),
        )


def downgrade() -> None:
    with op.batch_alter_table("final_submit_permits") as batch_op:
        batch_op.drop_constraint("ck_final_submit_permits_canary_digest", type_="check")
        batch_op.drop_constraint("ck_final_submit_permits_automation_authority", type_="check")
        batch_op.drop_constraint("ck_final_submit_permits_authority_kind", type_="check")

    op.execute(
        sa.text(
            "UPDATE final_submit_permits SET authority_kind = 'legacy', "
            "automation_policy_decision_digest = NULL, "
            "qualification_canary_authorization_digest = NULL "
            "WHERE authority_kind = 'qualification_canary'"
        )
    )

    with op.batch_alter_table("final_submit_permits") as batch_op:
        batch_op.drop_column("qualification_canary_authorization_digest")
        batch_op.create_check_constraint(
            "ck_final_submit_permits_authority_kind",
            "authority_kind IN ('explicit_operator', 'control_plane', "
            "'qualified_autopilot', 'legacy')",
        )
        batch_op.create_check_constraint(
            "ck_final_submit_permits_automation_authority",
            "(authority_kind = 'qualified_autopilot' "
            "AND automation_policy_decision_digest IS NOT NULL) OR "
            "(authority_kind <> 'qualified_autopilot' "
            "AND automation_policy_decision_digest IS NULL)",
        )

    with op.batch_alter_table("submissions") as batch_op:
        batch_op.drop_constraint(
            "uq_submissions_qualification_canary_authorization",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_submissions_qualification_canary_authorization",
            type_="foreignkey",
        )
        batch_op.drop_constraint("ck_submissions_automation_authority", type_="check")
        batch_op.drop_constraint("ck_submissions_authority_kind", type_="check")

    op.execute(
        sa.text(
            "UPDATE submissions SET authority_kind = 'legacy', "
            "automation_policy_decision_id = NULL, "
            "automation_policy_decision_digest = NULL, "
            "qualification_canary_authorization_id = NULL, "
            "qualification_canary_authorization_digest = NULL "
            "WHERE authority_kind = 'qualification_canary'"
        )
    )

    with op.batch_alter_table("submissions") as batch_op:
        batch_op.drop_column("qualification_canary_authorization_digest")
        batch_op.drop_column("qualification_canary_authorization_id")
        batch_op.create_check_constraint(
            "ck_submissions_authority_kind",
            "authority_kind IN ('explicit_operator', 'control_plane', "
            "'qualified_autopilot', 'legacy')",
        )
        batch_op.create_check_constraint(
            "ck_submissions_automation_authority",
            "(authority_kind = 'qualified_autopilot' "
            "AND automation_policy_decision_id IS NOT NULL "
            "AND automation_policy_decision_digest IS NOT NULL) OR "
            "(authority_kind <> 'qualified_autopilot' "
            "AND automation_policy_decision_id IS NULL "
            "AND automation_policy_decision_digest IS NULL)",
        )

    with op.batch_alter_table("adapter_qualification_records") as batch_op:
        batch_op.drop_constraint(
            "fk_adapter_qualification_records_attempt",
            type_="foreignkey",
        )

    op.drop_index(
        "ix_qualification_canary_authorizations_expiry",
        table_name="qualification_canary_authorizations",
    )
    op.drop_index(
        "ix_qualification_canary_authorizations_application",
        table_name="qualification_canary_authorizations",
    )
    op.drop_table("qualification_canary_authorizations")
    op.drop_index(
        "ix_adapter_qualification_records_application",
        table_name="adapter_qualification_records",
    )
    op.drop_index(
        "ix_adapter_qualification_records_effective",
        table_name="adapter_qualification_records",
    )
    op.drop_table("adapter_qualification_records")
