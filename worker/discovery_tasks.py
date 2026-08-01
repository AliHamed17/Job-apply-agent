"""Scheduled v5 discovery mesh controller."""

from __future__ import annotations

import structlog
from celery import shared_task

from core.config import get_settings
from core.governor import get_governor
from core.utils import run_async

logger = structlog.get_logger(__name__)


def _load_discovery_profile(settings, db):
    """Bind one discovery run to the authoritative immutable profile."""

    from profile.loader import load_profile_snapshot
    from profile.versioned_snapshot import (
        latest_profile_version,
        load_versioned_profile_snapshot,
    )

    profile_version = latest_profile_version(db)
    if profile_version is None:
        return load_profile_snapshot(settings.profile_path), None
    snapshot = load_versioned_profile_snapshot(db, version=profile_version)
    return snapshot.profile, snapshot.version


def _ensure_active_search_intents(db, settings, profile) -> int:
    """Auto-activate the initial CV-derived scope; later changes are explicit."""

    from profile.cv_routing import load_routing_config

    from discovery.search_intents import (
        activate_search_intents,
        active_search_intents,
        derive_search_intents,
    )

    version, intents = active_search_intents(db)
    if version is not None and intents:
        return version
    routing = load_routing_config(settings.cv_routing_path)
    derived = derive_search_intents(
        routing,
        profile_locations=profile.preferences.locations,
    )
    return int(activate_search_intents(db, derived).version)


@shared_task(name="worker.discovery_tasks.discover_jobs_task")
def discover_jobs_task(force: bool = False, source_key: str | None = None) -> int:
    """Poll due public feeds and local alerts; never crawl LinkedIn."""

    governor = get_governor()
    allowed, reason = governor.can_act()
    if not allowed and "kill switch" in reason:
        logger.info("discovery_skipped", reason=reason)
        return 0

    from core.automation_readiness import build_automation_readiness  # noqa: PLC0415
    from core.operations import readiness_report  # noqa: PLC0415
    from db.session import get_session_factory  # noqa: PLC0415
    from discovery.mesh import run_discovery_mesh  # noqa: PLC0415
    from discovery.settings import get_discovery_settings  # noqa: PLC0415
    from worker.rescore import requeue_scored_jobs_for_preparation  # noqa: PLC0415

    settings = get_settings()
    db = get_session_factory()()
    try:
        profile, profile_version = _load_discovery_profile(settings, db)
        try:
            dependency_report = readiness_report(
                settings,
                require_storage_write=False,
            )
            automation = build_automation_readiness(
                settings=settings,
                dependency_report=dependency_report,
                profile=profile,
                profile_version=profile_version,
            )
            preparation_ready = settings.auto_apply and automation["preparation_ready"] is True
            preparation_reasons = automation["stages"]["preparation"]["reason_codes"]
            if not settings.auto_apply:
                preparation_reasons = ["AUTO_PREPARE_DISABLED"]
        except Exception:
            preparation_ready = False
            preparation_reasons = ["PREPARATION_READINESS_UNAVAILABLE"]
        if preparation_ready:
            requeue_scored_jobs_for_preparation(
                db,
                tasks_always_eager=settings.tasks_always_eager,
                batch_size=settings.preparation_requeue_batch_size,
            )
        else:
            logger.info(
                "automatic_preparation_blocked",
                reason_codes=preparation_reasons,
            )

        if not settings.discovery_enabled:
            logger.info("discovery_skipped", reason="DISCOVERY_DISABLED")
            return 0
        try:
            _ensure_active_search_intents(db, settings, profile)
        except Exception as exc:
            db.rollback()
            logger.warning(
                "discovery_search_intent_blocked",
                reason_code=(
                    str(exc)
                    if str(exc).isupper() and len(str(exc)) <= 64
                    else "SEARCH_INTENT_CONFIGURATION_INVALID"
                ),
            )
            return 0

        result = run_async(
            run_discovery_mesh(
                db,
                settings=get_discovery_settings(),
                profile=profile,
                preparation_ready=preparation_ready,
                force=force,
                source_filter=source_key,
            )
        )
        return int(result["inserted"])
    finally:
        db.close()
