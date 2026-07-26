from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from api.routes import dashboard as dashboard_route
from core.submission_truth import (
    is_employer_verified,
    latest_employer_verified_count,
)
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
from db.session import get_db


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'truth.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _application(db, suffix: str = "1") -> Application:
    job = Job(
        title="Engineer",
        company="Example",
        source_url=f"https://example.test/jobs/{suffix}",
        apply_url=f"https://example.test/jobs/{suffix}",
        status=JobStatus.DRAFT,
    )
    db.add(job)
    db.flush()
    application = Application(job_id=job.id, status=JobStatus.DRAFT)
    db.add(application)
    db.flush()
    return application


def _success_attempt(
    db,
    application: Application,
    attempt_number: int = 1,
    **overrides,
):
    now = overrides.pop(
        "clock",
        datetime.now(UTC).replace(tzinfo=None),
    )
    evidence_observed_at = overrides.pop("evidence_observed_at", now)
    fingerprint = "a" * 64
    cv_hash = "b" * 64
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
    values = {
        "application_id": application.id,
        "attempt_number": attempt_number,
        "submitter_name": "greenhouse",
        "application_revision": application.revision,
        "adapter_name": "greenhouse",
        "adapter_version": "1.0.0",
        "selector_version": "fixture-v1",
        "profile_version": 1,
        "runner_release": "test-release",
        "status": SubmissionStatus.SUCCESS,
        "stage": "finished",
        "outcome": "confirmed_submitted",
        "reason_code": "EMPLOYER_VERIFIED",
        "submitted_at": now,
        "final_action_at": now,
        "confirmation_id": "receipt-1",
        "attachment_verified": True,
        "form_plan_id": plan.id,
        "form_plan_fingerprint": fingerprint,
        "requested_cv_id": "cv-ai",
        "requested_cv_hash": cv_hash,
        "attached_cv_id": "cv-ai",
        "attached_cv_hash": cv_hash,
        "verification_kind": "employer_application_id",
        "evidence_digest": "c" * 64,
    }
    values.update(overrides)
    attempt = Submission(**values)
    if (
        attempt.reason_code == "EMPLOYER_VERIFIED"
        and attempt.confirmation_id
        and attempt.confirmation_id.strip()
    ):
        attempt.evidence.append(
            SubmissionEvidence(
                evidence_type=attempt.verification_kind,
                evidence_digest=attempt.evidence_digest,
                employer_application_ref=attempt.confirmation_id.strip(),
                form_fingerprint=attempt.form_plan_fingerprint,
                cv_hash=attempt.attached_cv_hash,
                observed_at=evidence_observed_at,
            )
        )
    return attempt


def _legacy_attempt(application_id: int, attempt_number: int = 1, **overrides):
    values = {
        "application_id": application_id,
        "attempt_number": attempt_number,
        "submitter_name": "legacy",
        "status": SubmissionStatus.UNKNOWN,
        "stage": "finished",
        "outcome": "legacy_unverified",
        "reason_code": None,
        "submitted_at": None,
        "legacy_reported_at": datetime.now(UTC).replace(tzinfo=None),
    }
    values.update(overrides)
    return Submission(**values)


def test_employer_verification_is_fail_closed():
    now = datetime.now(UTC)
    evidence = SimpleNamespace(
        attempt_id=1,
        evidence_type="employer_application_id",
        evidence_digest="c" * 64,
        form_fingerprint="a" * 64,
        cv_hash="b" * 64,
        observed_at=now,
    )
    verified = SimpleNamespace(
        id=1,
        status=SubmissionStatus.SUCCESS,
        stage="finished",
        outcome="confirmed_submitted",
        reason_code="EMPLOYER_VERIFIED",
        submitted_at=now,
        final_action_at=now,
        attachment_verified=True,
        form_plan_id=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        profile_version=1,
        runner_release="test-release",
        form_plan_fingerprint="a" * 64,
        requested_cv_id="cv-ai",
        requested_cv_hash="b" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="b" * 64,
        verification_kind="employer_application_id",
        evidence_digest="c" * 64,
        evidence=[evidence],
    )
    assert is_employer_verified(verified)

    for field, value in (
        ("reason_code", "RECONCILED_SUBMITTED"),
        ("submitted_at", None),
        ("final_action_at", None),
        ("evidence_digest", None),
        ("stage", "verifying"),
        ("outcome", "legacy_unverified"),
        ("attachment_verified", False),
        ("status", SubmissionStatus.DRAFT_ONLY),
        ("evidence", []),
    ):
        candidate = SimpleNamespace(**vars(verified))
        setattr(candidate, field, value)
        assert not is_employer_verified(candidate)

    predating = SimpleNamespace(**vars(verified))
    predating.evidence = [
        SimpleNamespace(
            **{
                **vars(evidence),
                "observed_at": now - timedelta(seconds=1),
            }
        )
    ]
    assert not is_employer_verified(predating)

    impossible_order = SimpleNamespace(**vars(verified))
    impossible_order.submitted_at = now - timedelta(seconds=1)
    assert not is_employer_verified(impossible_order)


