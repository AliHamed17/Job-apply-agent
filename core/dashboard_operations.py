"""Privacy-bounded operational snapshot for the protected dashboard."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func

from core.adapter_qualification_service import effective_registered_descriptors
from core.application_state import prepared_application_count
from core.automation_policy_service import policy_usage_status
from core.operational_labels import (
    QUEUE_LABELS,
    normalize_adapter_version,
    normalize_ats,
    normalize_attachment_result,
    normalize_evidence_type,
    normalize_field_type,
    normalize_outcome,
    normalize_qualification_tier,
    normalize_reason_code,
    normalize_resolver,
    normalize_selector_version,
    normalize_stage,
    sql_normalize_adapter_version,
    sql_normalize_ats,
    sql_normalize_attachment_result,
    sql_normalize_evidence_type,
    sql_normalize_field_type,
    sql_normalize_outcome,
    sql_normalize_reason_code,
    sql_normalize_resolver,
    sql_normalize_selector_version,
    sql_normalize_stage,
)
from core.operational_metrics import authoritative_queue_depths
from core.runtime_identity import get_runtime_identity
from core.submission_truth import employer_verified_sql_conditions, latest_employer_verified_count
from db.models import (
    Application,
    BrowserQualificationRun,
    DiscoveryRun,
    DiscoverySourceState,
    Job,
    JobFitDecisionRecord,
    JobSourceOccurrenceRecord,
    JobStatus,
    OperationalMetricEvent,
    Submission,
    SubmissionEvidence,
)
from match.job_fit_store import decision_from_record

_DEPENDENCIES = (
    "database",
    "migration",
    "redis",
    "worker",
    "beat",
    "shared_storage",
    "browser",
    "llm",
)
_SAFE_RUNTIME_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_SAFE_ROUTE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_TYPES = frozenset(
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
_SUCCESS_REASONS = frozenset(
    {
        "NONE",
        "EMPLOYER_VERIFIED",
        "FORM_PLAN_READY",
        "OPERATOR_CONFIRMED_SUBMITTED",
        "SUCCESS",
    }
)
_MAX_RESPONSE_ROWS = 100
_MAX_RECENT_ROWS = 25
_MAX_HEARTBEAT_AGE_SECONDS = 31 * 24 * 60 * 60


def _naive_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current
    return current.astimezone(UTC).replace(tzinfo=None)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _runtime_token(value: object, *, fallback: str = "unavailable") -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_RUNTIME_TOKEN.fullmatch(candidate) else fallback


def _route_token(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _SAFE_ROUTE_TOKEN.fullmatch(candidate) else "other"


def _source_token(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in _SOURCE_TYPES else "other"


def _digest_token(value: object) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if _SHA256_TOKEN.fullmatch(candidate) else None


def _latest_fit_rows(db):
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


def _dependency_reason(name: str, detail: dict[str, Any], *, ok: bool) -> str:
    if ok:
        return "NONE"
    explicit = normalize_reason_code(detail.get("reason_code"))
    if explicit not in {"NONE", "OTHER"}:
        return explicit
    raw_detail = str(detail.get("detail") or "").strip().lower()
    if name == "migration":
        return "MIGRATION_MISMATCH"
    if raw_detail == "missing":
        return "HEARTBEAT_MISSING"
    if raw_detail == "invalid":
        return "HEARTBEAT_INVALID"
    if "age_seconds" in detail:
        return "HEARTBEAT_STALE"
    return "DEPENDENCY_UNAVAILABLE"


def _dependency_rows(
    readiness: dict[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    raw_checks = readiness.get("checks")
    checks = raw_checks if isinstance(raw_checks, dict) else {}
    rows: list[dict[str, Any]] = []
    for name in _DEPENDENCIES:
        raw_detail = checks.get(name)
        detail = raw_detail if isinstance(raw_detail, dict) else {}
        ok = bool(detail.get("ok"))
        raw_age = detail.get("age_seconds")
        age_seconds: float | None = None
        if isinstance(raw_age, (int, float)) and math.isfinite(float(raw_age)):
            age_seconds = round(
                max(0.0, min(float(raw_age), _MAX_HEARTBEAT_AGE_SECONDS)),
                1,
            )
        rows.append(
            {
                "name": name,
                "ok": ok,
                "status": "ready" if ok else "degraded",
                "reason_code": _dependency_reason(name, detail, ok=ok),
                "age_seconds": age_seconds,
                "last_seen_at": (
                    _aware_utc(now - timedelta(seconds=age_seconds))
                    if age_seconds is not None
                    else None
                ),
            }
        )
    return rows


def _adapter_identity(
    ats: object,
    adapter_version: object,
    selector_version: object,
) -> tuple[str, str, str]:
    normalized_ats = normalize_ats(ats)
    return (
        normalized_ats,
        normalize_adapter_version(adapter_version, ats=normalized_ats),
        normalize_selector_version(selector_version, ats=normalized_ats),
    )


def _sql_adapter_identity(
    ats_column,
    adapter_version_column,
    selector_version_column,
):
    ats = sql_normalize_ats(ats_column).label("normalized_ats")
    adapter_version = sql_normalize_adapter_version(
        adapter_version_column,
        ats_expression=ats,
    ).label("normalized_adapter_version")
    selector_version = sql_normalize_selector_version(
        selector_version_column,
        ats_expression=ats,
    ).label("normalized_selector_version")
    return ats, adapter_version, selector_version


def _submission_ats_source():
    return func.coalesce(
        func.nullif(func.trim(Submission.adapter_name), ""),
        Submission.submitter_name,
    )


def _attempt_stage_rows(db, *, since: datetime) -> list[dict[str, Any]]:
    stage_expression = sql_normalize_stage(Submission.stage).label("normalized_stage")
    ats_expression, version_expression, selector_expression = _sql_adapter_identity(
        _submission_ats_source(),
        Submission.adapter_version,
        Submission.selector_version,
    )
    count_expression = func.count(Submission.id).label("event_count")
    rows = (
        db.query(
            stage_expression,
            ats_expression,
            version_expression,
            selector_expression,
            count_expression,
        )
        .filter(Submission.created_at >= since)
        .group_by(
            stage_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .order_by(
            stage_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    return [
        {
            "stage": normalize_stage(stage),
            "ats": normalize_ats(ats),
            "adapter_version": normalize_adapter_version(version, ats=ats),
            "selector_version": normalize_selector_version(selector, ats=ats),
            "count": int(count or 0),
        }
        for stage, ats, version, selector, count in rows
    ]


def _attempt_outcome_rows(db, *, since: datetime) -> list[dict[str, Any]]:
    outcome_expression = sql_normalize_outcome(Submission.outcome).label("normalized_outcome")
    reason_expression = sql_normalize_reason_code(Submission.reason_code).label(
        "normalized_reason_code"
    )
    ats_expression, version_expression, selector_expression = _sql_adapter_identity(
        _submission_ats_source(),
        Submission.adapter_version,
        Submission.selector_version,
    )
    count_expression = func.count(Submission.id).label("event_count")
    rows = (
        db.query(
            outcome_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
            count_expression,
        )
        .filter(
            Submission.finished_at >= since,
            Submission.stage == "finished",
            Submission.outcome.isnot(None),
        )
        .group_by(
            outcome_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .order_by(
            count_expression.desc(),
            outcome_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    return [
        {
            "outcome": normalize_outcome(outcome),
            "reason_code": normalize_reason_code(reason),
            "ats": normalize_ats(ats),
            "adapter_version": normalize_adapter_version(version, ats=ats),
            "selector_version": normalize_selector_version(selector, ats=ats),
            "count": int(count or 0),
        }
        for outcome, reason, ats, version, selector, count in rows
    ]


def _form_resolution_rows(db, *, since: datetime) -> list[dict[str, Any]]:
    ats_expression, version_expression, selector_expression = _sql_adapter_identity(
        OperationalMetricEvent.ats,
        OperationalMetricEvent.adapter_version,
        OperationalMetricEvent.selector_version,
    )
    reason_expression = sql_normalize_reason_code(OperationalMetricEvent.reason_code).label(
        "normalized_reason_code"
    )
    field_expression = sql_normalize_field_type(OperationalMetricEvent.field_type).label(
        "normalized_field_type"
    )
    resolver_expression = sql_normalize_resolver(OperationalMetricEvent.resolver).label(
        "normalized_resolver"
    )
    count_expression = func.count(OperationalMetricEvent.id).label("event_count")
    rows = (
        db.query(
            resolver_expression,
            field_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
            count_expression,
        )
        .filter(
            OperationalMetricEvent.metric_name == "form_resolution",
            OperationalMetricEvent.occurred_at >= since,
        )
        .group_by(
            resolver_expression,
            field_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .order_by(
            count_expression.desc(),
            resolver_expression,
            field_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    return [
        {
            "resolver": normalize_resolver(resolver),
            "field_type": normalize_field_type(field_type),
            "reason_code": normalize_reason_code(reason),
            "ats": normalize_ats(ats),
            "adapter_version": normalize_adapter_version(version, ats=ats),
            "selector_version": normalize_selector_version(selector, ats=ats),
            "count": int(count or 0),
        }
        for resolver, field_type, reason, ats, version, selector, count in rows
    ]


def _attachment_rows(db, *, since: datetime) -> list[dict[str, Any]]:
    ats_expression, version_expression, selector_expression = _sql_adapter_identity(
        OperationalMetricEvent.ats,
        OperationalMetricEvent.adapter_version,
        OperationalMetricEvent.selector_version,
    )
    reason_expression = sql_normalize_reason_code(OperationalMetricEvent.reason_code).label(
        "normalized_reason_code"
    )
    attachment_expression = sql_normalize_attachment_result(
        OperationalMetricEvent.attachment_result
    ).label("normalized_attachment_result")
    count_expression = func.count(OperationalMetricEvent.id).label("event_count")
    rows = (
        db.query(
            attachment_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
            count_expression,
        )
        .filter(
            OperationalMetricEvent.metric_name == "attachment_result",
            OperationalMetricEvent.occurred_at >= since,
        )
        .group_by(
            attachment_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .order_by(
            count_expression.desc(),
            attachment_expression,
            reason_expression,
            ats_expression,
            version_expression,
            selector_expression,
        )
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    return [
        {
            "attachment_result": normalize_attachment_result(result),
            "result": normalize_attachment_result(result),
            "reason_code": normalize_reason_code(reason),
            "ats": normalize_ats(ats),
            "adapter_version": normalize_adapter_version(version, ats=ats),
            "selector_version": normalize_selector_version(selector, ats=ats),
            "count": int(count or 0),
        }
        for result, reason, ats, version, selector, count in rows
    ]


def _evidence_rows(db, *, since: datetime) -> list[dict[str, Any]]:
    ats_expression = sql_normalize_ats(_submission_ats_source()).label("normalized_ats")
    version_expression = sql_normalize_adapter_version(
        Submission.adapter_version,
        ats_expression=ats_expression,
    ).label("normalized_adapter_version")
    evidence_expression = sql_normalize_evidence_type(SubmissionEvidence.evidence_type).label(
        "normalized_evidence_type"
    )
    count_expression = func.count(SubmissionEvidence.id).label("event_count")
    rows = (
        db.query(
            evidence_expression,
            ats_expression,
            version_expression,
            count_expression,
        )
        .join(Submission, SubmissionEvidence.attempt_id == Submission.id)
        .filter(
            SubmissionEvidence.observed_at >= since,
            *employer_verified_sql_conditions(db),
        )
        .group_by(
            evidence_expression,
            ats_expression,
            version_expression,
        )
        .order_by(
            count_expression.desc(),
            evidence_expression,
            ats_expression,
            version_expression,
        )
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    return [
        {
            "evidence_type": normalize_evidence_type(evidence_type),
            "ats": normalize_ats(ats),
            "adapter_version": normalize_adapter_version(version, ats=ats),
            "verification": "employer_verified",
            "count": int(count or 0),
        }
        for evidence_type, ats, version, count in rows
    ]


def _failure_rows(db, *, since: datetime) -> list[dict[str, Any]]:
    aggregates: defaultdict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "last_seen_at": None}
    )
    event_ats, event_version, event_selector = _sql_adapter_identity(
        OperationalMetricEvent.ats,
        OperationalMetricEvent.adapter_version,
        OperationalMetricEvent.selector_version,
    )
    event_reason = sql_normalize_reason_code(OperationalMetricEvent.reason_code).label(
        "normalized_reason_code"
    )
    event_count = func.count(OperationalMetricEvent.id).label("event_count")
    event_last_seen = func.max(OperationalMetricEvent.occurred_at).label("last_seen_at")
    event_rows = (
        db.query(
            event_ats,
            event_version,
            event_selector,
            event_reason,
            event_count,
            event_last_seen,
        )
        .filter(
            OperationalMetricEvent.occurred_at >= since,
            event_reason.notin_(tuple(sorted(_SUCCESS_REASONS))),
        )
        .group_by(
            event_ats,
            event_version,
            event_selector,
            event_reason,
        )
        .order_by(
            event_count.desc(),
            event_reason,
            event_ats,
            event_version,
            event_selector,
        )
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    for ats, version, selector, reason, count, last_seen_at in event_rows:
        normalized_reason = normalize_reason_code(reason)
        if normalized_reason in _SUCCESS_REASONS:
            continue
        normalized_ats, normalized_version, normalized_selector = _adapter_identity(
            ats,
            version,
            selector,
        )
        key = (
            normalized_reason,
            normalized_ats,
            normalized_version,
            normalized_selector,
        )
        aggregate = aggregates[key]
        aggregate["count"] += int(count or 0)
        if aggregate["last_seen_at"] is None or (
            last_seen_at is not None and last_seen_at > aggregate["last_seen_at"]
        ):
            aggregate["last_seen_at"] = last_seen_at

    qualification_ats, qualification_version, qualification_selector = _sql_adapter_identity(
        BrowserQualificationRun.adapter_name,
        BrowserQualificationRun.adapter_version,
        BrowserQualificationRun.selector_version,
    )
    qualification_reason = sql_normalize_reason_code(BrowserQualificationRun.terminal_reason).label(
        "normalized_reason_code"
    )
    qualification_count = func.count(BrowserQualificationRun.id).label("event_count")
    qualification_last_seen = func.max(BrowserQualificationRun.created_at).label("last_seen_at")
    qualification_rows = (
        db.query(
            qualification_ats,
            qualification_version,
            qualification_selector,
            qualification_reason,
            qualification_count,
            qualification_last_seen,
        )
        .filter(
            BrowserQualificationRun.qualified.is_(False),
            BrowserQualificationRun.created_at >= since,
            qualification_reason.notin_(tuple(sorted(_SUCCESS_REASONS))),
        )
        .group_by(
            qualification_ats,
            qualification_version,
            qualification_selector,
            qualification_reason,
        )
        .order_by(
            qualification_count.desc(),
            qualification_reason,
            qualification_ats,
            qualification_version,
            qualification_selector,
        )
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    for ats, version, selector, reason, count, last_seen_at in qualification_rows:
        normalized_reason = normalize_reason_code(reason)
        if normalized_reason in _SUCCESS_REASONS:
            continue
        normalized_ats, normalized_version, normalized_selector = _adapter_identity(
            ats,
            version,
            selector,
        )
        key = (
            normalized_reason,
            normalized_ats,
            normalized_version,
            normalized_selector,
        )
        aggregate = aggregates[key]
        aggregate["count"] += int(count or 0)
        if aggregate["last_seen_at"] is None or (
            last_seen_at is not None and last_seen_at > aggregate["last_seen_at"]
        ):
            aggregate["last_seen_at"] = last_seen_at

    ordered = sorted(
        aggregates.items(),
        key=lambda item: (-int(item[1]["count"]), *item[0]),
    )
    return [
        {
            "reason_code": reason,
            "ats": ats,
            "adapter_version": version,
            "selector_version": selector,
            "count": int(aggregate["count"]),
            "last_seen_at": _aware_utc(aggregate["last_seen_at"]),
        }
        for (reason, ats, version, selector), aggregate in ordered[:_MAX_RESPONSE_ROWS]
    ]


def _discovery_source_rows(db) -> list[dict[str, Any]]:
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
            func.min(DiscoverySourceState.next_poll_at).label("next_poll_at"),
            func.max(DiscoverySourceState.last_success_at).label("last_success_at"),
            func.max(DiscoverySourceState.last_error_code).label("last_error_code"),
            func.max(DiscoverySourceState.cadence_seconds).label("cadence_seconds"),
        )
        .group_by(DiscoverySourceState.source_type)
        .order_by(DiscoverySourceState.source_type)
        .limit(_MAX_RESPONSE_ROWS)
        .all()
    )
    result: list[dict[str, Any]] = []
    for (
        source_type,
        source_count,
        enabled,
        degraded,
        healthy,
        next_poll_at,
        last_success_at,
        last_error_code,
        cadence_seconds,
    ) in rows:
        total = int(source_count or 0)
        enabled_total = int(enabled or 0)
        if enabled_total == 0:
            status = "disabled"
        elif int(degraded or 0) > 0:
            status = "degraded"
        elif int(healthy or 0) == enabled_total:
            status = "healthy"
        else:
            status = "unknown"
        result.append(
            {
                "source_type": _source_token(source_type),
                "status": status,
                "source_count": total,
                "enabled_count": enabled_total,
                "cadence_seconds": min(86_400, max(0, int(cadence_seconds or 0))),
                "next_poll_at": _aware_utc(next_poll_at),
                "last_success_at": _aware_utc(last_success_at),
                "last_error_code": (
                    normalize_reason_code(last_error_code) if last_error_code else None
                ),
            }
        )
    return result


def _pipeline_counts(db, *, since: datetime) -> dict[str, int]:
    duplicate_observations = (
        db.query(func.coalesce(func.sum(DiscoveryRun.duplicates), 0))
        .filter(DiscoveryRun.started_at >= since)
        .scalar()
    )
    latest_fit = _latest_fit_rows(db)
    return {
        "discovered": int(db.query(Job.id).filter(Job.created_at >= since).count()),
        "source_occurrences": int(
            db.query(JobSourceOccurrenceRecord.id)
            .filter(JobSourceOccurrenceRecord.first_seen_at >= since)
            .count()
        ),
        "deduplicated": int(duplicate_observations or 0),
        "eligible": int(latest_fit.filter(JobFitDecisionRecord.quality_eligible.is_(True)).count()),
        "prepared": int(prepared_application_count(db)),
        "quarantined": int(
            db.query(Application.id).filter(Application.status == JobStatus.NEEDS_REVIEW).count()
        ),
        "employer_confirmed": int(latest_employer_verified_count(db)),
    }


def _role_cv_matrix_rows(db) -> list[dict[str, Any]]:
    grouped = (
        _latest_fit_rows(db)
        .with_entities(
            JobFitDecisionRecord.selected_cv_id,
            JobFitDecisionRecord.disposition,
            JobFitDecisionRecord.quality_eligible,
            func.count(JobFitDecisionRecord.id).label("decision_count"),
            func.avg(JobFitDecisionRecord.fit_score).label("average_fit_score"),
            func.avg(JobFitDecisionRecord.routing_confidence).label("average_routing_confidence"),
        )
        .group_by(
            JobFitDecisionRecord.selected_cv_id,
            JobFitDecisionRecord.disposition,
            JobFitDecisionRecord.quality_eligible,
        )
        .order_by(func.count(JobFitDecisionRecord.id).desc())
        .limit(1000)
        .all()
    )
    aggregates: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "eligible": 0,
            "needs_review": 0,
            "excluded": 0,
            "total": 0,
            "fit_total": 0.0,
            "confidence_total": 0.0,
        }
    )
    for cv_id, disposition, quality_eligible, count, average_fit, average_confidence in grouped:
        route = _route_token(cv_id)
        total = int(count or 0)
        bucket = aggregates[route]
        bucket["total"] += total
        bucket["fit_total"] += float(average_fit or 0.0) * total
        bucket["confidence_total"] += float(average_confidence or 0.0) * total
        normalized_disposition = str(disposition or "needs_review").strip().lower()
        if quality_eligible is True and normalized_disposition == "eligible":
            bucket["eligible"] += total
        elif normalized_disposition == "excluded":
            bucket["excluded"] += total
        else:
            bucket["needs_review"] += total

    ordered = sorted(
        aggregates.items(),
        key=lambda item: (-int(item[1]["total"]), item[0]),
    )
    return [
        {
            "cv_route": route,
            "total": int(values["total"]),
            "eligible": int(values["eligible"]),
            "needs_review": int(values["needs_review"]),
            "excluded": int(values["excluded"]),
            "average_fit_score": round(
                float(values["fit_total"]) / max(1, int(values["total"])),
                1,
            ),
            "average_routing_confidence": round(
                float(values["confidence_total"]) / max(1, int(values["total"])),
                3,
            ),
        }
        for route, values in ordered[:_MAX_RESPONSE_ROWS]
    ]


def _recent_fit_rows(db) -> list[dict[str, Any]]:
    records = (
        _latest_fit_rows(db)
        .order_by(JobFitDecisionRecord.created_at.desc(), JobFitDecisionRecord.id.desc())
        .limit(_MAX_RECENT_ROWS)
        .all()
    )
    rows: list[dict[str, Any]] = []
    for record in records:
        try:
            decision = decision_from_record(record)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "decision_id": int(record.id),
                "job_id": int(record.job_id),
                "cv_route": _route_token(decision.selected_cv_id),
                "fit_score": round(float(decision.fit_score), 1),
                "routing_confidence": round(float(decision.routing_confidence), 3),
                "routing_margin": round(float(decision.routing_margin), 3),
                "disposition": decision.disposition.value,
                "quality_eligible": bool(decision.quality_eligible),
                "fallback_reason": (
                    normalize_reason_code(decision.routing_fallback_reason)
                    if decision.routing_fallback_reason
                    else None
                ),
                "hard_exclusions": [
                    normalize_reason_code(item) for item in decision.hard_exclusions
                ],
                "uncertainty": [normalize_reason_code(item) for item in decision.uncertainty],
                "unsupported_required_skill_count": len(decision.unsupported_required_skills),
                "evidence": [
                    {
                        "factor": item.factor,
                        "result": item.result,
                        "reason_codes": [
                            normalize_reason_code(reason) for reason in item.reason_codes
                        ],
                    }
                    for item in decision.evidence
                ],
                "created_at": _aware_utc(record.created_at),
            }
        )
    return rows


def _policy_status(db, *, now: datetime) -> dict[str, Any]:
    try:
        status = policy_usage_status(db, now=_aware_utc(now))
    except Exception:
        status = {
            "active": False,
            "reason_code": "AUTOMATION_STATUS_UNAVAILABLE",
            "kill_switch_active": True,
        }
    return {
        "active": bool(status.get("active")),
        "reason_code": (
            normalize_reason_code(status.get("reason_code")) if status.get("reason_code") else None
        ),
        "revision": max(0, int(status.get("revision") or 0)),
        "activated_at": status.get("activated_at"),
        "expires_at": status.get("expires_at"),
        "minimum_fit_score": (
            round(float(status["minimum_fit_score"]), 1)
            if isinstance(status.get("minimum_fit_score"), (int, float))
            and math.isfinite(float(status["minimum_fit_score"]))
            else None
        ),
        "daily_limit": max(0, int(status.get("daily_limit") or 0)),
        "daily_remaining": max(0, int(status.get("daily_remaining") or 0)),
        "hourly_limit": max(0, int(status.get("hourly_limit") or 0)),
        "hourly_remaining": max(0, int(status.get("hourly_remaining") or 0)),
        "company_limit": max(0, int(status.get("company_limit") or 0)),
        "company_window_days": max(0, int(status.get("company_window_days") or 0)),
        "permitted_adapters": sorted(
            {
                normalize_ats(item)
                for item in status.get("permitted_adapters", [])
                if normalize_ats(item) != "other"
            }
        )[:16],
        "geographies": sorted(
            {
                item
                for item in status.get("geographies", [])
                if item in {"israel", "worldwide_remote", "emea_remote"}
            }
        )[:8],
        "role_family_count": min(100, len(status.get("role_families", []))),
        "qualified_form_contract_count": max(
            0,
            int(status.get("qualified_form_contract_count") or 0),
        ),
        "kill_switch_active": bool(status.get("kill_switch_active")),
        "kill_switch_revision": max(0, int(status.get("kill_switch_revision") or 0)),
    }


def _recent_attempt_rows(db) -> list[dict[str, Any]]:
    attempts = (
        db.query(Submission)
        .order_by(Submission.created_at.desc(), Submission.id.desc())
        .limit(_MAX_RECENT_ROWS)
        .all()
    )
    return [
        {
            "attempt_id": int(attempt.id),
            "application_id": int(attempt.application_id),
            "attempt_number": int(attempt.attempt_number),
            "stage": normalize_stage(attempt.stage),
            "outcome": normalize_outcome(attempt.outcome),
            "reason_code": normalize_reason_code(attempt.reason_code),
            "ats": normalize_ats(attempt.adapter_name or attempt.submitter_name),
            "adapter_version": normalize_adapter_version(
                attempt.adapter_version,
                ats=attempt.adapter_name or attempt.submitter_name,
            ),
            "selector_version": normalize_selector_version(
                attempt.selector_version,
                ats=attempt.adapter_name or attempt.submitter_name,
            ),
            "cv_route": _route_token(
                attempt.attached_cv_id or attempt.requested_cv_id or attempt.selected_cv_id
            ),
            "form_fingerprint": _digest_token(attempt.form_plan_fingerprint),
            "attachment_verified": bool(attempt.attachment_verified),
            "verification_kind": normalize_evidence_type(attempt.verification_kind),
            "evidence_digest": _digest_token(attempt.evidence_digest),
            "authority_kind": _runtime_token(
                attempt.authority_kind,
                fallback="other",
            ).lower(),
            "created_at": _aware_utc(attempt.created_at),
            "started_at": _aware_utc(attempt.started_at),
            "final_action_at": _aware_utc(attempt.final_action_at),
            "finished_at": _aware_utc(attempt.finished_at),
        }
        for attempt in attempts
    ]


def build_operations_snapshot(
    db,
    readiness: dict[str, Any],
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one deterministic, redacted operational evidence snapshot."""

    generated_at = _naive_utc(now)
    bounded_window = max(1, min(int(window_days), 90))
    since = generated_at - timedelta(days=bounded_window)
    last_successful_discovery = (
        db.query(func.max(DiscoveryRun.finished_at))
        .filter(
            DiscoveryRun.status == "success",
            DiscoveryRun.finished_at.isnot(None),
        )
        .scalar()
    )
    queue_depths = authoritative_queue_depths(db)
    identity = get_runtime_identity()
    checks = readiness.get("checks")
    worker_detail = (
        checks.get("worker")
        if isinstance(checks, dict) and isinstance(checks.get("worker"), dict)
        else {}
    )

    adapter_matrix = [
        {
            "ats": normalize_ats(descriptor.platform),
            "adapter_version": normalize_adapter_version(
                descriptor.adapter_version,
                ats=descriptor.platform,
            ),
            "selector_version": normalize_selector_version(
                descriptor.selector_version,
                ats=descriptor.platform,
            ),
            "qualification_tier": normalize_qualification_tier(
                descriptor.qualification,
            ),
            "final_execution_enabled": bool(descriptor.allows_final_execution),
            "qualified_form_scope_count": len(descriptor.qualified_form_scope),
        }
        for descriptor in sorted(
            effective_registered_descriptors(db),
            key=lambda item: item.platform,
        )
    ]

    return {
        "generated_at": _aware_utc(generated_at),
        "window_days": bounded_window,
        "dependencies": _dependency_rows(readiness, now=generated_at),
        "last_successful_discovery": (
            {"finished_at": _aware_utc(last_successful_discovery)}
            if last_successful_discovery is not None
            else None
        ),
        "discovery_sources": _discovery_source_rows(db),
        "pipeline_counts": _pipeline_counts(db, since=since),
        "role_cv_matrix": _role_cv_matrix_rows(db),
        "recent_fit_decisions": _recent_fit_rows(db),
        "automation_policy": _policy_status(db, now=generated_at),
        "adapter_matrix": adapter_matrix,
        "failure_clusters": _failure_rows(db, since=since),
        "queue_depth": [{"queue": name, "count": int(queue_depths[name])} for name in QUEUE_LABELS],
        "attempt_stages": _attempt_stage_rows(db, since=since),
        "attempt_outcomes": _attempt_outcome_rows(db, since=since),
        "form_resolution": _form_resolution_rows(db, since=since),
        "attachment_results": _attachment_rows(db, since=since),
        "evidence_types": _evidence_rows(db, since=since),
        "recent_attempts": _recent_attempt_rows(db),
        "runtime_identity": {
            "build_sha": _runtime_token(identity.build_sha),
            "source_digest": _runtime_token(identity.source_digest),
            "ui_asset_digest": _runtime_token(identity.ui_asset_digest),
            "protocol_version": _runtime_token(identity.protocol_version),
            "boot_id": _runtime_token(identity.boot_id),
            "runner_release": _runtime_token(worker_detail.get("release_id")),
            "started_at": _aware_utc(identity.started_at),
        },
    }
