"""Re-score queued jobs against the current profile."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

import structlog

from db.models import Application, Job, JobStatus
from jobs.models import JobData
from match.scoring import score_job

logger = structlog.get_logger(__name__)

_RESCORE_STATUSES = (JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.DRAFT)
_EAGER_RESCORE_LOCK = threading.Lock()
_EAGER_RESCORE_THREADS: dict[int, threading.Thread] = {}


@dataclass(frozen=True, slots=True)
class RescoreBatchResult:
    """One bounded exact-profile rescore page."""

    updated: int
    last_job_id: int
    has_more: bool
    superseded: bool = False


def _score_value(job: Any, profile: Any) -> float:
    job_data = JobData(
        title=job.title,
        company=job.company or "",
        location=job.location or "",
        employment_type=job.employment_type or "",
        seniority=job.seniority or "",
        description=job.description or "",
        requirements=job.requirements or "",
        apply_url=job.apply_url or "",
        source_url=job.source_url,
        date_posted=job.date_posted or "",
        keywords=json.loads(job.keywords) if job.keywords else [],
    )
    return score_job(job_data, profile).total


def rescore_pending_jobs(db, profile) -> int:
    """Synchronously re-score not-yet-submitted jobs for explicit maintenance."""

    rows = db.query(Job).filter(Job.status.in_(_RESCORE_STATUSES)).all()
    for job in rows:
        job.score = _score_value(job, profile)
    db.commit()
    logger.info("rescored_pending_jobs", count=len(rows))
    return len(rows)


def rescore_pending_jobs_batch(
    db,
    profile,
    *,
    expected_profile_version: int,
    after_job_id: int,
    batch_size: int,
) -> RescoreBatchResult:
    """Compute outside the profile lock, then commit one exact-version page."""

    if not 1 <= batch_size <= 100:
        raise ValueError("rescore batch size must be between 1 and 100")
    rows = (
        db.query(Job)
        .filter(
            Job.status.in_(_RESCORE_STATUSES),
            Job.id > after_job_id,
        )
        .order_by(Job.id)
        .limit(batch_size + 1)
        .all()
    )
    selected = rows[:batch_size]
    has_more = len(rows) > batch_size
    if not selected:
        db.rollback()
        return RescoreBatchResult(
            updated=0,
            last_job_id=after_job_id,
            has_more=False,
        )

    scores = {int(job.id): _score_value(job, profile) for job in selected}
    last_job_id = max(scores)
    db.rollback()

    from profile.versioned_snapshot import latest_profile_version  # noqa: PLC0415
    from profile.writer import profile_write_transaction  # noqa: PLC0415

    with profile_write_transaction(db):
        current_profile_version = latest_profile_version(db)
        if current_profile_version != expected_profile_version:
            db.rollback()
            logger.info(
                "pending_job_rescore_batch_superseded",
                expected_profile_version=expected_profile_version,
                current_profile_version=current_profile_version,
            )
            return RescoreBatchResult(
                updated=0,
                last_job_id=last_job_id,
                has_more=False,
                superseded=True,
            )
        live_rows = (
            db.query(Job)
            .filter(
                Job.id.in_(scores),
                Job.status.in_(_RESCORE_STATUSES),
            )
            .all()
        )
        for job in live_rows:
            job.score = scores[int(job.id)]
        db.commit()

    logger.info(
        "pending_job_rescore_batch_completed",
        count=len(live_rows),
        expected_profile_version=expected_profile_version,
        last_job_id=last_job_id,
        has_more=has_more,
    )
    return RescoreBatchResult(
        updated=len(live_rows),
        last_job_id=last_job_id,
        has_more=has_more,
    )


def enqueue_pending_job_rescore(
    db,
    settings,
    *,
    expected_profile_version: int,
) -> int:
    """Queue a bounded background rescore controller; return affected row count."""

    pending_count = db.query(Job.id).filter(Job.status.in_(_RESCORE_STATUSES)).count()
    db.rollback()
    if pending_count == 0:
        return 0

    try:
        if settings.tasks_always_eager:
            _start_eager_pending_job_rescore(
                expected_profile_version=expected_profile_version,
                batch_size=settings.preparation_requeue_batch_size,
            )
        else:
            tasks_module = cast(Any, import_module("worker.tasks"))
            task = tasks_module.rescore_pending_jobs_task
            task.delay(expected_profile_version, 0)
    except Exception:
        logger.warning(
            "pending_job_rescore_queue_failed",
            reason_code="RESCORE_QUEUE_UNAVAILABLE",
            expected_profile_version=expected_profile_version,
        )
        return 0
    logger.info(
        "pending_job_rescore_queued",
        count=pending_count,
        expected_profile_version=expected_profile_version,
    )
    return int(pending_count)


def _drain_eager_pending_job_rescore(
    *,
    expected_profile_version: int,
    batch_size: int,
) -> int:
    """Iteratively drain brokerless local rescoring outside the request."""

    from profile.versioned_snapshot import (  # noqa: PLC0415
        latest_profile_version,
        load_versioned_profile_snapshot,
    )

    from db.session import get_session_factory  # noqa: PLC0415

    factory = get_session_factory()
    bootstrap = factory()
    try:
        if latest_profile_version(bootstrap) != expected_profile_version:
            bootstrap.rollback()
            return 0
        profile = load_versioned_profile_snapshot(
            bootstrap,
            version=expected_profile_version,
        ).profile
        bootstrap.rollback()
    finally:
        bootstrap.close()

    updated = 0
    after_job_id = 0
    while True:
        db = factory()
        try:
            result = rescore_pending_jobs_batch(
                db,
                profile,
                expected_profile_version=expected_profile_version,
                after_job_id=after_job_id,
                batch_size=batch_size,
            )
        finally:
            db.close()
        updated += result.updated
        if result.superseded or not result.has_more:
            return updated
        after_job_id = result.last_job_id


def _start_eager_pending_job_rescore(
    *,
    expected_profile_version: int,
    batch_size: int,
) -> bool:
    """Start one lifecycle-managed drainer per immutable profile revision."""

    with _EAGER_RESCORE_LOCK:
        existing = _EAGER_RESCORE_THREADS.get(expected_profile_version)
        if existing is not None and existing.is_alive():
            return True
        _EAGER_RESCORE_THREADS.pop(expected_profile_version, None)

    def drain() -> None:
        try:
            _drain_eager_pending_job_rescore(
                expected_profile_version=expected_profile_version,
                batch_size=batch_size,
            )
        except Exception as exc:
            logger.error(
                "eager_pending_job_rescore_failed",
                reason_code=type(exc).__name__,
                expected_profile_version=expected_profile_version,
            )
        finally:
            with _EAGER_RESCORE_LOCK:
                current = _EAGER_RESCORE_THREADS.get(expected_profile_version)
                if current is threading.current_thread():
                    _EAGER_RESCORE_THREADS.pop(expected_profile_version, None)

    thread = threading.Thread(
        target=drain,
        name=f"profile-rescore-v{expected_profile_version}",
        daemon=False,
    )
    with _EAGER_RESCORE_LOCK:
        existing = _EAGER_RESCORE_THREADS.get(expected_profile_version)
        if existing is not None and existing.is_alive():
            return True
        _EAGER_RESCORE_THREADS[expected_profile_version] = thread
        try:
            thread.start()
        except Exception:
            _EAGER_RESCORE_THREADS.pop(expected_profile_version, None)
            raise
    return True


def recover_eager_pending_job_rescore(settings) -> int:
    """Replay the latest durable profile revision after a brokerless restart."""

    if not settings.tasks_always_eager:
        return 0

    from profile.versioned_snapshot import latest_profile_version  # noqa: PLC0415

    from db.session import get_session_factory  # noqa: PLC0415

    db = get_session_factory()()
    try:
        expected_profile_version = latest_profile_version(db)
        if expected_profile_version is None:
            db.rollback()
            return 0
        return enqueue_pending_job_rescore(
            db,
            settings,
            expected_profile_version=expected_profile_version,
        )
    finally:
        db.close()


def wait_for_eager_pending_job_rescores(timeout: float | None = None) -> bool:
    """Join all managed brokerless drainers during graceful API shutdown."""

    if timeout is not None and timeout < 0:
        raise ValueError("rescore shutdown timeout must be non-negative")
    deadline = None if timeout is None else time.monotonic() + timeout

    while True:
        with _EAGER_RESCORE_LOCK:
            threads = tuple(_EAGER_RESCORE_THREADS.values())
        if not threads:
            return True
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
            if thread.is_alive() and deadline is not None and time.monotonic() >= deadline:
                return False


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
        report = readiness_report(
            settings,
            require_storage_write=False,
        )
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
