"""Candidate Command Center Aggregator API Router."""

from __future__ import annotations

from profile.loader import get_profile

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.submission_display import job_submission_display
from core.config import get_settings
from core.submission_truth import latest_employer_verified_count
from db.models import Application, Job, JobStatus
from db.session import get_db

router = APIRouter(tags=["command_center"])


class CommandCenterSummary(BaseModel):
    candidate_name: str
    discovery_active: bool
    auto_prepare_active: bool
    qualified_autopilot_active: bool
    # Deprecated compatibility alias for auto_prepare_active.
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
    submitted = latest_employer_verified_count(db)
    needs_review = (
        db.query(Application).filter(Application.status == JobStatus.NEEDS_REVIEW).count()
    )

    top_jobs_db = db.query(Job).order_by(Job.score.desc()).limit(5).all()
    top_jobs = []
    for job in top_jobs_db:
        display = job_submission_display(job)
        top_jobs.append(
            {
                "id": job.id,
                "title": job.title,
                "company": job.company,
                "score": job.score,
                "status": display.display_status,
                "source_status": display.source_status,
                "employer_verified": display.employer_verified,
            }
        )

    return CommandCenterSummary(
        candidate_name=get_profile().personal.name or "Candidate",
        discovery_active=settings.discovery_enabled,
        auto_prepare_active=settings.auto_apply,
        qualified_autopilot_active=False,
        auto_apply_active=settings.auto_apply,
        score_threshold=settings.auto_apply_threshold,
        governor_cap=settings.linkedin_daily_cap,
        total_jobs_scanned=total_jobs,
        total_applications=total_apps,
        submitted_count=submitted,
        needs_review_count=needs_review,
        top_matched_jobs=top_jobs,
    )
