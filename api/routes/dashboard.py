"""Dashboard API routes — summary view and manual URL ingestion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from core.application_state import (
    prepared_application_count,
    prepared_applications_query,
    reviewable_application_count,
)
from core.config import get_settings
from core.operational_labels import (
    normalize_adapter_version,
    normalize_ats,
    normalize_reason_code,
    normalize_selector_version,
    sql_normalize_adapter_version,
    sql_normalize_ats,
    sql_normalize_reason_code,
    sql_normalize_selector_version,
)
from core.operations import readiness_report
from core.submission_truth import (
    latest_employer_verified_count,
    latest_employer_verified_query,
)
from db.models import (
    Application,
    BrowserQualificationRun,
    CoverLetterFeedback,
    DiscoveryRun,
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

# In-memory bridge heartbeat store (resets on server restart, that's fine)
_bridge_last_seen: dict[str, str] = {}


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
    operational_status: str
    degraded_dependencies: list[str]
    last_successful_discovery: datetime | None
    cv_routing_total: int
    cv_routing_abstention_rate: float
    application_outcomes: dict[str, int]
    selector_failure_clusters: dict[str, int]
    browser_qualification_runs: int


class ManualIngestRequest(BaseModel):
    url: str | None = None
    urls: list[str] = Field(default_factory=list, max_length=50)
    sender: str = "manual"

    @model_validator(mode="after")
    def require_url(self):
        if not self.url and not self.urls:
            raise ValueError("Provide url or urls.")
        return self


class ManualIngestResult(BaseModel):
    url: str
    state: str
    url_id: int | None = None
    reason_code: str | None = None


class ManualIngestResponse(BaseModel):
    message: str
    results: list[ManualIngestResult]


class PipelineBottleneck(BaseModel):
    name: str
    count: int
    severity: str
    action: str


class RecentPipelineEvent(BaseModel):
    type: str
    id: int
    status: str
    source_status: str
    employer_verified: bool
    title: str
    created_at: datetime | None


class PipelineInsights(BaseModel):
    generated_at: datetime
    window_days: int
    queue_depth: dict[str, int]
    stale: dict[str, int]
    bottlenecks: list[PipelineBottleneck]
    top_opportunities: list[dict[str, Any]]
    recent_events: list[RecentPipelineEvent]


def _employer_verified_job_ids(db: Session) -> set[int]:
    """Return job IDs whose latest attempt satisfies the exact evidence contract."""
    return {
        job_id
        for (job_id,) in latest_employer_verified_query(db)
        .join(Application, Submission.application_id == Application.id)
        .with_entities(Application.job_id)
        .distinct()
        .all()
    }


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard_summary(db: Session = Depends(get_db)):
    """Get a summary of the pipeline state."""
    from sqlalchemy import func

    total_messages = db.query(Message).count()
    total_urls = db.query(ExtractedURL).count()
    total_jobs = db.query(Job).count()

    # Assign each job one truth-derived display state. A historical SUBMITTED
    # enum without exact employer evidence is an unverified record, not success.
    verified_job_ids = _employer_verified_job_ids(db)
    jobs_by_status: dict[str, int] = {}
    for job_id, source_status in db.query(Job.id, Job.status).all():
        display_status = (
            JobStatus.SUBMITTED.value
            if job_id in verified_job_ids
            else ("unverified" if source_status == JobStatus.SUBMITTED else source_status.value)
        )
        jobs_by_status[display_status] = jobs_by_status.get(display_status, 0) + 1

    apps_pending = reviewable_application_count(db)
    apps_approved = prepared_application_count(db)

    total_subs = db.query(Submission).count()
    success_subs = latest_employer_verified_count(db)

    # Score metrics — only over scored/draft/approved/submitted jobs
    score_row = (
        db.query(func.avg(Job.score), func.max(Job.score)).filter(Job.score.isnot(None)).one()
    )
    avg_score = round(score_row[0], 1) if score_row[0] is not None else None
    top_score = round(score_row[1], 1) if score_row[1] is not None else None

    jobs_skipped = db.query(Job).filter(Job.status == JobStatus.SKIPPED).count()

    apps_skipped = db.query(Application).filter(Application.status == JobStatus.SKIPPED).count()

    sub_failures = db.query(Submission).filter(Submission.status == SubmissionStatus.FAILED).count()

    feedback_count = db.query(CoverLetterFeedback).count()

    week_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
    jobs_last_7d = db.query(Job).filter(Job.created_at >= week_ago).count()

    urls_failed = db.query(ExtractedURL).filter(ExtractedURL.status == URLStatus.FAILED).count()
    urls_blocked = db.query(ExtractedURL).filter(ExtractedURL.status == URLStatus.BLOCKED).count()

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
    operations = await run_in_threadpool(readiness_report, get_settings())
    degraded_dependencies = [
        name for name, result in operations["checks"].items() if not result["ok"]
    ]
    last_successful_discovery = (
        db.query(func.max(DiscoveryRun.finished_at))
        .filter(
            DiscoveryRun.status == "success",
            DiscoveryRun.finished_at.isnot(None),
        )
        .scalar()
    )
    routing_total = (
        db.query(Application).filter(Application.cv_routing_confidence.isnot(None)).count()
    )
    routing_abstained = (
        db.query(Application)
        .filter(
            Application.cv_routing_fallback_reason.in_(
                ["abstained_low_confidence", "routing_not_configured"]
            )
        )
        .count()
    )
    outcome_rows = (
        db.query(Application.outcome, func.count(Application.id))
        .filter(Application.outcome.isnot(None))
        .group_by(Application.outcome)
        .all()
    )
    cluster_ats = sql_normalize_ats(BrowserQualificationRun.adapter_name).label("normalized_ats")
    cluster_version = sql_normalize_adapter_version(
        BrowserQualificationRun.adapter_version,
        ats_expression=cluster_ats,
    ).label("normalized_adapter_version")
    cluster_selector = sql_normalize_selector_version(
        BrowserQualificationRun.selector_version,
        ats_expression=cluster_ats,
    ).label("normalized_selector_version")
    cluster_reason = sql_normalize_reason_code(BrowserQualificationRun.terminal_reason).label(
        "normalized_reason_code"
    )
    cluster_count = func.count(BrowserQualificationRun.id).label("event_count")
    cluster_rows = (
        db.query(
            cluster_ats,
            cluster_version,
            cluster_selector,
            cluster_reason,
            cluster_count,
        )
        .filter(BrowserQualificationRun.qualified.is_(False))
        .group_by(
            cluster_ats,
            cluster_version,
            cluster_selector,
            cluster_reason,
        )
        .order_by(
            cluster_count.desc(),
            cluster_ats,
            cluster_version,
            cluster_selector,
            cluster_reason,
        )
        .limit(50)
        .all()
    )
    normalized_clusters: dict[tuple[str, str, str, str], int] = {}
    for adapter_name, adapter_version, selector_version, reason, count in cluster_rows:
        ats = normalize_ats(adapter_name)
        key = (
            ats,
            normalize_adapter_version(adapter_version, ats=ats),
            normalize_selector_version(selector_version, ats=ats),
            normalize_reason_code(reason),
        )
        normalized_clusters[key] = normalized_clusters.get(key, 0) + int(count or 0)
    bounded_clusters = sorted(
        normalized_clusters.items(),
        key=lambda item: (-item[1], *item[0]),
    )[:50]

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
        operational_status=operations["status"],
        degraded_dependencies=degraded_dependencies,
        last_successful_discovery=(
            last_successful_discovery.replace(tzinfo=UTC)
            if last_successful_discovery is not None and last_successful_discovery.tzinfo is None
            else last_successful_discovery
        ),
        cv_routing_total=routing_total,
        cv_routing_abstention_rate=(routing_abstained / routing_total if routing_total else 0.0),
        application_outcomes={outcome: count for outcome, count in outcome_rows},
        selector_failure_clusters={
            f"{ats}:{version}:{selector}:{reason}": count
            for (ats, version, selector, reason), count in bounded_clusters
        },
        browser_qualification_runs=db.query(BrowserQualificationRun).count(),
    )


@router.get("/dashboard/insights", response_model=PipelineInsights)
async def dashboard_insights(
    window_days: int = 7,
    stale_hours: int = 24,
    limit: int = 10,
    db: Session = Depends(get_db),
):
    """Return actionable pipeline insights for operators.

    The summary dashboard exposes raw counts; this endpoint turns the same data into
    queue depth, stale work, bottleneck recommendations, and the best pending
    opportunities so the operator knows what to fix or review next.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(days=max(1, min(window_days, 90)))
    stale_before = now - timedelta(hours=max(1, min(stale_hours, 168)))
    limit = max(1, min(limit, 50))

    queue_depth = {
        "urls_pending": db.query(ExtractedURL)
        .filter(ExtractedURL.status == URLStatus.PENDING)
        .count(),
        "jobs_extracted": db.query(Job).filter(Job.status == JobStatus.EXTRACTED).count(),
        "jobs_scored": db.query(Job).filter(Job.status == JobStatus.SCORED).count(),
        "applications_draft": reviewable_application_count(db),
        "applications_approved": prepared_application_count(db),
        "submissions_running": db.query(Submission)
        .filter(Submission.status == SubmissionStatus.RUNNING)
        .count(),
        "submissions_unknown": db.query(Submission)
        .filter(Submission.status == SubmissionStatus.UNKNOWN)
        .count(),
    }
    stale = {
        "urls_pending": db.query(ExtractedURL)
        .filter(
            ExtractedURL.status == URLStatus.PENDING,
            ExtractedURL.created_at < stale_before,
        )
        .count(),
        "applications_approved": prepared_applications_query(db)
        .filter(Application.updated_at < stale_before)
        .count(),
        "submissions_running": db.query(Submission)
        .filter(
            Submission.status == SubmissionStatus.RUNNING,
            Submission.started_at < stale_before,
        )
        .count(),
        "submissions_unknown": db.query(Submission)
        .filter(
            Submission.status == SubmissionStatus.UNKNOWN,
            Submission.created_at < stale_before,
        )
        .count(),
    }

    bottlenecks: list[PipelineBottleneck] = []
    if queue_depth["urls_pending"]:
        bottlenecks.append(
            PipelineBottleneck(
                name="URL processing backlog",
                count=queue_depth["urls_pending"],
                severity="warning" if stale["urls_pending"] == 0 else "critical",
                action=(
                    "Check fetcher logs and ensure processing workers are consuming "
                    "the processing queue."
                ),
            )
        )
    if queue_depth["applications_draft"]:
        bottlenecks.append(
            PipelineBottleneck(
                name="Applications awaiting approval",
                count=queue_depth["applications_draft"],
                severity="info",
                action=(
                    "Review drafts, confirm CV routing, then prepare or skip "
                    "them from the dashboard."
                ),
            )
        )
    if queue_depth["submissions_unknown"]:
        bottlenecks.append(
            PipelineBottleneck(
                name="Unknown submission outcomes",
                count=queue_depth["submissions_unknown"],
                severity="critical",
                action="Reconcile unknown attempts before retrying to avoid duplicates.",
            )
        )
    if stale["submissions_running"]:
        bottlenecks.append(
            PipelineBottleneck(
                name="Stale running submissions",
                count=stale["submissions_running"],
                severity="critical",
                action=(
                    "Inspect browser traces and mark the attempt reconciled if "
                    "the worker died mid-submit."
                ),
            )
        )

    top_jobs = (
        db.query(Job)
        .filter(
            Job.score.isnot(None),
            Job.status.in_([JobStatus.SCORED, JobStatus.DRAFT, JobStatus.NEEDS_REVIEW]),
        )
        .order_by(Job.score.desc(), Job.created_at.desc())
        .limit(limit)
        .all()
    )
    top_opportunities = [
        {
            "id": job.id,
            "title": job.title,
            "company": job.company or "",
            "score": job.score,
            "status": job.status.value if job.status else "",
            "apply_url": job.apply_url or job.source_url,
        }
        for job in top_jobs
    ]

    recent_jobs = (
        db.query(Job)
        .filter(Job.created_at >= since)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .all()
    )
    verified_job_ids = _employer_verified_job_ids(db)
    recent_events = [
        RecentPipelineEvent(
            type="job",
            id=job.id,
            status=(
                JobStatus.SUBMITTED.value
                if job.id in verified_job_ids
                else (
                    "unverified"
                    if job.status == JobStatus.SUBMITTED
                    else (job.status.value if job.status else "")
                )
            ),
            source_status=job.status.value if job.status else "",
            employer_verified=job.id in verified_job_ids,
            title=f"{job.title} — {job.company or 'Unknown company'}",
            created_at=job.created_at,
        )
        for job in recent_jobs
    ]

    return PipelineInsights(
        generated_at=now,
        window_days=max(1, min(window_days, 90)),
        queue_depth=queue_depth,
        stale=stale,
        bottlenecks=bottlenecks,
        top_opportunities=top_opportunities,
        recent_events=recent_events,
    )


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(hostname)


