"""The retired v3 task must never manufacture or finalize an attempt.

Attempt finalization now belongs to ``worker.submission_commands`` and is
covered by the submission-command kernel tests. This compatibility entrypoint
accepts an application ID only so stale broker messages fail closed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Application, Base, Job, JobStatus, Submission
from submitters.base import SubmissionResult


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'submit.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _approved_application(factory):
    db = factory()
    job = Job(
        title="AI Engineer",
        company="Greenhouse Co",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        source_url="https://boards.greenhouse.io/acme/jobs/1",
        status=JobStatus.APPROVED,
        score=90.0,
        location="",
        employment_type="",
        seniority="",
        description="",
        requirements="",
    )
    db.add(job)
    db.flush()
    app = Application(
        job_id=job.id,
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers="{}",
        status=JobStatus.APPROVED,
    )
    db.add(app)
    db.commit()
    app_id = app.id
    db.close()
    return app_id


@pytest.mark.parametrize(
    "obsolete_result",
    [
        SubmissionResult(
            success=True,
            platform="greenhouse",
            status="submitted",
            confirmation_id="abc123",
            confirmation_url="https://boards.greenhouse.io/confirm/abc123",
            reason_code="EMPLOYER_VERIFIED",
        ),
        SubmissionResult(
            success=False,
            platform="greenhouse",
            status="failed",
            error="boom",
        ),
        SubmissionResult(
            success=False,
            platform="greenhouse",
            status="unknown",
            error="Submit clicked but no success confirmation appeared",
            reason_code="SUBMIT_UNCONFIRMED",
        ),
    ],
)
def test_legacy_task_does_not_interpret_or_finalize_submitter_results(
    tmp_path,
    obsolete_result,
):
    factory = _factory(tmp_path)
    app_id = _approved_application(factory)
    submit = AsyncMock(return_value=obsolete_result)

    with patch("submitters.greenhouse.GreenhouseSubmitter.submit", new=submit):
        from worker.tasks import submit_application_task

        result = submit_application_task.apply(args=[app_id]).get()

    assert result == {
        "state": "blocked",
        "reason_code": "DATABASE_COMMAND_REQUIRED",
    }
    submit.assert_not_awaited()

    db = factory()
    app = db.get(Application, app_id)
    assert app.status == JobStatus.APPROVED
    assert app.job.status == JobStatus.APPROVED
    assert db.query(Submission).filter(Submission.application_id == app_id).count() == 0
    db.close()
