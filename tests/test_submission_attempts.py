from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import (
    Application,
    Base,
    Job,
    JobStatus,
    SubmissionStatus,
)
from worker.submission_attempts import (
    claim_attempt,
    mark_stale_attempts_unknown,
)


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'attempts.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _approved_application(factory) -> int:
    db = factory()
    job = Job(title="Engineer", source_url="https://example.test/1", status=JobStatus.APPROVED)
    db.add(job)
    db.flush()
    app = Application(job_id=job.id, status=JobStatus.APPROVED)
    db.add(app)
    db.commit()
    app_id = app.id
    db.close()
    return app_id


def test_task_redelivery_cannot_claim_twice(tmp_path):
    factory = _factory(tmp_path)
    app_id = _approved_application(factory)
    first_db, second_db = factory(), factory()
    try:
        first = claim_attempt(first_db, app_id)
        second = claim_attempt(second_db, app_id)
        assert first is not None
        assert first.status == SubmissionStatus.RUNNING
        assert second is None
    finally:
        first_db.close()
        second_db.close()


def test_stale_running_attempt_becomes_unknown_and_needs_review(tmp_path):
    factory = _factory(tmp_path)
    app_id = _approved_application(factory)
    db = factory()
    attempt = claim_attempt(db, app_id)
    attempt.started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db.commit()

    assert mark_stale_attempts_unknown(db, stale_minutes=15) == 1
    db.refresh(attempt)
    assert attempt.status == SubmissionStatus.UNKNOWN
    assert attempt.application.status == JobStatus.NEEDS_REVIEW
    assert claim_attempt(db, app_id) is None
    db.close()


def test_failed_attempt_can_be_followed_by_new_numbered_attempt(tmp_path):
    factory = _factory(tmp_path)
    app_id = _approved_application(factory)
    db = factory()
    first = claim_attempt(db, app_id)
    first.status = SubmissionStatus.FAILED
    db.commit()
    second = claim_attempt(db, app_id)
    assert second is not None
    assert second.attempt_number == 2
    assert second.idempotency_key != first.idempotency_key
    db.close()