@router.post("/dashboard/ingest", response_model=ManualIngestResponse, status_code=202)
async def manual_ingest(req: ManualIngestRequest, db: Session = Depends(get_db)):
    """Accept one or more URLs and report queue acceptance truthfully."""
    from ingestion.url_utils import normalize_url, url_hash
    from worker.tasks import process_url_task

    raw_urls = ([req.url] if req.url else []) + req.urls
    unique_urls = list(dict.fromkeys(value.strip() for value in raw_urls if value.strip()))
    results: list[ManualIngestResult] = []
    settings = get_settings()
    for raw_url in unique_urls:
        if not _valid_http_url(raw_url):
            results.append(
                ManualIngestResult(
                    url=raw_url,
                    state="rejected",
                    reason_code="INVALID_URL",
                )
            )
            continue

        normalized = normalize_url(raw_url)
        uhash = url_hash(normalized)
        existing = db.query(ExtractedURL).filter(ExtractedURL.url_hash == uhash).first()
        if existing:
            results.append(
                ManualIngestResult(
                    url=raw_url,
                    state="duplicate",
                    url_id=existing.id,
                    reason_code="URL_ALREADY_EXISTS",
                )
            )
            continue

        msg = Message(
            whatsapp_message_id=f"manual-{uhash[:16]}",
            sender_phone=req.sender,
            body=raw_url,
        )
        db.add(msg)
        db.flush()
        db_url = ExtractedURL(
            message_id=msg.id,
            original_url=raw_url,
            normalized_url=normalized,
            url_hash=uhash,
        )
        db.add(db_url)
        db.commit()

        try:
            if settings.tasks_always_eager:
                process_url_task.apply(args=[db_url.id])
            else:
                process_url_task.delay(db_url.id)
        except Exception as exc:
            db.rollback()
            db_url = db.get(ExtractedURL, db_url.id)
            if db_url is not None:
                db_url.status = URLStatus.FAILED
                db_url.fetch_error = "QUEUE_ENQUEUE_FAILED"
                db.commit()
            logger.error(
                "manual_ingest_enqueue_failed",
                error_type=type(exc).__name__,
            )
            results.append(
                ManualIngestResult(
                    url=raw_url,
                    state="failed",
                    url_id=db_url.id if db_url is not None else None,
                    reason_code="QUEUE_ENQUEUE_FAILED",
                )
            )
            continue

        results.append(
            ManualIngestResult(
                url=raw_url,
                state="accepted",
                url_id=db_url.id,
            )
        )

    counts = {
        state: sum(result.state == state for result in results)
        for state in ("accepted", "duplicate", "rejected", "failed")
    }
    logger.info("manual_ingest_batch", **counts)
    return ManualIngestResponse(
        message=(
            f"{counts['accepted']} accepted, {counts['duplicate']} duplicate, "
            f"{counts['rejected']} rejected, {counts['failed']} failed."
        ),
        results=results,
    )


