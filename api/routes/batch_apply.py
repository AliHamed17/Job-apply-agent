"""Batch apply API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.session import get_db
from worker.batch_runner import trigger_batch_auto_apply

router = APIRouter(tags=["control"])


class BatchApplyRequest(BaseModel):
    min_score: float = 80.0
    max_batch_size: int = 10


class BatchApplyResponse(BaseModel):
    triggered_count: int
    skipped_count: int
    job_ids: list[int] = Field(default_factory=list)
    application_ids: list[int] = Field(default_factory=list)
    message: str


@router.post("/control/batch-apply", response_model=BatchApplyResponse)
async def trigger_batch_apply_queue(
    payload: BatchApplyRequest,
    db: Session = Depends(get_db),
):
    """Preview a score-filtered batch; this endpoint never approves it."""
    summary = trigger_batch_auto_apply(
        db,
        min_score=payload.min_score,
        max_batch_size=payload.max_batch_size,
    )

    return BatchApplyResponse(
        triggered_count=summary.triggered_count,
        skipped_count=summary.skipped_count,
        job_ids=summary.job_ids,
        application_ids=summary.application_ids,
        message=(
            "Review the listed applications, then approve the exact IDs with "
            "POST /api/applications/batch-approve."
        ),
    )
