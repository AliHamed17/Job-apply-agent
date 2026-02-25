"""Feedback loop API routes (Phase 10).

Allows the user to submit corrections to LLM-generated cover letters.
Corrected pairs are stored in ``cover_letter_feedback`` and retrieved by
the generation layer to inject as few-shot examples.

Endpoints:
    POST /api/applications/{id}/feedback   Submit cover letter correction
    GET  /api/feedback                     List all feedback (newest first)
    GET  /api/feedback/examples            Retrieve few-shot examples for LLM
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import Application, CoverLetterFeedback
from db.session import get_db

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["feedback"])


# ── Pydantic schemas ──────────────────────────────────────────────────────


class FeedbackSubmit(BaseModel):
    """Request body for submitting a cover letter correction."""

    corrected_text: str = Field(..., min_length=50, description="The corrected cover letter")
    feedback_note: str | None = Field(
        None,
        description="Optional explanation of what was wrong or improved",
    )


class FeedbackResponse(BaseModel):
    """Single feedback record."""

    id: int
    application_id: int
    original_text: str
    corrected_text: str
    feedback_note: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class FewShotExample(BaseModel):
    """A single few-shot example for the LLM prompt."""

    bad: str = Field(..., description="Original (sub-optimal) cover letter")
    good: str = Field(..., description="Corrected (preferred) cover letter")
    note: str | None = Field(None, description="Context note")


# ── Routes ────────────────────────────────────────────────────────────────


@router.post(
    "/applications/{application_id}/feedback",
    response_model=FeedbackResponse,
    summary="Submit cover letter correction",
)
def submit_feedback(
    application_id: int,
    body: FeedbackSubmit,
    db: Session = Depends(get_db),
) -> FeedbackResponse:
    """Record a user correction to an LLM-generated cover letter.

    The original draft is automatically read from the application record.
    The corrected version is stored alongside the original for future
    few-shot learning.
    """
    app = db.query(Application).filter(Application.id == application_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    if not app.cover_letter:
        raise HTTPException(
            status_code=400,
            detail="Application has no generated cover letter to provide feedback on",
        )

    fb = CoverLetterFeedback(
        application_id=application_id,
        original_text=app.cover_letter,
        corrected_text=body.corrected_text,
        feedback_note=body.feedback_note,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)

    logger.info(
        "feedback_submitted",
        application_id=application_id,
        feedback_id=fb.id,
    )
    return FeedbackResponse.model_validate(fb)


@router.get(
    "/feedback",
    response_model=list[FeedbackResponse],
    summary="List all cover letter feedback",
)
def list_feedback(
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[FeedbackResponse]:
    """Return feedback records (newest first)."""
    rows = (
        db.query(CoverLetterFeedback)
        .order_by(CoverLetterFeedback.created_at.desc())
        .limit(limit)
        .all()
    )
    return [FeedbackResponse.model_validate(r) for r in rows]


@router.get(
    "/feedback/examples",
    response_model=list[FewShotExample],
    summary="Retrieve few-shot examples for LLM prompting",
)
def get_few_shot_examples(
    limit: int = 5,
    db: Session = Depends(get_db),
) -> list[FewShotExample]:
    """Return the most recent feedback pairs formatted as few-shot examples.

    The generation layer calls this to prepend correction examples to the
    cover letter prompt, steering future outputs toward the user's style.
    """
    rows = (
        db.query(CoverLetterFeedback)
        .order_by(CoverLetterFeedback.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        FewShotExample(
            bad=r.original_text,
            good=r.corrected_text,
            note=r.feedback_note,
        )
        for r in rows
    ]
