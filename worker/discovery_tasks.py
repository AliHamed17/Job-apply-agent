"""Scheduled LinkedIn discovery task."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from celery import shared_task

from core.config import get_settings
from core.governor import get_governor
from core.utils import run_async

logger = structlog.get_logger(__name__)


@shared_task(name="worker.discovery_tasks.discover_jobs_task")
def discover_jobs_task() -> int:
    gov = get_governor()
    ok, reason = gov.can_act()
    if not ok and "kill switch" in reason:
        logger.info("discovery_skipped", reason=reason)
        return 0

    from profile.loader import get_profile  # noqa: PLC0415
    from profile.readiness import (  # noqa: PLC0415
        profile_discovery_readiness_issues,
    )

    from core.automation_readiness import current_automation_readiness  # noqa: PLC0415
    from core.operational_metrics import record_discovery_result  # noqa: PLC0415
    from core.operations import readiness_report  # noqa: PLC0415
    from db.models import DiscoveryRun  # noqa: PLC0415
    from db.session import get_session_factory  # noqa: PLC0415
    from discovery.ingest import ingest_discovered_jobs  # noqa: PLC0415
    from discovery.linkedin_search import run_discovery  # noqa: PLC0415
    from discovery.public_sources import fetch_remotive_jobs  # noqa: PLC0415
    from worker.rescore import requeue_scored_jobs_for_preparation  # noqa: PLC0415

    settings = get_settings()
    if not settings.discovery_enabled:
        logger.info("discovery_skipped", reason="DISCOVERY_DISABLED")
        return 0
    db = get_session_factory()()
    profile = get_profile()
    inserted = 0

    def start_run(source: str) -> DiscoveryRun:
        run = DiscoveryRun(source=source, status="running", inserted=0)
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def finish_run(
        run: DiscoveryRun, status: str, count: int = 0, reason_code: str | None = None
    ) -> None:
        run.status = status
        run.inserted = count
        run.reason_code = reason_code
        run.finished_at = datetime.now(UTC).replace(tzinfo=None)
        record_discovery_result(db, run, occurred_at=run.finished_at)
        db.commit()

    try:
        readiness_issues = profile_discovery_readiness_issues(profile)
        if readiness_issues:
            logger.warning(
                "discovery_profile_not_ready",
                reason_codes=readiness_issues,
            )
            for source in ("remotive", "linkedin_search"):
                run = start_run(source)
                finish_run(
                    run,
                    "blocked",
                    reason_code=readiness_issues[0],
                )
            return 0

        try:
            dependency_report = readiness_report(settings)
            automation = current_automation_readiness(
                settings=settings,
                dependency_report=dependency_report,
                db=db,
            )
            preparation_ready = settings.auto_apply and automation["preparation_ready"] is True
            preparation_reasons = automation["stages"]["preparation"]["reason_codes"]
            if not settings.auto_apply:
                preparation_reasons = ["AUTO_PREPARE_DISABLED"]
        except Exception:
            preparation_ready = False
            preparation_reasons = ["PREPARATION_READINESS_UNAVAILABLE"]
        if not preparation_ready:
            logger.info(
                "automatic_preparation_blocked",
                reason_codes=preparation_reasons,
            )
        else:
            requeue_scored_jobs_for_preparation(
                db,
                tasks_always_eager=settings.tasks_always_eager,
            )

        if settings.public_discovery_enabled:
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                hours=max(1, settings.public_discovery_interval_h)
            )
            recent_public = (
                db.query(DiscoveryRun)
                .filter(
                    DiscoveryRun.source == "remotive",
                    DiscoveryRun.finished_at >= cutoff,
                )
                .first()
            )
            if recent_public is None:
                public_run = start_run("remotive")
                try:
                    public_jobs = run_async(fetch_remotive_jobs(profile, settings))
                    public_count = ingest_discovered_jobs(
                        db,
                        public_jobs,
                        source="remotive",
                        easy_apply=False,
                        tasks_always_eager=settings.tasks_always_eager,
                        preparation_ready=preparation_ready,
                    )
                    inserted += public_count
                    finish_run(public_run, "success", public_count)
                except Exception:
                    db.rollback()
                    logger.exception("public_discovery_failed", source="remotive")
                    finish_run(public_run, "failed", reason_code="SOURCE_UNAVAILABLE")

        linkedin_run = start_run("linkedin_search")
        if not ok:
            finish_run(
                linkedin_run,
                "skipped",
                reason_code="GOVERNOR_DENIED",
            )
            return inserted

        try:
            linkedin_count = run_async(
                run_discovery(
                    db,
                    profile,
                    settings,
                    gov,
                    preparation_ready=preparation_ready,
                )
            )
            inserted += linkedin_count
            in_cooldown = bool(gov.status().get("in_cooldown"))
            finish_run(
                linkedin_run,
                "challenge" if in_cooldown else "success",
                linkedin_count,
                "CHALLENGE_DETECTED" if in_cooldown else None,
            )
        except Exception:
            db.rollback()
            logger.exception("linkedin_discovery_failed")
            finish_run(linkedin_run, "failed", reason_code="SOURCE_UNAVAILABLE")
        return inserted
    finally:
        db.close()
