"""Culture fit API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from jobs.models import JobData as JobDataModel
from llm.culture_fit import evaluate_culture_fit
from profile.cv_content_cache import get_cv_text_by_id
from profile.loader import get_profile

router = APIRouter(tags=["applications"])


class CultureFitResponse(BaseModel):
    application_id: int
    job_title: str
    company: str
    culture_fit_score: int
    cultural_highlights: list[str] = Field(default_factory=list)
    behavioral_talking_points: list[str] = Field(default_factory=list)
    caution_flags: list[str] = Field(default_factory=list)


@router.get("/applications/{id}/culture-fit", response_model=CultureFitResponse)
async def get_application_culture_fit(
    id: int,
    db: Session = Depends(get_db),
):
    """Evaluate company culture and technical fit for an application."""
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

    eval_result = await evaluate_culture_fit(job_ref, profile, cv_text=cv_text)

    return CultureFitResponse(
        application_id=app.id,
        job_title=db_job.title or "",
        company=db_job.company or "",
        culture_fit_score=eval_result.culture_fit_score,
        cultural_highlights=eval_result.cultural_highlights,
        behavioral_talking_points=eval_result.behavioral_talking_points,
        caution_flags=eval_result.caution_flags,
    )
