"""Jobs API routes — list, view, and filter extracted jobs."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_
from sqlalchemy.orm import Session

from db.models import (
    Application,
    Job,
    JobStatus,
    Submission,
    SubmissionStatus,
)
from db.session import get_db
from ingestion.url_utils import identify_job_platform

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["jobs"])


class JobResponse(BaseModel):
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
    created_at: str
    platform: str

    model_config = ConfigDict(from_attributes=True)


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(
    status: str | None = Query(None, description="Filter by status"),
    min_score: float | None = Query(None, description="Minimum score"),
    platform: str | None = Query(None, description="Filter by job platform"),
    has_application: bool | None = Query(None, description="Filter jobs with generated application"),
    start_date: str | None = Query(None, description="ISO date lower bound for created_at"),
    end_date: str | None = Query(None, description="ISO date upper bound for created_at"),
    sort_by: str = Query("created_at", description="Sort field: created_at|score"),
    sort_order: str = Query("desc", description="Sort order: asc|desc"),
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

    if has_application is not None:
        if has_application:
            query = query.join(Application, Application.job_id == Job.id)
        else:
            query = query.outerjoin(Application, Application.job_id == Job.id).filter(
                Application.id.is_(None)
            )

    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date)
            query = query.filter(Job.created_at >= start_dt)
        except ValueError:
            pass

    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date)
            query = query.filter(Job.created_at <= end_dt)
        except ValueError:
            pass

    sort_desc = sort_order.lower() != "asc"
    if sort_by == "score":
        query = query.order_by(Job.score.desc() if sort_desc else Job.score.asc())
    else:
        query = query.order_by(Job.created_at.desc() if sort_desc else Job.created_at.asc())

    if platform:
        platform_filter = platform.lower().strip()
        platform_predicates = {
            "greenhouse": ["%greenhouse.io%", "%boards.greenhouse.io%"],
            "lever": ["%lever.co%", "%jobs.lever.co%"],
            "workday": ["%myworkdayjobs.com%", "%workday%"],
            "linkedin": ["%linkedin.com/jobs%"],
            "indeed": ["%indeed.com%"],
        }
        patterns = platform_predicates.get(platform_filter, [])
        if patterns:
            query = query.filter(
                or_(
                    *([Job.apply_url.ilike(p) for p in patterns] + [Job.source_url.ilike(p) for p in patterns])
                )
            )

    jobs = query.offset(offset).limit(limit).all()

    return [
        JobResponse(
            id=j.id,
            title=j.title,
            company=j.company or "",
            location=j.location or "",
            employment_type=j.employment_type or "",
            seniority=j.seniority or "",
            apply_url=j.apply_url or "",
            source_url=j.source_url,
            date_posted=j.date_posted or "",
            score=j.score,
            status=j.status.value if j.status else "",
            created_at=j.created_at.isoformat() if j.created_at else "",
            platform=identify_job_platform(j.apply_url or j.source_url),
        )
        for j in jobs
    ]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, db: Session = Depends(get_db)):
    """Get a single job by ID."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

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
        status=job.status.value if job.status else "",
        created_at=job.created_at.isoformat() if job.created_at else "",
        platform=identify_job_platform(job.apply_url or job.source_url),
    )


@router.post("/jobs/{job_id}/apply-now")
async def apply_now_for_job(job_id: int, db: Session = Depends(get_db)):
    """Quick-apply a single job by approving its draft application and queueing submission."""
    from worker.tasks import submit_application_task

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    app = db.query(Application).filter(Application.job_id == job_id).first()
    if not app:
        raise HTTPException(status_code=400, detail="No generated application found for this job")

    existing_submission = db.query(Submission).filter(Submission.application_id == app.id).first()
    if existing_submission and existing_submission.status in {
        SubmissionStatus.PENDING,
        SubmissionStatus.SUCCESS,
    }:
        return {"message": "Submission already pending or completed", "job_id": job_id}

    if app.status == JobStatus.APPROVED:
        submit_application_task.delay(app.id)
        return {"message": "Application already approved; submission queued", "job_id": job_id}

    if app.status != JobStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft applications can be quick-applied")

    app.status = JobStatus.APPROVED
    app.approved_at = datetime.utcnow()
    job.status = JobStatus.APPROVED
    db.commit()

    submit_application_task.delay(app.id)
    logger.info("job_quick_applied", job_id=job_id, application_id=app.id)
    return {"message": "Application approved and queued", "job_id": job_id}
