"""Add signed autopilot policy, decisions, and kill-switch history.

Revision ID: 018_signed_autopilot_policy
Revises: 017_job_fit_decisions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "018_signed_autopilot_policy"
down_revision: str | None = "017_job_fit_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def upgrade() -> None:
    op.create_table(
        "automation_policy_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("policy_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("signing_key_id", sa.String(36), nullable=False),
        sa.Column("signature", sa.String(86), nullable=False),
        sa.Column("active_slot", sa.Integer(), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(32), nullable=True),
        sa.Column("revocation_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "policy_id",
            "revision",
            name="uq_automation_policy_revisions_identity",
        ),
        sa.UniqueConstraint(
            "payload_digest",
            name="uq_automation_policy_revisions_digest",
        ),
        sa.UniqueConstraint(
            "active_slot",
            name="uq_automation_policy_revisions_active_slot",
        ),
        sa.CheckConstraint(
            "schema_version = 'auto-submit-policy.v1' "
            "AND revision > 0 AND expires_at > activated_at "
            "AND (active_slot IS NULL OR active_slot = 1)",
            name="ck_automation_policy_revisions_core",
        ),
        sa.CheckConstraint(
            f"{_sha256_check('payload_digest')} AND length(signature) = 86 "
            "AND length(payload_json) BETWEEN 2 AND 32768",
            name="ck_automation_policy_revisions_crypto",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by IS NULL "
            "AND revocation_reason IS NULL AND active_slot = 1) OR "
            "(revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
            "AND revocation_reason IS NOT NULL AND active_slot IS NULL)",
            name="ck_automation_policy_revisions_revocation",
        ),
    )
    op.create_index(
        "ix_automation_policy_revisions_expiry",
        "automation_policy_revisions",
        ["expires_at"],
    )

    op.create_table(
        "autopilot_inspection_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column(
            "policy_revision_id",
            sa.Integer(),
            sa.ForeignKey("automation_policy_revisions.id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("claim_token", sa.String(36), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "application_id",
            "application_revision",
            "policy_revision_id",
            name="uq_autopilot_inspection_runs_exact",
        ),
        sa.CheckConstraint(
            "application_revision > 0 AND state IN ('queued', 'running', 'finished')",
            name="ck_autopilot_inspection_runs_core",
        ),
        sa.CheckConstraint(
            "(state = 'queued' AND claimed_at IS NULL AND lease_expires_at IS NULL "
            "AND claim_token IS NULL "
            "AND finished_at IS NULL AND reason_code IS NULL) OR "
            "(state = 'running' AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND claim_token IS NOT NULL AND lease_expires_at > claimed_at AND finished_at IS NULL "
            "AND reason_code IS NULL) OR "
            "(state = 'finished' AND claimed_at IS NOT NULL AND lease_expires_at IS NULL "
            "AND claim_token IS NULL "
            "AND finished_at IS NOT NULL AND reason_code IS NOT NULL)",
            name="ck_autopilot_inspection_runs_state",
        ),
    )
    op.create_index(
        "ix_autopilot_inspection_runs_claim",
        "autopilot_inspection_runs",
        ["state", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "application_policy_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "policy_revision_id",
            sa.Integer(),
            sa.ForeignKey("automation_policy_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column(
            "fit_decision_id",
            sa.Integer(),
            sa.ForeignKey("job_fit_decisions.id"),
            nullable=False,
        ),
        sa.Column(
            "form_plan_id",
            sa.Integer(),
            sa.ForeignKey("form_plans.id"),
            nullable=False,
        ),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("job_digest", sa.String(64), nullable=False),
        sa.Column("company_digest", sa.String(64), nullable=False),
        sa.Column("fit_decision_digest", sa.String(64), nullable=False),
        sa.Column("form_plan_public_id", sa.String(36), nullable=False),
        sa.Column("form_fingerprint", sa.String(64), nullable=False),
        sa.Column("form_contract_digest", sa.String(64), nullable=False),
        sa.Column("selected_cv_hash", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("confirmed_answer_revision", sa.String(64), nullable=False),
        sa.Column("adapter_name", sa.String(64), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("selector_version", sa.String(64), nullable=False),
        sa.Column("fit_score", sa.Float(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason_codes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("authority_expires_at", sa.DateTime(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "id",
            "decision_digest",
            name="uq_application_policy_decisions_id_digest",
        ),
        sa.UniqueConstraint(
            "application_id",
            "policy_revision_id",
            "application_revision",
            "form_plan_id",
            "decision_digest",
            name="uq_application_policy_decisions_exact",
        ),
        sa.CheckConstraint(
            "application_revision > 0 AND profile_version > 0 "
            "AND fit_score >= 0 AND fit_score <= 100",
            name="ck_application_policy_decisions_metrics",
        ),
        sa.CheckConstraint(
            f"{_sha256_check('decision_digest')} "
            f"AND {_sha256_check('policy_digest')} "
            f"AND {_sha256_check('job_digest')} "
            f"AND {_sha256_check('company_digest')} "
            f"AND {_sha256_check('fit_decision_digest')} "
            f"AND {_sha256_check('form_fingerprint')} "
            f"AND {_sha256_check('form_contract_digest')} "
            f"AND {_sha256_check('selected_cv_hash')} "
            f"AND {_sha256_check('confirmed_answer_revision')}",
            name="ck_application_policy_decisions_digests",
        ),
        sa.CheckConstraint(
            "(allowed = true AND reason_codes_json = '[]' "
            "AND authority_expires_at IS NOT NULL "
            "AND authority_expires_at > evaluated_at) OR "
            "(allowed = false AND reason_codes_json <> '[]' "
            "AND authority_expires_at IS NULL)",
            name="ck_application_policy_decisions_outcome",
        ),
    )
    op.create_index(
        "uq_application_policy_decisions_one_allowed",
        "application_policy_decisions",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("allowed = true"),
        sqlite_where=sa.text("allowed = 1"),
    )
    op.create_index(
        "ix_application_policy_decisions_limits",
        "application_policy_decisions",
        ["allowed", "evaluated_at", "company_digest"],
    )

    op.create_table(
        "automation_kill_switch_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("revision", name="uq_automation_kill_switch_revision"),
        sa.UniqueConstraint("command_digest", name="uq_automation_kill_switch_command"),
        sa.CheckConstraint(
            "revision > 0 AND source IN ('local_operator', 'vercel_signed_kill') "
            "AND length(trim(reason_code)) BETWEEN 2 AND 64",
            name="ck_automation_kill_switch_core",
        ),
        sa.CheckConstraint(
            "command_digest IS NULL OR " + _sha256_check("command_digest"),
            name="ck_automation_kill_switch_command_digest",
        ),
        sa.CheckConstraint(
            "source <> 'vercel_signed_kill' OR (active = true AND command_digest IS NOT NULL)",
            name="ck_automation_kill_switch_remote_only_stops",
        ),
    )
    op.create_index(
        "ix_automation_kill_switch_created",
        "automation_kill_switch_events",
        ["created_at", "id"],
    )

    with op.batch_alter_table("browser_qualification_runs") as batch_op:
        batch_op.add_column(sa.Column("form_contract_digest", sa.String(64), nullable=True))
        batch_op.create_check_constraint(
            "ck_browser_qualification_live_contract",
            "qualification_tier <> 'live_canary_qualified' OR form_contract_digest IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_browser_qualification_contract_digest",
            "form_contract_digest IS NULL OR " + _sha256_check("form_contract_digest"),
        )

    with op.batch_alter_table("submissions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "authority_kind",
                sa.String(32),
                nullable=False,
                server_default="explicit_operator",
            )
        )
        batch_op.add_column(sa.Column("automation_policy_decision_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("automation_policy_decision_digest", sa.String(64), nullable=True)
        )
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
        batch_op.create_foreign_key(
            "fk_submissions_automation_policy_decision",
            "application_policy_decisions",
            ["automation_policy_decision_id", "automation_policy_decision_digest"],
            ["id", "decision_digest"],
        )

    with op.batch_alter_table("final_submit_permits") as batch_op:
        batch_op.add_column(
            sa.Column(
                "authority_kind",
                sa.String(32),
                nullable=False,
                server_default="explicit_operator",
            )
        )
        batch_op.add_column(
            sa.Column("automation_policy_decision_digest", sa.String(64), nullable=True)
        )
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
        batch_op.create_check_constraint(
            "ck_final_submit_permits_policy_digest",
            "automation_policy_decision_digest IS NULL OR "
            + _sha256_check("automation_policy_decision_digest"),
        )


def downgrade() -> None:
    with op.batch_alter_table("final_submit_permits") as batch_op:
        batch_op.drop_constraint("ck_final_submit_permits_policy_digest", type_="check")
        batch_op.drop_constraint("ck_final_submit_permits_automation_authority", type_="check")
        batch_op.drop_constraint("ck_final_submit_permits_authority_kind", type_="check")
        batch_op.drop_column("automation_policy_decision_digest")
        batch_op.drop_column("authority_kind")

    with op.batch_alter_table("submissions") as batch_op:
        batch_op.drop_constraint(
            "fk_submissions_automation_policy_decision",
            type_="foreignkey",
        )
        batch_op.drop_constraint("ck_submissions_automation_authority", type_="check")
        batch_op.drop_constraint("ck_submissions_authority_kind", type_="check")
        batch_op.drop_column("automation_policy_decision_digest")
        batch_op.drop_column("automation_policy_decision_id")
        batch_op.drop_column("authority_kind")

    with op.batch_alter_table("browser_qualification_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_browser_qualification_contract_digest",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_browser_qualification_live_contract",
            type_="check",
        )
        batch_op.drop_column("form_contract_digest")

    op.drop_index(
        "ix_automation_kill_switch_created",
        table_name="automation_kill_switch_events",
    )
    op.drop_table("automation_kill_switch_events")
    op.drop_index(
        "ix_application_policy_decisions_limits",
        table_name="application_policy_decisions",
    )
    op.drop_index(
        "uq_application_policy_decisions_one_allowed",
        table_name="application_policy_decisions",
    )
    op.drop_table("application_policy_decisions")
    op.drop_index(
        "ix_autopilot_inspection_runs_claim",
        table_name="autopilot_inspection_runs",
    )
    op.drop_table("autopilot_inspection_runs")
    op.drop_index(
        "ix_automation_policy_revisions_expiry",
        table_name="automation_policy_revisions",
    )
    op.drop_table("automation_policy_revisions")
