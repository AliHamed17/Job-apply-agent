"""Upgrade/downgrade evidence for strict ATS qualification authority."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from db.models import Base

ROOT = Path(__file__).resolve().parents[1]
REVISION_018 = "018_signed_autopilot_policy"


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic(path: Path, *args: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = _database_url(path)
    environment["APP_ENV"] = "test"
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def _insert_canary_history(engine) -> None:
    """Seed one complete canary chain to prove downgrade preserves the attempt."""

    values = {
        "authorization": "a" * 64,
        "contract": "c" * 64,
        "cv_hash": "d" * 64,
        "dry_evidence": "1" * 64,
        "fixture": "e" * 64,
        "fingerprint": "f" * 64,
        "job_url": "2" * 64,
        "live_evidence": "3" * 64,
        "nonce": "4" * 64,
        "permit_nonce": "5" * 64,
        "release": "6" * 64,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE applications SET selected_cv_id = 'cv-ai', "
                "selected_cv_hash = :cv_hash, profile_version = 1 WHERE id = 1"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO form_plans "
                "(id, plan_id, application_id, application_revision, adapter_name, "
                "adapter_version, selector_version, fingerprint, selected_cv_id, "
                "selected_cv_hash, attached_cv_id, attached_cv_hash, attachment_verified, "
                "attachment_verification_source, attachment_verified_at, profile_version, "
                "fields_json, disclosures_json, decisions_json, blockers_json, locale, "
                "answer_policy_version, session_verified_at, created_at, expires_at) VALUES "
                "(1, '00000000-0000-4000-8000-000000000019', 1, 1, 'greenhouse', "
                "'1.0.0', 'greenhouse-candidate-v9', :fingerprint, 'cv-ai', :cv_hash, "
                "'cv-ai', :cv_hash, true, 'browser_upload_receipt', CURRENT_TIMESTAMP, 1, "
                "'[]', '[]', '[]', '[]', 'en', 'answer-policy-v1', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, datetime('now', '+30 minutes'))"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO adapter_qualification_records "
                "(id, qualification_tier, adapter_name, adapter_version, selector_version, "
                "execution_contract_version, form_fingerprint, form_contract_digest, "
                "fixture_digest, application_id, application_revision, form_plan_id, "
                "attempt_id, job_url_hash, evidence_digest, runner_release, qualified_at) "
                "VALUES (1, 'dry_run_qualified', 'greenhouse', '1.0.0', "
                "'greenhouse-candidate-v9', 'two-phase-v2', :fingerprint, :contract, "
                ":fixture, 1, 1, 1, NULL, :job_url, :dry_evidence, :release, "
                "CURRENT_TIMESTAMP)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO qualification_canary_authorizations "
                "(id, authorization_digest, nonce_hash, application_id, application_revision, "
                "form_plan_id, dry_run_qualification_id, adapter_name, adapter_version, "
                "selector_version, execution_contract_version, form_fingerprint, "
                "form_contract_digest, selected_cv_hash, job_url_hash, runner_release, "
                "issued_at, expires_at, consumed_at) VALUES "
                "(1, :authorization, :nonce, 1, 1, 1, 1, 'greenhouse', '1.0.0', "
                "'greenhouse-candidate-v9', 'two-phase-v2', :fingerprint, :contract, "
                ":cv_hash, :job_url, :release, CURRENT_TIMESTAMP, "
                "datetime('now', '+5 minutes'), CURRENT_TIMESTAMP)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO submissions "
                "(id, application_id, attempt_number, idempotency_key, submitter_name, status, "
                "stage, outcome, application_revision, adapter_name, adapter_version, "
                "selector_version, form_plan_id, form_plan_fingerprint, selected_cv_id, "
                "requested_cv_id, requested_cv_hash, attached_cv_id, attached_cv_hash, "
                "attachment_verified, profile_version, runner_release, authority_kind, "
                "qualification_canary_authorization_id, "
                "qualification_canary_authorization_digest, created_at) VALUES "
                "(1, 1, 1, 'qualification-canary-history', 'greenhouse', 'pending', "
                "'finished', 'unknown', 1, 'greenhouse', '1.0.0', "
                "'greenhouse-candidate-v9', 1, :fingerprint, 'cv-ai', 'cv-ai', :cv_hash, "
                "'cv-ai', :cv_hash, true, 1, :release, 'qualification_canary', 1, "
                ":authorization, CURRENT_TIMESTAMP)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO final_submit_permits "
                "(attempt_id, nonce_hash, job_url_hash, application_revision, adapter_name, "
                "adapter_version, selector_version, form_plan_fingerprint, cv_hash, issued_at, "
                "expires_at, authority_kind, qualification_canary_authorization_digest) "
                "VALUES (1, :permit_nonce, :job_url, 1, 'greenhouse', '1.0.0', "
                "'greenhouse-candidate-v9', :fingerprint, :cv_hash, CURRENT_TIMESTAMP, "
                "datetime('now', '+5 minutes'), 'qualification_canary', :authorization)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO adapter_qualification_records "
                "(id, qualification_tier, adapter_name, adapter_version, selector_version, "
                "execution_contract_version, form_fingerprint, form_contract_digest, "
                "fixture_digest, application_id, application_revision, form_plan_id, "
                "attempt_id, job_url_hash, evidence_digest, runner_release, qualified_at) "
                "VALUES (2, 'live_canary_qualified', 'greenhouse', '1.0.0', "
                "'greenhouse-candidate-v9', 'two-phase-v2', :fingerprint, :contract, "
                ":fixture, 1, 1, 1, 1, :job_url, :live_evidence, :release, "
                "CURRENT_TIMESTAMP)"
            ),
            values,
        )


def test_ats_qualification_migration_round_trip_preserves_telemetry(tmp_path) -> None:
    database = tmp_path / "ats-qualification-migration.db"
    _alembic(database, "upgrade", REVISION_018)
    engine = create_engine(_database_url(database))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (title, source_url, status, created_at) "
                "VALUES ('Existing', 'https://example.test/existing', "
                "'draft', CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO applications (job_id, status, revision, created_at, updated_at) "
                "VALUES (1, 'draft', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO browser_qualification_runs "
                "(selector_version, terminal_reason, qualified, trace_json, adapter_name, "
                "adapter_version, qualification_tier, form_fingerprint, "
                "form_contract_digest, fixture_digest, created_at) VALUES "
                "('legacy-v1', 'LIVE_CANARY_CONFIRMED', true, '[]', 'greenhouse', "
                "'1.0.0', 'live_canary_qualified', :fingerprint, :contract, :fixture, "
                "CURRENT_TIMESTAMP)"
            ),
            {
                "fingerprint": "f" * 64,
                "contract": "c" * 64,
                "fixture": "e" * 64,
            },
        )
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    inspector = inspect(engine)
    assert {
        "adapter_qualification_records",
        "qualification_canary_authorizations",
    }.issubset(inspector.get_table_names())
    submission_columns = {column["name"] for column in inspector.get_columns("submissions")}
    assert {
        "qualification_canary_authorization_id",
        "qualification_canary_authorization_digest",
    }.issubset(submission_columns)
    permit_columns = {column["name"] for column in inspector.get_columns("final_submit_permits")}
    assert "qualification_canary_authorization_digest" in permit_columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM applications")) == 1
        assert connection.scalar(text("SELECT count(*) FROM browser_qualification_runs")) == 1
        # Legacy telemetry is deliberately never upgraded into live authority.
        assert connection.scalar(text("SELECT count(*) FROM adapter_qualification_records")) == 0
        assert (
            connection.scalar(
                text(
                    "SELECT qualification_tier FROM browser_qualification_runs "
                    "WHERE selector_version = 'legacy-v1'"
                )
            )
            == "live_canary_qualified"
        )
        differences = compare_metadata(
            MigrationContext.configure(
                connection,
                opts={"compare_type": False, "compare_server_default": True},
            ),
            Base.metadata,
        )
    relevant = [
        difference
        for difference in differences
        if any(
            marker in repr(difference)
            for marker in (
                "adapter_qualification_records",
                "qualification_canary_authorizations",
                "qualification_canary_authorization",
            )
        )
    ]
    assert relevant == []
    engine.dispose()

    engine = create_engine(_database_url(database))
    _insert_canary_history(engine)
    engine.dispose()

    _alembic(database, "downgrade", REVISION_018)
    engine = create_engine(_database_url(database))
    inspector = inspect(engine)
    assert {
        "adapter_qualification_records",
        "qualification_canary_authorizations",
    }.isdisjoint(inspector.get_table_names())
    submission_columns = {column["name"] for column in inspector.get_columns("submissions")}
    assert {
        "qualification_canary_authorization_id",
        "qualification_canary_authorization_digest",
    }.isdisjoint(submission_columns)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM applications")) == 1
        assert connection.scalar(text("SELECT count(*) FROM browser_qualification_runs")) == 1
        downgraded_attempt = (
            connection.execute(
                text("SELECT authority_kind, status, stage, outcome FROM submissions WHERE id = 1")
            )
            .mappings()
            .one()
        )
        assert dict(downgraded_attempt) == {
            "authority_kind": "legacy",
            "status": "pending",
            "stage": "finished",
            "outcome": "unknown",
        }
        assert (
            connection.scalar(
                text("SELECT authority_kind FROM final_submit_permits WHERE attempt_id = 1")
            )
            == "legacy"
        )
    engine.dispose()

    _alembic(database, "upgrade", "head")
