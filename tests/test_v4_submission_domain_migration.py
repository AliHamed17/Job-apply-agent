"""Upgrade/downgrade evidence for the v4 submission-domain migration."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from db.models import Base

ROOT = Path(__file__).resolve().parents[1]
_PRESERVED_SUBMISSION_COLUMNS = (
    "id",
    "application_id",
    "submitter_name",
    "confirmation_url",
    "confirmation_id",
    "error_message",
    "created_at",
    "attempt_number",
    "idempotency_key",
    "reason_code",
    "diagnostic_details",
    "selected_cv_id",
    "profile_version",
    "started_at",
    "finished_at",
    "reconciled_at",
    "reconciliation_note",
)
_FORM_PLAN_BINDING_COLUMNS = [
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
]
_EVIDENCE_BINDING_COLUMNS = [
    "id",
    "evidence_digest",
    "verification_kind",
    "form_plan_fingerprint",
    "attached_cv_hash",
]


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic(path: Path, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url(path)
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _preserved_submission_snapshot(path: Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    columns = ", ".join(_PRESERVED_SUBMISSION_COLUMNS)
    rows = list(connection.execute(f"SELECT {columns} FROM submissions ORDER BY id"))
    connection.close()
    return rows


def _application_job_lifecycle_snapshot(path: Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    rows = list(
        connection.execute(
            "SELECT applications.id, applications.status, "
            "strftime('%Y-%m-%d %H:%M:%f', applications.approved_at), "
            "applications.approval_source, applications.needs_review_reason, "
            "strftime('%Y-%m-%d %H:%M:%f', applications.updated_at), "
            "jobs.id, jobs.status "
            "FROM applications JOIN jobs ON jobs.id = applications.job_id "
            "ORDER BY applications.id"
        )
    )
    connection.close()
    return rows


def _seed_v3_history(path: Path) -> None:
    connection = sqlite3.connect(path)
    timestamp = "2026-07-01 12:00:00"
    submissions = (
        (1, 1, "success", "EMPLOYER_VERIFIED", timestamp),
        (2, 1, "pending", None, None),
        (3, 1, "running", None, None),
        (4, 1, "draft_only", "DRY_RUN_DISCARDED", None),
        (5, 1, "failed", "SELECTOR_DRIFT", None),
        (6, 1, "failed", "SUBMIT_UNCONFIRMED", None),
        (6, 2, "draft_only", "DRY_RUN_DISCARDED", None),
        (7, 1, "unknown", "OPERATOR_CONFIRMED_SUBMITTED", None),
        (8, 1, "failed", "RECONCILED_NOT_SUBMITTED", None),
    )
    for row_id in range(1, 9):
        application_status = "failed" if row_id == 5 else "draft"
        connection.execute(
            "INSERT INTO jobs (id, title, source_url, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                row_id,
                f"Job {row_id}",
                f"https://example.invalid/{row_id}",
                application_status,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO applications "
            "(id, job_id, status, approved_at, created_at, updated_at, "
            "approval_source, needs_review_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                row_id,
                application_status,
                timestamp if row_id in {1, 2, 5, 7, 8} else None,
                timestamp,
                timestamp,
                "legacy_operator" if row_id in {1, 2, 5, 7, 8} else None,
                f"legacy-review-{row_id}" if row_id in {3, 6} else None,
            ),
        )
    for submission_id, (
        application_id,
        attempt_number,
        status,
        reason_code,
        submitted_at,
    ) in enumerate(submissions, start=1):
        connection.execute(
            "INSERT INTO submissions "
            "(id, application_id, submitter_name, status, confirmation_url, "
            "confirmation_id, error_message, submitted_at, created_at, "
            "attempt_number, idempotency_key, reason_code, diagnostic_details, "
            "selected_cv_id, profile_version, started_at, finished_at, "
            "reconciled_at, reconciliation_note) "
            "VALUES (?, ?, 'greenhouse', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'cv-ai', ?, ?, ?, ?, ?)",
            (
                submission_id,
                application_id,
                status,
                f"https://confirm.invalid/{submission_id}",
                f"receipt-{submission_id}",
                f"legacy-error-{submission_id}",
                submitted_at,
                timestamp,
                attempt_number,
                f"legacy-key-{submission_id}",
                reason_code,
                f'{{"legacy":{submission_id}}}',
                submission_id,
                timestamp,
                timestamp,
                timestamp,
                f"legacy-note-{submission_id}",
            ),
        )
    connection.commit()
    connection.close()


def test_upgrade_classifies_history_without_claiming_legacy_success(tmp_path):
    database = tmp_path / "migration.db"
    _alembic(database, "upgrade", "008_employer_automation")
    _seed_v3_history(database)
    legacy_snapshot = _preserved_submission_snapshot(database)
    lifecycle_snapshot = _application_job_lifecycle_snapshot(database)
    _alembic(database, "upgrade", "009_submission_domain_kernel")
    assert _preserved_submission_snapshot(database) == legacy_snapshot
    assert _application_job_lifecycle_snapshot(database) != lifecycle_snapshot

    engine = create_engine(_database_url(database))
    with engine.connect() as connection:
        applications = connection.execute(
            text(
                "SELECT id, status, revision, prepared_revision, approved_at, "
                "approval_source, needs_review_reason "
                "FROM applications ORDER BY id"
            )
        ).mappings()
        applications = list(applications)
        assert all(row["revision"] == 1 for row in applications)
        assert all(row["prepared_revision"] is None for row in applications)
        assert all(row["approved_at"] is None for row in applications)
        assert [row["status"] for row in applications] == [
            "needs_review",
            "needs_review",
            "needs_review",
            "draft",
            "failed",
            "needs_review",
            "submitted",
            "failed",
        ]
        assert applications[0]["needs_review_reason"] == "LEGACY_UNVERIFIED"
        assert applications[1]["needs_review_reason"] == "STALE_INDETERMINATE"
        assert applications[2]["needs_review_reason"] == "STALE_INDETERMINATE"
        assert applications[4]["needs_review_reason"] == "SELECTOR_DRIFT"
        assert applications[5]["needs_review_reason"] == "STALE_INDETERMINATE"
        assert applications[6]["needs_review_reason"] is None
        assert applications[7]["needs_review_reason"] == "RECONCILED_NOT_SUBMITTED"

        jobs = list(connection.execute(text("SELECT id, status FROM jobs ORDER BY id")).mappings())
        assert [row["status"] for row in jobs] == [
            "needs_review",
            "needs_review",
            "needs_review",
            "draft",
            "failed",
            "needs_review",
            "submitted",
            "failed",
        ]

        rows = list(
            connection.execute(
                text(
                    "SELECT id, status, stage, outcome, submitted_at, "
                    "legacy_reported_at, adapter_name, requested_cv_id, "
                    "reconciled_at, reconciliation_note, "
                    "reconciliation_source, reconciliation_evidence_ref "
                    "FROM submissions ORDER BY id"
                )
            ).mappings()
        )
        assert len(rows) == 9
        assert rows[0]["status"] == "success"
        assert rows[0]["stage"] == "finished"
        assert rows[0]["outcome"] == "legacy_unverified"
        assert rows[0]["submitted_at"] is None
        assert rows[0]["legacy_reported_at"] is not None
        assert rows[1]["status"] == "unknown"
        assert rows[1]["outcome"] == "unknown"
        assert rows[2]["status"] == "unknown"
        assert rows[2]["outcome"] == "unknown"
        assert rows[3]["outcome"] == "draft_only"
        assert rows[4]["status"] == "failed"
        assert rows[4]["outcome"] == "failed_before_commit"
        assert rows[5]["status"] == "unknown"
        assert rows[5]["outcome"] == "unknown"
        assert rows[6]["outcome"] == "draft_only"
        assert rows[7]["status"] == "unknown"
        assert rows[7]["outcome"] == "operator_confirmed"
        assert rows[7]["submitted_at"] is None
        assert rows[7]["reconciliation_source"] == "legacy_import"
        assert rows[7]["reconciliation_evidence_ref"] == "legacy-submission:8"
        assert rows[7]["reconciled_at"] is not None
        assert rows[7]["reconciliation_note"] == "legacy-note-8"
        assert rows[8]["status"] == "failed"
        assert rows[8]["outcome"] == "failed_before_commit"
        assert rows[8]["submitted_at"] is None
        assert rows[8]["reconciliation_source"] == "legacy_import"
        assert rows[8]["reconciliation_evidence_ref"] == "legacy-submission:9"
        assert rows[8]["reconciled_at"] is not None
        assert rows[8]["reconciliation_note"] == "legacy-note-9"
        assert all(row["adapter_name"] == "greenhouse" for row in rows)
        assert all(row["requested_cv_id"] == "cv-ai" for row in rows)

    tables = set(inspect(engine).get_table_names())
    assert {
        "_submission_domain_legacy_state",
        "form_plans",
        "final_submit_permits",
        "submission_commands",
        "submission_evidence",
    } <= tables


def test_downgrade_preserves_attempt_rows_and_restores_legacy_timestamp(tmp_path):
    database = tmp_path / "roundtrip.db"
    _alembic(database, "upgrade", "008_employer_automation")
    _seed_v3_history(database)
    legacy_snapshot = _preserved_submission_snapshot(database)
    lifecycle_snapshot = _application_job_lifecycle_snapshot(database)
    _alembic(database, "upgrade", "009_submission_domain_kernel")

    engine = create_engine(_database_url(database))
    with engine.begin() as connection:
        # A long post-v4 key proves downgrade does not truncate or delete rows.
        connection.execute(
            text(
                "INSERT INTO jobs (id, title, source_url, status, created_at) "
                "VALUES (9, 'Job 9', 'https://example.invalid/9', 'draft', "
                "'2026-07-01 12:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO applications "
                "(id, job_id, status, created_at, updated_at, revision) "
                "VALUES (9, 9, 'draft', '2026-07-01 12:00:00', "
                "'2026-07-01 12:00:00', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO submissions "
                "(id, application_id, submitter_name, status, created_at, "
                "attempt_number, idempotency_key, stage, outcome, "
                "application_revision, attachment_verified) "
                "VALUES (10, 9, 'greenhouse', 'draft_only', "
                "'2026-07-01 12:00:00', 1, :key, 'finished', 'draft_only', 1, 0)"
            ),
            {"key": "k" * 80},
        )
    engine.dispose()

    _alembic(database, "downgrade", "008_employer_automation")
    assert _preserved_submission_snapshot(database)[: len(legacy_snapshot)] == legacy_snapshot
    assert _application_job_lifecycle_snapshot(database)[: len(lifecycle_snapshot)] == (
        lifecycle_snapshot
    )

    engine = create_engine(_database_url(database))
    with engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT id, status, submitted_at, idempotency_key FROM submissions ORDER BY id"
                )
            ).mappings()
        )
        assert len(rows) == 10
        assert rows[0]["status"] == "success"
        assert rows[0]["submitted_at"] is not None
        assert rows[7]["status"] == "unknown"
        assert rows[7]["submitted_at"] is None
        assert rows[8]["status"] == "failed"
        assert rows[8]["submitted_at"] is None
        assert rows[-1]["idempotency_key"] == "k" * 80

    inspector = inspect(engine)
    assert "stage" not in {column["name"] for column in inspector.get_columns("submissions")}
    assert "form_plans" not in set(inspector.get_table_names())
    assert "_submission_domain_legacy_state" not in set(inspector.get_table_names())
    assert "fk_submissions_exact_form_plan" not in {
        foreign_key["name"] for foreign_key in inspector.get_foreign_keys("submissions")
    }


def test_profile_version_duplicates_are_normalized_quarantined_and_restored(tmp_path):
    database = tmp_path / "duplicate-profile-versions.db"
    _alembic(database, "upgrade", "008_employer_automation")
    _seed_v3_history(database)
    engine = create_engine(_database_url(database))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, title, source_url, status, created_at) VALUES "
                "(9, 'Unsubmitted ambiguous profile', "
                "'https://example.invalid/profile-ambiguous', 'draft', "
                "'2026-07-01 12:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO applications "
                "(id, job_id, status, profile_version, created_at, updated_at) "
                "VALUES (9, 9, 'draft', 8, "
                "'2026-07-01 12:00:00', '2026-07-01 12:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_profile_versions "
                "(id, profile_yaml, version, created_at) VALUES "
                "(1, 'personal:\n  name: First', 8, '2026-07-01 12:00:00'), "
                "(2, 'personal:\n  name: Second', 8, '2026-07-02 12:00:00')"
            )
        )
    lifecycle_snapshot = _application_job_lifecycle_snapshot(database)
    engine.dispose()

    _alembic(database, "upgrade", "009_submission_domain_kernel")
    engine = create_engine(_database_url(database))
    with engine.begin() as connection:
        versions = list(
            connection.execute(
                text("SELECT id, version, profile_yaml FROM user_profile_versions ORDER BY id")
            )
        )
        assert [(row.id, row.version) for row in versions] == [(1, 8), (2, 9)]
        assert "First" in versions[0].profile_yaml
        assert "Second" in versions[1].profile_yaml
        application = (
            connection.execute(
                text("SELECT status, needs_review_reason FROM applications WHERE id = 7")
            )
            .mappings()
            .one()
        )
        assert application == {
            "status": "needs_review",
            "needs_review_reason": "PROFILE_VERSION_AMBIGUOUS",
        }
        job_status = connection.execute(text("SELECT status FROM jobs WHERE id = 7")).scalar_one()
        assert job_status == "needs_review"
        unsubmitted = (
            connection.execute(
                text(
                    "SELECT status, approved_at, approval_source, "
                    "prepared_revision, needs_review_reason "
                    "FROM applications WHERE id = 9"
                )
            )
            .mappings()
            .one()
        )
        assert unsubmitted == {
            "status": "needs_review",
            "approved_at": None,
            "approval_source": None,
            "prepared_revision": None,
            "needs_review_reason": "PROFILE_VERSION_AMBIGUOUS",
        }
        assert (
            connection.execute(text("SELECT status FROM jobs WHERE id = 9")).scalar_one()
            == "needs_review"
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO user_profile_versions "
                    "(profile_yaml, version, created_at) "
                    "VALUES ('personal: {}', 8, '2026-07-03 12:00:00')"
                )
            )
    assert "_submission_domain_profile_version_state" in set(inspect(engine).get_table_names())
    engine.dispose()

    _alembic(database, "downgrade", "008_employer_automation")
    engine = create_engine(_database_url(database))
    with engine.connect() as connection:
        versions = list(
            connection.execute(
                text("SELECT id, version, profile_yaml FROM user_profile_versions ORDER BY id")
            )
        )
        assert [(row.id, row.version) for row in versions] == [(1, 8), (2, 8)]
        assert "First" in versions[0].profile_yaml
        assert "Second" in versions[1].profile_yaml
    assert _application_job_lifecycle_snapshot(database) == lifecycle_snapshot
    assert "_submission_domain_profile_version_state" not in set(inspect(engine).get_table_names())
    engine.dispose()


def test_v4_migration_matches_orm_metadata_for_new_schema(tmp_path):
    database = tmp_path / "schema-parity.db"
    _alembic(database, "upgrade", "009_submission_domain_kernel")
    engine = create_engine(_database_url(database))
    with engine.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(
                connection,
                opts={
                    "compare_type": False,
                    "compare_server_default": True,
                },
            ),
            Base.metadata,
        )

    v4_schema_names = {
        "form_plans",
        "final_submit_permits",
        "submission_commands",
        "submission_evidence",
        "prepared_revision",
        "application_revision",
        "stage",
        "outcome",
        "adapter_version",
        "selector_version",
        "form_plan_fingerprint",
        "requested_cv_hash",
        "attached_cv_hash",
        "attachment_verified",
        "final_action_at",
        "verification_kind",
        "evidence_digest",
        "runner_release",
        "legacy_reported_at",
        "reconciliation_source",
        "reconciliation_evidence_ref",
    }
    unexpected = [
        difference
        for difference in differences
        if any(name in repr(difference) for name in v4_schema_names)
    ]
    assert unexpected == []

    inspector = inspect(engine)
    submission_foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("submissions")
    }
    exact_form_plan = submission_foreign_keys["fk_submissions_exact_form_plan"]
    assert exact_form_plan["constrained_columns"] == _FORM_PLAN_BINDING_COLUMNS
    assert exact_form_plan["referred_table"] == "form_plans"
    assert exact_form_plan["referred_columns"] == [
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
    ]

    exact_evidence = submission_foreign_keys["fk_submissions_confirmed_evidence"]
    assert exact_evidence["constrained_columns"] == _EVIDENCE_BINDING_COLUMNS
    assert exact_evidence["referred_table"] == "submission_evidence"
    assert exact_evidence["referred_columns"] == [
        "attempt_id",
        "evidence_digest",
        "evidence_type",
        "form_fingerprint",
        "cv_hash",
    ]
    assert exact_evidence["options"] == {
        "deferrable": True,
        "initially": "DEFERRED",
    }

    form_plan_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("form_plans")
    }
    assert form_plan_uniques["uq_form_plans_submission_binding"] == [
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
    ]
    evidence_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("submission_evidence")
    }
    assert evidence_uniques["uq_submission_evidence_binding"] == [
        "attempt_id",
        "evidence_digest",
        "evidence_type",
        "form_fingerprint",
        "cv_hash",
    ]
