"""Bounded readiness for discovery, preparation, and employer submission."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from profile.cv_routing import load_routing_config
from profile.models import UserProfile
from profile.readiness import (
    profile_discovery_readiness_issues,
    profile_preparation_readiness_issues,
    profile_submission_readiness_issues,
)
from typing import Any

from core.config import Settings
from submitters.platforms import AdapterDescriptor, registered_adapters

_SUBMISSION_DEPENDENCIES = (
    "database",
    "migration",
    "redis",
    "worker",
    "beat",
    "shared_storage",
    "browser",
    "llm",
)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _check_ok(report: Mapping[str, Any], name: str) -> bool:
    checks = report.get("checks")
    if not isinstance(checks, Mapping):
        return False
    value = checks.get(name)
    return bool(value.get("ok")) if isinstance(value, Mapping) else bool(value)


def _routing_issues(settings: Settings) -> list[str]:
    routing_path = Path(settings.cv_routing_path)
    cv_root = Path(settings.cv_directory).resolve()
    if not routing_path.is_file():
        return ["CV_ROUTING_CONFIG_MISSING"]
    try:
        config = load_routing_config(routing_path)
    except (OSError, ValueError):
        return ["CV_ROUTING_CONFIG_INVALID"]
    if not config.cvs:
        return ["CV_ROUTING_CONFIG_EMPTY"]
    if not cv_root.is_dir() or not os.access(cv_root, os.R_OK):
        return ["CV_STORAGE_UNAVAILABLE"]

    missing = False
    for cv in config.cvs:
        candidate = (cv_root / cv.file).resolve()
        try:
            candidate.relative_to(cv_root)
        except ValueError:
            return ["CV_ROUTING_PATH_UNSAFE"]
        try:
            if (
                candidate.suffix.casefold() != ".pdf"
                or not candidate.is_file()
                or candidate.stat().st_size <= 0
            ):
                missing = True
        except OSError:
            missing = True
    return ["CV_ARTIFACTS_MISSING"] if missing else []


def _stage(reason_codes: Iterable[str]) -> dict[str, Any]:
    bounded = _unique(reason_codes)
    return {"ready": not bounded, "reason_codes": bounded}


def build_automation_readiness(
    *,
    settings: Settings,
    dependency_report: Mapping[str, Any],
    profile: UserProfile,
    profile_version: int | None,
    adapters: Iterable[AdapterDescriptor] | None = None,
) -> dict[str, Any]:
    """Return privacy-safe stage readiness without exposing candidate values."""

    discovery_reasons = profile_discovery_readiness_issues(profile)
    preparation_reasons = [
        *profile_preparation_readiness_issues(profile),
        *(
            []
            if profile_version is not None and profile_version >= 1
            else ["PROFILE_VERSION_MISSING"]
        ),
        *_routing_issues(settings),
    ]
    if not _check_ok(dependency_report, "llm"):
        preparation_reasons.append("LLM_NOT_READY")
    if not _check_ok(dependency_report, "shared_storage"):
        preparation_reasons.append("SHARED_STORAGE_UNAVAILABLE")

    submission_reasons = [
        *profile_submission_readiness_issues(profile),
        *preparation_reasons,
    ]
    if not settings.db_is_postgres:
        submission_reasons.append("POSTGRESQL_REQUIRED")
    for dependency in _SUBMISSION_DEPENDENCIES:
        if not _check_ok(dependency_report, dependency):
            submission_reasons.append(f"{dependency.upper()}_NOT_READY")
    if settings.dry_run:
        submission_reasons.append("DRY_RUN_ENABLED")
    if settings.draft_only:
        submission_reasons.append("DRAFT_ONLY_ENABLED")
    if not settings.portal_final_submit_enabled:
        submission_reasons.append("FINAL_SUBMIT_DISABLED")
    if not settings.live_automation_acknowledged:
        submission_reasons.append("LIVE_AUTOMATION_NOT_ACKNOWLEDGED")
    if not settings.operator_auth_configured:
        submission_reasons.append("OPERATOR_AUTH_REQUIRED")

    inventory = tuple(adapters if adapters is not None else registered_adapters())
    if not any(descriptor.allows_final_execution for descriptor in inventory):
        submission_reasons.append("ADAPTER_NOT_QUALIFIED")

    browser_root = Path(settings.portal_browser_profile_root)
    if not (browser_root.is_dir() and os.access(browser_root, os.R_OK | os.W_OK)):
        submission_reasons.append("BROWSER_PROFILE_STORAGE_UNAVAILABLE")

    stages = {
        "discovery": _stage(discovery_reasons),
        "preparation": _stage(preparation_reasons),
        "submission": _stage(submission_reasons),
    }
    return {
        "discovery_ready": stages["discovery"]["ready"],
        "preparation_ready": stages["preparation"]["ready"],
        "submission_ready": stages["submission"]["ready"],
        "stages": stages,
    }
