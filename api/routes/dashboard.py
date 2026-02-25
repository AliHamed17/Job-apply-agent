"""Dashboard API routes — summary view and manual URL ingestion."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import Application, ExtractedURL, Job, JobStatus, Message, Submission
from db.session import get_db

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["dashboard"])


class DashboardSummary(BaseModel):
    total_messages: int
    total_urls: int
    total_jobs: int
    jobs_by_status: dict[str, int]
    applications_pending: int
    applications_approved: int
    submissions_total: int
    submissions_success: int


class ManualIngestRequest(BaseModel):
    url: str
    sender: str = "manual"


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard_summary(db: Session = Depends(get_db)):
    """Get a summary of the pipeline state."""
    from sqlalchemy import func

    total_messages = db.query(Message).count()
    total_urls = db.query(ExtractedURL).count()
    total_jobs = db.query(Job).count()

    # Jobs by status
    status_counts = (
        db.query(Job.status, func.count(Job.id))
        .group_by(Job.status)
        .all()
    )
    jobs_by_status = {s.value: c for s, c in status_counts}

    apps_pending = db.query(Application).filter(
        Application.status == JobStatus.DRAFT
    ).count()
    apps_approved = db.query(Application).filter(
        Application.status == JobStatus.APPROVED
    ).count()

    total_subs = db.query(Submission).count()
    success_subs = db.query(Submission).filter(
        Submission.status == "success"  # SubmissionStatus enum
    ).count()

    return DashboardSummary(
        total_messages=total_messages,
        total_urls=total_urls,
        total_jobs=total_jobs,
        jobs_by_status=jobs_by_status,
        applications_pending=apps_pending,
        applications_approved=apps_approved,
        submissions_total=total_subs,
        submissions_success=success_subs,
    )


@router.post("/ingest")
async def manual_ingest(req: ManualIngestRequest, db: Session = Depends(get_db)):
    """Manually ingest a URL (useful for testing without WhatsApp)."""
    from ingestion.url_utils import normalize_url, url_hash

    normalized = normalize_url(req.url)
    uhash = url_hash(normalized)

    # Check dedup
    existing = db.query(ExtractedURL).filter(ExtractedURL.url_hash == uhash).first()
    if existing:
        return {"message": "URL already processed", "url_id": existing.id}

    # Create a pseudo-message
    msg = Message(
        whatsapp_message_id=f"manual-{uhash[:16]}",
        sender_phone=req.sender,
        body=req.url,
    )
    db.add(msg)
    db.flush()

    db_url = ExtractedURL(
        message_id=msg.id,
        original_url=req.url,
        normalized_url=normalized,
        url_hash=uhash,
    )
    db.add(db_url)
    db.commit()

    # Enqueue processing
    from worker.tasks import process_url_task
    process_url_task.delay(db_url.id)

    logger.info("manual_ingest", url=req.url)
    return {"message": "URL queued for processing", "url_id": db_url.id}
