"""Jobs API routes — list, view, filter, and ingest job URLs."""

from __future__ import annotations

from profile.cv_routing import parse_required_skills

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.submission_display import job_submission_display
from db.models import ExtractedURL, Job, JobStatus, Message, URLStatus
from db.session import get_db
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData
from match.job_fit import JobFitDecisionV1, job_content_digest
from match.job_fit_store import latest_job_fit_decision

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["jobs"])


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    company: str
    location: str
    employment_type: str
    seniority: str
    apply_url: str
    source_url: str
    date_posted: str
    score: float | None
    status: str
    display_status: str
    employer_verified: bool
    created_at: str


def _serialize_job(job: Job) -> JobResponse:
    submission_display = job_submission_display(job)
    return JobResponse(
        id=job.id,
        title=job.title,
        company=job.company or "",
        location=job.location or "",
        employment_type=job.employment_type or "",
        seniority=job.seniority or "",
        apply_url=job.apply_url or "",
        source_url=job.source_url,
        date_posted=job.date_posted or "",
        score=job.score,
        status=submission_display.source_status,
        display_status=submission_display.display_status,
        employer_verified=submission_display.employer_verified,
        created_at=job.created_at.isoformat() if job.created_at else "",
    )


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(
    status: str | None = Query(None, description="Filter by status"),
    min_score: float | None = Query(None, description="Minimum score"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List extracted jobs with optional filtering."""
    query = db.query(Job)

    if status:
        try:
            status_enum = JobStatus(status)
            query = query.filter(Job.status == status_enum)
        except ValueError:
            pass

    if min_score is not None:
        query = query.filter(Job.score >= min_score)

    jobs = query.order_by(Job.created_at.desc()).offset(offset).limit(limit).all()

    return [_serialize_job(job) for job in jobs]


class IngestRequest(BaseModel):
    url: str
    sender: str = "api"
    source: str = "manual"


class AutomationDecisionResponse(BaseModel):
    job_id: int
    decision_id: int
    decision_digest: str
    created_at: str
    decision: JobFitDecisionV1
    quality_eligible: bool
    submission_authorized: bool = False
    authority_reason: str = "QUALITY_DECISION_IS_NOT_SUBMISSION_AUTHORITY"


@router.post("/ingest")
async def ingest_url(body: IngestRequest, db: Session = Depends(get_db)):
    """Accept a job URL from the WhatsApp bridge or the dashboard.

    Returns {"added": 1, "skipped": 0} or {"added": 0, "skipped": 1}.
    """
    from core.config import get_settings
    from worker.tasks import process_url_task

    raw = (body.url or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="url is required")

    normalized = normalize_url(raw)
    uhash = url_hash(normalized)

    # Dedup — already seen this exact URL
    if db.query(ExtractedURL).filter(ExtractedURL.url_hash == uhash).first():
        return {"added": 0, "skipped": 1}

    # Create a synthetic message record for traceability
    db_msg = Message(
        whatsapp_message_id=f"ingest-{uhash[:16]}",
        sender_phone=body.sender,
        body=raw,
    )
    db.add(db_msg)
    db.flush()

    db_url = ExtractedURL(
        message_id=db_msg.id,
        original_url=raw,
        normalized_url=normalized,
        url_hash=uhash,
        status=URLStatus.PENDING,
    )
    db.add(db_url)
    db.flush()
    db.commit()

    settings = get_settings()
    if settings.tasks_always_eager:
        process_url_task.apply(args=[db_url.id])
    else:
        process_url_task.delay(db_url.id)

    logger.info("url_ingested", url=normalized, source=body.source)
    return {"added": 1, "skipped": 0}


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, db: Session = Depends(get_db)):
    """Get a single job by ID."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Job not found")

    return _serialize_job(job)


@router.get(
    "/jobs/{job_id}/automation-decision",
    response_model=AutomationDecisionResponse,
)
async def get_job_automation_decision(
    job_id: int,
    db: Session = Depends(get_db),
) -> AutomationDecisionResponse:
    """Return the latest calibrated quality decision without granting send authority."""

    job = db.query(Job).filter(Job.id == job_id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    latest = latest_job_fit_decision(db, job_id=job_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="Job fit decision not available")
    record, decision = latest
    current_digest = job_content_digest(
        JobData(
            title=job.title or "",
            company=job.company or "",
            location=job.location or "",
            employment_type=job.employment_type or "",
            seniority=job.seniority or "",
            description=job.description or "",
            requirements=job.requirements or "",
            apply_url=job.apply_url or "",
            source_url=job.source_url,
            date_posted=job.date_posted or "",
            keywords=parse_required_skills(job.keywords),
        )
    )
    if current_digest != decision.job_digest:
        raise HTTPException(status_code=409, detail="JOB_FIT_DECISION_STALE")
    return AutomationDecisionResponse(
        job_id=job_id,
        decision_id=record.id,
        decision_digest=record.decision_digest,
        created_at=record.created_at.isoformat() if record.created_at else "",
        decision=decision,
        quality_eligible=decision.quality_eligible,
    )
