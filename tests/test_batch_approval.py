from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from core.config import Settings
from db.models import Application, ApplicationEvent, Base, Job, JobStatus, Submission
from db.session import get_db
from worker.batch_runner import trigger_batch_auto_apply


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _routing_files(tmp_path: Path) -> tuple[Path, Path]:
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    (cv_dir / "software.pdf").write_bytes(b"%PDF-1.4 sanitized fixture")
    config = tmp_path / "routing.yaml"
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


def _draft(factory, score: float = 90.0, cv_id: str | None = "software") -> int:
    db = factory()
    job = Job(
        title="Software Engineer",
        source_url="https://example.test/jobs/1",
        apply_url="https://example.test/jobs/1",
        status=JobStatus.DRAFT,
        score=score,
    )
    db.add(job)
    db.flush()
    application = Application(
        job_id=job.id,
        status=JobStatus.DRAFT,
        selected_cv_id=cv_id,
    )
    db.add(application)
    db.commit()
    application_id = application.id
    db.close()
    return application_id


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
    return TestClient(app)


def test_batch_preview_is_read_only(tmp_path):
    factory = _factory(tmp_path)
    application_id = _draft(factory)
    db = factory()
    summary = trigger_batch_auto_apply(db, min_score=80, max_batch_size=10)
    db.close()

    assert summary.triggered_count == 0
    assert summary.application_ids == [application_id]

    check = factory()
    assert check.get(Application, application_id).status == JobStatus.DRAFT
    check.close()


def test_exact_batch_preparation_records_provenance_without_queueing(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    config, cv_dir = _routing_files(tmp_path)
    application_id = _draft(factory)
    settings = Settings(
        _env_file=None,
        cv_routing_path=str(config),
        cv_directory=str(cv_dir),
    )
    client = _client(factory, settings, monkeypatch)

    response = client.post(
        "/api/applications/batch-prepare",
        json={
            "application_ids": [application_id],
            "acknowledgement": "APPROVE_SELECTED_APPLICATIONS",
        },
    )
    assert response.status_code == 202
    assert response.json()["prepared_application_ids"] == [application_id]
    assert response.json()["queued_application_ids"] == []
    repeated = client.post(
        "/api/applications/batch-approve",
        json={
            "application_ids": [application_id],
            "acknowledgement": "PREPARE_SELECTED_APPLICATIONS",
        },
    )
    assert repeated.status_code == 409

    db = factory()
    application = db.get(Application, application_id)
    assert application.status == JobStatus.DRAFT
    assert application.approval_source == "batch_prepare"
    assert db.query(Submission).count() == 0
    event = (
        db.query(ApplicationEvent).filter(ApplicationEvent.application_id == application_id).one()
    )
    assert event.event_type == "application_prepared"
    assert event.actor == "batch_operator"
    assert "batch_prepare" in (event.details or "")
    summary = trigger_batch_auto_apply(db, min_score=80, max_batch_size=10)
    assert summary.application_ids == []
    db.close()


def test_legacy_approve_alias_prepares_without_creating_attempt(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    config, cv_dir = _routing_files(tmp_path)
    application_id = _draft(factory)
    settings = Settings(
        _env_file=None,
        cv_routing_path=str(config),
        cv_directory=str(cv_dir),
    )
    client = _client(factory, settings, monkeypatch)
    response = client.post(
        f"/api/applications/{application_id}/approve",
    )

    assert response.status_code == 202
    assert response.json()["state"] == "prepared"
    assert response.json()["attempt_id"] is None
    assert response.json()["verified"] is False
    db = factory()
    application = db.get(Application, application_id)
    assert application.status == JobStatus.DRAFT
    assert application.approval_source == "manual_prepare"
    assert db.query(Submission).count() == 0
    db.close()


def test_batch_approval_is_atomic_when_one_application_is_invalid(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    config, cv_dir = _routing_files(tmp_path)
    valid_id = _draft(factory)
    invalid_id = _draft(factory, cv_id=None)
    settings = Settings(
        _env_file=None,
        cv_routing_path=str(config),
        cv_directory=str(cv_dir),
    )
    client = _client(factory, settings, monkeypatch)

    response = client.post(
        "/api/applications/batch-approve",
        json={
            "application_ids": [valid_id, invalid_id],
            "acknowledgement": "APPROVE_SELECTED_APPLICATIONS",
        },
    )
    assert response.status_code == 409

    db = factory()
    assert db.get(Application, valid_id).status == JobStatus.DRAFT
    assert db.get(Application, invalid_id).status == JobStatus.DRAFT
    assert db.query(ApplicationEvent).count() == 0
    db.close()
