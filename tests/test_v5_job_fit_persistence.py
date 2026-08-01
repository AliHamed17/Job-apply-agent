from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes import jobs as jobs_routes
from db.models import Application, Base, Job, JobFitDecisionRecord
from db.session import get_db
from jobs.models import JobData
from match.job_fit import unavailable_job_fit_decision
from match.job_fit_store import decision_from_record, persist_job_fit_decision


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _job_data(title: str = "ML Engineer") -> JobData:
    return JobData(
        title=title,
        company="Example",
        location="Israel",
        source_url="https://example.test/jobs/fit",
    )


def test_fit_decisions_are_insert_only_and_digest_idempotent():
    engine, factory = _database()
    with factory() as db:
        job = Job(
            title="ML Engineer",
            company="Example",
            location="Israel",
            source_url="https://example.test/jobs/fit",
        )
        db.add(job)
        db.flush()
        decision = unavailable_job_fit_decision(
            _job_data(),
            profile_version=1,
            reason_code="FIT_QUALIFICATION_INVALID",
        )

        first = persist_job_fit_decision(db, job_id=job.id, decision=decision)
        repeated = persist_job_fit_decision(db, job_id=job.id, decision=decision)
        changed = persist_job_fit_decision(
            db,
            job_id=job.id,
            decision=unavailable_job_fit_decision(
                _job_data("Senior ML Engineer"),
                profile_version=2,
                reason_code="FIT_ROUTING_CONFIG_MISSING",
            ),
        )
        db.commit()

        assert first.id == repeated.id
        assert changed.id != first.id
        assert db.query(JobFitDecisionRecord).count() == 2
        assert decision_from_record(first) == decision
    engine.dispose()


def test_automation_decision_api_is_evidence_bounded_and_never_authorizes_send():
    engine, factory = _database()
    with factory() as db:
        job = Job(
            title="ML Engineer",
            company="Example",
            location="Israel",
            source_url="https://example.test/jobs/fit",
        )
        db.add(job)
        db.flush()
        decision = unavailable_job_fit_decision(
            _job_data(),
            profile_version=1,
            reason_code="FIT_QUALIFICATION_INVALID",
        )
        record = persist_job_fit_decision(db, job_id=job.id, decision=decision)
        db.commit()
        job_id = job.id
        record_id = record.id

    app = FastAPI()
    app.include_router(jobs_routes.router, prefix="/api")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    response = TestClient(app).get(f"/api/jobs/{job_id}/automation-decision")

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision_id"] == record_id
    assert payload["quality_eligible"] is False
    assert payload["submission_authorized"] is False
    assert payload["authority_reason"] == "QUALITY_DECISION_IS_NOT_SUBMISSION_AUTHORITY"
    serialized = response.text
    assert "example.test" not in serialized

    with factory() as db:
        db.get(Job, job_id).title = "Changed after evaluation"
        db.commit()
    stale = TestClient(app).get(f"/api/jobs/{job_id}/automation-decision")
    assert stale.status_code == 409
    assert stale.json()["detail"] == "JOB_FIT_DECISION_STALE"

    engine.dispose()


def test_application_cannot_bind_a_fit_decision_from_another_job():
    engine, factory = _database()
    with factory() as db:
        first = Job(title="First", source_url="https://example.test/first")
        second = Job(title="Second", source_url="https://example.test/second")
        db.add_all([first, second])
        db.flush()
        decision = unavailable_job_fit_decision(
            _job_data("First"),
            profile_version=1,
            reason_code="FIT_QUALIFICATION_INVALID",
        )
        record = persist_job_fit_decision(db, job_id=first.id, decision=decision)
        db.add(Application(job_id=second.id, job_fit_decision_id=record.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    engine.dispose()
