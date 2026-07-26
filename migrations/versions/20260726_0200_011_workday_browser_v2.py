"""Add versioned browser-adapter qualification metadata.

Revision ID: 011_workday_browser_v2
Revises: 010_ollama_form_plan_v1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011_workday_browser_v2"
down_revision: str | None = "010_ollama_form_plan_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_check_sql(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def upgrade() -> None:
    with op.batch_alter_table("operator_approved_answers") as batch:
        # Legacy reusable rows cannot prove an exact field contract and remain
        # NULL/fail-closed. New confirmations always persist this digest.
        batch.add_column(
            sa.Column(
                "field_contract_fingerprint",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch.create_check_constraint(
            "ck_operator_approved_answers_field_contract",
            "field_contract_fingerprint IS NULL OR "
            f"{_sha256_check_sql('field_contract_fingerprint')}",
        )
    op.create_index(
        "ix_operator_approved_answers_field_contract",
        "operator_approved_answers",
        [
            "adapter_name",
            "adapter_version",
            "selector_version",
            "field_contract_fingerprint",
            "revoked_at",
        ],
    )

    with op.batch_alter_table("form_plans") as batch:
        # Revision 010 plans did not persist the source/time of attachment
        # evidence.  Leave those columns NULL for legacy rows so they remain
        # review-ineligible rather than manufacturing retrospective proof.
        batch.add_column(
            sa.Column(
                "attachment_verification_source",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("attachment_verified_at", sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            "ck_form_plans_attachment_evidence_metadata",
            "(attachment_verification_source IS NULL "
            "AND attachment_verified_at IS NULL) OR "
            "(attachment_verification_source IS NOT NULL "
            "AND length(trim(attachment_verification_source)) BETWEEN 1 AND 64 "
            "AND attachment_verified_at IS NOT NULL)",
        )

    with op.batch_alter_table("browser_qualification_runs") as batch:
        # All metadata is nullable so revision 006 records remain byte-for-byte
        # classifiable as legacy observations instead of receiving a new tier.
        batch.add_column(sa.Column("adapter_name", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("adapter_version", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("qualification_tier", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("form_fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("fixture_digest", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            "ck_browser_qualification_tier",
            "qualification_tier IS NULL OR qualification_tier IN "
            "('disabled', 'dry_run_only', 'fixture_qualified', "
            "'dry_run_qualified', 'live_canary_qualified')",
        )
        batch.create_check_constraint(
            "ck_browser_qualification_metadata_complete",
            "(adapter_name IS NULL AND adapter_version IS NULL "
            "AND qualification_tier IS NULL AND form_fingerprint IS NULL "
            "AND fixture_digest IS NULL) OR "
            "(adapter_name IS NOT NULL AND adapter_version IS NOT NULL "
            "AND qualification_tier IS NOT NULL AND fixture_digest IS NOT NULL "
            "AND length(trim(adapter_name)) BETWEEN 1 AND 64 "
            "AND length(trim(adapter_version)) BETWEEN 1 AND 32)",
        )
        batch.create_check_constraint(
            "ck_browser_qualification_digests",
            f"(fixture_digest IS NULL OR {_sha256_check_sql('fixture_digest')}) "
            f"AND (form_fingerprint IS NULL OR "
            f"{_sha256_check_sql('form_fingerprint')})",
        )
        batch.create_check_constraint(
            "ck_browser_qualification_live_evidence",
            "qualification_tier IS NULL "
            "OR qualification_tier != 'live_canary_qualified' "
            "OR (qualified = true "
            "AND terminal_reason = 'LIVE_CANARY_CONFIRMED' "
            "AND form_fingerprint IS NOT NULL)",
        )

    op.create_index(
        "ix_browser_qualification_adapter_tier",
        "browser_qualification_runs",
        ["adapter_name", "adapter_version", "qualification_tier", "created_at"],
    )
    op.create_index(
        "ix_browser_qualification_adapter_form",
        "browser_qualification_runs",
        ["adapter_name", "adapter_version", "form_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_browser_qualification_adapter_form",
        table_name="browser_qualification_runs",
    )
    op.drop_index(
        "ix_browser_qualification_adapter_tier",
        table_name="browser_qualification_runs",
    )
    with op.batch_alter_table("browser_qualification_runs") as batch:
        batch.drop_constraint(
            "ck_browser_qualification_live_evidence",
            type_="check",
        )
        batch.drop_constraint(
            "ck_browser_qualification_digests",
            type_="check",
        )
        batch.drop_constraint(
            "ck_browser_qualification_metadata_complete",
            type_="check",
        )
        batch.drop_constraint(
            "ck_browser_qualification_tier",
            type_="check",
        )
        batch.drop_column("fixture_digest")
        batch.drop_column("form_fingerprint")
        batch.drop_column("qualification_tier")
        batch.drop_column("adapter_version")
        batch.drop_column("adapter_name")

    with op.batch_alter_table("form_plans") as batch:
        batch.drop_constraint(
            "ck_form_plans_attachment_evidence_metadata",
            type_="check",
        )
        batch.drop_column("attachment_verified_at")
        batch.drop_column("attachment_verification_source")

    op.drop_index(
        "ix_operator_approved_answers_field_contract",
        table_name="operator_approved_answers",
    )
    with op.batch_alter_table("operator_approved_answers") as batch:
        batch.drop_constraint(
            "ck_operator_approved_answers_field_contract",
            type_="check",
        )
        batch.drop_column("field_contract_fingerprint")
