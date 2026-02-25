"""Dashboard API routes — summary view and manual URL ingestion."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import Application, ExtractedURL, Job, JobStatus, Message, Submission, URLStatus
from db.session import get_db
from match.scoring import AUTO_APPLY_THRESHOLD

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


class URLPipelineItem(BaseModel):
    url_id: int
    original_url: str
    normalized_url: str
    status: str
    created_at: str
    jobs_found: int
    applications_ready: int
    auto_apply_candidates: int


class URLPipelineSummary(BaseModel):
    items: list[URLPipelineItem]
    total: int


class URLAutoApplyResponse(BaseModel):
    url_id: int
    approved_count: int
    queued_submission_count: int
    skipped_count: int
    message: str

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




@router.get("/urls", response_model=URLPipelineSummary)
async def list_pipeline_urls(
    status: str | None = Query(None, description="Filter by URL status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List extracted URLs with readiness and auto-apply visibility."""
    query = db.query(ExtractedURL)

    if status:
        try:
            query = query.filter(ExtractedURL.status == URLStatus(status))
        except ValueError:
            pass

    total = query.count()
    rows = query.order_by(ExtractedURL.created_at.desc()).offset(offset).limit(limit).all()

    items: list[URLPipelineItem] = []
    for row in rows:
        jobs = db.query(Job).filter(Job.extracted_url_id == row.id).all()
        apps_ready = 0
        auto_candidates = 0

        for job in jobs:
            app = db.query(Application).filter(Application.job_id == job.id).first()
            if app and app.status == JobStatus.DRAFT:
                apps_ready += 1

            if (
                job.score is not None
                and job.score >= AUTO_APPLY_THRESHOLD
                and app is not None
                and app.status == JobStatus.DRAFT
            ):
                auto_candidates += 1

        items.append(
            URLPipelineItem(
                url_id=row.id,
                original_url=row.original_url,
                normalized_url=row.normalized_url,
                status=row.status.value if row.status else "",
                created_at=row.created_at.isoformat() if row.created_at else "",
                jobs_found=len(jobs),
                applications_ready=apps_ready,
                auto_apply_candidates=auto_candidates,
            )
        )

    return URLPipelineSummary(items=items, total=total)


@router.post("/urls/{url_id}/auto-apply", response_model=URLAutoApplyResponse)
async def auto_apply_for_url(url_id: int, db: Session = Depends(get_db)):
    """Approve and queue eligible draft applications found from a specific URL."""
    from worker.tasks import submit_application_task

    extracted = db.query(ExtractedURL).filter(ExtractedURL.id == url_id).first()
    if not extracted:
        raise HTTPException(status_code=404, detail="URL not found")

    jobs = db.query(Job).filter(Job.extracted_url_id == url_id).all()
    if not jobs:
        return URLAutoApplyResponse(
            url_id=url_id,
            approved_count=0,
            queued_submission_count=0,
            skipped_count=0,
            message="No jobs found for this URL",
        )

    approved_count = 0
    queued_count = 0
    skipped = 0

    for job in jobs:
        app = db.query(Application).filter(Application.job_id == job.id).first()
        if not app:
            skipped += 1
            continue

        if app.status != JobStatus.DRAFT:
            skipped += 1
            continue

        if job.score is None or job.score < AUTO_APPLY_THRESHOLD:
            skipped += 1
            continue

        app.status = JobStatus.APPROVED
        app.approved_at = datetime.utcnow()
        job.status = JobStatus.APPROVED
        approved_count += 1

    db.commit()

    for job in jobs:
        app = db.query(Application).filter(Application.job_id == job.id).first()
        if app and app.status == JobStatus.APPROVED:
            submit_application_task.delay(app.id)
            queued_count += 1

    logger.info(
        "url_auto_apply_triggered",
        url_id=url_id,
        approved_count=approved_count,
        queued_submission_count=queued_count,
        skipped_count=skipped,
    )

    return URLAutoApplyResponse(
        url_id=url_id,
        approved_count=approved_count,
        queued_submission_count=queued_count,
        skipped_count=skipped,
        message="Eligible applications were approved and queued",
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
