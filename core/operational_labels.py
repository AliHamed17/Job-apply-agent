"""Finite, privacy-safe label normalization for operational reporting.

Every value crossing into Prometheus or the operations dashboard passes through
this module.  Unknown database values collapse to ``other`` rather than becoming
new labels, and no free text, URL, title, company, question, answer, or identity
is ever returned.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, case, func, or_

from core.submission_domain import (
    AnswerProvenance,
    AttemptOutcome,
    AttemptStage,
    EvidenceType,
    FieldType,
    ReasonCode,
)
from submitters.platforms import QualificationTier, registered_adapters

NONE_LABEL = "none"
OTHER_LABEL = "other"

OPERATIONAL_METRIC_NAMES = frozenset(
    {
        "attempt_stage",
        "attempt_outcome",
        "retry",
        "governor_denial",
        "discovery_result",
        "form_resolution",
        "attachment_result",
        "browser_failure",
        "outbound_result",
    }
)

_DESCRIPTORS = registered_adapters()
ATS_LABELS = frozenset(
    {
        NONE_LABEL,
        OTHER_LABEL,
        "unknown",
        "generic_portal",
        *(descriptor.platform for descriptor in _DESCRIPTORS),
    }
)
ADAPTER_VERSION_LABELS = frozenset(
    {
        NONE_LABEL,
        OTHER_LABEL,
        *(descriptor.adapter_version for descriptor in _DESCRIPTORS),
    }
)
SELECTOR_VERSION_LABELS = frozenset(
    {
        NONE_LABEL,
        OTHER_LABEL,
        *(descriptor.selector_version for descriptor in _DESCRIPTORS),
    }
)
STAGE_LABELS = frozenset({NONE_LABEL, OTHER_LABEL, *(item.value for item in AttemptStage)})
OUTCOME_LABELS = frozenset({NONE_LABEL, OTHER_LABEL, *(item.value for item in AttemptOutcome)})
FIELD_TYPE_LABELS = frozenset({NONE_LABEL, OTHER_LABEL, *(item.value for item in FieldType)})
RESOLVER_LABELS = frozenset({NONE_LABEL, OTHER_LABEL, *(item.value for item in AnswerProvenance)})
EVIDENCE_TYPE_LABELS = frozenset(
    {
        NONE_LABEL,
        OTHER_LABEL,
        "operator_confirmed",
        *(item.value for item in EvidenceType),
    }
)
ATTACHMENT_RESULT_LABELS = frozenset(
    {NONE_LABEL, OTHER_LABEL, "verified", "unverified", "not_applicable", "unknown"}
)
QUALIFICATION_TIER_LABELS = frozenset(
    {NONE_LABEL, OTHER_LABEL, *(item.value for item in QualificationTier)}
)

# Stable non-domain results already persisted by compatibility, discovery, and
# reconciliation paths.  Keeping the extension explicit prevents raw strings
# from silently becoming new public labels.
_EXTRA_REASON_CODES = {
    "NONE",
    "OTHER",
    "EMPLOYER_VERIFIED",
    "OPERATOR_CONFIRMED_SUBMITTED",
    "RECONCILED_NOT_SUBMITTED",
    "LEGACY_UNVERIFIED",
    "FORM_PLAN_READY",
    "PROFILE_INCOMPLETE",
    "SOURCE_UNAVAILABLE",
    "SAFE_PRECOMMIT_REDELIVERY",
    "APPLICATION_REJECTED",
    "APPLICATION_CANCELLED",
    "JOB_NOT_FOUND",
    "JOB_URL_INVALID",
    "ADAPTER_VERSION_CHANGED",
    "FORM_PLAN_EXPIRED",
    "SELECTED_CV_UNAVAILABLE",
    "DATABASE_COMMAND_REQUIRED",
    "COMMAND_STATE_INVALID",
    "DEPENDENCY_UNAVAILABLE",
    "HEARTBEAT_MISSING",
    "HEARTBEAT_STALE",
    "HEARTBEAT_INVALID",
    "MIGRATION_MISMATCH",
    "KILL_SWITCH",
    "COOLDOWN",
    "ACTIVE_HOURS",
    "DAILY_CAP",
    "MINIMUM_GAP",
    "POLICY",
    "SUCCESS",
    "FAILED",
    "BLOCKED",
    "SKIPPED",
    "CHALLENGE",
}
REASON_CODE_LABELS = frozenset({item.value for item in ReasonCode} | _EXTRA_REASON_CODES)

QUEUE_LABELS = (
    "urls_pending",
    "jobs_extracted",
    "jobs_scored",
    "applications_draft",
    "applications_prepared",
    "submission_commands_pending",
    "submission_commands_claimed",
    "submissions_queued",
    "submissions_inspecting",
    "submissions_preparing",
    "submissions_ready",
    "submissions_committing",
    "submissions_verifying",
    "submissions_unknown",
)

OPERATIONAL_LABEL_NAMES = (
    "metric",
    "ats",
    "adapter_version",
    "selector_version",
    "stage",
    "outcome",
    "reason_code",
    "field_type",
    "resolver",
    "attachment_result",
    "evidence_type",
)


def _finite(value: object, allowed: frozenset[str], *, missing: str = NONE_LABEL) -> str:
    if value is None:
        return missing
    candidate = str(getattr(value, "value", value) or "").strip()
    if not candidate:
        return missing
    return candidate if candidate in allowed else OTHER_LABEL


def normalize_metric_name(value: object) -> str:
    return _finite(value, OPERATIONAL_METRIC_NAMES)


def normalize_ats(value: object) -> str:
    return _finite(str(value or "").strip().lower() or None, ATS_LABELS)


def normalize_adapter_version(value: object, *, ats: object = None) -> str:
    normalized = _finite(value, ADAPTER_VERSION_LABELS)
    if normalized in {NONE_LABEL, OTHER_LABEL}:
        return normalized
    normalized_ats = normalize_ats(ats)
    if normalized_ats in {NONE_LABEL, OTHER_LABEL, "unknown", "generic_portal"}:
        return OTHER_LABEL
    descriptor = next(
        (item for item in _DESCRIPTORS if item.platform == normalized_ats),
        None,
    )
    return normalized if descriptor and descriptor.adapter_version == normalized else OTHER_LABEL


def normalize_selector_version(value: object, *, ats: object = None) -> str:
    normalized = _finite(value, SELECTOR_VERSION_LABELS)
    if normalized in {NONE_LABEL, OTHER_LABEL}:
        return normalized
    normalized_ats = normalize_ats(ats)
    if normalized_ats in {NONE_LABEL, OTHER_LABEL, "unknown", "generic_portal"}:
        return OTHER_LABEL
    descriptor = next(
        (item for item in _DESCRIPTORS if item.platform == normalized_ats),
        None,
    )
    return normalized if descriptor and descriptor.selector_version == normalized else OTHER_LABEL


def normalize_stage(value: object) -> str:
    return _finite(value, STAGE_LABELS)


def normalize_outcome(value: object) -> str:
    return _finite(value, OUTCOME_LABELS)


def normalize_reason_code(value: object) -> str:
    if value is None or not str(getattr(value, "value", value) or "").strip():
        return "NONE"
    candidate = str(getattr(value, "value", value)).strip().upper()
    return candidate if candidate in REASON_CODE_LABELS else "OTHER"


def normalize_field_type(value: object) -> str:
    return _finite(value, FIELD_TYPE_LABELS)


def normalize_resolver(value: object) -> str:
    return _finite(value, RESOLVER_LABELS)


def normalize_attachment_result(value: object) -> str:
    if isinstance(value, bool):
        return "verified" if value else "unverified"
    return _finite(value, ATTACHMENT_RESULT_LABELS)


def normalize_evidence_type(value: object) -> str:
    return _finite(value, EVIDENCE_TYPE_LABELS)


def normalize_qualification_tier(value: object) -> str:
    return _finite(value, QUALIFICATION_TIER_LABELS)


def finite_values(values: Iterable[object], normalizer) -> tuple[str, ...]:
    """Normalize, deduplicate, and deterministically order a label collection."""

    return tuple(sorted({normalizer(value) for value in values}))


def _sql_finite(
    column,
    allowed: frozenset[str],
    *,
    missing: str = NONE_LABEL,
    transform=None,
):
    """Normalize before SQL grouping so raw cardinality never reaches Python."""

    trimmed = func.trim(column)
    candidate = transform(trimmed) if transform is not None else trimmed
    return case(
        (or_(column.is_(None), trimmed == ""), missing),
        (candidate.in_(tuple(sorted(allowed))), candidate),
        else_=OTHER_LABEL,
    )


def sql_normalize_ats(column):
    return _sql_finite(column, ATS_LABELS, transform=func.lower)


def sql_normalize_metric_name(column):
    return _sql_finite(column, OPERATIONAL_METRIC_NAMES)


def sql_normalize_adapter_version(column, *, ats_expression):
    trimmed = func.trim(column)
    qualified_versions = [
        (
            and_(
                ats_expression == descriptor.platform,
                trimmed == descriptor.adapter_version,
            ),
            descriptor.adapter_version,
        )
        for descriptor in _DESCRIPTORS
    ]
    return case(
        (or_(column.is_(None), trimmed == ""), NONE_LABEL),
        (trimmed == NONE_LABEL, NONE_LABEL),
        (trimmed == OTHER_LABEL, OTHER_LABEL),
        *qualified_versions,
        else_=OTHER_LABEL,
    )


def sql_normalize_selector_version(column, *, ats_expression):
    trimmed = func.trim(column)
    qualified_versions = [
        (
            and_(
                ats_expression == descriptor.platform,
                trimmed == descriptor.selector_version,
            ),
            descriptor.selector_version,
        )
        for descriptor in _DESCRIPTORS
    ]
    return case(
        (or_(column.is_(None), trimmed == ""), NONE_LABEL),
        (trimmed == NONE_LABEL, NONE_LABEL),
        (trimmed == OTHER_LABEL, OTHER_LABEL),
        *qualified_versions,
        else_=OTHER_LABEL,
    )


def sql_normalize_stage(column):
    return _sql_finite(column, STAGE_LABELS)


def sql_normalize_outcome(column):
    return _sql_finite(column, OUTCOME_LABELS)


def sql_normalize_reason_code(column):
    trimmed = func.trim(column)
    candidate = func.upper(trimmed)
    return case(
        (or_(column.is_(None), trimmed == ""), "NONE"),
        (candidate.in_(tuple(sorted(REASON_CODE_LABELS))), candidate),
        else_="OTHER",
    )


def sql_normalize_field_type(column):
    return _sql_finite(column, FIELD_TYPE_LABELS)


def sql_normalize_resolver(column):
    return _sql_finite(column, RESOLVER_LABELS)


def sql_normalize_attachment_result(column):
    return _sql_finite(column, ATTACHMENT_RESULT_LABELS)


def sql_normalize_evidence_type(column):
    return _sql_finite(column, EVIDENCE_TYPE_LABELS)
