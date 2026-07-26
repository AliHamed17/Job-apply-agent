"""Add the evidence-verified submission domain persistence kernel.

Revision ID: 009_submission_domain_kernel
Revises: 008_employer_automation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009_submission_domain_kernel"
down_revision: str | None = "008_employer_automation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KNOWN_PRECOMMIT_FAILURES = {
    "ADAPTER_NOT_QUALIFIED",
    "ADAPTER_ROUTE_MISMATCH",
    "BROWSER_UNAVAILABLE",
    "BUILD_MISMATCH",
    "CHALLENGE_DETECTED",
    "GOVERNOR_DEFERRED",
    "MFA_REQUIRED",
    "PORTAL_ADAPTER_REQUIRED",
    "PORTAL_SESSION_REQUIRED",
    "PORTAL_URL_INVALID",
    "QUEUE_ENQUEUE_FAILED",
    "REQUIRED_FIELD_UNKNOWN",
    "RUNTIME_NOT_READY",
    "SELECTED_CV_UNAVAILABLE",
    "SELECTOR_DRIFT",
    "SESSION_EXPIRED",
    "SUBMIT_PERMIT_REQUIRED",
}


def _sha256_check_sql(column_name: str) -> str:
    """Return a SQLite/PostgreSQL-compatible lowercase SHA-256 check."""
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def _backfill_applications(bind) -> dict[int, int]:
    applications = sa.Table("applications", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.select(
            applications.c.id,
            applications.c.approved_at,
        )
    ).all()
    revisions: dict[int, int] = {}
    for row in rows:
        revisions[row.id] = 1
        values: dict[str, int] = {"revision": 1}
        if row.approved_at is not None:
            values["prepared_revision"] = 1
        bind.execute(applications.update().where(applications.c.id == row.id).values(**values))
    return revisions


def _backfill_submissions(bind, application_revisions: dict[int, int]) -> None:
    submissions = sa.Table("submissions", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(sa.select(submissions)).all()
    for row in rows:
        old_status = str(row.status)
        new_status = old_status
        # Preserve any historical reporting timestamp separately. No pre-v4
        # row has v4 employer evidence, so none may retain submitted_at.
        legacy_reported_at = row.submitted_at

        if row.reason_code == "OPERATOR_CONFIRMED_SUBMITTED":
            # A legacy operator reconciliation closes the workflow, but it is
            # not employer evidence and must never be presented as green.
            outcome = "operator_confirmed"
            new_status = "unknown"
        elif row.reason_code == "RECONCILED_NOT_SUBMITTED":
            # The operator established that no external action completed, so
            # this remains a definitive, retryable pre-commit failure.
            outcome = "failed_before_commit"
            new_status = "failed"
        elif old_status == "success":
            outcome = "legacy_unverified"
        elif old_status == "draft_only":
            outcome = "draft_only"
        elif old_status == "failed" and row.reason_code in _KNOWN_PRECOMMIT_FAILURES:
            outcome = "failed_before_commit"
        else:
            # Pending/running rows are stale once the old workers are stopped.
            # Unknown and ambiguous historical failures must never be retried.
            outcome = "unknown"
            new_status = "unknown"

        finished_at = row.finished_at or row.submitted_at or row.created_at
        values = {
            "status": new_status,
            "stage": "finished",
            "outcome": outcome,
            "application_revision": application_revisions.get(row.application_id, 1),
            "adapter_name": row.submitter_name,
            "requested_cv_id": row.selected_cv_id,
            "attachment_verified": False,
            "legacy_reported_at": legacy_reported_at,
            "submitted_at": None,
        }
        if row.reason_code in {
            "OPERATOR_CONFIRMED_SUBMITTED",
            "RECONCILED_NOT_SUBMITTED",
        }:
            values.update(
                reconciliation_source="legacy_import",
                reconciliation_evidence_ref=f"legacy-submission:{row.id}",
            )
        if row.finished_at is None:
            values["finished_at"] = finished_at
        bind.execute(submissions.update().where(submissions.c.id == row.id).values(**values))


def _snapshot_application_submission_state(bind) -> None:
    """Archive the v3 lifecycle projection so downgrade is data-reversible."""

    applications = sa.Table("applications", sa.MetaData(), autoload_with=bind)
    jobs = sa.Table("jobs", sa.MetaData(), autoload_with=bind)
    snapshots = sa.Table(
        "_submission_domain_legacy_state",
        sa.MetaData(),
        autoload_with=bind,
    )
    rows = bind.execute(
        sa.select(
            applications.c.id.label("application_id"),
            applications.c.status.label("application_status"),
            applications.c.approved_at,
            applications.c.approval_source,
            applications.c.needs_review_reason,
            applications.c.updated_at,
            applications.c.job_id,
            jobs.c.status.label("job_status"),
        ).join(jobs, jobs.c.id == applications.c.job_id)
    ).mappings()
    for row in rows:
        bind.execute(snapshots.insert().values(**dict(row)))


def _normalize_profile_versions(bind) -> set[int]:
    """Make legacy profile identities unique while preserving downgrade data."""

    metadata = sa.MetaData()
    profiles = sa.Table("user_profile_versions", metadata, autoload_with=bind)
    archive = sa.Table(
        "_submission_domain_profile_version_state",
        metadata,
        autoload_with=bind,
    )
    rows = bind.execute(
        sa.select(profiles.c.id, profiles.c.version).order_by(
            profiles.c.version,
            profiles.c.id,
        )
    ).all()
    if not rows:
        return set()

    maximum = max(int(row.version) for row in rows)
    used: set[int] = set()
    ambiguous: set[int] = set()
    for row in rows:
        original = int(row.version)
        if original not in used:
            used.add(original)
            continue
        ambiguous.add(original)
        maximum += 1
        while maximum in used:
            maximum += 1
        bind.execute(
            archive.insert().values(
                profile_version_id=row.id,
                original_version=original,
            )
        )
        bind.execute(profiles.update().where(profiles.c.id == row.id).values(version=maximum))
        used.add(maximum)
    return ambiguous


def _backfill_application_submission_state(
    bind,
    *,
    ambiguous_profile_versions: set[int],
) -> None:
    """Project each latest historical attempt into one truthful app/job state."""

    metadata = sa.MetaData()
    applications = sa.Table("applications", metadata, autoload_with=bind)
    jobs = sa.Table("jobs", metadata, autoload_with=bind)
    submissions = sa.Table("submissions", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            submissions.c.application_id,
            submissions.c.outcome,
            submissions.c.reason_code,
            submissions.c.profile_version,
        ).order_by(
            submissions.c.application_id,
            submissions.c.attempt_number.desc(),
            submissions.c.id.desc(),
        )
    ).all()
    attempts_by_application: dict[int, list] = {}
    for row in rows:
        attempts_by_application.setdefault(row.application_id, []).append(row)

    for application_id, history in attempts_by_application.items():
        # A later draft/failure cannot prove that an older ambiguous action did
        # not reach the employer. Any unreconciled unknown or legacy success
        # therefore quarantines the entire application until reconciliation.
        row = next((item for item in history if item.outcome == "unknown"), None)
        if row is None:
            row = next(
                (item for item in history if item.outcome == "legacy_unverified"),
                history[0],
            )
        application = bind.execute(
            sa.select(
                applications.c.job_id,
                applications.c.profile_version,
            ).where(applications.c.id == application_id)
        ).first()
        if application is None:
            continue
        profile_identity_ambiguous = (
            application.profile_version in ambiguous_profile_versions
            or any(item.profile_version in ambiguous_profile_versions for item in history)
        )

        app_values = {
            "approved_at": None,
            "approval_source": None,
            "prepared_revision": None,
        }
        job_status: str | None = None
        if profile_identity_ambiguous:
            app_values.update(
                status="needs_review",
                needs_review_reason="PROFILE_VERSION_AMBIGUOUS",
            )
            job_status = "needs_review"
        elif row.outcome == "unknown":
            app_values.update(
                status="needs_review",
                needs_review_reason="STALE_INDETERMINATE",
            )
            job_status = "needs_review"
        elif row.outcome == "legacy_unverified":
            app_values.update(
                status="needs_review",
                needs_review_reason="LEGACY_UNVERIFIED",
            )
            job_status = "needs_review"
        elif row.outcome == "operator_confirmed":
            app_values.update(
                status="submitted",
                needs_review_reason=None,
            )
            job_status = "submitted"
        elif row.outcome == "failed_before_commit":
            app_values.update(
                status="failed",
                needs_review_reason=row.reason_code or "FAILED_BEFORE_COMMIT",
            )
            job_status = "failed"
        elif row.outcome == "draft_only":
            app_values.update(
                status="draft",
                needs_review_reason=None,
            )
            job_status = "draft"

        bind.execute(
            applications.update().where(applications.c.id == application_id).values(**app_values)
        )
        if job_status is not None:
            bind.execute(
                jobs.update().where(jobs.c.id == application.job_id).values(status=job_status)
            )

    if ambiguous_profile_versions:
        untouched_filter = applications.c.profile_version.in_(ambiguous_profile_versions)
        if attempts_by_application:
            untouched_filter = sa.and_(
                untouched_filter,
                applications.c.id.not_in(list(attempts_by_application)),
            )
        untouched = bind.execute(
            sa.select(
                applications.c.id,
                applications.c.job_id,
            ).where(untouched_filter)
        ).all()
        for application in untouched:
            bind.execute(
                applications.update()
                .where(applications.c.id == application.id)
                .values(
                    status="needs_review",
                    approved_at=None,
                    approval_source=None,
                    prepared_revision=None,
                    needs_review_reason="PROFILE_VERSION_AMBIGUOUS",
                )
            )
            bind.execute(
                jobs.update().where(jobs.c.id == application.job_id).values(status="needs_review")
            )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Revision 004 added the ``unknown`` submissionstatus label. PostgreSQL
        # cannot use an enum value added earlier in the same transaction, and
        # Alembic may run a base-to-head upgrade in one transaction. Establish
        # a boundary before this migration classifies stale rows as unknown.
        with op.get_context().autocommit_block():
            op.execute(sa.text("SELECT 1"))
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"

    op.create_table(
        "_submission_domain_profile_version_state",
        sa.Column(
            "profile_version_id",
            sa.Integer(),
            primary_key=True,
        ),
        sa.Column("original_version", sa.Integer(), nullable=False),
    )
    ambiguous_profile_versions = _normalize_profile_versions(bind)
    with op.batch_alter_table(
        "user_profile_versions",
        recreate=recreate,
    ) as batch:
        batch.create_unique_constraint(
            "uq_user_profile_versions_version",
            ["version"],
        )

    with op.batch_alter_table("applications", recreate=recreate) as batch:
        batch.add_column(
            sa.Column(
                "revision",
                sa.Integer(),
                nullable=True,
                server_default=sa.text("1"),
            )
        )
        batch.add_column(sa.Column("prepared_revision", sa.Integer(), nullable=True))

    application_revisions = _backfill_applications(bind)

    with op.batch_alter_table("applications", recreate=recreate) as batch:
        batch.alter_column(
            "revision",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        )

    op.create_table(
        "form_plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id"),
            nullable=False,
        ),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column("adapter_name", sa.String(64), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("selector_version", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("selected_cv_id", sa.String(255), nullable=False),
        sa.Column("selected_cv_hash", sa.String(64), nullable=False),
        sa.Column("attached_cv_id", sa.String(255), nullable=True),
        sa.Column("attached_cv_hash", sa.String(64), nullable=True),
        sa.Column(
            "attachment_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column(
            "fields_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "decisions_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "blockers_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("session_verified_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        sa.Column("invalidation_reason", sa.String(64), nullable=True),
        sa.UniqueConstraint("plan_id", name="uq_form_plans_plan_id"),
        sa.UniqueConstraint(
            "id",
            "application_id",
            "application_revision",
            "adapter_name",
            "adapter_version",
            "selector_version",
            "fingerprint",
            "selected_cv_id",
            "selected_cv_hash",
            "attached_cv_id",
            "attached_cv_hash",
            "attachment_verified",
            "profile_version",
            name="uq_form_plans_submission_binding",
        ),
    )
    op.create_index(
        "ix_form_plans_application_revision",
        "form_plans",
        ["application_id", "application_revision"],
    )
    op.create_index("ix_form_plans_expires_at", "form_plans", ["expires_at"])
    op.create_index("ix_form_plans_fingerprint", "form_plans", ["fingerprint"])

    with op.batch_alter_table("submissions", recreate=recreate) as batch:
        batch.add_column(sa.Column("stage", sa.String(24), nullable=True))
        batch.add_column(sa.Column("outcome", sa.String(32), nullable=True))
        batch.add_column(sa.Column("application_revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("adapter_name", sa.String(64), nullable=True))
        batch.add_column(sa.Column("adapter_version", sa.String(32), nullable=True))
        batch.add_column(sa.Column("selector_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("form_plan_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("form_plan_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("requested_cv_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("requested_cv_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("attached_cv_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("attached_cv_hash", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "attachment_verified",
                sa.Boolean(),
                nullable=True,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("final_action_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("verification_kind", sa.String(64), nullable=True))
        batch.add_column(sa.Column("evidence_digest", sa.String(64), nullable=True))
        batch.add_column(sa.Column("runner_release", sa.String(64), nullable=True))
        batch.add_column(sa.Column("legacy_reported_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("reconciliation_source", sa.String(32), nullable=True))
        batch.add_column(sa.Column("reconciliation_evidence_ref", sa.String(255), nullable=True))

    op.create_table(
        "_submission_domain_legacy_state",
        sa.Column("application_id", sa.Integer(), primary_key=True),
        sa.Column("application_status", sa.String(32), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approval_source", sa.String(32), nullable=True),
        sa.Column("needs_review_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("job_status", sa.String(32), nullable=False),
    )

    _backfill_submissions(bind, application_revisions)
    _snapshot_application_submission_state(bind)
    _backfill_application_submission_state(
        bind,
        ambiguous_profile_versions=ambiguous_profile_versions,
    )

    with op.batch_alter_table("submissions", recreate=recreate) as batch:
        batch.alter_column(
            "idempotency_key",
            existing_type=sa.String(36),
            type_=sa.String(128),
            existing_nullable=False,
        )
        batch.alter_column(
            "stage",
            existing_type=sa.String(24),
            nullable=False,
            server_default="queued",
        )
        batch.alter_column(
            "application_revision",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        )
        batch.alter_column(
            "attachment_verified",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
        batch.create_check_constraint(
            "ck_submissions_attempt_stage",
            "stage IN ('queued', 'inspecting', 'preparing', 'ready', "
            "'committing', 'verifying', 'finished')",
        )
        batch.create_check_constraint(
            "ck_submissions_attempt_outcome",
            "outcome IS NULL OR outcome IN "
            "('confirmed_submitted', 'already_applied', 'needs_review', "
            "'unknown', 'failed_before_commit', 'draft_only', "
            "'operator_confirmed', 'legacy_unverified')",
        )
        batch.create_check_constraint(
            "ck_submissions_stage_outcome_consistent",
            "(stage = 'finished' AND outcome IS NOT NULL) OR "
            "(stage <> 'finished' AND outcome IS NULL)",
        )
        batch.create_check_constraint(
            "ck_submissions_success_outcome",
            "status <> 'success' OR "
            "(stage = 'finished' AND "
            "outcome IN ('confirmed_submitted', 'legacy_unverified'))",
        )
        batch.create_check_constraint(
            "ck_submissions_submitted_at_verified",
            "submitted_at IS NULL OR "
            "(stage = 'finished' AND outcome = 'confirmed_submitted' "
            "AND status = 'success')",
        )
        batch.create_check_constraint(
            "ck_submissions_confirmed_evidence",
            "outcome <> 'confirmed_submitted' OR "
            "(status = 'success' AND submitted_at IS NOT NULL "
            "AND final_action_at IS NOT NULL "
            "AND submitted_at >= final_action_at "
            "AND form_plan_id IS NOT NULL "
            "AND adapter_name IS NOT NULL "
            "AND length(trim(adapter_name)) > 0 "
            "AND adapter_version IS NOT NULL "
            "AND length(trim(adapter_version)) > 0 "
            "AND selector_version IS NOT NULL "
            "AND length(trim(selector_version)) > 0 "
            "AND profile_version IS NOT NULL "
            "AND profile_version > 0 "
            "AND runner_release IS NOT NULL "
            "AND length(trim(runner_release)) > 0 "
            "AND length(runner_release) <= 64 "
            "AND requested_cv_id IS NOT NULL "
            "AND length(trim(requested_cv_id)) > 0 "
            "AND attached_cv_id IS NOT NULL "
            "AND length(trim(attached_cv_id)) > 0 "
            "AND requested_cv_id = attached_cv_id "
            "AND requested_cv_hash IS NOT NULL "
            "AND attachment_verified = true "
            "AND attached_cv_hash IS NOT NULL "
            "AND requested_cv_hash = attached_cv_hash "
            f"AND {_sha256_check_sql('attached_cv_hash')} "
            "AND form_plan_fingerprint IS NOT NULL "
            f"AND {_sha256_check_sql('form_plan_fingerprint')} "
            "AND verification_kind IS NOT NULL "
            "AND verification_kind IN "
            "('employer_application_id', 'api_receipt', "
            "'candidate_portal_record', 'visible_post_click_confirmation') "
            "AND evidence_digest IS NOT NULL "
            f"AND {_sha256_check_sql('evidence_digest')})",
        )
        batch.create_foreign_key(
            "fk_submissions_exact_form_plan",
            "form_plans",
            [
                "form_plan_id",
                "application_id",
                "application_revision",
                "adapter_name",
                "adapter_version",
                "selector_version",
                "form_plan_fingerprint",
                "requested_cv_id",
                "requested_cv_hash",
                "attached_cv_id",
                "attached_cv_hash",
                "attachment_verified",
                "profile_version",
            ],
            [
                "id",
                "application_id",
                "application_revision",
                "adapter_name",
                "adapter_version",
                "selector_version",
                "fingerprint",
                "selected_cv_id",
                "selected_cv_hash",
                "attached_cv_id",
                "attached_cv_hash",
                "attachment_verified",
                "profile_version",
            ],
        )
        batch.create_index("ix_submissions_stage", ["stage"])
        batch.create_index("ix_submissions_outcome", ["outcome"])

    op.create_index(
        "uq_submissions_one_unfinished_per_application",
        "submissions",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("stage <> 'finished'"),
        sqlite_where=sa.text("stage <> 'finished'"),
    )

    op.create_table(
        "final_submit_permits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "attempt_id",
            sa.Integer(),
            sa.ForeignKey("submissions.id"),
            nullable=False,
        ),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("job_url_hash", sa.String(64), nullable=False),
        sa.Column("application_revision", sa.Integer(), nullable=False),
        sa.Column("adapter_name", sa.String(64), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("selector_version", sa.String(64), nullable=False),
        sa.Column("form_plan_fingerprint", sa.String(64), nullable=False),
        sa.Column("cv_hash", sa.String(64), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("attempt_id", name="uq_final_submit_permits_attempt_id"),
        sa.UniqueConstraint("nonce_hash", name="uq_final_submit_permits_nonce_hash"),
    )
    op.create_index(
        "ix_final_submit_permits_expires_at",
        "final_submit_permits",
        ["expires_at"],
    )

    op.create_table(
        "submission_commands",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "attempt_id",
            sa.Integer(),
            sa.ForeignKey("submissions.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'completed', 'cancelled')",
            name="ck_submission_commands_state",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL "
            "AND completed_at IS NULL) "
            "OR (state = 'claimed' AND claimed_at IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claim_token IS NOT NULL "
            "AND completed_at IS NULL) "
            "OR (state IN ('completed', 'cancelled') "
            "AND completed_at IS NOT NULL AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL)",
            name="ck_submission_commands_state_metadata",
        ),
        sa.UniqueConstraint("attempt_id", name="uq_submission_commands_attempt_id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_submission_commands_idempotency_key",
        ),
    )
    op.create_index(
        "ix_submission_commands_state_available",
        "submission_commands",
        ["state", "available_at"],
    )

    op.create_table(
        "submission_evidence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "attempt_id",
            sa.Integer(),
            sa.ForeignKey("submissions.id"),
            nullable=False,
        ),
        sa.Column("evidence_type", sa.String(64), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("employer_application_ref", sa.String(255), nullable=True),
        sa.Column("receipt_ref", sa.String(255), nullable=True),
        sa.Column("portal_record_ref", sa.String(255), nullable=True),
        sa.Column("form_fingerprint", sa.String(64), nullable=False),
        sa.Column("cv_hash", sa.String(64), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "attempt_id",
            "evidence_digest",
            name="uq_submission_evidence_attempt_digest",
        ),
        sa.UniqueConstraint(
            "attempt_id",
            "evidence_digest",
            "evidence_type",
            "form_fingerprint",
            "cv_hash",
            name="uq_submission_evidence_binding",
        ),
        sa.CheckConstraint(
            "evidence_type IN "
            "('employer_application_id', 'api_receipt', "
            "'candidate_portal_record', 'visible_post_click_confirmation')",
            name="ck_submission_evidence_type",
        ),
        sa.CheckConstraint(
            f"{_sha256_check_sql('evidence_digest')} "
            f"AND {_sha256_check_sql('form_fingerprint')} "
            f"AND {_sha256_check_sql('cv_hash')}",
            name="ck_submission_evidence_digests",
        ),
        sa.CheckConstraint(
            "(evidence_type = 'employer_application_id' "
            "AND employer_application_ref IS NOT NULL "
            "AND length(trim(employer_application_ref)) > 0 "
            "AND receipt_ref IS NULL AND portal_record_ref IS NULL) "
            "OR (evidence_type = 'api_receipt' "
            "AND receipt_ref IS NOT NULL "
            "AND length(trim(receipt_ref)) > 0 "
            "AND employer_application_ref IS NULL AND portal_record_ref IS NULL) "
            "OR (evidence_type = 'candidate_portal_record' "
            "AND portal_record_ref IS NOT NULL "
            "AND length(trim(portal_record_ref)) > 0 "
            "AND employer_application_ref IS NULL AND receipt_ref IS NULL) "
            "OR (evidence_type = 'visible_post_click_confirmation' "
            "AND employer_application_ref IS NULL "
            "AND receipt_ref IS NULL AND portal_record_ref IS NULL)",
            name="ck_submission_evidence_typed_reference",
        ),
    )
    op.create_index(
        "ix_submission_evidence_attempt_id",
        "submission_evidence",
        ["attempt_id"],
    )
    op.create_index(
        "ix_submission_evidence_type",
        "submission_evidence",
        ["evidence_type"],
    )
    with op.batch_alter_table("submissions", recreate=recreate) as batch:
        batch.create_foreign_key(
            "fk_submissions_confirmed_evidence",
            "submission_evidence",
            [
                "id",
                "evidence_digest",
                "verification_kind",
                "form_plan_fingerprint",
                "attached_cv_hash",
            ],
            [
                "attempt_id",
                "evidence_digest",
                "evidence_type",
                "form_fingerprint",
                "cv_hash",
            ],
            deferrable=True,
            initially="DEFERRED",
        )


def _restore_legacy_submission_state(bind) -> None:
    submissions = sa.Table("submissions", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(sa.select(submissions)).all()
    for row in rows:
        status = row.status
        submitted_at = row.legacy_reported_at or row.submitted_at
        if row.outcome == "legacy_unverified":
            status = "success"
            submitted_at = row.legacy_reported_at
        elif row.outcome == "confirmed_submitted":
            status = "success"
        elif row.outcome == "draft_only":
            status = "draft_only"
        elif row.outcome == "failed_before_commit":
            status = "failed"
        elif row.outcome in {
            "already_applied",
            "needs_review",
            "operator_confirmed",
            "unknown",
        }:
            status = "unknown"
            if row.legacy_reported_at is None:
                submitted_at = None
        elif row.stage != "finished":
            status = "pending"
            submitted_at = None

        bind.execute(
            submissions.update()
            .where(submissions.c.id == row.id)
            .values(status=status, submitted_at=submitted_at)
        )


def _restore_legacy_application_submission_state(bind) -> None:
    """Restore exactly the v3 application/job lifecycle values we rewrote."""

    metadata = sa.MetaData()
    applications = sa.Table("applications", metadata, autoload_with=bind)
    jobs = sa.Table("jobs", metadata, autoload_with=bind)
    snapshots = sa.Table(
        "_submission_domain_legacy_state",
        metadata,
        autoload_with=bind,
    )
    for row in bind.execute(sa.select(snapshots)).mappings():
        bind.execute(
            applications.update()
            .where(applications.c.id == row["application_id"])
            .values(
                status=row["application_status"],
                approved_at=row["approved_at"],
                approval_source=row["approval_source"],
                needs_review_reason=row["needs_review_reason"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(
            jobs.update().where(jobs.c.id == row["job_id"]).values(status=row["job_status"])
        )


def _restore_legacy_profile_versions(bind) -> None:
    """Restore exact pre-v4 duplicate identities after uniqueness is removed."""

    metadata = sa.MetaData()
    profiles = sa.Table("user_profile_versions", metadata, autoload_with=bind)
    archive = sa.Table(
        "_submission_domain_profile_version_state",
        metadata,
        autoload_with=bind,
    )
    for row in bind.execute(sa.select(archive)).mappings():
        bind.execute(
            profiles.update()
            .where(profiles.c.id == row["profile_version_id"])
            .values(version=row["original_version"])
        )


def downgrade() -> None:
    bind = op.get_bind()
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"

    with op.batch_alter_table("submissions", recreate=recreate) as batch:
        batch.drop_constraint(
            "fk_submissions_confirmed_evidence",
            type_="foreignkey",
        )
        batch.drop_constraint("ck_submissions_confirmed_evidence", type_="check")
        batch.drop_constraint(
            "ck_submissions_submitted_at_verified",
            type_="check",
        )
        batch.drop_constraint("ck_submissions_success_outcome", type_="check")
        batch.drop_constraint(
            "fk_submissions_exact_form_plan",
            type_="foreignkey",
        )

    # New auxiliary records cannot be represented by v3. Submission rows and
    # their legacy timestamps are restored before the auxiliary tables go away.
    _restore_legacy_submission_state(bind)
    _restore_legacy_application_submission_state(bind)

    with op.batch_alter_table(
        "user_profile_versions",
        recreate=recreate,
    ) as batch:
        batch.drop_constraint(
            "uq_user_profile_versions_version",
            type_="unique",
        )
    _restore_legacy_profile_versions(bind)

    op.drop_index("ix_submission_evidence_type", table_name="submission_evidence")
    op.drop_index(
        "ix_submission_evidence_attempt_id",
        table_name="submission_evidence",
    )
    op.drop_table("submission_evidence")

    op.drop_index(
        "ix_submission_commands_state_available",
        table_name="submission_commands",
    )
    op.drop_table("submission_commands")

    op.drop_index(
        "ix_final_submit_permits_expires_at",
        table_name="final_submit_permits",
    )
    op.drop_table("final_submit_permits")

    op.drop_index(
        "uq_submissions_one_unfinished_per_application",
        table_name="submissions",
    )
    submissions = sa.Table("submissions", sa.MetaData(), autoload_with=bind)
    longest_key = bind.execute(
        sa.select(sa.func.max(sa.func.length(submissions.c.idempotency_key)))
    ).scalar()
    can_restore_legacy_key_width = longest_key is None or longest_key <= 36
    with op.batch_alter_table("submissions", recreate=recreate) as batch:
        if can_restore_legacy_key_width:
            batch.alter_column(
                "idempotency_key",
                existing_type=sa.String(128),
                type_=sa.String(36),
                existing_nullable=False,
            )
        batch.drop_index("ix_submissions_outcome")
        batch.drop_index("ix_submissions_stage")
        batch.drop_constraint(
            "ck_submissions_stage_outcome_consistent",
            type_="check",
        )
        batch.drop_constraint("ck_submissions_attempt_outcome", type_="check")
        batch.drop_constraint("ck_submissions_attempt_stage", type_="check")
        for name in (
            "reconciliation_evidence_ref",
            "reconciliation_source",
            "legacy_reported_at",
            "runner_release",
            "evidence_digest",
            "verification_kind",
            "final_action_at",
            "attachment_verified",
            "attached_cv_hash",
            "attached_cv_id",
            "requested_cv_hash",
            "requested_cv_id",
            "form_plan_fingerprint",
            "form_plan_id",
            "selector_version",
            "adapter_version",
            "adapter_name",
            "application_revision",
            "outcome",
            "stage",
        ):
            batch.drop_column(name)

    op.drop_index("ix_form_plans_fingerprint", table_name="form_plans")
    op.drop_index("ix_form_plans_expires_at", table_name="form_plans")
    op.drop_index(
        "ix_form_plans_application_revision",
        table_name="form_plans",
    )
    op.drop_table("form_plans")
    op.drop_table("_submission_domain_legacy_state")
    op.drop_table("_submission_domain_profile_version_state")

    with op.batch_alter_table("applications", recreate=recreate) as batch:
        batch.drop_column("prepared_revision")
        batch.drop_column("revision")