@pytest.mark.asyncio
async def test_application_hides_legacy_submitted_timestamp(tmp_path):
    db = _db(tmp_path)
    application = _application(db)
    db.add(_legacy_attempt(application.id, confirmation_id="legacy"))
    db.commit()

    response = await applications_route.get_application(application.id, db)

    assert response.submission_status == "unknown"
    assert response.submission_verified is False
    assert response.submitted_at is None
    assert response.attempts[0].verified is False
    assert response.attempts[0].submitted_at is None
    db.close()


@pytest.mark.asyncio
async def test_application_responses_expose_form_plan_expiry_and_invalidation(tmp_path):
    db = _db(tmp_path)
    application = _application(db)
    attempt = _success_attempt(db, application)
    db.add(attempt)
    db.flush()
    plan = db.get(FormPlan, attempt.form_plan_id)
    assert plan is not None
    invalidated_at = datetime.now(UTC).replace(tzinfo=None)
    plan.invalidated_at = invalidated_at
    plan.invalidation_reason = "FORM_CHANGED"
    expires_at = plan.expires_at
    db.commit()

    detail = await applications_route.get_application(application.id, db)
    listing = await applications_route.list_applications(status=None, db=db)
    listed = next(item for item in listing if item.id == application.id)

    for response in (detail, listed):
        assert response.form_plan_valid is False
        assert response.form_plan_expires_at == expires_at.isoformat()
        assert response.form_plan_invalidated_at == invalidated_at.isoformat()
    db.close()


@pytest.mark.asyncio
async def test_dashboard_counts_only_latest_employer_verified_attempt(
    tmp_path,
    monkeypatch,
):
    db = _db(tmp_path)
    first = _application(db, "1")
    db.add(_success_attempt(db, first, 1, confirmation_id="old-receipt"))
    db.add(
        Submission(
            application_id=first.id,
            attempt_number=2,
            submitter_name="dry_run",
            status=SubmissionStatus.DRAFT_ONLY,
            reason_code="DRY_RUN_DISCARDED",
        )
    )
    second = _application(db, "2")
    db.add(_success_attempt(db, second, 1, confirmation_id="current-receipt"))
    prepared = _application(db, "3")
    prepared.approved_at = datetime.now(UTC).replace(tzinfo=None)
    prepared.approval_source = "manual_prepare"
    prepared.prepared_revision = prepared.revision
    empty_evidence = _application(db, "4")
    db.add(_legacy_attempt(empty_evidence.id, 1, confirmation_id=""))
    db.commit()
    monkeypatch.setattr(
        dashboard_route,
        "readiness_report",
        lambda _settings: {"status": "ready", "checks": {}},
    )

    summary = await dashboard_route.dashboard_summary(db)

    assert summary.submissions_total == 4
    assert summary.submissions_success == 1
    assert summary.applications_pending == 3
    assert summary.applications_approved == 1
    db.close()


def test_employer_verified_query_rejects_evidence_that_predates_final_action(tmp_path):
    db = _db(tmp_path)
    application = _application(db, "predating")
    final_action_at = datetime.now(UTC).replace(tzinfo=None)
    db.add(
        _success_attempt(
            db,
            application,
            final_action_at=final_action_at,
            submitted_at=final_action_at + timedelta(seconds=1),
            evidence_observed_at=final_action_at - timedelta(seconds=1),
        )
    )
    db.commit()

    assert latest_employer_verified_count(db) == 0
    attempt = db.query(Submission).one()
    assert not is_employer_verified(attempt)
    db.close()


