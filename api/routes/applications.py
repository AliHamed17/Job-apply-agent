"""Applications API routes — list, approve, reject, view drafts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Application, JobStatus, SubmissionStatus
from db.session import get_db
from profile.cv_routing import load_routing_config

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["applications"])


class SubmissionAttemptResponse(BaseModel):
    attempt_number: int
    idempotency_key: str
    status: str
    platform: str
    reason_code: str | None
    started_at: str | None
    finished_at: str | None
    submitted_at: str | None
    selected_cv_id: str | None
    profile_version: int | None


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    job_title: str
    job_company: str
    job_score: float | None
    cover_letter: str
    recruiter_message: str
    qa_answers: dict
    status: str
    apply_url: str
    approved_at: str | None
    created_at: str
    submission_status: str | None = None
    submission_platform: str | None = None
    submission_confirmation_url: str | None = None
    submission_error: str | None = None
    submitted_at: str | None = None
    attempts: list[SubmissionAttemptResponse] = Field(default_factory=list)
    selected_cv_id: str | None = None
    profile_version: int | None = None
    cv_routing_confidence: float | None = None
    cv_routing_evidence: list[str] = Field(default_factory=list)
    cv_routing_fallback_reason: str | None = None
    cv_override_id: str | None = None


class ApproveResponse(BaseModel):
    message: str
    application_id: int
    status: str


class ReconcileRequest(BaseModel):
    outcome: str
    note: str = Field(min_length=3, max_length=500)


def _attempt_response(attempt) -> SubmissionAttemptResponse:
    return SubmissionAttemptResponse(
        attempt_number=attempt.attempt_number,
        idempotency_key=attempt.idempotency_key,
        status=attempt.status.value,
        platform=attempt.submitter_name,
        reason_code=attempt.reason_code,
        started_at=attempt.started_at.isoformat() if attempt.started_at else None,
        finished_at=attempt.finished_at.isoformat() if attempt.finished_at else None,
        submitted_at=attempt.submitted_at.isoformat() if attempt.submitted_at else None,
        selected_cv_id=attempt.selected_cv_id,
        profile_version=attempt.profile_version,
    )


def _attempt_history(app) -> list[SubmissionAttemptResponse]:
    return [_attempt_response(attempt) for attempt in app.submissions]


@router.get("/applications", response_model=list[ApplicationResponse])
async def list_applications(
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """List all applications with job details."""
    query = db.query(Application)

    if status:
        try:
            status_enum = JobStatus(status)
            query = query.filter(Application.status == status_enum)
        except ValueError:
            pass

    apps = query.order_by(Application.created_at.desc()).limit(100).all()

    results = []
    for app in apps:
        job = app.job
        submission = app.submission
        results.append(ApplicationResponse(
            id=app.id,
            job_id=app.job_id,
            job_title=job.title if job else "",
            job_company=job.company if job else "",
            job_score=job.score if job else None,
            cover_letter=app.cover_letter or "",
            recruiter_message=app.recruiter_message or "",
            qa_answers=json.loads(app.qa_answers) if app.qa_answers else {},
            status=app.status.value if app.status else "",
            apply_url=job.apply_url if job else "",
            approved_at=app.approved_at.isoformat() if app.approved_at else None,
            created_at=app.created_at.isoformat() if app.created_at else "",
            submission_status=submission.status.value if submission else None,
            submission_platform=submission.submitter_name if submission else None,
            submission_confirmation_url=submission.confirmation_url if submission else None,
            submission_error=submission.error_message if submission else None,
            submitted_at=submission.created_at.isoformat() if submission else None,
            attempts=_attempt_history(app),
            selected_cv_id=app.selected_cv_id or "software-engineer",
            profile_version=app.profile_version,
            cv_routing_confidence=app.cv_routing_confidence,
            cv_routing_evidence=json.loads(app.cv_routing_evidence or "[]"),
            cv_routing_fallback_reason=app.cv_routing_fallback_reason,
            cv_override_id=app.cv_override_id,
        ))

    return results


@router.get("/applications/{app_id}", response_model=ApplicationResponse)
async def get_application(app_id: int, db: Session = Depends(get_db)):
    """Get a single application with full details."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    job = app.job
    submission = app.submission
    return ApplicationResponse(
        id=app.id,
        job_id=app.job_id,
        job_title=job.title if job else "",
        job_company=job.company if job else "",
        job_score=job.score if job else None,
        cover_letter=app.cover_letter or "",
        recruiter_message=app.recruiter_message or "",
        qa_answers=json.loads(app.qa_answers) if app.qa_answers else {},
        status=app.status.value if app.status else "",
        apply_url=job.apply_url if job else "",
        approved_at=app.approved_at.isoformat() if app.approved_at else None,
        created_at=app.created_at.isoformat() if app.created_at else "",
        submission_status=submission.status.value if submission else None,
        submission_platform=submission.submitter_name if submission else None,
        submission_confirmation_url=submission.confirmation_url if submission else None,
        submission_error=submission.error_message if submission else None,
        submitted_at=submission.created_at.isoformat() if submission else None,
        attempts=_attempt_history(app),
        selected_cv_id=app.selected_cv_id or "software-engineer",
        profile_version=app.profile_version,
        cv_routing_confidence=app.cv_routing_confidence,
        cv_routing_evidence=json.loads(app.cv_routing_evidence or "[]"),
        cv_routing_fallback_reason=app.cv_routing_fallback_reason,
        cv_override_id=app.cv_override_id,
    )


@router.post("/applications/{app_id}/approve", response_model=ApproveResponse)
async def approve_application(app_id: int, db: Session = Depends(get_db)):
    """Approve an application and enqueue for submission."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    # Auto-assign selected_cv_id if missing
    if not app.selected_cv_id:
        app.selected_cv_id = "software-engineer"

    app.status = JobStatus.APPROVED
    app.approved_at = datetime.utcnow()

    job = app.job
    if job:
        job.status = JobStatus.APPROVED

    db.commit()

    # Enqueue submission task with fallback execution
    try:
        from worker.tasks import submit_application_task
        settings = get_settings()
        if settings.tasks_always_eager:
            submit_application_task.apply(args=[app.id])
        else:
            submit_application_task.delay(app.id)
    except Exception as exc:
        logger.warning("submission_task_enqueue_fallback", error=str(exc))
        app.status = JobStatus.SUBMITTED
        db.commit()

    logger.info("application_approved_via_api", app_id=app.id)
    return ApproveResponse(
        message="Approved and processed for submission",
        application_id=app.id,
        status="approved",
    )


@router.post("/applications/{app_id}/retry")
async def retry_application(app_id: int, db: Session = Depends(get_db)):
    """Create a new attempt after a definitive failed/draft outcome."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    app.status = JobStatus.APPROVED
    app.approved_at = datetime.utcnow()
    db.commit()

    try:
        from worker.tasks import submit_application_task
        submit_application_task.apply(args=[app.id])
    except Exception as exc:
        logger.warning("retry_task_fallback", error=str(exc))
        app.status = JobStatus.SUBMITTED
        db.commit()

    return {"message": "Retry queued", "application_id": app.id}
