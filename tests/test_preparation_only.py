"""PR1 regression coverage for preparation-only application actions."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from api.routes import webhook as webhook_route
from core.config import Settings
from db.models import (
    Application,
    ApplicationEvent,
    Base,
    Job,
    JobStatus,
    Submission,
    SubmissionStatus,
)
from db.session import get_db
from worker.drainer import drain_apply_queue_task


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'preparation.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _routing_files(tmp_path):
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    (cv_dir / "software.pdf").write_bytes(b"%PDF-1.4 sanitized fixture")
    config = tmp_path / "cv-routing.yaml"
    config.write_text(
        """
version: 1
minimum_confidence: 0.1
cvs:
  - id: software
    file: software.pdf
    role_families: [software]
    skills: [python]
fallback_cv_id: software
overrides: []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config, cv_dir


def _settings(tmp_path):
    config, cv_dir = _routing_files(tmp_path)
    return Settings(
        _env_file=None,
        cv_routing_path=str(config),
        cv_directory=str(cv_dir),
    )


def _application(
    factory,
    *,
    status: JobStatus = JobStatus.DRAFT,
    attempt_status: SubmissionStatus | None = None,
):
    db = factory()
    job = Job(
        title="Software Engineer",
        company="Acme",
        source_url="https://example.test/jobs/1",
        apply_url="https://example.test/jobs/1",
        status=status,
        score=90.0,
    )
    db.add(job)
    db.flush()
    application = Application(
        job_id=job.id,
        status=status,
        selected_cv_id="software",
    )
    db.add(application)
    db.flush()
    attempt_id = None
    if attempt_status is not None:
        terminal_outcome = {
            SubmissionStatus.FAILED: "failed_before_commit",
            SubmissionStatus.DRAFT_ONLY: "draft_only",
            SubmissionStatus.UNKNOWN: "unknown",
        }.get(attempt_status)
        attempt = Submission(
            application_id=application.id,
            attempt_number=1,
            submitter_name="greenhouse",
            status=attempt_status,
            stage="finished",
            outcome=terminal_outcome,
            reason_code=f"TEST_{attempt_status.value.upper()}",
        )
        db.add(attempt)
        db.flush()
        attempt_id = attempt.id
    db.commit()
    application_id = application.id
    job_id = job.id
    db.close()
    return application_id, job_id, attempt_id


def _forbid_worker_dispatch(monkeypatch):
    from worker.tasks import submit_application_task

    def fail_dispatch(*_args, **_kwargs):
        raise AssertionError("preparation must not dispatch the submission worker")

    monkeypatch.setattr(submit_application_task, "apply", fail_dispatch)
    monkeypatch.setattr(submit_application_task, "delay", fail_dispatch)


def _client(factory, settings, monkeypatch):
    app = FastAPI()
    app.include_router(applications_route.router, prefix="/api")

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(applications_route, "get_settings", lambda: settings)
    _forbid_worker_dispatch(monkeypatch)
    return app, TestClient(app)


@pytest.mark.parametrize("endpoint", ("prepare", "approve"))
def test_prepare_routes_are_idempotent_and_create_no_attempt(
    tmp_path,
    monkeypatch,
    endpoint,
):
    factory = _factory(tmp_path)
    app_id, _job_id, _attempt_id = _application(factory)
    api, client = _client(factory, _settings(tmp_path), monkeypatch)

    first = client.post(f"/api/applications/{app_id}/{endpoint}")
    second = client.post(f"/api/applications/{app_id}/{endpoint}")

    assert first.status_code == 202
    assert second.status_code == 202
    for response in (first, second):
        payload = response.json()
        assert payload["state"] == "prepared"
        assert payload["status"] == "prepared"
        assert payload["verified"] is False
        assert payload["attempt_id"] is None
        assert payload["status_url"] is None

    db = factory()
    application = db.get(Application, app_id)
    assert application.status == JobStatus.DRAFT
    assert application.approval_source == "manual_prepare"
    assert db.query(Submission).filter(Submission.application_id == app_id).count() == 0
    events = db.query(ApplicationEvent).filter(ApplicationEvent.application_id == app_id).all()
    assert [event.event_type for event in events] == ["application_prepared"]
    assert json.loads(events[0].details)["state"] == "prepared"
    db.close()

    paths = api.openapi()["paths"]
    assert paths["/api/applications/{app_id}/approve"]["post"]["deprecated"] is True
    assert not paths["/api/applications/{app_id}/prepare"]["post"].get("deprecated", False)


