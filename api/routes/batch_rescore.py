"""Batch job re-scoring API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.session import get_db
from match.batch_rescore import batch_rescore_all_jobs

router = APIRouter(tags=["jobs"])


class RescoreResponse(BaseModel):
    total_evaluated: int
    updated_count: int


@router.post("/jobs/rescore", response_model=RescoreResponse)
async def trigger_batch_rescore(db: Session = Depends(get_db)):
    """Re-calculate match scores for all pending and draft jobs against updated profile preferences."""
    result = batch_rescore_all_jobs(db)
    return RescoreResponse(
        total_evaluated=result["total_evaluated"],
        updated_count=result["updated_count"],
    )
