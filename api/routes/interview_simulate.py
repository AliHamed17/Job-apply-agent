"""Mock interview simulator API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from jobs.models import JobData as JobDataModel
from llm.interview_simulator import evaluate_interview_answer
from profile.cv_content_cache import get_cv_text_by_id
from profile.loader import get_profile

router = APIRouter(tags=["applications"])


class SimulateRequest(BaseModel):
    question: str
    candidate_answer: str


class SimulateResponse(BaseModel):
    application_id: int
    question: str
    candidate_answer: str
    score: int
    strengths: list[str] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    improved_answer: str


@router.post("/applications/{id}/interview-simulate", response_model=SimulateResponse)
async def simulate_interview_response(
    id: int,
    payload: SimulateRequest,
    db: Session = Depends(get_db),
):
    """Evaluate candidate's response to an interview question with instant feedback & coaching."""
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

    eval_result = await evaluate_interview_answer(
        question=payload.question,
        candidate_answer=payload.candidate_answer,
        job=job_ref,
        profile=profile,
        cv_text=cv_text,
    )

    return SimulateResponse(
        application_id=app.id,
        question=payload.question,
        candidate_answer=payload.candidate_answer,
        score=eval_result.score,
        strengths=eval_result.strengths,
        missing_points=eval_result.missing_points,
        improved_answer=eval_result.improved_answer,
    )
