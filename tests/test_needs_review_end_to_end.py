"""Safety coverage for blocked form answers and the retired v3 task.

Submission results are now interpreted only by the database-authoritative
command worker. The application-ID compatibility task must not invoke a
submitter or translate a stale broker message into database state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Application, Base, Job, JobStatus, Submission
from submitters.safe_fill import needs_review_error


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nr.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _approved(factory, apply_url):
    db = factory()
    job = Job(
        title="AI Engineer",
        company="Acme",
        apply_url=apply_url,
        source_url=apply_url,
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
    ("submitter_path", "apply_url"),
    [
        (
            "submitters.greenhouse.GreenhouseSubmitter.submit",
            "https://boards.greenhouse.io/acme/jobs/1",
        ),
        ("submitters.lever.LeverSubmitter.submit", "https://jobs.lever.co/acme/1"),
        (
            "submitters.workable.WorkableSubmitter.submit",
            "https://apply.workable.com/acme/j/ABC/",
        ),
    ],
)
def test_legacy_task_cannot_consume_a_blocked_submitter_result(
    tmp_path,
    submitter_path,
    apply_url,
):
    factory = _factory(tmp_path)
    app_id = _approved(factory, apply_url)
    submit = AsyncMock()

    with patch(submitter_path, new=submit):
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
    assert app.needs_review_reason is None
    assert db.query(Submission).filter(Submission.application_id == app_id).count() == 0
    db.close()


def test_needs_review_marker_preserves_blocking_questions():
    marker = needs_review_error(
        [
            "Years of Kubernetes experience?",
            "Do you hold a security clearance?",
        ]
    )

    assert marker.startswith("NEEDS_REVIEW:")
    assert "Years of Kubernetes experience?" in marker
    assert "security clearance" in marker


def test_no_submitter_pairs_needs_review_with_failed_status():
    """Every blocked return must use draft_only, not failed."""
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
        "these blocked returns use status='failed'; the retired v3 task cannot "
        f"interpret them, and the command worker expects draft_only: {offenders}"
    )
