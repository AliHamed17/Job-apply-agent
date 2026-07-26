"""Content that would embarrass the candidate must not reach an employer.

Two separate leaks, both observed on a real generated application:

  * an unset salary range (min=max=0, the model default) rendered into the
    Q&A prompt as the literal "Salary expectation: 0–0 ILS", and the model
    answered accordingly
  * has_placeholders was computed and logged but gated nothing, so a letter
    still containing "[PLACEHOLDER: notice period]" could auto-approve and
    submit verbatim
"""

from __future__ import annotations

from profile.models import UserProfile
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus
from llm.generation import GeneratedApplication
from llm.prompts import QA_ANSWERS_PROMPT, build_salary_guidance

# ── salary guidance ───────────────────────────────────────────────────


def test_unset_salary_never_renders_a_zero_range():
    guidance = build_salary_guidance(0, 0, "ILS")
    assert "0" not in guidance
    assert "not specified" in guidance.lower() or "open" in guidance.lower()


def test_real_salary_range_is_stated():
    assert build_salary_guidance(30000, 45000, "ILS") == ("Salary expectation: 30000–45000 ILS.")


@pytest.mark.parametrize(
    ("lo", "hi", "expect"),
    [(30000, 0, "from 30000 ILS"), (0, 45000, "up to 45000 ILS")],
)
def test_half_open_salary_range(lo, hi, expect):
    assert expect in build_salary_guidance(lo, hi, "ILS")


def test_rendered_qa_prompt_has_no_zero_salary():
    """The end-to-end string the model actually sees."""
    rendered = QA_ANSWERS_PROMPT.format(
        job_title="AI Engineer",
        company="Acme",
        name="Example Candidate",
        user_location="Haifa",
        work_authorization="Israeli citizen",
        resume_text="...",
        salary_guidance=build_salary_guidance(0, 0, "ILS"),
    )
    assert "0–0" not in rendered
    assert "0-0" not in rendered


# ── placeholders block auto-approval ──────────────────────────────────


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'q.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _routing_config(tmp_path):
    """A config that confidently routes the test job, so CV routing is not
    what blocks auto-approval — the placeholder guard is what we're testing."""
    path = tmp_path / "cv_routing.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cvs": [
                    {
                        "id": "ai",
                        "file": "ai.pdf",
                        "title_terms": ["ai", "engineer"],
                        "skills": ["python"],
                    }
                ],
                "fallback_cv_id": "ai",
                "minimum_confidence": 0.1,
            }
        ),
        encoding="utf-8",
    )
    return path


def _scored_job(factory, score=95.0):
    db = factory()
    job = Job(
        title="AI Engineer",
        company="Acme",
        source_url="https://x",
        status=JobStatus.SCORED,
        score=score,
        location="",
        employment_type="",
        seniority="",
        description="",
        requirements="",
        apply_url="",
    )
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()
    return job_id


def _run(factory, tmp_path, job_id, generated):
    settings = Settings(
        _env_file=None,
        draft_only=False,
        auto_apply=True,
        auto_apply_threshold=50.0,
        min_apply_score=40.0,
        llm_cv_alignment=False,  # keep the LLM out of routing here
        cv_routing_path=str(_routing_config(tmp_path)),
        cv_directory=str(tmp_path),
    )
    # Auto-approval chains straight into submit_application_task, which would
    # move the row on past APPROVED before we could observe it. Stub the
    # chained task so these assertions describe generation alone.
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch("worker.tasks.submit_application_task"),
        patch(
            "llm.generation.generate_full_application",
            new=AsyncMock(return_value=generated),
        ),
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])


def test_placeholder_application_is_not_auto_approved(tmp_path):
    factory = _factory(tmp_path)
    job_id = _scored_job(factory)

    _run(
        factory,
        tmp_path,
        job_id,
        GeneratedApplication(
            cover_letter="Dear team, I can start in [PLACEHOLDER: notice period].",
            recruiter_message="hi",
            qa_answers={"notice_period": "[PLACEHOLDER: notice period]"},
            has_placeholders=True,
            placeholder_fields=["notice period"],
        ),
    )

    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).one()
    assert app.status == JobStatus.DRAFT, "placeholders must block auto-approval"
    assert app.approved_at is None
    assert app.needs_review_reason == "UNFILLED_PLACEHOLDER"
    db.close()


def test_clean_text_without_a_verified_cv_artifact_still_requires_review(tmp_path):
    """Clean prose alone cannot make an attachment-unbound draft eligible."""
    factory = _factory(tmp_path)
    job_id = _scored_job(factory)

    _run(
        factory,
        tmp_path,
        job_id,
        GeneratedApplication(
            cover_letter="Dear team, I can start within 30 days.",
            recruiter_message="hi",
            qa_answers={"notice_period": "30 days"},
            has_placeholders=False,
            placeholder_fields=[],
        ),
    )

    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).one()
    assert app.status == JobStatus.DRAFT
    assert app.approved_at is None
    assert app.approval_source is None
    assert app.needs_review_reason == "MATERIAL_CV_ARTIFACT_REQUIRED"
    assert app.material_eligible is False
    db.close()
