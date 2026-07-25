"""Batch job re-scoring and CV re-evaluation engine."""

from __future__ import annotations

import structlog
from db.models import Job, JobStatus
from match.scoring import score_job
from profile.loader import get_profile

logger = structlog.get_logger(__name__)


def batch_rescore_all_jobs(db) -> dict[str, int]:
    """Re-evaluate scores for all pending and draft jobs using latest candidate profile preferences."""
    profile = get_profile()
    jobs = db.query(Job).filter(Job.status.in_([JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.DRAFT])).all()


    updated_count = 0
    for job in jobs:
        old_score = job.score
        score_res = score_job(job, profile)
        new_score = score_res.total if hasattr(score_res, "total") else float(score_res)
        job.score = new_score
        if old_score != new_score:
            updated_count += 1


    db.commit()
    logger.info("batch_rescore_completed", total_evaluated=len(jobs), updated_count=updated_count)
    return {"total_evaluated": len(jobs), "updated_count": updated_count}
