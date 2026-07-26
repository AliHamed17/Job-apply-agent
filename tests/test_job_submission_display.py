"""Regression coverage for truth-derived job and export presentation states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import dashboard as dashboard_route
from api.routes import export as export_route
from api.routes import jobs as jobs_route
from api.routes import widgets as widgets_route
from api.submission_display import job_submission_display
from db.models import (
    Application,
    Base,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionEvidence,
    SubmissionStatus,
)


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'job-display.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _application(db, suffix: str, *, status: JobStatus) -> Application:
    job = Job(
        title=f"Engineer {suffix}",
        company="Example",
        source_url=f"https://example.test/jobs/{suffix}",
        apply_url=f"https://example.test/jobs/{suffix}",
        status=status,
    )
    db.add(job)
    db.flush()
    application = Application(job_id=job.id, status=status)
    db.add(application)
    db.flush()
    return application


def _add_verified_attempt(db, application: Application) -> Submission:
    now = datetime.now(UTC).replace(tzinfo=None)
    fingerprint = "a" * 64
    cv_hash = "b" * 64
    evidence_digest = "c" * 64
    plan = FormPlan(
        plan_id=str(uuid4()),
        application_id=application.id,
        application_revision=application.revision,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        fingerprint=fingerprint,
        selected_cv_id="cv-ai",
        selected_cv_hash=cv_hash,
        attached_cv_id="cv-ai",
        attached_cv_hash=cv_hash,
        attachment_verified=True,
        profile_version=1,
        fields_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.flush()
    attempt = Submission(
        application_id=application.id,
        attempt_number=1,
        submitter_name="greenhouse",
        application_revision=application.revision,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        profile_version=1,
        runner_release="test-release",
        status=SubmissionStatus.SUCCESS,
        stage="finished",
        outcome="confirmed_submitted",
        reason_code="EMPLOYER_VERIFIED",
        submitted_at=now,
        final_action_at=now,
        confirmation_id="receipt-1",
        attachment_verified=True,
        form_plan_id=plan.id,
        form_plan_fingerprint=fingerprint,
        requested_cv_id="cv-ai",
        requested_cv_hash=cv_hash,
        attached_cv_id="cv-ai",
        attached_cv_hash=cv_hash,
        verification_kind="employer_application_id",
        evidence_digest=evidence_digest,
    )
    attempt.evidence.append(
        SubmissionEvidence(
            evidence_type="employer_application_id",
            evidence_digest=evidence_digest,
            employer_application_ref="receipt-1",
            form_fingerprint=fingerprint,
            cv_hash=cv_hash,
            observed_at=now,
        )
    )
    db.add(attempt)
    return attempt


@pytest.mark.asyncio
async def test_job_api_never_presents_operator_or_legacy_status_as_verified(tmp_path):
    db = _db(tmp_path)
    operator = _application(db, "operator", status=JobStatus.SUBMITTED)
    legacy = _application(db, "legacy", status=JobStatus.SUBMITTED)
    verified = _application(db, "verified", status=JobStatus.SUBMITTED)
    db.add_all(
        [
            Submission(
                application_id=operator.id,
                attempt_number=1,
                submitter_name="operator_reconciliation",
                status=SubmissionStatus.UNKNOWN,
                stage="finished",
                outcome="operator_confirmed",
                reason_code="OPERATOR_CONFIRMED_SUBMITTED",
                verification_kind="operator_confirmed",
            ),
            Submission(
                application_id=legacy.id,
                attempt_number=1,
                submitter_name="legacy",
                status=SubmissionStatus.UNKNOWN,
                stage="finished",
                outcome="legacy_unverified",
            ),
        ]
    )
    _add_verified_attempt(db, verified)
    db.commit()

    responses = await jobs_route.list_jobs(
        status=None,
        min_score=None,
        limit=50,
        offset=0,
        db=db,
    )
    by_title = {response.title: response for response in responses}

    assert by_title["Engineer operator"].status == "submitted"
    assert by_title["Engineer operator"].display_status == "unverified"
    assert by_title["Engineer operator"].employer_verified is False
    assert by_title["Engineer legacy"].display_status == "unverified"
    assert by_title["Engineer legacy"].employer_verified is False
    assert by_title["Engineer verified"].display_status == "submitted"
    assert by_title["Engineer verified"].employer_verified is True
    db.close()


@pytest.mark.asyncio
async def test_dashboard_and_export_split_unverified_submission_records(tmp_path, monkeypatch):
    db = _db(tmp_path)
    operator = _application(db, "operator", status=JobStatus.SUBMITTED)
    verified = _application(db, "verified", status=JobStatus.SUBMITTED)
    db.add(
        Submission(
            application_id=operator.id,
            attempt_number=1,
            submitter_name="operator_reconciliation",
            status=SubmissionStatus.UNKNOWN,
            stage="finished",
            outcome="operator_confirmed",
            reason_code="OPERATOR_CONFIRMED_SUBMITTED",
            verification_kind="operator_confirmed",
        )
    )
    _add_verified_attempt(db, verified)
    db.commit()
    monkeypatch.setattr(
        dashboard_route,
        "readiness_report",
        lambda _settings: {"status": "ready", "checks": {}},
    )

    summary = await dashboard_route.dashboard_summary(db)
    exported = await export_route.export_applications(format="json", db=db)
    widget = await widgets_route.get_widget_summary(db)
    records = {row["title"]: row for row in exported}
    widget_records = {row["job_title"]: row for row in widget.latest_actions}

    assert summary.jobs_by_status["submitted"] == 1
    assert summary.jobs_by_status["unverified"] == 1
    assert summary.submissions_success == 1
    assert records["Engineer operator"]["status"] == "unverified"
    assert records["Engineer operator"]["source_status"] == "submitted"
    assert records["Engineer operator"]["employer_verified"] is False
    assert records["Engineer verified"]["status"] == "submitted"
    assert records["Engineer verified"]["employer_verified"] is True
    assert widget_records["Engineer operator"]["status"] == "unverified"
    assert widget_records["Engineer operator"]["employer_verified"] is False
    assert widget_records["Engineer verified"]["status"] == "submitted"
    assert widget_records["Engineer verified"]["employer_verified"] is True
    db.close()


def test_truth_display_uses_latest_attempt_not_raw_submitted_enum(tmp_path):
    db = _db(tmp_path)
    application = _application(db, "raw", status=JobStatus.SUBMITTED)
    db.commit()

    display = job_submission_display(application.job)

    assert display.source_status == "submitted"
    assert display.display_status == "unverified"
    assert display.employer_verified is False
    db.close()
