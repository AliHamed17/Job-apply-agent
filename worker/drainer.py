"""Priority drain of the approved-application queue + stale-job expiry."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from celery import shared_task

from db.models import Application, Job, JobStatus, Submission, SubmissionStatus

logger = structlog.get_logger(__name__)

_STALE_STATUSES = (JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.DRAFT)


def select_next_application(db) -> int | None:
    """Highest Job.score among APPROVED applications; ties → lowest job id.

    Defensive belt-and-suspenders: excludes applications whose latest
    lifecycle could have produced an external action. Definitively failed or
    draft-only attempts remain eligible only after an explicit retry has put
    the application back in APPROVED.
    """
    row = (
        db.query(Application)
        .join(Job, Application.job_id == Job.id)
        .filter(
            Application.status == JobStatus.APPROVED,
            ~db.query(Submission.id)
            .filter(
                Submission.application_id == Application.id,
                Submission.status.in_(
                    (
                        SubmissionStatus.PENDING,
                        SubmissionStatus.RUNNING,
                        SubmissionStatus.SUCCESS,
                        SubmissionStatus.UNKNOWN,
                    )
                ),
            )
            .exists(),
        )
        .order_by(Job.score.desc(), Job.id.asc())
        .first()
    )
    return row.id if row else None


def expire_stale_jobs(db, now: datetime, ttl_days: int) -> int:
    cutoff = now - timedelta(days=ttl_days)
    rows = db.query(Job).filter(Job.status.in_(_STALE_STATUSES), Job.created_at < cutoff).all()
    for j in rows:
        j.status = JobStatus.SKIPPED
    db.commit()
    logger.info("expired_stale_jobs", count=len(rows))
    return len(rows)


@shared_task(name="worker.drainer.drain_apply_queue_task")
def drain_apply_queue_task() -> int:
    """Do not dispatch legacy approved rows without a one-use submit permit."""
    logger.info("drain_skipped", reason="SUBMIT_PERMIT_REQUIRED")
    return 0


@shared_task(name="worker.drainer.expire_stale_jobs_task")
def expire_stale_jobs_task() -> int:
    from core.config import get_settings  # noqa: PLC0415
    from db.session import get_session_factory  # noqa: PLC0415

    db = get_session_factory()()
    try:
        return expire_stale_jobs(db, datetime.utcnow(), get_settings().queue_ttl_days)
    finally:
        db.close()


@shared_task(name="worker.drainer.reconcile_stale_attempts_task")
def reconcile_stale_attempts_task() -> int:
    from db.session import get_session_factory  # noqa: PLC0415
    from worker.submission_attempts import mark_stale_attempts_unknown  # noqa: PLC0415

    db = get_session_factory()()
    try:
        return mark_stale_attempts_unknown(db)
    finally:
        db.close()
