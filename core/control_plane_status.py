"""Build the minimal redacted operations summary sent to the cloud control plane."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

from sqlalchemy import case, func

from core.adapter_qualification_service import effective_registered_descriptors
from core.application_state import prepared_application_count
from core.automation_policy_service import policy_usage_status
from core.submission_truth import latest_employer_verified_count
from db.models import (
    Application,
    DiscoveryRun,
    DiscoverySourceState,
    Job,
    JobFitDecisionRecord,
    JobSourceOccurrenceRecord,
    JobStatus,
)

_SOURCE_CODES = frozenset(
    {
        "ashby",
        "generic_feed",
        "generic_jsonld",
        "gmail_alert",
        "greenhouse",
        "lever",
        "linkedin_partner",
        "remotive",
        "smartrecruiters",
    }
)
_ADAPTER_CODES = frozenset({"ashby", "greenhouse", "lever", "smartrecruiters", "workday"})
_MAX_COUNTER = 2_147_483_647


class ControlPlaneStatus(TypedDict):
    pipeline: dict[str, int]
    policy: dict[str, Any]
    sources: list[dict[str, object]]
    adapters: list[dict[str, object]]


def _bounded_count(value: object, *, maximum: int = _MAX_COUNTER) -> int:
    try:
        parsed = int(str(value or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(parsed, maximum))


def _aware_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _latest_fit_query(db):
    latest = (
        db.query(
            JobFitDecisionRecord.job_id.label("job_id"),
            func.max(JobFitDecisionRecord.id).label("latest_id"),
        )
        .group_by(JobFitDecisionRecord.job_id)
        .subquery()
    )
    return db.query(JobFitDecisionRecord).join(
        latest,
        JobFitDecisionRecord.id == latest.c.latest_id,
    )


def _pipeline(db) -> dict[str, int]:
    return {
        "discovered": _bounded_count(db.query(Job.id).count()),
        "source_occurrences": _bounded_count(db.query(JobSourceOccurrenceRecord.id).count()),
        "deduplicated": _bounded_count(
            db.query(func.coalesce(func.sum(DiscoveryRun.duplicates), 0)).scalar()
        ),
        "eligible": _bounded_count(
            _latest_fit_query(db).filter(JobFitDecisionRecord.quality_eligible.is_(True)).count()
        ),
        "prepared": _bounded_count(prepared_application_count(db)),
        "quarantined": _bounded_count(
            db.query(Application.id).filter(Application.status == JobStatus.NEEDS_REVIEW).count()
        ),
        "employer_confirmed": _bounded_count(latest_employer_verified_count(db)),
    }


def _sources(db) -> list[dict[str, object]]:
    enabled_count = func.sum(case((DiscoverySourceState.enabled.is_(True), 1), else_=0)).label(
        "enabled_count"
    )
    degraded_count = func.sum(
        case((DiscoverySourceState.health_status == "degraded", 1), else_=0)
    ).label("degraded_count")
    healthy_count = func.sum(
        case((DiscoverySourceState.health_status == "healthy", 1), else_=0)
    ).label("healthy_count")
    rows = (
        db.query(
            DiscoverySourceState.source_type,
            func.count(DiscoverySourceState.id).label("source_count"),
            enabled_count,
            degraded_count,
            healthy_count,
        )
        .filter(DiscoverySourceState.source_type.in_(tuple(sorted(_SOURCE_CODES))))
        .group_by(DiscoverySourceState.source_type)
        .order_by(DiscoverySourceState.source_type)
        .all()
    )
    result: list[dict[str, object]] = []
    for source, total, enabled, degraded, healthy in rows:
        total_count = _bounded_count(total)
        enabled_total = _bounded_count(enabled)
        if enabled_total == 0:
            status = "disabled"
        elif _bounded_count(degraded) > 0:
            status = "degraded"
        elif _bounded_count(healthy) == enabled_total:
            status = "healthy"
        else:
            status = "unknown"
        result.append(
            {
                "source": str(source),
                "status": status,
                "enabled_count": enabled_total,
                "source_count": total_count,
            }
        )
    return result


def _adapters(db) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for descriptor in sorted(
        effective_registered_descriptors(db),
        key=lambda item: item.platform,
    ):
        if descriptor.platform not in _ADAPTER_CODES:
            continue
        rows.append(
            {
                "adapter": descriptor.platform,
                "qualification_tier": str(descriptor.qualification.value),
                "final_execution_enabled": bool(descriptor.allows_final_execution),
                "qualified_form_scope_count": _bounded_count(len(descriptor.qualified_form_scope)),
            }
        )
    return rows


def _policy(db, *, now: datetime) -> dict[str, Any]:
    try:
        status = policy_usage_status(db, now=now)
    except Exception:
        status = {
            "active": False,
            "reason_code": "AUTOMATION_STATUS_UNAVAILABLE",
            "kill_switch_active": False,
        }
    kill_switch_active = bool(status.get("kill_switch_active"))
    if bool(status.get("active")) and not kill_switch_active:
        state = "active"
    elif status.get("reason_code") == "AUTOMATION_POLICY_NOT_ACTIVE":
        state = "inactive"
    else:
        state = "blocked"
    expires_at = _aware_timestamp(status.get("expires_at"))
    if state == "active" and expires_at is None:
        state = "blocked"
    return {
        "state": state,
        "revision": _bounded_count(status.get("revision")),
        "expires_at": expires_at,
        "daily_remaining": _bounded_count(status.get("daily_remaining"), maximum=25),
        "hourly_remaining": _bounded_count(status.get("hourly_remaining"), maximum=5),
        "kill_switch_active": kill_switch_active,
    }


def build_control_plane_status(
    db,
    *,
    now: datetime | None = None,
) -> ControlPlaneStatus:
    """Return only the finite fields admitted by the signed heartbeat protocol."""

    timestamp = _aware_timestamp(now or datetime.now(UTC))
    assert timestamp is not None
    return {
        "pipeline": _pipeline(db),
        "policy": _policy(db, now=timestamp),
        "sources": _sources(db),
        "adapters": _adapters(db),
    }


__all__ = ["build_control_plane_status"]
