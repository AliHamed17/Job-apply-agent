"""Add evidence-bounded form planning and material audit persistence.

Revision ID: 010_ollama_form_plan_v1
Revises: 009_submission_domain_kernel
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_ollama_form_plan_v1"
down_revision: str | None = "009_submission_domain_kernel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
_OPERATOR_ANSWER_ARCHIVE = "_operator_approved_answers_010_archive"


def _sha256_check_sql(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def _archive_operator_answers() -> None:
    """Preserve operator-confirmed facts across the rollback boundary."""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_OPERATOR_ANSWER_ARCHIVE):
        raise RuntimeError("operator-answer downgrade archive already exists")
    source = sa.Table(
        "operator_approved_answers",
        sa.MetaData(),
        autoload_with=bind,
    )
    archive = sa.Table(
        _OPERATOR_ANSWER_ARCHIVE,
        sa.MetaData(),
        *[sa.Column(column.name, column.type, nullable=True) for column in source.columns],
    )
    archive.create(bind)
    column_names = [column.name for column in source.columns]
    bind.execute(
        archive.insert().from_select(
            column_names,
            sa.select(*(source.c[name] for name in column_names)),
        )
    )


def _restore_operator_answers() -> None:
    """Restore and then remove the downgrade archive, if one exists."""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_OPERATOR_ANSWER_ARCHIVE):
        return
    archive = sa.Table(
        _OPERATOR_ANSWER_ARCHIVE,
        sa.MetaData(),
        autoload_with=bind,
    )
    target = sa.Table(
        "operator_approved_answers",
        sa.MetaData(),
        autoload_with=bind,
    )
    column_names = [column.name for column in target.columns]
    bind.execute(
        target.insert().from_select(
            column_names,
            sa.select(*(archive.c[name] for name in column_names)),
        )
    )
    if bind.dialect.name == "postgresql":
        max_id = bind.execute(sa.select(sa.func.max(target.c.id))).scalar_one_or_none()
        if max_id is not None:
            bind.execute(
                sa.text(
                    "SELECT setval("
                    "pg_get_serial_sequence('operator_approved_answers', 'id'), "
                    ":max_id, true)"
                ),
                {"max_id": max_id},
            )
    archive.drop(bind)


def upgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("selected_cv_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("material_eligible", sa.Boolean(), nullable=True))
        batch.add_column(
            sa.Column(
                "material_blockers_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            )
        )
        batch.add_column(
            sa.Column(
                "material_claims_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            )
        )
        batch.add_column(sa.Column("material_model_provider", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("material_model_name", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("material_model_digest", sa.String(length=71), nullable=True))
        batch.add_column(sa.Column("material_prompt_version", sa.String(length=32), nullable=True))
        batch.create_check_constraint(
            "ck_applications_selected_cv_hash",
            f"selected_cv_hash IS NULL OR {_sha256_check_sql('selected_cv_hash')}",
        )
        batch.create_check_constraint(
            "ck_applications_material_identity_complete",
            "(material_prompt_version IS NULL AND material_model_provider IS NULL "
            "AND material_model_name IS NULL AND material_model_digest IS NULL) OR "
            "(material_prompt_version IS NOT NULL AND material_model_provider IS NOT NULL "
            "AND material_model_name IS NOT NULL AND material_model_digest IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_applications_material_eligible_audited",
            "material_eligible IS NULL OR material_eligible = false OR "
            "(selected_cv_hash IS NOT NULL "
            "AND material_prompt_version IS NOT NULL "
            "AND material_model_provider IS NOT NULL "
            "AND material_model_name IS NOT NULL "
            "AND material_model_digest IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_applications_material_model_digest",
            "material_model_digest IS NULL OR "
            "(length(material_model_digest) = 71 "
            "AND substr(material_model_digest, 1, 7) = 'sha256:' "
            f"AND {_sha256_check_sql('substr(material_model_digest, 8)')})",
        )

    with op.batch_alter_table("form_plans") as batch:
        batch.add_column(
            sa.Column("locale", sa.String(length=32), nullable=False, server_default="en")
        )
        batch.add_column(
            sa.Column(
                "answer_policy_version",
                sa.String(length=64),
                nullable=False,
                server_default="answer-policy-v1",
            )
        )
        batch.add_column(sa.Column("llm_prompt_version", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("llm_model_provider", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("llm_model_name", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("llm_model_digest", sa.String(length=71), nullable=True))
        batch.create_check_constraint(
            "ck_form_plans_llm_identity_complete",
            "(llm_prompt_version IS NULL AND llm_model_provider IS NULL "
            "AND llm_model_name IS NULL AND llm_model_digest IS NULL) OR "
            "(llm_prompt_version IS NOT NULL AND llm_model_provider IS NOT NULL "
            "AND llm_model_name IS NOT NULL AND llm_model_digest IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_form_plans_policy_metadata",
            "length(trim(locale)) > 0 AND length(trim(answer_policy_version)) > 0",
        )
        batch.create_check_constraint(
            "ck_form_plans_llm_model_digest",
            "llm_model_digest IS NULL OR "
            "(length(llm_model_digest) = 71 "
            "AND substr(llm_model_digest, 1, 7) = 'sha256:' "
            f"AND {_sha256_check_sql('substr(llm_model_digest, 8)')})",
        )

    # Plans created before this policy existed were never evaluated by it.
    # Mark them explicitly stale instead of silently granting the current
    # policy version through the new column's server default.
    op.execute(sa.text("UPDATE form_plans SET answer_policy_version = 'legacy-unverified'"))

    op.create_table(
        "operator_approved_answers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("canonical_field", sa.String(length=255), nullable=False),
        sa.Column("field_type", sa.String(length=32), nullable=False),
        sa.Column("option_set_hash", sa.String(length=64), nullable=False),
        sa.Column("locale", sa.String(length=32), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("selected_cv_id", sa.String(length=255), nullable=False),
        sa.Column("selected_cv_hash", sa.String(length=64), nullable=False),
        sa.Column("adapter_name", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("selector_version", sa.String(length=64), nullable=False),
        sa.Column("form_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("answer_json", sa.Text(), nullable=False),
        sa.Column(
            "evidence_source",
            sa.String(length=32),
            nullable=False,
            server_default="operator_confirmation",
        ),
        sa.Column(
            "evidence_reference",
            sa.String(length=255),
            nullable=False,
            server_default="operator_confirmation",
        ),
        sa.Column("approved_by", sa.String(length=64), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=64), nullable=True),
        sa.Column("revocation_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by IS NULL AND revocation_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
            "AND revocation_reason IS NOT NULL)",
            name="ck_operator_approved_answers_revocation",
        ),
        sa.CheckConstraint(
            "profile_version > 0 "
            f"AND {_sha256_check_sql('option_set_hash')} "
            f"AND {_sha256_check_sql('selected_cv_hash')} "
            f"AND {_sha256_check_sql('form_fingerprint')} "
            "AND length(trim(canonical_field)) > 0 "
            "AND length(trim(locale)) > 0 "
            "AND length(trim(policy_version)) > 0 "
            "AND length(trim(evidence_source)) > 0 "
            "AND length(trim(evidence_reference)) > 0 "
            "AND length(answer_json) BETWEEN 1 AND 4000",
            name="ck_operator_approved_answers_bounded_context",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operator_approved_answers_lookup",
        "operator_approved_answers",
        [
            "canonical_field",
            "field_type",
            "option_set_hash",
            "locale",
            "profile_version",
            "selected_cv_hash",
            "adapter_name",
            "adapter_version",
            "selector_version",
            "form_fingerprint",
            "policy_version",
        ],
        unique=False,
    )
    op.create_index(
        "ix_operator_approved_answers_revoked_at",
        "operator_approved_answers",
        ["revoked_at"],
        unique=False,
    )
    _restore_operator_answers()


def downgrade() -> None:
    _archive_operator_answers()
    op.drop_index(
        "ix_operator_approved_answers_revoked_at",
        table_name="operator_approved_answers",
    )
    op.drop_index(
        "ix_operator_approved_answers_lookup",
        table_name="operator_approved_answers",
    )
    op.drop_table("operator_approved_answers")

    with op.batch_alter_table("form_plans") as batch:
        batch.drop_constraint("ck_form_plans_llm_model_digest", type_="check")
        batch.drop_constraint("ck_form_plans_policy_metadata", type_="check")
        batch.drop_constraint("ck_form_plans_llm_identity_complete", type_="check")
        batch.drop_column("llm_model_digest")
        batch.drop_column("llm_model_name")
        batch.drop_column("llm_model_provider")
        batch.drop_column("llm_prompt_version")
        batch.drop_column("answer_policy_version")
        batch.drop_column("locale")

    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("ck_applications_material_model_digest", type_="check")
        batch.drop_constraint("ck_applications_material_eligible_audited", type_="check")
        batch.drop_constraint("ck_applications_material_identity_complete", type_="check")
        batch.drop_constraint("ck_applications_selected_cv_hash", type_="check")
        batch.drop_column("material_prompt_version")
        batch.drop_column("material_model_digest")
        batch.drop_column("material_model_name")
        batch.drop_column("material_model_provider")
        batch.drop_column("material_claims_json")
        batch.drop_column("material_blockers_json")
        batch.drop_column("material_eligible")
        batch.drop_column("selected_cv_hash")
