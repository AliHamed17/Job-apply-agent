"""AI Form dry-run simulator API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from jobs.models import JobData as JobDataModel
from profile.loader import get_profile
from submitters.dry_run import run_form_preflight

router = APIRouter(tags=["applications"])


class DryRunResponse(BaseModel):
    application_id: int
    platform_detected: str
    is_ready_for_submission: bool
    mapped_fields_count: int
    unmapped_questions: list[str] = Field(default_factory=list)
    qa_generated_answers: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


@router.post("/applications/{id}/dry-run", response_model=DryRunResponse)
async def dry_run_application_form(
    id: int,
    db: Session = Depends(get_db),
):
    """Simulate application form filling without clicking final submit."""
    app = db.query(Application).filter(Application.id == id).first()
    if not app or not app.job:
        raise HTTPException(status_code=404, detail=f"Application {id} not found")

    profile = get_profile()
    db_job = app.job

    job_ref = JobDataModel(
        title=db_job.title or "",
        company=db_job.company or "",
        location=db_job.location or "",
        description=db_job.description or "",
        requirements=db_job.requirements or "",
        apply_url=db_job.apply_url or "https://example.com/apply",
    )

    report = run_form_preflight(
        app_id=app.id,
        job=job_ref,
        profile=profile,
        qa_answers=app.qa_answers or {},
    )


    return DryRunResponse(
        application_id=report.application_id,
        platform_detected=report.platform_detected,
        is_ready_for_submission=report.is_ready_for_submission,
        mapped_fields_count=report.mapped_fields_count,
        unmapped_questions=report.unmapped_questions,
        qa_generated_answers=report.qa_generated_answers,
        warnings=report.warnings,
    )
