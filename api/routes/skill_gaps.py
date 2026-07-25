"""Skill gap analysis API route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from profile.cv_content_cache import get_cv_text_by_id
from profile.loader import get_profile
from profile.skill_gaps import analyze_skill_gaps

router = APIRouter(tags=["applications"])


class SkillGapResponse(BaseModel):
    application_id: int
    selected_cv_id: str | None
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


@router.get("/applications/{id}/skill-gaps", response_model=SkillGapResponse)
async def get_application_skill_gaps(
    id: int,
    db: Session = Depends(get_db),
):
    """Analyze skill coverage between job description and candidate aligned CV."""
    app = db.query(Application).filter(Application.id == id).first()
    if not app or not app.job:
        raise HTTPException(status_code=404, detail=f"Application {id} not found")

    profile = get_profile()
    cv_text = get_cv_text_by_id(app.selected_cv_id) if app.selected_cv_id else profile.resume.text
    db_job = app.job

    analysis = analyze_skill_gaps(
        job_description=db_job.description or "",
        job_requirements=db_job.requirements or "",
        cv_text=cv_text,
    )

    return SkillGapResponse(
        application_id=app.id,
        selected_cv_id=app.selected_cv_id,
        matched_skills=analysis.matched_skills,
        missing_skills=analysis.missing_skills,
        recommendations=analysis.recommendations,
    )
