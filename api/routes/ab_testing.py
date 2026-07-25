"""A/B testing analytics API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.session import get_db
from match.ab_testing import compute_ab_test_analytics

router = APIRouter(tags=["analytics"])


class ABVariantResponse(BaseModel):
    cv_id: str
    total_applications: int
    interviews_count: int
    conversion_rate_pct: float


class ABTestingResponse(BaseModel):
    total_analyzed: int
    winning_cv_id: str | None
    variants: list[ABVariantResponse] = Field(default_factory=list)


@router.get("/analytics/ab-testing", response_model=ABTestingResponse)
async def get_ab_testing_analytics(db: Session = Depends(get_db)):
    """Return performance conversion breakdown across CV variants."""
    report = compute_ab_test_analytics(db)
    return ABTestingResponse(
        total_analyzed=report.total_analyzed,
        winning_cv_id=report.winning_cv_id,
        variants=[
            ABVariantResponse(
                cv_id=v.cv_id,
                total_applications=v.total_applications,
                interviews_count=v.interviews_count,
                conversion_rate_pct=v.conversion_rate_pct,
            )
            for v in report.variants
        ],
    )
