"""Run local discovery on a schedule without requiring Redis or Celery Beat.

This convenience runner is intentionally qualification-only. It refuses to
start unless both DRY_RUN and DRAFT_ONLY are enabled.
"""

from __future__ import annotations

import signal
import threading
from profile.loader import get_profile
from profile.readiness import profile_readiness_issues

import structlog

from core.config import Settings, get_settings
from worker.discovery_tasks import discover_jobs_task

logger = structlog.get_logger(__name__)
_stop = threading.Event()


def validate_safe_mode(settings: Settings) -> None:
    if not settings.dry_run or not settings.draft_only:
        raise RuntimeError(
            "Safe automation requires DRY_RUN=true and DRAFT_ONLY=true"
        )


def _request_stop(_signum, _frame) -> None:
    _stop.set()


def main() -> int:
    settings = get_settings()
    validate_safe_mode(settings)
    readiness_issues = profile_readiness_issues(get_profile())
    if readiness_issues:
        raise RuntimeError(
            "Profile is not ready for automation: " + ", ".join(readiness_issues)
        )
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    interval_seconds = max(1, settings.discovery_interval_h) * 3600
    logger.info(
        "safe_automation_started",
        interval_seconds=interval_seconds,
        final_submission_enabled=False,
    )

    while not _stop.is_set():
        try:
            inserted = discover_jobs_task()
            logger.info("safe_automation_cycle_complete", inserted=inserted)
        except Exception:
            logger.exception("safe_automation_cycle_failed")
        _stop.wait(interval_seconds)
    logger.info("safe_automation_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