@router.get("/urls")
async def list_urls(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List extracted URLs with their status and job counts."""
    from sqlalchemy import func

    rows = (
        db.query(
            ExtractedURL,
            func.count(Job.id).label("job_count"),
        )
        .outerjoin(Job, Job.extracted_url_id == ExtractedURL.id)
        .group_by(ExtractedURL.id)
        .order_by(ExtractedURL.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": u.id,
            "url": u.normalized_url,
            "status": u.status.value if u.status else "unknown",
            "jobs_found": cnt,
            "error": u.fetch_error,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u, cnt in rows
    ]


@router.post("/urls/{url_id}/retry")
async def retry_url(url_id: int, db: Session = Depends(get_db)):
    """Re-queue a URL for re-processing (useful when no jobs were extracted)."""
    db_url = db.query(ExtractedURL).filter(ExtractedURL.id == url_id).first()
    if not db_url:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="URL not found")

    # Reset status so the task re-processes it
    db_url.status = URLStatus.PENDING
    db_url.fetch_error = None
    db.commit()

    from worker.tasks import process_url_task

    settings = get_settings()
    if settings.tasks_always_eager:
        process_url_task.apply(args=[db_url.id])
    else:
        process_url_task.delay(db_url.id)

    logger.info("url_retry_queued", url_id=url_id, url=db_url.normalized_url)
    return {"message": "URL re-queued for processing", "url_id": url_id}


@router.get("/messages")
async def list_messages(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List recent WhatsApp messages (serialized)."""
    rows = db.query(Message).order_by(Message.received_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id": m.id,
            "sender_phone": m.sender_phone,
            "body": m.body or "",
            "created_at": m.received_at.isoformat() if m.received_at else None,
            "url_count": len(m.extracted_urls),
        }
        for m in rows
    ]


# ── Bridge Heartbeat ─────────────────────────────────────────────────────────


@router.post("/bridge/heartbeat")
async def bridge_heartbeat(request: Request):
    """Receive a heartbeat ping from the WhatsApp bridge process.

    The bridge calls this every 60 s so the dashboard can show
    whether it is currently connected.
    """
    try:
        data: dict[str, Any] = await request.json()
    except Exception:
        data = {}
    bridge_id = str(data.get("id", "default"))
    groups = int(data.get("groups_watched", 0))
    _bridge_last_seen[bridge_id] = datetime.utcnow().isoformat()
    logger.debug("bridge_heartbeat", bridge_id=bridge_id, groups=groups)
    return {"status": "ok", "bridge_id": bridge_id}


@router.get("/bridge/status")
async def bridge_status():
    """Return the connection status of the WhatsApp bridge."""
    if not _bridge_last_seen:
        return {"connected": False, "last_seen": None, "groups_watched": 0}
    last_seen_str = max(_bridge_last_seen.values())
    last_seen_dt = datetime.fromisoformat(last_seen_str)
    connected = (datetime.utcnow() - last_seen_dt).total_seconds() < 120
    return {"connected": connected, "last_seen": last_seen_str}
