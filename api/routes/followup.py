"""Follow-up planner API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from jobs.models import JobData as JobDataModel
from llm.followup_planner import generate_followup_plan
from profile.cv_content_cache import get_cv_text_by_id
from profile.loader import get_profile

router = APIRouter(tags=["applications"])


class FollowUpResponse(BaseModel):
    application_id: int
    job_title: str
    company: str
    stage1_day3_checkin: str
    stage2_day7_value_add: str
    stage3_day14_inquiry: str


@router.get("/applications/{id}/followup-plan", response_model=FollowUpResponse)
async def get_application_followup_plan(
    id: int,
    db: Session = Depends(get_db),
):
    """Generate 3-stage strategic follow-up plan for an application."""
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

    plan = await generate_followup_plan(job_ref, profile, cv_text=cv_text)

    return FollowUpResponse(
        application_id=app.id,
        job_title=db_job.title or "",
        company=db_job.company or "",
        stage1_day3_checkin=plan.stage1_day3_checkin,
        stage2_day7_value_add=plan.stage2_day7_value_add,
        stage3_day14_inquiry=plan.stage3_day14_inquiry,
    )
