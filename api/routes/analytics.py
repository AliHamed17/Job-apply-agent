"""Match analytics API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.session import get_db
from match.analytics import compute_match_analytics

router = APIRouter(tags=["analytics"])


class AnalyticsResponse(BaseModel):
    total_jobs: int
    average_score: float
    cv_distribution: dict[str, int] = Field(default_factory=dict)
    location_distribution: dict[str, int] = Field(default_factory=dict)
    top_matched_skills: list[str] = Field(default_factory=list)


@router.get("/analytics/summary", response_model=AnalyticsResponse)
async def get_match_analytics_summary(db: Session = Depends(get_db)):
    """Return aggregated career match analytics and skill radar data."""
    summary = compute_match_analytics(db)
    return AnalyticsResponse(
        total_jobs=summary.total_jobs,
        average_score=summary.average_score,
        cv_distribution=summary.cv_distribution,
        location_distribution=summary.location_distribution,
        top_matched_skills=summary.top_matched_skills,
    )
