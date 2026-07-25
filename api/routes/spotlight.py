"""Portfolio spotlight API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from jobs.models import JobData as JobDataModel
from profile.loader import get_profile
from profile.spotlight_matcher import match_portfolio_spotlight

router = APIRouter(tags=["profile"])


class SpotlightResponse(BaseModel):
    application_id: int
    spotlight_title: str
    relevant_keywords: list[str] = Field(default_factory=list)
    showcase_text: str


@router.get("/applications/{id}/portfolio-spotlight", response_model=SpotlightResponse)
async def get_application_portfolio_spotlight(
    id: int,
    db: Session = Depends(get_db),
):
    """Retrieve matched portfolio showcase text tailored for the job."""
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
    )

    match = match_portfolio_spotlight(job_ref, profile)

    return SpotlightResponse(
        application_id=app.id,
        spotlight_title=match.spotlight_title,
        relevant_keywords=match.relevant_keywords,
        showcase_text=match.showcase_text,
    )
