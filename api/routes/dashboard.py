"""Dashboard API routes — summary view and manual URL ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import (
    Application,
    CoverLetterFeedback,
    ExtractedURL,
    Job,
    JobStatus,
    Message,
    Submission,
    SubmissionStatus,
    URLStatus,
)
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
    # Extended metrics
    avg_job_score: float | None
    top_job_score: float | None
    jobs_skipped: int
    applications_skipped: int
    submission_failures: int
    feedback_count: int
    jobs_last_7d: int
    urls_failed: int
    urls_blocked: int
    score_distribution: dict[str, int]


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
        Submission.status == SubmissionStatus.SUCCESS
    ).count()

    # Score metrics — only over scored/draft/approved/submitted jobs
    score_row = (
        db.query(func.avg(Job.score), func.max(Job.score))
        .filter(Job.score.isnot(None))
        .one()
    )
    avg_score = round(score_row[0], 1) if score_row[0] is not None else None
    top_score = round(score_row[1], 1) if score_row[1] is not None else None

    jobs_skipped = db.query(Job).filter(Job.status == JobStatus.SKIPPED).count()

    apps_skipped = db.query(Application).filter(
        Application.status == JobStatus.SKIPPED
    ).count()

    sub_failures = db.query(Submission).filter(
        Submission.status == SubmissionStatus.FAILED
    ).count()

    feedback_count = db.query(CoverLetterFeedback).count()

    week_ago = datetime.utcnow() - timedelta(days=7)
    jobs_last_7d = db.query(Job).filter(Job.created_at >= week_ago).count()

    urls_failed = db.query(ExtractedURL).filter(
        ExtractedURL.status == URLStatus.FAILED
    ).count()
    urls_blocked = db.query(ExtractedURL).filter(
        ExtractedURL.status == URLStatus.BLOCKED
    ).count()

    # Score distribution across 5 buckets
    from sqlalchemy import case as sa_case
    bucket_expr = sa_case(
        (Job.score < 20, "0-20"),
        (Job.score < 40, "20-40"),
        (Job.score < 60, "40-60"),
        (Job.score < 80, "60-80"),
        else_="80-100",
    )
    dist_rows = (
        db.query(bucket_expr, func.count(Job.id))
        .filter(Job.score.isnot(None))
        .group_by(bucket_expr)
        .all()
    )
    score_distribution = {bucket: count for bucket, count in dist_rows}

    return DashboardSummary(
        total_messages=total_messages,
        total_urls=total_urls,
        total_jobs=total_jobs,
        jobs_by_status=jobs_by_status,
        applications_pending=apps_pending,
        applications_approved=apps_approved,
        submissions_total=total_subs,
        submissions_success=success_subs,
        avg_job_score=avg_score,
        top_job_score=top_score,
        jobs_skipped=jobs_skipped,
        applications_skipped=apps_skipped,
        submission_failures=sub_failures,
        feedback_count=feedback_count,
        jobs_last_7d=jobs_last_7d,
        urls_failed=urls_failed,
        urls_blocked=urls_blocked,
        score_distribution=score_distribution,
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
