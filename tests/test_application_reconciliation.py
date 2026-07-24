from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.applications import ReconcileRequest, reconcile_application, retry_application
from db.models import (
    Application,
    Base,
    Job,
    JobStatus,
    Submission,
    SubmissionStatus,
)


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
    assert result["outcome"] == "confirmed_not_submitted"
    assert app.status == JobStatus.DRAFT
    assert app.submission.status == SubmissionStatus.FAILED
    assert app.submission.reason_code == "RECONCILED_NOT_SUBMITTED"
    db.close()


@pytest.mark.asyncio
async def test_reconcile_confirmed_submitted_closes_application(tmp_path):
    db, app_id = _unknown_attempt(tmp_path)
    await reconcile_application(
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
    assert app.submission.status == SubmissionStatus.SUCCESS
    db.close()
