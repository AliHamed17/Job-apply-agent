"""Re-score queued jobs against the current profile."""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any, cast

import structlog

from db.models import Application, Job, JobStatus
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
            title=j.title,
            company=j.company or "",
            location=j.location or "",
            employment_type=j.employment_type or "",
            seniority=j.seniority or "",
            description=j.description or "",
            requirements=j.requirements or "",
            apply_url=j.apply_url or "",
            source_url=j.source_url,
            date_posted=j.date_posted or "",
            keywords=json.loads(j.keywords) if j.keywords else [],
        )
        j.score = score_job(job_data, profile).total
        updated += 1
    db.commit()
    logger.info("rescored_pending_jobs", count=updated)
    return updated


def requeue_scored_jobs_for_preparation(
    db,
    *,
    tasks_always_eager: bool,
    batch_size: int,
) -> int:
    """Re-enter scoring for discovery rows that previously stopped at SCORE."""

    if not 1 <= batch_size <= 100:
        raise ValueError("preparation requeue batch size must be between 1 and 100")
    rows = (
        db.query(Job.id)
        .outerjoin(Application, Application.job_id == Job.id)
        .filter(
            Job.status == JobStatus.SCORED,
            Application.id.is_(None),
        )
        .order_by(Job.id)
        .limit(batch_size)
        .all()
    )
    job_ids = [int(row[0]) for row in rows]
    # Callers invoke this only after committing their profile/job mutation.
    # Release the read transaction before an eager task opens its own writer.
    db.rollback()

    # Resolve the Celery task at dispatch time. Keeping this boundary late-bound
    # avoids importing the full task graph into profile/CV intake processes.
    tasks_module = cast(Any, import_module("worker.tasks"))
    score_job_task = tasks_module.score_job_task

    queued = 0
    for job_id in job_ids:
        try:
            if tasks_always_eager:
                score_job_task.apply(args=[job_id, True])
            else:
                score_job_task.delay(job_id, True)
            queued += 1
        except Exception:
            logger.warning(
                "scored_job_requeue_failed",
                job_id=job_id,
                reason_code="PREPARATION_QUEUE_UNAVAILABLE",
            )
    if queued:
        logger.info("scored_jobs_requeued_for_preparation", count=queued)
    return queued


def auto_prepare_scored_jobs_if_ready(db, settings) -> int:
    """Requeue blocked discovery jobs only when the canonical stage is enabled."""

    if not settings.auto_apply:
        return 0

    from core.automation_readiness import current_automation_readiness  # noqa: PLC0415
    from core.operations import readiness_report  # noqa: PLC0415

    try:
        report = readiness_report(settings)
        automation = current_automation_readiness(
            settings=settings,
            dependency_report=report,
            db=db,
        )
    except Exception:
        logger.info(
            "scored_job_requeue_blocked",
            reason_code="PREPARATION_READINESS_UNAVAILABLE",
        )
        return 0
    if automation["preparation_ready"] is not True:
        return 0
    return requeue_scored_jobs_for_preparation(
        db,
        tasks_always_eager=settings.tasks_always_eager,
        batch_size=settings.preparation_requeue_batch_size,
    )
