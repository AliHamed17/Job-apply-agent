"""Re-score queued jobs against the current profile."""

from __future__ import annotations

import json

import structlog

from db.models import Job, JobStatus
from jobs.models import JobData
from match.scoring import score_job

logger = structlog.get_logger(__name__)

_RESCORE_STATUSES = (JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.DRAFT)


def rescore_pending_jobs(db, profile) -> int:
    """Re-score not-yet-submitted jobs; returns the number updated."""
    rows = db.query(Job).filter(Job.status.in_(_RESCORE_STATUSES)).all()
    updated = 0
    for j in rows:
        job_data = JobData(
            title=j.title, company=j.company or "", location=j.location or "",
            employment_type=j.employment_type or "", seniority=j.seniority or "",
            description=j.description or "", requirements=j.requirements or "",
            apply_url=j.apply_url or "", source_url=j.source_url,
            date_posted=j.date_posted or "",
            keywords=json.loads(j.keywords) if j.keywords else [],
        )
        j.score = score_job(job_data, profile).total
        updated += 1
    db.commit()
    logger.info("rescored_pending_jobs", count=updated)
    return updated