@pytest.mark.asyncio
async def test_multi_url_ingest_reports_accept_reject_and_duplicate(
    tmp_path,
    monkeypatch,
):
    db = _db(tmp_path)
    queued: list[int] = []
    monkeypatch.setattr(
        dashboard_route,
        "get_settings",
        lambda: SimpleNamespace(tasks_always_eager=True),
    )
    from worker import tasks

    monkeypatch.setattr(
        tasks.process_url_task,
        "apply",
        lambda args: queued.append(args[0]),
    )

    first = await dashboard_route.manual_ingest(
        dashboard_route.ManualIngestRequest(
            urls=[
                "https://boards.greenhouse.io/example/jobs/123",
                "http://[::1",
                "not-a-url",
            ]
        ),
        db,
    )
    second = await dashboard_route.manual_ingest(
        dashboard_route.ManualIngestRequest(url="https://boards.greenhouse.io/example/jobs/123"),
        db,
    )

    assert [result.state for result in first.results] == [
        "accepted",
        "rejected",
        "rejected",
    ]
    assert second.results[0].state == "duplicate"
    assert len(queued) == 1
    db.close()


def test_multi_url_ingest_is_exposed_on_unambiguous_api_route(
    tmp_path,
    monkeypatch,
):
    db = _db(tmp_path)
    api = FastAPI()
    api.include_router(dashboard_route.router, prefix="/api")

    def override_db():
        yield db

    api.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        dashboard_route,
        "get_settings",
        lambda: SimpleNamespace(tasks_always_eager=True),
    )
    from worker import tasks

    monkeypatch.setattr(
        tasks.process_url_task,
        "apply",
        lambda *_args, **_kwargs: None,
    )
    response = TestClient(api).post(
        "/api/dashboard/ingest",
        json={
            "urls": [
                "https://boards.greenhouse.io/example/jobs/456",
                "not-a-url",
            ]
        },
    )

    assert response.status_code == 202
    assert [item["state"] for item in response.json()["results"]] == [
        "accepted",
        "rejected",
    ]
    db.close()


@pytest.mark.asyncio
async def test_manual_reconciliation_never_becomes_employer_verified(tmp_path):
    db = _db(tmp_path)
    application = _application(db)
    application.status = JobStatus.NEEDS_REVIEW
    db.add(
        Submission(
            application_id=application.id,
            attempt_number=1,
            submitter_name="workday",
            status=SubmissionStatus.UNKNOWN,
            reason_code="FINAL_ACTION_UNCONFIRMED",
        )
    )
    db.commit()

    result = await applications_route.reconcile_application(
        application.id,
        applications_route.ReconcileRequest(
            outcome="confirmed_submitted",
            source="candidate_portal",
            reference="operator checked account history",
            note="A matching record is visible.",
        ),
        db,
    )
    db.expire_all()
    attempt = db.get(Application, application.id).submission

    assert result["verification_kind"] == "operator_confirmed"
    assert result["verified"] is False
    assert attempt.reason_code == "OPERATOR_CONFIRMED_SUBMITTED"
    assert attempt.submitted_at is None
    assert not is_employer_verified(attempt)
    db.close()


def test_submission_report_excludes_unverified_and_private_content(
    tmp_path,
    monkeypatch,
    capsys,
):
    import show_submitted_records

    db = _db(tmp_path)
    operator_only = _application(db, "operator")
    operator_only.job.title = "Operator-only record"
    operator_only.status = JobStatus.SUBMITTED
    db.add(
        Submission(
            application_id=operator_only.id,
            attempt_number=1,
            submitter_name="operator_reconciliation",
            status=SubmissionStatus.UNKNOWN,
            stage="finished",
            outcome="operator_confirmed",
            reason_code="OPERATOR_CONFIRMED_SUBMITTED",
            confirmation_id="operator-note",
            verification_kind="operator_confirmed",
        )
    )
    verified = _application(db, "verified")
    verified.job.title = "Verified record"
    verified.cover_letter = "PRIVATE COVER LETTER"
    verified.qa_answers = '{"private_answer":"SECRET ANSWER"}'
    verified.selected_cv_id = "private-cv-name"
    db.add(_success_attempt(db, verified, confirmation_id="receipt"))
    db.commit()
    monkeypatch.setattr(
        show_submitted_records,
        "get_session_factory",
        lambda: lambda: db,
    )

    show_submitted_records.display_submitted_records()

    output = capsys.readouterr().out
    assert "Verified record" in output
    assert "Operator-only record" not in output
    assert "EMPLOYER-VERIFIED" in output
    assert "PRIVATE COVER LETTER" not in output
    assert "SECRET ANSWER" not in output
    assert "private-cv-name" not in output
