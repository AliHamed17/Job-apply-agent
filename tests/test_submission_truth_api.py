from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from api.routes import dashboard as dashboard_route
from core.submission_truth import is_employer_verified
from db.models import (
    Application,
    Base,
    Job,
    JobStatus,
    Submission,
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


def _success_attempt(application_id: int, attempt_number: int = 1, **overrides):
    values = {
        "application_id": application_id,
        "attempt_number": attempt_number,
        "submitter_name": "greenhouse",
        "status": SubmissionStatus.SUCCESS,
        "reason_code": "EMPLOYER_VERIFIED",
        "submitted_at": datetime.now(UTC).replace(tzinfo=None),
        "confirmation_id": "receipt-1",
    }
    values.update(overrides)
    return Submission(**values)


def test_employer_verification_is_fail_closed():
    verified = SimpleNamespace(
        status=SubmissionStatus.SUCCESS,
        reason_code="EMPLOYER_VERIFIED",
        submitted_at=datetime.now(UTC),
        confirmation_id="receipt",
        confirmation_url=None,
    )
    assert is_employer_verified(verified)

    for field, value in (
        ("reason_code", "RECONCILED_SUBMITTED"),
        ("submitted_at", None),
        ("confirmation_id", None),
        ("confirmation_id", ""),
        ("confirmation_id", "   "),
        ("status", SubmissionStatus.DRAFT_ONLY),
    ):
        candidate = SimpleNamespace(**vars(verified))
        setattr(candidate, field, value)
        if field == "confirmation_id":
            candidate.confirmation_url = None
        assert not is_employer_verified(candidate)


@pytest.mark.asyncio
async def test_application_hides_legacy_submitted_timestamp(tmp_path):
    db = _db(tmp_path)
    application = _application(db)
    db.add(
        _success_attempt(
            application.id,
            reason_code=None,
            confirmation_id="legacy",
        )
    )
    db.commit()

    response = await applications_route.get_application(application.id, db)

    assert response.submission_status == "success"
    assert response.submission_verified is False
    assert response.submitted_at is None
    assert response.attempts[0].verified is False
    assert response.attempts[0].submitted_at is None
    db.close()


@pytest.mark.asyncio
async def test_dashboard_counts_only_latest_employer_verified_attempt(
    tmp_path,
    monkeypatch,
):
    db = _db(tmp_path)
    first = _application(db, "1")
    db.add(_success_attempt(first.id, 1, confirmation_id="old-receipt"))
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
    db.add(_success_attempt(second.id, 1, confirmation_id="current-receipt"))
    prepared = _application(db, "3")
    prepared.approved_at = datetime.now(UTC).replace(tzinfo=None)
    prepared.approval_source = "manual_prepare"
    empty_evidence = _application(db, "4")
    db.add(_success_attempt(empty_evidence.id, 1, confirmation_id=""))
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
                "not-a-url",
            ]
        ),
        db,
    )
    second = await dashboard_route.manual_ingest(
        dashboard_route.ManualIngestRequest(url="https://boards.greenhouse.io/example/jobs/123"),
        db,
    )

    assert [result.state for result in first.results] == ["accepted", "rejected"]
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
        _success_attempt(
            operator_only.id,
            reason_code="OPERATOR_CONFIRMED_SUBMITTED",
            confirmation_id="operator-note",
        )
    )
    verified = _application(db, "verified")
    verified.job.title = "Verified record"
    verified.cover_letter = "PRIVATE COVER LETTER"
    verified.qa_answers = '{"private_answer":"SECRET ANSWER"}'
    verified.selected_cv_id = "private-cv-name"
    db.add(_success_attempt(verified.id, confirmation_id="receipt"))
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
