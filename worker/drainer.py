"""Priority drain of the approved-application queue + stale-job expiry."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from celery import shared_task

from core.application_mutations import (
    ApplicationMutationBlockedError,
    ApplicationMutationIntent,
    lock_application_for_mutation,
    lock_job_without_application_for_mutation,
    transition_locked_application_to_skipped,
)
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
    job_ids = [
        row[0]
        for row in db.query(Job.id)
        .filter(Job.status.in_(_STALE_STATUSES), Job.created_at < cutoff)
        .order_by(Job.id)
        .all()
    ]
    # Release the candidate-scan transaction. Each mutation below takes its
    # own app-first lock and rechecks status before writing.
    db.rollback()

    expired = 0
    for job_id in job_ids:
        try:
            locked = lock_application_for_mutation(
                db,
                job_id=job_id,
                intent=ApplicationMutationIntent.TERMINAL,
                allow_missing=True,
            )
        except ApplicationMutationBlockedError as exc:
            db.rollback()
            logger.info(
                "stale_job_expiry_blocked",
                job_id=job_id,
                reason_code=exc.reason_code,
            )
            continue

        if locked is not None:
            job = locked.job
            if job is None or job.status not in _STALE_STATUSES or job.created_at >= cutoff:
                db.rollback()
                continue
            transition_locked_application_to_skipped(
                db,
                locked,
                actor="system",
                reason_code="COMMAND_EXPIRED",
                rejection_reason="Expired from stale application queue",
                event_type="application_expired",
                now=now,
            )
            db.commit()
            expired += 1
            continue

        # Jobs without application content can still expire. The shared helper
        # proves absence before taking the Job lock and uses a non-blocking
        # second check to avoid a Job->Application deadlock if content appears.
        try:
            job = lock_job_without_application_for_mutation(
                db,
                job_id=job_id,
                intent=ApplicationMutationIntent.TERMINAL,
            )
        except ApplicationMutationBlockedError as exc:
            db.rollback()
            logger.info(
                "stale_job_expiry_blocked",
                job_id=job_id,
                reason_code=exc.reason_code,
            )
            continue
        if job.status not in _STALE_STATUSES or job.created_at >= cutoff:
            db.rollback()
            continue
        job.status = JobStatus.SKIPPED
        db.commit()
        expired += 1

    logger.info("expired_stale_jobs", count=expired)
    return expired


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
    """Compatibility alias for the database-command-aware reconciler.

    The former implementation inspected only the legacy ``status`` and
    ``started_at`` fields.  Running it beside the durable command reconciler
    could therefore mutate a command-backed attempt without atomically
    updating its outbox row.  Keep the task name for queued messages, but make
    the command lifecycle authoritative.
    """
    from db.session import get_session_factory  # noqa: PLC0415
    from worker.submission_commands import (  # noqa: PLC0415
        reconcile_stale_submission_commands,
    )

    db = get_session_factory()()
    try:
        return reconcile_stale_submission_commands(db)
    finally:
        db.close()
