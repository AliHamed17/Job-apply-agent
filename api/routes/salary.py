"""Salary negotiator brief API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from jobs.models import JobData as JobDataModel
from llm.salary_negotiator import generate_salary_brief
from profile.cv_content_cache import get_cv_text_by_id
from profile.loader import get_profile

router = APIRouter(tags=["salary"])


class SalaryBriefResponse(BaseModel):
    application_id: int
    job_title: str
    company: str
    currency: str
    estimated_percentiles: dict[str, int]
    negotiation_talking_points: list[str] = Field(default_factory=list)
    counter_offer_script: str


@router.get("/applications/{id}/salary-brief", response_model=SalaryBriefResponse)
async def get_application_salary_brief(
    id: int,
    db: Session = Depends(get_db),
):
    """Generate market salary estimation and compensation negotiator brief."""
    app = db.query(Application).filter(Application.id == id).first()
    if not app or not app.job:
        raise HTTPException(status_code=404, detail=f"Application {id} not found")

    profile = get_profile()
    db_job = app.job
    cv_text = get_cv_text_by_id(app.selected_cv_id) if app.selected_cv_id else None

    job_ref = JobDataModel(
        title=db_job.title or "",
        company=db_job.company or "",
        location=db_job.location or "",
        description=db_job.description or "",
        requirements=db_job.requirements or "",
    )

    brief = await generate_salary_brief(job_ref, profile, cv_text=cv_text)

    return SalaryBriefResponse(
        application_id=app.id,
        job_title=db_job.title or "",
        company=db_job.company or "",
        currency=brief.currency,
        estimated_percentiles=brief.estimated_percentiles,
        negotiation_talking_points=brief.negotiation_talking_points,
        counter_offer_script=brief.counter_offer_script,
    )
