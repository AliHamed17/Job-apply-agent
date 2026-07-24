"""Scheduled LinkedIn discovery task."""

from __future__ import annotations

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
    if not ok:
        logger.info("discovery_skipped", reason=reason)
        return 0
    from db.session import get_session_factory  # noqa: PLC0415
    from profile.loader import get_profile      # noqa: PLC0415
    from discovery.linkedin_search import run_discovery  # noqa: PLC0415

    settings = get_settings()
    db = get_session_factory()()
    try:
        return run_async(run_discovery(db, get_profile(), settings, gov))
    finally:
        db.close()
