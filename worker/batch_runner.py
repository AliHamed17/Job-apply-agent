"""Multi-job application batch queue controller."""

from __future__ import annotations

from dataclasses import dataclass
import structlog

from db.models import Job, JobStatus

logger = structlog.get_logger(__name__)


@dataclass
class BatchApplySummary:
    triggered_count: int
    skipped_count: int
    job_ids: list[int]


def trigger_batch_auto_apply(db, min_score: float = 80.0, max_batch_size: int = 10) -> BatchApplySummary:
    """Find top-scoring jobs and trigger submission pipeline."""
    jobs = (
        db.query(Job)
        .filter(Job.status.in_([JobStatus.SCORED, JobStatus.DRAFT]))
        .filter(Job.score >= min_score)
        .order_by(Job.score.desc())
        .limit(max_batch_size)
        .all()
    )

    job_ids = []
    for j in jobs:
        j.status = JobStatus.APPROVED
        job_ids.append(j.id)

    db.commit()
    logger.info("batch_apply_triggered", count=len(job_ids), min_score=min_score)

    return BatchApplySummary(
        triggered_count=len(job_ids),
        skipped_count=0,
        job_ids=job_ids,
    )
