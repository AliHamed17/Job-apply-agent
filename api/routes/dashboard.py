"""Dashboard API routes — summary view and manual URL ingestion."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Application, ExtractedURL, Job, JobStatus, Message, Submission, URLStatus
from db.session import get_db
from ingestion.url_utils import normalize_url, url_hash
from match.scoring import AUTO_APPLY_THRESHOLD

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["dashboard"])


def _detect_auth_requirement(fetch_error: str | None) -> bool:
    text = (fetch_error or "").lower()
    return ("auth" in text) or ("login" in text) or ("sign in" in text)


def _detect_auth_provider_hint(url: str, fetch_error: str | None) -> str | None:
    source = f"{url} {(fetch_error or '')}".lower()
    if "google" in source or "accounts.google.com" in source:
        return "google"
    if "microsoft" in source or "login.microsoftonline.com" in source or "azuread" in source:
        return "microsoft"
    if "okta" in source:
        return "okta"
    if "auth0" in source:
        return "auth0"
    if _detect_auth_requirement(fetch_error):
        return "generic_sso"
    return None

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
    model_config = ConfigDict(from_attributes=True)
    url_id: int
    original_url: str
    normalized_url: str
    status: str
    created_at: str
    jobs_found: int
    applications_ready: int
    auto_apply_candidates: int
    requires_auth: bool
    auth_provider_hint: str | None


class URLPipelineSummary(BaseModel):
    items: list[URLPipelineItem]
    total: int


class URLAutoApplyResponse(BaseModel):
    url_id: int
    approved_count: int
    queued_submission_count: int
    skipped_count: int
    message: str


class URLAuthResolveRequest(BaseModel):
    authenticated_url: str


class URLAuthResolveResponse(BaseModel):
    url_id: int
    old_url: str
    authenticated_url: str
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
                requires_auth=_detect_auth_requirement(row.fetch_error),
                auth_provider_hint=_detect_auth_provider_hint(
                    row.normalized_url,
                    row.fetch_error,
                ),
            )
        )

    return URLPipelineSummary(items=items, total=total)




@router.post("/urls/{url_id}/resolve-auth", response_model=URLAuthResolveResponse)
async def resolve_auth_for_url(
    url_id: int,
    req: URLAuthResolveRequest,
    db: Session = Depends(get_db),
):
    """Replace an auth-gated URL with a manually authenticated URL and re-queue it."""
    from worker.tasks import process_url_task

    row = db.query(ExtractedURL).filter(ExtractedURL.id == url_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="URL not found")

    if not _detect_auth_requirement(row.fetch_error):
        raise HTTPException(status_code=400, detail="URL is not marked as auth-required")

    normalized_new = normalize_url(req.authenticated_url)
    parsed_old = urlparse(row.normalized_url)
    parsed_new = urlparse(normalized_new)

    if parsed_new.scheme.lower() not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Authenticated URL must be http/https")

    if parsed_new.username or parsed_new.password:
        raise HTTPException(
            status_code=400,
            detail="Authenticated URL must not include credentials",
        )

    old_host = parsed_old.netloc.lower()
    new_host = parsed_new.netloc.lower()
    if not new_host:
        raise HTTPException(status_code=400, detail="Invalid authenticated URL")

    if old_host and new_host != old_host:
        raise HTTPException(
            status_code=400,
            detail="Authenticated URL host must match original host",
        )

    if parsed_old.path and parsed_old.path != "/":
        if not parsed_new.path.startswith(parsed_old.path):
            raise HTTPException(
                status_code=400,
                detail="Authenticated URL path must stay within the original URL path",
            )

    old_url = row.normalized_url

    row.original_url = req.authenticated_url
    row.normalized_url = normalized_new
    row.url_hash = url_hash(normalized_new)
    row.fetch_error = None
    row.status = URLStatus.PENDING
    db.commit()

    process_url_task.delay(row.id)

    logger.info("url_auth_resolved", url_id=row.id, host=new_host)
    return URLAuthResolveResponse(
        url_id=row.id,
        old_url=old_url,
        authenticated_url=normalized_new,
        message="Authenticated URL updated and re-queued",
    )


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

    settings = get_settings()
    approved_count = 0
    queued_count = 0
    skipped = 0
    approved_app_ids: list[int] = []

    for job in jobs:
        app = db.query(Application).filter(Application.job_id == job.id).first()
        if not app:
            skipped += 1
            continue

        if app.status != JobStatus.DRAFT:
            skipped += 1
            continue

        if (
            not settings.auto_apply_all_jobs
            and (job.score is None or job.score < AUTO_APPLY_THRESHOLD)
        ):
            skipped += 1
            continue

        app.status = JobStatus.APPROVED
        app.approved_at = datetime.utcnow()
        job.status = JobStatus.APPROVED
        approved_count += 1
        approved_app_ids.append(app.id)

    db.commit()

    for application_id in approved_app_ids:
        submit_application_task.delay(application_id)
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
