"""Applications API routes — list, approve, reject, view drafts."""

from __future__ import annotations

import json
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import Application, JobStatus
from db.session import get_db

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["applications"])


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
    submission_confirmation_url: str | None = None
    submission_error: str | None = None
    submitted_at: str | None = None

    class Config:
        from_attributes = True


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
            submission_confirmation_url=submission.confirmation_url if submission else None,
            submission_error=submission.error_message if submission else None,
            submitted_at=submission.created_at.isoformat() if submission else None,
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
    )


@router.get("/profile")
async def get_profile_summary():
    """Return the user profile fields used when filling application forms."""
    from profile.loader import get_profile
    try:
        profile = get_profile()
        p = profile.model_dump()
        personal = p.get("personal", {})
        links = p.get("links", {})
        resume = p.get("resume", {})
        return {
            "name":      personal.get("name", ""),
            "email":     personal.get("email", ""),
            "phone":     personal.get("phone", ""),
            "location":  personal.get("location", ""),
            "linkedin":  links.get("linkedin", ""),
            "github":    links.get("github", ""),
            "portfolio": links.get("portfolio", "") or links.get("website", ""),
            "resume_pdf": resume.get("pdf_path", "") if resume else "",
            "skills":    p.get("skills", [])[:20],
        }
    except Exception as exc:
        return {"error": str(exc)}


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
    from core.config import get_settings
    from worker.tasks import submit_application_task
    settings = get_settings()
    if settings.tasks_always_eager:
        submit_application_task.apply(args=[app.id])
    else:
        submit_application_task.delay(app.id)

    logger.info("application_approved_via_api", app_id=app.id)
    return ApproveResponse(
        message="Approved and queued for submission",
        application_id=app.id,
        status="approved",
    )


@router.post("/applications/{app_id}/retry")
async def retry_application(app_id: int, db: Session = Depends(get_db)):
    """Re-queue a failed or draft application for submission."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    if app.status not in (JobStatus.DRAFT, JobStatus.APPROVED):
        # Force back to approved so submission gate passes
        app.status = JobStatus.APPROVED
        app.approved_at = datetime.utcnow()
        if app.job:
            app.job.status = JobStatus.APPROVED
        db.commit()

    # Delete existing failed submission record so a new one is created
    if app.submission:
        db.delete(app.submission)
        db.commit()

    from core.config import get_settings
    from worker.tasks import submit_application_task
    settings = get_settings()
    if settings.tasks_always_eager:
        submit_application_task.apply(args=[app.id])
    else:
        submit_application_task.delay(app.id)

    logger.info("application_retry_queued", app_id=app.id)
    return {"message": "Re-queued for submission", "application_id": app.id}


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
