"""Candidate Command Center Aggregator API Router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Application, Job, JobStatus
from db.session import get_db

router = APIRouter(tags=["command_center"])


class CommandCenterSummary(BaseModel):
    candidate_name: str
    auto_apply_active: bool
    score_threshold: float
    governor_cap: int
    total_jobs_scanned: int
    total_applications: int
    submitted_count: int
    needs_review_count: int
    top_matched_jobs: list[dict] = Field(default_factory=list)


@router.get("/command-center/summary", response_model=CommandCenterSummary)
async def get_command_center_summary(db: Session = Depends(get_db)):
    """Consolidate candidate Command Center summary data."""
    settings = get_settings()

    total_jobs = db.query(Job).count()
    total_apps = db.query(Application).count()
    submitted = db.query(Application).filter(Application.status == JobStatus.SUBMITTED).count()
    needs_review = db.query(Application).filter(Application.status == JobStatus.NEEDS_REVIEW).count()

    top_jobs_db = db.query(Job).order_by(Job.score.desc()).limit(5).all()
    top_jobs = [
        {
            "id": j.id,
            "title": j.title,
            "company": j.company,
            "score": j.score,
            "status": str(j.status),
        }
        for j in top_jobs_db
    ]

    return CommandCenterSummary(
        candidate_name="Ali Hamed",
        auto_apply_active=settings.auto_apply,
        score_threshold=settings.auto_apply_threshold,
        governor_cap=45,
        total_jobs_scanned=total_jobs,
        total_applications=total_apps,
        submitted_count=submitted,
        needs_review_count=needs_review,
        top_matched_jobs=top_jobs,
    )

