from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from api.routes import cv_routing as cv_routing_route
from api.routes.applications import (
    ReconcileRequest,
    reconcile_application,
    reconcile_submission_attempt,
    retry_application,
)
from db.models import (
    Application,
    Base,
    Job,
    JobStatus,
    Submission,
    SubmissionStatus,
)
from db.session import get_db


def _unknown_attempt(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reconcile.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    job = Job(title="Engineer", source_url="https://example.test/1", status=JobStatus.NEEDS_REVIEW)
    db.add(job)
    db.flush()
    app = Application(job_id=job.id, status=JobStatus.NEEDS_REVIEW)
    db.add(app)
    db.flush()
    db.add(
        Submission(
            application_id=app.id,
            attempt_number=1,
            submitter_name="linkedin",
            status=SubmissionStatus.UNKNOWN,
            reason_code="STALE_INDETERMINATE",
        )
    )
    db.commit()
    return db, app.id


def _legacy_unverified_attempt(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-reconcile.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    job = Job(
        title="Legacy Engineer",
        source_url="https://example.test/legacy",
        status=JobStatus.NEEDS_REVIEW,
    )
    application = Application(
        job=job,
        status=JobStatus.NEEDS_REVIEW,
        selected_cv_id="cv-legacy",
    )
    db.add(application)
    db.flush()
    attempt = Submission(
        application_id=application.id,
        attempt_number=1,
        submitter_name="legacy",
        status=SubmissionStatus.SUCCESS,
        stage="finished",
        outcome="legacy_unverified",
        reason_code="LEGACY_UNVERIFIED",
    )
    db.add(attempt)
    db.commit()
    return db, application.id, attempt.id


@pytest.mark.asyncio
async def test_unknown_attempt_cannot_retry_before_reconciliation(tmp_path):
    db, app_id = _unknown_attempt(tmp_path)
    with pytest.raises(HTTPException) as exc:
        await retry_application(app_id, db)
    assert exc.value.status_code == 409
    db.close()


@pytest.mark.asyncio
async def test_reconcile_confirmed_not_submitted_allows_later_manual_retry(tmp_path):
    db, app_id = _unknown_attempt(tmp_path)
    result = await reconcile_application(
        app_id,
        ReconcileRequest(
            outcome="confirmed_not_submitted",
            note="Checked LinkedIn application history.",
        ),
        db,
    )
    app = db.get(Application, app_id)
    assert result["outcome"] == "failed_before_commit"
    assert result["reconciliation_result"] == "confirmed_not_submitted"
    assert app.status == JobStatus.DRAFT
    assert app.submission.status == SubmissionStatus.FAILED
    assert app.submission.reason_code == "RECONCILED_NOT_SUBMITTED"
    db.close()


@pytest.mark.asyncio
async def test_reconcile_confirmed_submitted_closes_application(tmp_path):
    db, app_id = _unknown_attempt(tmp_path)
    result = await reconcile_application(
        app_id,
        ReconcileRequest(
            outcome="confirmed_submitted",
            note="Confirmed in LinkedIn application history.",
        ),
        db,
    )
    app = db.get(Application, app_id)
    assert app.status == JobStatus.SUBMITTED
    assert app.job.status == JobStatus.SUBMITTED
    assert app.submission.status == SubmissionStatus.UNKNOWN
    assert app.submission.outcome == "operator_confirmed"
    assert app.submission.verification_kind == "operator_confirmed"
    assert app.submission.submitted_at is None
    assert result["outcome"] == "operator_confirmed"
    assert result["reconciliation_result"] == "confirmed_submitted"
    assert result["verified"] is False
    db.close()


@pytest.mark.asyncio
async def test_legacy_unverified_can_be_reconciled_without_becoming_green(tmp_path):
    db, app_id, attempt_id = _legacy_unverified_attempt(tmp_path)

    result = await reconcile_submission_attempt(
        attempt_id,
        ReconcileRequest(
            outcome="confirmed_submitted",
            note="Manually checked the historical candidate portal record.",
            reference="redacted-legacy-record",
        ),
        db,
    )

    attempt = db.get(Submission, attempt_id)
    application = db.get(Application, app_id)
    assert result["verified"] is False
    assert attempt.status == SubmissionStatus.UNKNOWN
    assert attempt.outcome == "operator_confirmed"
    assert attempt.submitted_at is None
    assert attempt.verification_kind == "operator_confirmed"
    assert application.status == JobStatus.SUBMITTED
    db.close()


@pytest.mark.asyncio
async def test_legacy_unverified_not_submitted_becomes_retryable(tmp_path):
    db, app_id, attempt_id = _legacy_unverified_attempt(tmp_path)

    result = await reconcile_submission_attempt(
        attempt_id,
        ReconcileRequest(
            outcome="confirmed_not_submitted",
            note="Historical portal contains no application record.",
        ),
        db,
    )

    attempt = db.get(Submission, attempt_id)
    application = db.get(Application, app_id)
    assert result["verified"] is False
    assert attempt.status == SubmissionStatus.FAILED
    assert attempt.outcome == "failed_before_commit"
    assert application.status == JobStatus.DRAFT

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(applications_route, "_validate_selected_cv", lambda _app: None)
        retry = await retry_application(app_id, db)
    assert retry.state == "prepared"
    db.close()


@pytest.mark.asyncio
async def test_operator_confirmed_attempt_cannot_be_reconciled_twice(tmp_path):
    db, app_id = _unknown_attempt(tmp_path)
    attempt_id = db.get(Application, app_id).submission.id
    await reconcile_submission_attempt(
        attempt_id,
        ReconcileRequest(
            outcome="confirmed_submitted",
            note="Confirmed once in the candidate portal.",
        ),
        db,
    )

    with pytest.raises(HTTPException) as exc:
        await reconcile_submission_attempt(
            attempt_id,
            ReconcileRequest(
                outcome="confirmed_not_submitted",
                note="A conflicting second reconciliation must be rejected.",
            ),
            db,
        )

    assert exc.value.status_code == 409
    db.expire_all()
    assert db.get(Submission, attempt_id).outcome == "operator_confirmed"
    db.close()


@pytest.mark.asyncio
async def test_reconciliation_stays_in_review_until_all_unknown_history_is_resolved(
    tmp_path,
):
    db, app_id = _unknown_attempt(tmp_path)
    first_attempt = db.get(Application, app_id).submission
    second_attempt = Submission(
        application_id=app_id,
        attempt_number=2,
        submitter_name="legacy",
        status=SubmissionStatus.UNKNOWN,
        stage="finished",
        outcome="unknown",
        reason_code="STALE_INDETERMINATE",
    )
    db.add(second_attempt)
    db.commit()

    await reconcile_submission_attempt(
        first_attempt.id,
        ReconcileRequest(
            outcome="confirmed_not_submitted",
            note="First historical attempt is absent from the portal.",
        ),
        db,
    )

    application = db.get(Application, app_id)
    assert application.status == JobStatus.NEEDS_REVIEW
    assert application.job.status == JobStatus.NEEDS_REVIEW
    assert application.needs_review_reason == "STALE_INDETERMINATE"
    with pytest.raises(HTTPException) as exc:
        await retry_application(app_id, db)
    assert exc.value.status_code == 409
    db.rollback()

    await reconcile_submission_attempt(
        second_attempt.id,
        ReconcileRequest(
            outcome="confirmed_not_submitted",
            note="Second historical attempt is also absent from the portal.",
        ),
        db,
    )
    assert application.status == JobStatus.DRAFT
    assert application.job.status == JobStatus.DRAFT
    db.close()


@pytest.mark.asyncio
async def test_repeat_reconciliation_is_rejected_without_overwriting_first_result(tmp_path):
    db, app_id = _unknown_attempt(tmp_path)
    attempt_id = db.get(Application, app_id).submission.id
    first = await reconcile_submission_attempt(
        attempt_id,
        ReconcileRequest(
            outcome="confirmed_not_submitted",
            note="Checked candidate portal history.",
            reference="portal-record-1",
        ),
        db,
    )

    with pytest.raises(HTTPException) as exc:
        await reconcile_submission_attempt(
            attempt_id,
            ReconcileRequest(
                outcome="confirmed_submitted",
                note="Conflicting second operator result.",
                reference="portal-record-2",
            ),
            db,
        )

    assert first["outcome"] == "failed_before_commit"
    assert first["reconciliation_result"] == "confirmed_not_submitted"
    assert exc.value.status_code == 409
    db.expire_all()
    attempt = db.get(Submission, attempt_id)
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reconciliation_evidence_ref == "portal-record-1"
    db.close()


def test_outcome_route_is_unique_and_rejects_unbounded_values(tmp_path):
    db, _app_id = _unknown_attempt(tmp_path)
    api = FastAPI()
    api.include_router(applications_route.router, prefix="/api")
    api.include_router(cv_routing_route.router, prefix="/api")

    def override_db():
        yield db

    api.dependency_overrides[get_db] = override_db
    matching_routes = [
        route
        for router in (applications_route.router, cv_routing_route.router)
        for route in router.routes
        if getattr(route, "path", None) == "/applications/{application_id}/outcome"
        and "POST" in getattr(route, "methods", set())
    ]
    assert len(matching_routes) == 1

    unsupported = TestClient(api).post(
        f"/api/applications/{_app_id}/outcome",
        json={"outcome": "arbitrary-external-text", "note": "x"},
    )
    oversized = TestClient(api).post(
        f"/api/applications/{_app_id}/outcome",
        json={"outcome": "interview", "note": "x" * 501},
    )
    assert unsupported.status_code == 422
    assert oversized.status_code == 422
    db.close()
