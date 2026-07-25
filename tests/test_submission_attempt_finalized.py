"""Regression: the claimed Submission row must be finalized after a real submit.

submit_application_task claims a Submission ORM row up front (status RUNNING)
so a redelivered task can't double-apply, then writes the outcome back to it at
the end. The cascade loop used to rebind `attempt` — the name holding that ORM
row — to the submitter's SubmissionResult dataclass. Three consequences, all
silent:

  * the DB row stayed RUNNING forever, so mark_stale_attempts_unknown reaped it
    15 minutes later and forced the Application AND Job to NEEDS_REVIEW — even
    for submissions that actually succeeded
  * `attempt.status = sub_status` wrote a SubmissionStatus enum onto the
    dataclass's `status` (a str), corrupting the value the code then reads back
  * the except-handler's db.get(Submission, attempt.id) raised AttributeError,
    losing the intended fail-closed path

Only reachable with draft_only=False, which is why the suite never caught it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import (
    Application,
    Base,
    Job,
    JobStatus,
    Submission,
    SubmissionStatus,
)
from submitters.base import SubmissionResult


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'submit.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _approved_application(factory):
    db = factory()
    job = Job(
        title="AI Engineer", company="Greenhouse Co",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        source_url="https://boards.greenhouse.io/acme/jobs/1",
        status=JobStatus.APPROVED, score=90.0,
        location="", employment_type="", seniority="", description="",
        requirements="",
    )
    db.add(job)
    db.flush()
    app = Application(
        job_id=job.id, cover_letter="letter", recruiter_message="msg",
        qa_answers="{}", status=JobStatus.APPROVED,
    )
    db.add(app)
    db.commit()
    app_id = app.id
    db.close()
    return app_id


def _settings(**kw):
    return Settings(
        _env_file=None,
        draft_only=False,          # the only mode that reaches the cascade
        auto_apply=True,
        cv_routing_path="does-not-exist.yaml",
        **kw,
    )


@pytest.mark.parametrize(
    ("submit_result", "expected_status"),
    [
        (
            SubmissionResult(
                success=True, platform="greenhouse", status="submitted",
                confirmation_id="abc123",
                confirmation_url="https://boards.greenhouse.io/confirm/abc123",
            ),
            SubmissionStatus.SUCCESS,
        ),
        (
            SubmissionResult(
                success=False, platform="greenhouse", status="failed",
                error="boom",
            ),
            SubmissionStatus.DRAFT_ONLY,  # cascade falls back to a draft
        ),
    ],
)
def test_claimed_submission_row_is_finalized(tmp_path, submit_result, expected_status):
    factory = _factory(tmp_path)
    app_id = _approved_application(factory)

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=_settings()), \
         patch("profile.loader.get_profile"), \
         patch(
             "submitters.greenhouse.GreenhouseSubmitter.submit",
             new=AsyncMock(return_value=submit_result),
         ):
        from worker.tasks import submit_application_task
        submit_application_task.apply(args=[app_id])

    db = factory()
    row = db.query(Submission).filter(Submission.application_id == app_id).one()
    # The bug left this at RUNNING forever -> reaped into NEEDS_REVIEW later.
    assert row.status == expected_status, (
        f"claimed Submission row not finalized (still {row.status})"
    )
    assert row.finished_at is not None, "finished_at never written to the DB row"
    db.close()


def test_successful_submission_persists_confirmation(tmp_path):
    """A real success must land its confirmation on the DB row, not a dataclass."""
    factory = _factory(tmp_path)
    app_id = _approved_application(factory)

    ok = SubmissionResult(
        success=True, platform="greenhouse", status="submitted",
        confirmation_id="conf-42",
        confirmation_url="https://boards.greenhouse.io/confirm/conf-42",
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=_settings()), \
         patch("profile.loader.get_profile"), \
         patch(
             "submitters.greenhouse.GreenhouseSubmitter.submit",
             new=AsyncMock(return_value=ok),
         ):
        from worker.tasks import submit_application_task
        submit_application_task.apply(args=[app_id])

    db = factory()
    row = db.query(Submission).filter(Submission.application_id == app_id).one()
    assert row.confirmation_id == "conf-42"
    assert row.confirmation_url == "https://boards.greenhouse.io/confirm/conf-42"
    assert row.submitted_at is not None
    assert row.submitter_name == "greenhouse"
    db.close()
