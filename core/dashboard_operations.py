"""Privacy-bounded operational snapshot for the protected dashboard."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func

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
from core.submission_truth import employer_verified_sql_conditions
from db.models import (
    BrowserQualificationRun,
    DiscoveryRun,
    OperationalMetricEvent,
    Submission,
    SubmissionEvidence,
)
from submitters.platforms import registered_adapters

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
        for descriptor in sorted(registered_adapters(), key=lambda item: item.platform)
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
        "adapter_matrix": adapter_matrix,
        "failure_clusters": _failure_rows(db, since=since),
        "queue_depth": [{"queue": name, "count": int(queue_depths[name])} for name in QUEUE_LABELS],
        "attempt_stages": _attempt_stage_rows(db, since=since),
        "attempt_outcomes": _attempt_outcome_rows(db, since=since),
        "form_resolution": _form_resolution_rows(db, since=since),
        "attachment_results": _attachment_rows(db, since=since),
        "evidence_types": _evidence_rows(db, since=since),
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
