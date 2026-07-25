"""A blocked required question must reach the operator, not vanish into DRAFT.

tests/test_safe_fill.py only checks that needs_review_error() returns a
string with the right prefix. That passes happily while the end-to-end
contract is broken, which is exactly what happened: submitters reported the
block as status="failed", and worker/tasks.py replaced the whole result with
DraftOnlySubmitter's (error=None) *before* parsing the NEEDS_REVIEW marker.
The application landed in DRAFT with needs_review_reason=None and the
operator never learned which question stopped it.

These tests drive the real submit_application_task against SQLite so the
submitter -> worker -> DB path is covered, not just the string helper.
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
from submitters.safe_fill import needs_review_error


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nr.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _approved(factory, apply_url):
    db = factory()
    job = Job(
        title="AI Engineer", company="Acme",
        apply_url=apply_url, source_url=apply_url,
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


def _settings():
    return Settings(
        _env_file=None, draft_only=False, auto_apply=True,
        cv_routing_path="does-not-exist.yaml",
    )


@pytest.mark.parametrize(
    ("submitter_path", "apply_url"),
    [
        ("submitters.greenhouse.GreenhouseSubmitter.submit",
         "https://boards.greenhouse.io/acme/jobs/1"),
        ("submitters.lever.LeverSubmitter.submit",
         "https://jobs.lever.co/acme/1"),
        ("submitters.workable.WorkableSubmitter.submit",
         "https://apply.workable.com/acme/j/ABC/"),
    ],
)
def test_blocked_question_surfaces_as_needs_review(tmp_path, submitter_path, apply_url):
    factory = _factory(tmp_path)
    app_id = _approved(factory, apply_url)

    blocked = SubmissionResult(
        success=True,
        platform="x",
        status="draft_only",
        error=needs_review_error(["Years of Kubernetes experience?"]),
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=_settings()), \
         patch("profile.loader.get_profile"), \
         patch(submitter_path, new=AsyncMock(return_value=blocked)):
        from worker.tasks import submit_application_task
        submit_application_task.apply(args=[app_id])

    db = factory()
    app = db.query(Application).filter(Application.id == app_id).one()
    row = db.query(Submission).filter(Submission.application_id == app_id).one()

    assert app.needs_review_reason is not None, (
        "the blocking question was lost before reaching the operator"
    )
    assert "Years of Kubernetes experience?" in app.needs_review_reason
    assert app.status == JobStatus.NEEDS_REVIEW
    assert app.job.status == JobStatus.NEEDS_REVIEW
    assert row.reason_code == "REQUIRED_FIELD_UNKNOWN"
    db.close()


def test_needs_review_survives_a_failed_status_submitter(tmp_path):
    """Defence in depth: the reason must survive even if a submitter reports
    the block as status="failed" and triggers the draft fallback."""
    factory = _factory(tmp_path)
    app_id = _approved(factory, "https://boards.greenhouse.io/acme/jobs/2")

    blocked_as_failed = SubmissionResult(
        success=False,
        platform="greenhouse",
        status="failed",
        error=needs_review_error(["Do you hold a security clearance?"]),
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=_settings()), \
         patch("profile.loader.get_profile"), \
         patch(
             "submitters.greenhouse.GreenhouseSubmitter.submit",
             new=AsyncMock(return_value=blocked_as_failed),
         ):
        from worker.tasks import submit_application_task
        submit_application_task.apply(args=[app_id])

    db = factory()
    app = db.query(Application).filter(Application.id == app_id).one()
    assert app.needs_review_reason is not None
    assert "security clearance" in app.needs_review_reason
    assert app.status == JobStatus.NEEDS_REVIEW
    db.close()


def test_no_submitter_pairs_needs_review_with_failed_status():
    """Every blocked return must use draft_only, not failed.

    Source-level on purpose. The end-to-end tests above inject their own
    SubmissionResult, so they exercise the worker path but cannot see what
    the submitters really return — which is precisely how the original
    status="failed" defect got past a green suite. Driving all ten real
    submitters would mean mocking Playwright ten times over; asserting the
    contract at the call site is the cheap check that actually catches a
    regression here.

    status="failed" still *works* thanks to the hoist in worker/tasks.py,
    but it routes through the draft fallback and discards the submitter's
    own platform name, so draft_only remains the correct signal.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("submitters").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "SubmissionResult":
                continue
            kwargs = {k.arg: k.value for k in node.keywords}
            error = kwargs.get("error")
            uses_marker = (
                isinstance(error, ast.Call)
                and getattr(error.func, "id", "") == "needs_review_error"
            )
            status = kwargs.get("status")
            status_val = getattr(status, "value", None)
            if uses_marker and status_val == "failed":
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "these blocked returns use status='failed'; the draft fallback in "
        f"worker/tasks.py replaces them and loses the platform name: {offenders}"
    )


def test_ordinary_draft_is_not_mislabelled_needs_review(tmp_path):
    """A plain draft with no blocking question must stay a plain draft."""
    factory = _factory(tmp_path)
    app_id = _approved(factory, "https://boards.greenhouse.io/acme/jobs/3")

    plain = SubmissionResult(
        success=True, platform="greenhouse", status="draft_only", error=None
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=_settings()), \
         patch("profile.loader.get_profile"), \
         patch(
             "submitters.greenhouse.GreenhouseSubmitter.submit",
             new=AsyncMock(return_value=plain),
         ):
        from worker.tasks import submit_application_task
        submit_application_task.apply(args=[app_id])

    db = factory()
    app = db.query(Application).filter(Application.id == app_id).one()
    row = db.query(Submission).filter(Submission.application_id == app_id).one()
    assert app.needs_review_reason is None
    assert app.status == JobStatus.DRAFT
    assert row.status == SubmissionStatus.DRAFT_ONLY
    db.close()
