from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Application, Job, JobStatus, Submission
from worker.submission_attempts import claim_attempt


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)
def test_concurrent_postgres_claims_create_one_attempt():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine)
    db = factory()
    job = Job(
        title="Concurrent claim test",
        source_url="https://example.test/concurrent-claim",
        status=JobStatus.APPROVED,
    )
    db.add(job)
    db.flush()
    app = Application(job_id=job.id, status=JobStatus.APPROVED)
    db.add(app)
    db.commit()
    app_id, job_id = app.id, job.id
    db.close()

    def claim():
        session = factory()
        try:
            attempt = claim_attempt(session, app_id)
            return attempt.id if attempt else None
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        assert sum(result is not None for result in results) == 1
        check = factory()
        assert check.query(Submission).filter_by(application_id=app_id).count() == 1
        check.close()
    finally:
        cleanup = factory()
        cleanup.query(Submission).filter_by(application_id=app_id).delete()
        cleanup.query(Application).filter_by(id=app_id).delete()
        cleanup.query(Job).filter_by(id=job_id).delete()
        cleanup.commit()
        cleanup.close()
