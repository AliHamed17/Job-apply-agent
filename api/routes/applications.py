"""Applications API routes — list, approve, reject, view drafts."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache

import redis
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Application, JobStatus, Submission, SubmissionStatus
from db.session import get_db

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["applications"])


@lru_cache
def _get_redis_client():
    try:
        settings = get_settings()
        client = redis.from_url(settings.redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
        client.ping()
        return client
    except Exception:
        return None


class ApplicationResponse(BaseModel):
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

    model_config = ConfigDict(from_attributes=True)


class ApproveResponse(BaseModel):
    message: str
    application_id: int
    status: str


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
    )


@router.post("/applications/{app_id}/approve", response_model=ApproveResponse)
async def approve_application(app_id: int, db: Session = Depends(get_db)):
    """Approve an application and enqueue for submission."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    if app.status == JobStatus.APPROVED:
        return ApproveResponse(
            message="Already approved",
            application_id=app.id,
            status="approved",
        )

    app.status = JobStatus.APPROVED
    app.approved_at = datetime.utcnow()

    job = app.job
    if job:
        job.status = JobStatus.APPROVED

    db.commit()

    # Enqueue submission task
    from worker.tasks import submit_application_task
    submit_application_task.delay(app.id)

    logger.info("application_approved_via_api", app_id=app.id)
    return ApproveResponse(
        message="Approved and queued for submission",
        application_id=app.id,
        status="approved",
    )


@router.post("/applications/{app_id}/reject")
async def reject_application(
    app_id: int,
    reason: str = "Rejected by user",
    db: Session = Depends(get_db),
):
    """Reject / skip an application."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    app.status = JobStatus.SKIPPED
    app.rejected_at = datetime.utcnow()
    app.rejection_reason = reason

    job = app.job
    if job:
        job.status = JobStatus.SKIPPED

    db.commit()
    logger.info("application_rejected_via_api", app_id=app.id, reason=reason)
    return {"message": "Application rejected", "application_id": app.id}


@router.get("/submissions")
async def list_submissions(db: Session = Depends(get_db)):
    """List submission queue entries with job/application context."""
    rows = db.query(Submission).order_by(Submission.created_at.desc()).limit(200).all()
    payload = []
    for row in rows:
        app = row.application
        job = app.job if app else None
        payload.append(
            {
                "submission_id": row.id,
                "application_id": row.application_id,
                "job_id": app.job_id if app else None,
                "job_title": job.title if job else "",
                "company": job.company if job else "",
                "status": row.status.value if row.status else "",
                "submitter_name": row.submitter_name,
                "error_message": row.error_message,
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "confirmation_url": row.confirmation_url,
            }
        )
    return payload


@router.post("/applications/{app_id}/retry-submit")
async def retry_submission(
    app_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Retry submission for eligible application states with optional force override."""
    from worker.tasks import submit_application_task

    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    submission = app.submission

    if not force:
        if app.status != JobStatus.APPROVED:
            raise HTTPException(status_code=400, detail="Only approved applications can be retried")

        if submission and submission.status == SubmissionStatus.PENDING:
            return {"message": "Submission already pending", "application_id": app_id}

        if submission and submission.status not in {
            SubmissionStatus.FAILED,
            SubmissionStatus.NEEDS_HUMAN_CONFIRMATION,
            SubmissionStatus.DRAFT_ONLY,
        }:
            raise HTTPException(status_code=400, detail="Submission state is not retry-eligible")

    redis_client = _get_redis_client()
    cooldown_key = f"submission_retry_cooldown:{app_id}"
    if redis_client is not None:
        try:
            if not force and redis_client.get(cooldown_key):
                return {"message": "Retry is on cooldown", "application_id": app_id}
            redis_client.setex(cooldown_key, 30, "1")
        except Exception:
            pass

    submit_application_task.delay(app_id)
    logger.info(
        "submission_retry_queued",
        application_id=app_id,
        force=force,
        previous_submission_status=submission.status.value if submission and submission.status else None,
    )
    return {"message": "Submission retry queued", "application_id": app_id, "force": force}
