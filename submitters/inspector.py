"""Submitters diagnostic and browser automation health inspector."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog
from core.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass
class SubmitterHealthReport:
    playwright_installed: bool
    live_auto_apply_active: bool
    auto_apply_threshold: float
    cv_alignment_enabled: bool
    registered_platforms: list[str] = field(default_factory=list)


def inspect_submitter_health() -> SubmitterHealthReport:
    """Inspect live submitters, browser availability, and system readiness."""
    settings = get_settings()

    playwright_ok = False
    try:
        import playwright  # noqa: F401
        playwright_ok = True
    except ImportError:
        playwright_ok = False

    platforms = [
        "linkedin_v2",
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "smartrecruiters",
        "jobvite",
        "indeed",
        "icims",
        "comeet",
    ]

    return SubmitterHealthReport(
        playwright_installed=playwright_ok,
        live_auto_apply_active=settings.auto_apply,
        auto_apply_threshold=settings.auto_apply_threshold,
        cv_alignment_enabled=settings.llm_cv_alignment,
        registered_platforms=platforms,
    )
