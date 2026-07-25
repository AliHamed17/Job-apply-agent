"""Application re-alignment API route — re-runs CV selection and LLM generation."""

from __future__ import annotations

import json
from pathlib import Path
import structlog

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Application, Job, JobStatus

from db.session import get_db
from jobs.models import JobData as JobDataModel
from profile.cv_content_cache import get_cv_text_by_id
from profile.cv_routing import (
    RoutingDecision,
    RoutingJob,
    load_routing_config,
    parse_required_skills,
    route_cv,
    validate_cv_alignment,
)
from profile.loader import get_profile
from llm.generation import generate_full_application

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["applications"])


class RealignRequest(BaseModel):
    forced_cv_id: str | None = Field(default=None, description="Optional CV ID to force align")
    user_guidance: str | None = Field(default=None, description="Optional steering instructions for LLM generation")


class RealignResponse(BaseModel):
    id: int
    job_id: int
    job_title: str
    selected_cv_id: str | None
    cover_letter: str
    recruiter_message: str
    qa_answers: dict
    cv_routing_confidence: float | None
    cv_routing_evidence: list[str]
    status: str


@router.post("/applications/{id}/realign", response_model=RealignResponse)
async def realign_application(
    id: int,
    payload: RealignRequest = RealignRequest(),
    db: Session = Depends(get_db),
):
    """Re-run CV routing alignment and regenerate tailored application materials."""
    app = db.query(Application).filter(Application.id == id).first()
    if not app or not app.job:
        raise HTTPException(status_code=404, detail=f"Application {id} not found")

    settings = get_settings()
    profile = get_profile()
    db_job = app.job

    routing_path = Path(settings.cv_routing_path)
    selected_cv_id = payload.forced_cv_id
    confidence = 1.0 if payload.forced_cv_id else 0.0
    evidence = ["user_forced_alignment"] if payload.forced_cv_id else []
    fallback_reason = None

    if not selected_cv_id and routing_path.exists():
        routing_config = load_routing_config(routing_path)
        rjob = RoutingJob(
            title=db_job.title or "",
            description=" ".join(filter(None, [db_job.description, db_job.requirements])),
            seniority=db_job.seniority or "",
            required_skills=parse_required_skills(db_job.keywords),
        )
        decision = route_cv(rjob, routing_config)
        if settings.llm_cv_alignment and decision.selected_cv_id:
            decision = await validate_cv_alignment(rjob, decision, routing_config)

        selected_cv_id = decision.selected_cv_id
        confidence = decision.confidence
        evidence = decision.matched_evidence
        fallback_reason = decision.fallback_reason

    cv_text = get_cv_text_by_id(selected_cv_id) if selected_cv_id else None

    job_ref = JobDataModel(
        title=db_job.title or "",
        company=db_job.company or "",
        location=db_job.location or "",
        employment_type=db_job.employment_type or "",
        seniority=db_job.seniority or "",
        description=db_job.description or "",
        requirements=db_job.requirements or "",
        apply_url=db_job.apply_url or "",
        source_url=db_job.source_url or "",
    )

    generated = await generate_full_application(job_ref, profile, cv_text=cv_text)

    app.selected_cv_id = selected_cv_id
    app.cover_letter = generated.cover_letter
    app.recruiter_message = generated.recruiter_message
    app.qa_answers = json.dumps(generated.qa_answers)
    app.cv_routing_confidence = confidence
    app.cv_routing_evidence = json.dumps(evidence)
    app.cv_routing_fallback_reason = fallback_reason
    db.commit()
    db.refresh(app)

    logger.info(
        "application_realigned",
        application_id=app.id,
        selected_cv_id=selected_cv_id,
        forced=bool(payload.forced_cv_id),
    )

    return RealignResponse(
        id=app.id,
        job_id=app.job_id,
        job_title=db_job.title or "",
        selected_cv_id=app.selected_cv_id,
        cover_letter=app.cover_letter or "",
        recruiter_message=app.recruiter_message or "",
        qa_answers=json.loads(app.qa_answers) if app.qa_answers else {},
        cv_routing_confidence=app.cv_routing_confidence,
        cv_routing_evidence=json.loads(app.cv_routing_evidence or "[]"),
        status=app.status.value if hasattr(app.status, "value") else str(app.status),
    )
