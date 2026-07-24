"""Priority drain of the approved-application queue + stale-job expiry."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from celery import shared_task

from db.models import Application, Job, JobStatus, Submission

logger = structlog.get_logger(__name__)

_STALE_STATUSES = (JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.DRAFT)


def select_next_application(db) -> int | None:
    """Highest Job.score among APPROVED applications; ties → lowest job id.

    Defensive belt-and-suspenders: excludes any Application that already
    has a Submission row, even if it is (incorrectly) still APPROVED —
    e.g. a stray status left by a bug elsewhere. Without this, such a
    row would be re-selected and re-submitted on every drain tick,
    tripping the Submission.application_id UNIQUE constraint.
    """
    row = (
        db.query(Application)
        .join(Job, Application.job_id == Job.id)
        .outerjoin(Submission, Submission.application_id == Application.id)
        .filter(Application.status == JobStatus.APPROVED, Submission.id.is_(None))
        .order_by(Job.score.desc(), Job.id.asc())
        .first()
    )
    return row.id if row else None


def expire_stale_jobs(db, now: datetime, ttl_days: int) -> int:
    cutoff = now - timedelta(days=ttl_days)
    rows = (
        db.query(Job)
        .filter(Job.status.in_(_STALE_STATUSES), Job.created_at < cutoff)
        .all()
    )
    for j in rows:
        j.status = JobStatus.SKIPPED
    db.commit()
    logger.info("expired_stale_jobs", count=len(rows))
    return len(rows)


@shared_task(name="worker.drainer.drain_apply_queue_task")
def drain_apply_queue_task() -> int:
    from core.governor import get_governor          # noqa: PLC0415
    from db.session import get_session_factory      # noqa: PLC0415
    from worker.tasks import submit_application_task  # noqa: PLC0415

    gov = get_governor()
    ok, reason = gov.can_act()
    if not ok:
        logger.info("drain_skipped", reason=reason)
        return 0
    db = get_session_factory()()
    try:
        app_id = select_next_application(db)
        if app_id is None:
            return 0
        submit_application_task.apply(args=[app_id])  # governor.record_application in submit path
        return 1
    finally:
        db.close()


@shared_task(name="worker.drainer.expire_stale_jobs_task")
def expire_stale_jobs_task() -> int:
    from core.config import get_settings            # noqa: PLC0415
    from db.session import get_session_factory       # noqa: PLC0415

    db = get_session_factory()()
    try:
        return expire_stale_jobs(db, datetime.utcnow(), get_settings().queue_ttl_days)
    finally:
        db.close()