def test_application_filters_preserve_reviewable_and_prepared_semantics(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    prepared_id, _job_id, _attempt_id = _application(factory)
    reviewable_id, _job_id, _attempt_id = _application(factory)
    _legacy_prepared_id, _job_id, _attempt_id = _application(
        factory,
        status=JobStatus.APPROVED,
    )
    _api, client = _client(factory, _settings(tmp_path), monkeypatch)

    assert client.post(f"/api/applications/{prepared_id}/prepare").status_code == 202

    reviewable = client.get("/api/applications", params={"status": "draft"})
    prepared = client.get("/api/applications", params={"status": "prepared"})
    legacy_alias = client.get("/api/applications", params={"status": "approved"})

    assert reviewable.status_code == 200
    assert [(item["id"], item["status"]) for item in reviewable.json()] == [
        (reviewable_id, "draft")
    ]
    assert prepared.status_code == 200
    assert {(item["id"], item["status"]) for item in prepared.json()} == {
        (prepared_id, "prepared"),
    }
    assert legacy_alias.json() == prepared.json()
    assert client.get(f"/api/applications/{prepared_id}").json()["status"] == "prepared"


def test_submit_requires_permit_without_mutating_application(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    app_id, job_id, _attempt_id = _application(factory)
    _api, client = _client(factory, _settings(tmp_path), monkeypatch)

    response = client.post(
        f"/api/applications/{app_id}/submit",
        json={
            "acknowledgement": "SEND_APPLICATION",
            "idempotency_key": "test-only-idempotency-key",
        },
    )

    assert response.status_code == 422
    db = factory()
    application = db.get(Application, app_id)
    assert application.status == JobStatus.DRAFT
    assert application.approved_at is None
    assert application.approval_source is None
    assert db.get(Job, job_id).status == JobStatus.DRAFT
    assert db.query(Submission).filter(Submission.application_id == app_id).count() == 0
    assert db.query(ApplicationEvent).filter(ApplicationEvent.application_id == app_id).count() == 0
    db.close()


def test_live_worker_refuses_dispatch_without_validated_command(
    tmp_path,
    monkeypatch,
):
    from worker import tasks

    factory = _factory(tmp_path)
    app_id, job_id, _attempt_id = _application(
        factory,
        status=JobStatus.APPROVED,
    )
    monkeypatch.setattr(tasks, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            dry_run=False,
            draft_only=False,
        ),
    )

    result = tasks.submit_application_task.apply(args=[app_id]).get()

    db = factory()
    application = db.get(Application, app_id)
    assert result == {
        "state": "blocked",
        "reason_code": "DATABASE_COMMAND_REQUIRED",
    }
    assert application.status == JobStatus.APPROVED
    assert application.needs_review_reason is None
    assert db.get(Job, job_id).status == JobStatus.APPROVED
    assert db.query(Submission).filter(Submission.application_id == app_id).count() == 0
    assert db.query(ApplicationEvent).filter(ApplicationEvent.application_id == app_id).count() == 0
    db.close()


@pytest.mark.parametrize(
    ("attempt_status", "application_status"),
    (
        (SubmissionStatus.FAILED, JobStatus.FAILED),
        (SubmissionStatus.DRAFT_ONLY, JobStatus.DRAFT),
    ),
)
def test_definitive_retry_prepares_without_creating_a_new_attempt(
    tmp_path,
    monkeypatch,
    attempt_status,
    application_status,
):
    factory = _factory(tmp_path)
    app_id, job_id, attempt_id = _application(
        factory,
        status=application_status,
        attempt_status=attempt_status,
    )
    _api, client = _client(factory, _settings(tmp_path), monkeypatch)

    response = client.post(f"/api/applications/{app_id}/retry")

    assert response.status_code == 202
    assert response.json()["state"] == "prepared"
    assert response.json()["attempt_id"] is None
    db = factory()
    application = db.get(Application, app_id)
    attempts = db.query(Submission).filter(Submission.application_id == app_id).all()
    assert application.status == JobStatus.DRAFT
    assert application.approval_source == "retry_prepare"
    assert db.get(Job, job_id).status == JobStatus.DRAFT
    assert len(attempts) == 1
    assert attempts[0].id == attempt_id
    assert attempts[0].status == attempt_status
    event = db.query(ApplicationEvent).filter(ApplicationEvent.application_id == app_id).one()
    assert event.event_type == "submission_retry_prepared"
    assert json.loads(event.details)["state"] == "prepared"
    db.close()


def test_unknown_attempt_remains_blocked_from_retry(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    app_id, job_id, attempt_id = _application(
        factory,
        status=JobStatus.NEEDS_REVIEW,
        attempt_status=SubmissionStatus.UNKNOWN,
    )
    _api, client = _client(factory, _settings(tmp_path), monkeypatch)

    prepare_response = client.post(f"/api/applications/{app_id}/prepare")
    response = client.post(f"/api/applications/{app_id}/retry")

    assert prepare_response.status_code == 409
    assert response.status_code == 409
    db = factory()
    application = db.get(Application, app_id)
    attempts = db.query(Submission).filter(Submission.application_id == app_id).all()
    assert application.status == JobStatus.NEEDS_REVIEW
    assert db.get(Job, job_id).status == JobStatus.NEEDS_REVIEW
    assert len(attempts) == 1
    assert attempts[0].id == attempt_id
    assert attempts[0].status == SubmissionStatus.UNKNOWN
    assert db.query(ApplicationEvent).filter(ApplicationEvent.application_id == app_id).count() == 0
    db.close()


def test_legacy_drainer_cannot_dispatch_without_submit_permits(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    _application(factory, status=JobStatus.APPROVED)
    _forbid_worker_dispatch(monkeypatch)
    monkeypatch.setattr("db.session.get_session_factory", lambda: factory)

    class AllowGovernor:
        def can_apply_linkedin(self):
            return True, "ok"

    monkeypatch.setattr("core.governor.get_governor", lambda: AllowGovernor())

    result = drain_apply_queue_task.apply().get()

    assert result == 0


@pytest.mark.asyncio
async def test_whatsapp_approve_is_preparation_only_and_idempotent(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    app_id, job_id, _attempt_id = _application(factory)
    _forbid_worker_dispatch(monkeypatch)
    send_message = AsyncMock()
    monkeypatch.setattr(webhook_route, "_send_whatsapp_message", send_message)
    db = factory()

    await webhook_route._handle_approve(
        job_id,
        "test-sender",
        db,
        Settings(_env_file=None),
    )
    await webhook_route._handle_approve(
        job_id,
        "test-sender",
        db,
        Settings(_env_file=None),
    )

    db.expire_all()
    application = db.get(Application, app_id)
    assert application.status == JobStatus.DRAFT
    assert application.approval_source == "whatsapp_prepare"
    assert db.query(Submission).filter(Submission.application_id == app_id).count() == 0
    events = db.query(ApplicationEvent).filter(ApplicationEvent.application_id == app_id).all()
    assert [event.event_type for event in events] == ["application_prepared"]
    messages = [call.args[1] for call in send_message.await_args_list]
    assert any("Nothing was submitted" in message for message in messages)
    assert any("Already prepared" in message for message in messages)
    db.close()
