"""Read-only batch candidate selection for explicit operator review."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from db.models import Application, Job, JobStatus

logger = structlog.get_logger(__name__)


@dataclass
class BatchApplySummary:
    triggered_count: int
    skipped_count: int
    job_ids: list[int]
    application_ids: list[int]


def trigger_batch_auto_apply(
    db,
    min_score: float = 80.0,
    max_batch_size: int = 10,
) -> BatchApplySummary:
    """Find reviewable candidates without changing or queuing them."""
    rows = (
        db.query(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .filter(
            Application.status == JobStatus.DRAFT,
            Application.approved_at.is_(None),
        )
        .filter(Job.score >= min_score)
        .order_by(Job.score.desc())
        .limit(max_batch_size)
        .all()
    )
    job_ids = [job.id for _app, job in rows]
    application_ids = [app.id for app, _job in rows]
    logger.info("batch_review_candidates_selected", count=len(rows), min_score=min_score)

    return BatchApplySummary(
        triggered_count=0,
        skipped_count=0,
        job_ids=job_ids,
        application_ids=application_ids,
    )
