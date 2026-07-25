"""Mobile quick-action and dashboard widgets API route."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.application_state import prepared_application_count
from core.submission_truth import latest_employer_verified_count
from db.models import Application, JobStatus
from db.session import get_db

router = APIRouter(tags=["widgets"])


class WidgetSummaryResponse(BaseModel):
    total_applications: int
    approved_count: int
    needs_review_count: int
    submitted_count: int
    latest_actions: list[dict] = Field(default_factory=list)


@router.get("/widgets/summary", response_model=WidgetSummaryResponse)
async def get_widget_summary(db: Session = Depends(get_db)):
    """Return compact mobile widget summary payload."""
    total = db.query(Application).count()
    approved = prepared_application_count(db)
    review = db.query(Application).filter(Application.status == JobStatus.NEEDS_REVIEW).count()
    submitted = latest_employer_verified_count(db)

    recent_apps = db.query(Application).order_by(Application.created_at.desc()).limit(5).all()

    actions = [
        {
            "id": a.id,
            "job_title": a.job.title if a.job else "Unknown",
            "company": a.job.company if a.job else "Unknown",
            "status": a.status.value if hasattr(a.status, "value") else str(a.status),
            "selected_cv_id": a.selected_cv_id,
        }
        for a in recent_apps
    ]

    return WidgetSummaryResponse(
        total_applications=total,
        approved_count=approved,
        needs_review_count=review,
        submitted_count=submitted,
        latest_actions=actions,
    )
