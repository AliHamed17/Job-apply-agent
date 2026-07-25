"""Notification digest API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.session import get_db
from jobs.tracker import generate_executive_digest

router = APIRouter(tags=["notifications"])


class DigestResponse(BaseModel):
    total_jobs_scanned: int
    total_applications: int
    auto_applied_count: int
    needs_review_count: int
    submitted_count: int
    interview_invited_count: int
    conversion_rate_pct: float
    summary_text: str


@router.get("/notifications/digest", response_model=DigestResponse)
async def get_notification_digest(db: Session = Depends(get_db)):
    """Return executive summary digest for email / WhatsApp notification dispatch."""
    digest = generate_executive_digest(db)
    return DigestResponse(
        total_jobs_scanned=digest.total_jobs_scanned,
        total_applications=digest.total_applications,
        auto_applied_count=digest.auto_applied_count,
        needs_review_count=digest.needs_review_count,
        submitted_count=digest.submitted_count,
        interview_invited_count=digest.interview_invited_count,
        conversion_rate_pct=digest.conversion_rate_pct,
        summary_text=digest.summary_text,
    )
