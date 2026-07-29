"""Durable, deduplicated operational metrics shared across API and workers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.application_state import (
    prepared_application_count,
    reviewable_application_count,
)
from core.operational_labels import (
    OPERATIONAL_LABEL_NAMES,
    QUEUE_LABELS,
    normalize_adapter_version,
    normalize_ats,
    normalize_attachment_result,
    normalize_evidence_type,
    normalize_field_type,
    normalize_metric_name,
    normalize_outcome,
    normalize_reason_code,
    normalize_resolver,
    normalize_selector_version,
    normalize_stage,
    sql_normalize_adapter_version,
    sql_normalize_ats,
    sql_normalize_attachment_result,
    sql_normalize_evidence_type,
    sql_normalize_field_type,
    sql_normalize_metric_name,
    sql_normalize_outcome,
    sql_normalize_reason_code,
    sql_normalize_resolver,
    sql_normalize_selector_version,
    sql_normalize_stage,
)
from db.models import (
    ExtractedURL,
    Job,
    JobStatus,
    OperationalMetricEvent,
    OperationalMetricReceipt,
    OperationalMetricRollup,
    Submission,
    SubmissionCommand,
    URLStatus,
)

_ROLLUP_DIMENSIONS = (
    "metric_name",
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
_MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000
_DURATION_BUCKETS_MS = (1_000, 5_000, 15_000, 60_000, 300_000, 900_000)
OPERATIONAL_EVENT_RETENTION_DAYS = 90
OPERATIONAL_EVENT_MAX_ROWS = 100_000
_OPERATIONAL_PRUNE_LOCK_ID = 0x4A4F424D45545249


@dataclass(frozen=True, slots=True)
class OperationalLabels:
    """One completely normalized, finite operational label set."""

    metric_name: str
    ats: str
    adapter_version: str
    selector_version: str
    stage: str
    outcome: str
    reason_code: str
    field_type: str
    resolver: str
    attachment_result: str
    evidence_type: str

    @classmethod
    def normalize(
        cls,
        *,
        metric_name: object,
        ats: object = None,
        adapter_version: object = None,
        selector_version: object = None,
        stage: object = None,
        outcome: object = None,
        reason_code: object = None,
        field_type: object = None,
        resolver: object = None,
        attachment_result: object = None,
        evidence_type: object = None,
    ) -> OperationalLabels:
        normalized_ats = normalize_ats(ats)
        return cls(
            metric_name=normalize_metric_name(metric_name),
            ats=normalized_ats,
            adapter_version=normalize_adapter_version(
                adapter_version,
                ats=normalized_ats,
            ),
            selector_version=normalize_selector_version(
                selector_version,
                ats=normalized_ats,
            ),
            stage=normalize_stage(stage),
            outcome=normalize_outcome(outcome),
            reason_code=normalize_reason_code(reason_code),
            field_type=normalize_field_type(field_type),
            resolver=normalize_resolver(resolver),
            attachment_result=normalize_attachment_result(attachment_result),
            evidence_type=normalize_evidence_type(evidence_type),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "metric_name": self.metric_name,
            "ats": self.ats,
            "adapter_version": self.adapter_version,
            "selector_version": self.selector_version,
            "stage": self.stage,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "field_type": self.field_type,
            "resolver": self.resolver,
            "attachment_result": self.attachment_result,
            "evidence_type": self.evidence_type,
        }


def _naive_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current
    return current.astimezone(UTC).replace(tzinfo=None)


def _digest(namespace: str, value: object) -> str:
    material = f"job-agent-operational-v1:{namespace}:{value}".encode()
    return hashlib.sha256(material).hexdigest()


def _duration_ms(value: float | None) -> int | None:
    if value is None:
        return None
    try:
        milliseconds = int(round(float(value) * 1000))
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, min(milliseconds, _MAX_DURATION_MS))


def _histogram_increments(duration_ms: int | None) -> dict[str, int]:
    if duration_ms is None:
        return {
            "duration_count": 0,
            "duration_sum_ms": 0,
            "duration_le_1s": 0,
            "duration_le_5s": 0,
            "duration_le_15s": 0,
            "duration_le_60s": 0,
            "duration_le_300s": 0,
            "duration_le_900s": 0,
            "duration_le_inf": 0,
        }
    buckets = {
        "duration_le_1s": int(duration_ms <= _DURATION_BUCKETS_MS[0]),
        "duration_le_5s": int(duration_ms <= _DURATION_BUCKETS_MS[1]),
        "duration_le_15s": int(duration_ms <= _DURATION_BUCKETS_MS[2]),
        "duration_le_60s": int(duration_ms <= _DURATION_BUCKETS_MS[3]),
        "duration_le_300s": int(duration_ms <= _DURATION_BUCKETS_MS[4]),
        "duration_le_900s": int(duration_ms <= _DURATION_BUCKETS_MS[5]),
    }
    return {
        "duration_count": 1,
        "duration_sum_ms": duration_ms,
        **buckets,
        "duration_le_inf": 1,
    }


def _dialect_insert(db, model):
    name = db.bind.dialect.name
    if name == "postgresql":
        return postgresql_insert(model)
    if name == "sqlite":
        return sqlite_insert(model)
    return None


def _claim_metric_receipt(
    db,
    *,
    event_key: str,
    recorded_at: datetime,
) -> bool:
    """Permanently claim one non-personal event digest in this transaction."""

    values = {"event_key": event_key, "recorded_at": recorded_at}
    insert_statement = _dialect_insert(db, OperationalMetricReceipt)
    if insert_statement is None:
        exists = (
            db.query(OperationalMetricReceipt.event_key)
            .filter(OperationalMetricReceipt.event_key == event_key)
            .first()
        )
        if exists is not None:
            return False
        db.add(OperationalMetricReceipt(**values))
        db.flush()
        return True
    result = db.execute(
        insert_statement.values(**values)
        .on_conflict_do_nothing(index_elements=["event_key"])
        .returning(OperationalMetricReceipt.event_key)
    )
    return result.scalar_one_or_none() is not None


def _coordinate_operational_pruning(db) -> None:
    """Serialize PostgreSQL writers until their transaction commits."""

    if db.bind.dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(_OPERATIONAL_PRUNE_LOCK_ID)))


def prune_operational_events(
    db,
    *,
    now: datetime | None = None,
) -> int:
    """Enforce the exact event-detail window and row cap.

    Cumulative rollups and privacy-safe receipts are intentionally retained.
    Only labeled detail is deleted. PostgreSQL writers share a transaction
    advisory lock so the final committed detail count cannot exceed the cap.
    """

    _coordinate_operational_pruning(db)
    timestamp = _naive_utc(now)
    cutoff = timestamp - timedelta(days=OPERATIONAL_EVENT_RETENTION_DAYS)
    expired_result = db.execute(
        delete(OperationalMetricEvent).where(
            OperationalMetricEvent.occurred_at < cutoff,
        )
    )
    removed = max(0, int(expired_result.rowcount or 0))

    count = int(db.query(func.count(OperationalMetricEvent.id)).scalar() or 0)
    overflow = max(0, count - OPERATIONAL_EVENT_MAX_ROWS)
    if overflow:
        oldest_ids = (
            select(OperationalMetricEvent.id)
            .order_by(
                OperationalMetricEvent.occurred_at.asc(),
                OperationalMetricEvent.id.asc(),
            )
            .limit(overflow)
        )
        overflow_result = db.execute(
            delete(OperationalMetricEvent).where(
                OperationalMetricEvent.id.in_(oldest_ids),
            )
        )
        removed += max(0, int(overflow_result.rowcount or 0))
    return removed


def record_operational_event(
    db,
    *,
    dedup_key: str,
    entity_key: str,
    metric_name: object,
    occurred_at: datetime | None = None,
    duration_seconds: float | None = None,
    ats: object = None,
    adapter_version: object = None,
    selector_version: object = None,
    stage: object = None,
    outcome: object = None,
    reason_code: object = None,
    field_type: object = None,
    resolver: object = None,
    attachment_result: object = None,
    evidence_type: object = None,
) -> bool:
    """Insert one metric event and increment its rollup exactly once.

    The caller owns the surrounding transaction.  A rollback therefore removes
    both the domain mutation and its metric, while a broker redelivery reuses
    the same event key and cannot increment the cumulative rollup twice.
    """

    timestamp = _naive_utc(occurred_at)
    event_key = _digest("event", dedup_key)
    if not _claim_metric_receipt(
        db,
        event_key=event_key,
        recorded_at=_naive_utc(),
    ):
        return False

    labels = OperationalLabels.normalize(
        metric_name=metric_name,
        ats=ats,
        adapter_version=adapter_version,
        selector_version=selector_version,
        stage=stage,
        outcome=outcome,
        reason_code=reason_code,
        field_type=field_type,
        resolver=resolver,
        attachment_result=attachment_result,
        evidence_type=evidence_type,
    )
    milliseconds = _duration_ms(duration_seconds)
    event_values: dict[str, Any] = {
        "event_key": event_key,
        "entity_key": _digest("entity", entity_key),
        **labels.as_dict(),
        "duration_ms": milliseconds,
        "occurred_at": timestamp,
        "created_at": timestamp,
    }

    event_insert = _dialect_insert(db, OperationalMetricEvent)
    if event_insert is None:
        db.add(OperationalMetricEvent(**event_values))
        db.flush()
    else:
        db.execute(event_insert.values(**event_values))

    increments = _histogram_increments(milliseconds)
    rollup_values: dict[str, Any] = {
        **labels.as_dict(),
        "event_count": 1,
        **increments,
        "updated_at": timestamp,
    }
    rollup_insert = _dialect_insert(db, OperationalMetricRollup)
    if rollup_insert is None:
        query = db.query(OperationalMetricRollup)
        for name in _ROLLUP_DIMENSIONS:
            query = query.filter(
                getattr(OperationalMetricRollup, name) == rollup_values[name],
            )
        row = query.with_for_update().one_or_none()
        if row is None:
            db.add(OperationalMetricRollup(**rollup_values))
        else:
            row.event_count += 1
            for name, increment in increments.items():
                setattr(row, name, getattr(row, name) + increment)
            row.updated_at = timestamp
        db.flush()
        prune_operational_events(db)
        return True

    excluded_updates = {
        "event_count": OperationalMetricRollup.event_count + 1,
        "updated_at": timestamp,
    }
    for name, increment in increments.items():
        excluded_updates[name] = getattr(OperationalMetricRollup, name) + increment
    db.execute(
        rollup_insert.values(**rollup_values).on_conflict_do_update(
            index_elements=list(_ROLLUP_DIMENSIONS),
            set_=excluded_updates,
        )
    )
    prune_operational_events(db)
    return True


def _attempt_identity(attempt) -> tuple[str, str, str]:
    ats = attempt.adapter_name or attempt.submitter_name
    return (
        normalize_ats(ats),
        normalize_adapter_version(attempt.adapter_version, ats=ats),
        normalize_selector_version(attempt.selector_version, ats=ats),
    )


def record_attempt_stage(
    db,
    attempt,
    *,
    stage: object,
    previous_stage: object | None = None,
    occurred_at: datetime,
    transition_key: str | None = None,
) -> bool:
    timestamp = _naive_utc(occurred_at)
    entity = f"submission-attempt:{attempt.id}"
    entity_digest = _digest("entity", entity)
    previous = (
        db.query(OperationalMetricEvent)
        .filter(
            OperationalMetricEvent.entity_key == entity_digest,
            OperationalMetricEvent.metric_name == "attempt_stage",
        )
        .order_by(
            OperationalMetricEvent.occurred_at.desc(),
            OperationalMetricEvent.id.desc(),
        )
        .first()
    )
    duration = (
        max(0.0, (timestamp - previous.occurred_at).total_seconds())
        if previous_stage is not None and previous is not None
        else None
    )
    ats, adapter_version, selector_version = _attempt_identity(attempt)
    token = transition_key or timestamp.isoformat(timespec="microseconds")
    return record_operational_event(
        db,
        dedup_key=f"{entity}:stage:{normalize_stage(stage)}:{token}",
        entity_key=entity,
        metric_name="attempt_stage",
        occurred_at=timestamp,
        duration_seconds=duration,
        ats=ats,
        adapter_version=adapter_version,
        selector_version=selector_version,
        stage=previous_stage if previous_stage is not None else stage,
    )


def record_attempt_outcome(
    db,
    attempt,
    *,
    occurred_at: datetime,
    event_kind: str = "terminal",
) -> bool:
    timestamp = _naive_utc(occurred_at)
    started = attempt.started_at or attempt.created_at
    duration = (
        max(0.0, (timestamp - _naive_utc(started)).total_seconds()) if started is not None else None
    )
    ats, adapter_version, selector_version = _attempt_identity(attempt)
    attachment = (
        normalize_attachment_result(bool(attempt.attachment_verified))
        if attempt.form_plan_id is not None or attempt.attached_cv_id is not None
        else "not_applicable"
    )
    return record_operational_event(
        db,
        dedup_key=(
            f"submission-attempt:{attempt.id}:{event_kind}:{attempt.outcome}:{attempt.reason_code}"
        ),
        entity_key=f"submission-attempt:{attempt.id}",
        metric_name="attempt_outcome",
        occurred_at=timestamp,
        duration_seconds=duration,
        ats=ats,
        adapter_version=adapter_version,
        selector_version=selector_version,
        stage=attempt.stage,
        outcome=attempt.outcome,
        reason_code=attempt.reason_code,
        attachment_result=attachment,
        evidence_type=attempt.verification_kind,
    )


def record_retry(db, attempt, *, occurred_at: datetime) -> bool:
    if int(attempt.attempt_number or 1) <= 1:
        return False
    ats, adapter_version, selector_version = _attempt_identity(attempt)
    return record_operational_event(
        db,
        dedup_key=f"submission-attempt:{attempt.id}:retry",
        entity_key=f"submission-attempt:{attempt.id}",
        metric_name="retry",
        occurred_at=occurred_at,
        ats=ats,
        adapter_version=adapter_version,
        selector_version=selector_version,
        stage=attempt.stage,
    )


def record_governor_denial(
    db,
    attempt,
    *,
    occurred_at: datetime,
    reason_code: str,
) -> bool:
    ats, adapter_version, selector_version = _attempt_identity(attempt)
    return record_operational_event(
        db,
        dedup_key=f"submission-attempt:{attempt.id}:governor-denial",
        entity_key=f"submission-attempt:{attempt.id}",
        metric_name="governor_denial",
        occurred_at=occurred_at,
        ats=ats,
        adapter_version=adapter_version,
        selector_version=selector_version,
        stage=attempt.stage,
        reason_code=reason_code,
    )


def record_form_decision(
    db,
    *,
    plan,
    field,
    decision,
    occurred_at: datetime,
    event_kind: str = "inspection",
) -> bool:
    disposition = getattr(decision, "disposition", None)
    disposition_value = str(getattr(disposition, "value", disposition) or "")
    reason = "REQUIRED_FIELD_UNKNOWN" if disposition_value != "resolved" else "FORM_PLAN_READY"
    return record_operational_event(
        db,
        dedup_key=f"form-plan:{plan.plan_id}:{event_kind}:field:{field.field_id}",
        entity_key=f"form-plan:{plan.plan_id}",
        metric_name="form_resolution",
        occurred_at=occurred_at,
        ats=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        reason_code=reason,
        field_type=field.field_type,
        resolver=getattr(decision, "provenance", None),
    )


def record_form_plan_metrics(
    db,
    *,
    plan,
    occurred_at: datetime,
    event_kind: str = "inspection",
) -> int:
    decisions = {item.field_id: item for item in plan.decisions}
    inserted = 0
    for field in plan.fields:
        decision = decisions.get(field.field_id)
        if decision is None:
            continue
        inserted += int(
            record_form_decision(
                db,
                plan=plan,
                field=field,
                decision=decision,
                occurred_at=occurred_at,
                event_kind=event_kind,
            )
        )
    attachment_result = (
        "verified"
        if plan.attachment_verified
        else ("unverified" if plan.attached_cv_id or plan.attached_cv_hash else "not_applicable")
    )
    inserted += int(
        record_operational_event(
            db,
            dedup_key=f"form-plan:{plan.plan_id}:{event_kind}:attachment",
            entity_key=f"form-plan:{plan.plan_id}",
            metric_name="attachment_result",
            occurred_at=occurred_at,
            ats=plan.adapter_name,
            adapter_version=plan.adapter_version,
            selector_version=plan.selector_version,
            reason_code=(
                "FORM_PLAN_READY" if plan.attachment_verified else "ATTACHMENT_UNVERIFIED"
            ),
            attachment_result=attachment_result,
        )
    )
    return inserted


def record_discovery_result(db, run, *, occurred_at: datetime) -> bool:
    timestamp = _naive_utc(occurred_at)
    duration = (
        max(0.0, (timestamp - _naive_utc(run.started_at)).total_seconds())
        if run.started_at is not None
        else None
    )
    ats = "linkedin" if run.source == "linkedin_search" else None
    reason = run.reason_code or str(run.status or "").upper()
    return record_operational_event(
        db,
        dedup_key=f"discovery-run:{run.id}:finished:{run.status}",
        entity_key=f"discovery-run:{run.id}",
        metric_name="discovery_result",
        occurred_at=timestamp,
        duration_seconds=duration,
        ats=ats,
        reason_code=reason,
    )


def authoritative_queue_depths(db) -> dict[str, int]:
    """Return every fixed queue gauge from authoritative database state."""

    stages = {
        stage: (db.query(func.count(Submission.id)).filter(Submission.stage == stage).scalar() or 0)
        for stage in ("queued", "inspecting", "preparing", "ready", "committing", "verifying")
    }
    values = {
        "urls_pending": db.query(ExtractedURL)
        .filter(ExtractedURL.status == URLStatus.PENDING)
        .count(),
        "jobs_extracted": db.query(Job).filter(Job.status == JobStatus.EXTRACTED).count(),
        "jobs_scored": db.query(Job).filter(Job.status == JobStatus.SCORED).count(),
        "applications_draft": reviewable_application_count(db),
        "applications_prepared": prepared_application_count(db),
        "submission_commands_pending": db.query(SubmissionCommand)
        .filter(SubmissionCommand.state == "pending")
        .count(),
        "submission_commands_claimed": db.query(SubmissionCommand)
        .filter(SubmissionCommand.state == "claimed")
        .count(),
        **{f"submissions_{stage}": int(count) for stage, count in stages.items()},
        "submissions_unknown": db.query(Submission)
        .filter(
            Submission.stage == "finished",
            Submission.outcome == "unknown",
        )
        .count(),
    }
    return {name: int(values.get(name, 0)) for name in QUEUE_LABELS}


class DurableOperationalCollector:
    """Prometheus collector backed by cumulative database rollups."""

    _registry_marker = "job-agent-durable-operational-v1"

    @staticmethod
    def _new_families():
        from prometheus_client.core import CounterMetricFamily, HistogramMetricFamily

        return (
            CounterMetricFamily(
                "job_agent_operational_events",
                "Durable deduplicated operational events with finite labels.",
                labels=list(OPERATIONAL_LABEL_NAMES),
            ),
            HistogramMetricFamily(
                "job_agent_operational_duration_seconds",
                "Durable operational latency with fixed buckets and finite labels.",
                labels=list(OPERATIONAL_LABEL_NAMES),
            ),
        )

    def describe(self):
        """Reserve metric family names without connecting to the database."""

        try:
            counter, histogram = self._new_families()
        except ImportError:  # pragma: no cover - dependency-light smoke
            return
        yield counter
        yield histogram

    def collect(self):
        try:
            counter, histogram = self._new_families()
            from db.session import get_session_factory
        except ImportError:  # pragma: no cover - dependency-light smoke
            return

        db = None
        try:
            db = get_session_factory()()
            metric_expression = sql_normalize_metric_name(
                OperationalMetricRollup.metric_name
            ).label("metric_name")
            ats_expression = sql_normalize_ats(OperationalMetricRollup.ats).label("ats")
            adapter_expression = sql_normalize_adapter_version(
                OperationalMetricRollup.adapter_version,
                ats_expression=ats_expression,
            ).label("adapter_version")
            selector_expression = sql_normalize_selector_version(
                OperationalMetricRollup.selector_version,
                ats_expression=ats_expression,
            ).label("selector_version")
            stage_expression = sql_normalize_stage(OperationalMetricRollup.stage).label("stage")
            outcome_expression = sql_normalize_outcome(OperationalMetricRollup.outcome).label(
                "outcome"
            )
            reason_expression = sql_normalize_reason_code(
                OperationalMetricRollup.reason_code
            ).label("reason_code")
            field_expression = sql_normalize_field_type(OperationalMetricRollup.field_type).label(
                "field_type"
            )
            resolver_expression = sql_normalize_resolver(OperationalMetricRollup.resolver).label(
                "resolver"
            )
            attachment_expression = sql_normalize_attachment_result(
                OperationalMetricRollup.attachment_result
            ).label("attachment_result")
            evidence_expression = sql_normalize_evidence_type(
                OperationalMetricRollup.evidence_type
            ).label("evidence_type")
            dimensions = (
                metric_expression,
                ats_expression,
                adapter_expression,
                selector_expression,
                stage_expression,
                outcome_expression,
                reason_expression,
                field_expression,
                resolver_expression,
                attachment_expression,
                evidence_expression,
            )
            rows = (
                db.query(
                    *dimensions,
                    func.sum(OperationalMetricRollup.event_count).label("event_count"),
                    func.sum(OperationalMetricRollup.duration_count).label("duration_count"),
                    func.sum(OperationalMetricRollup.duration_sum_ms).label("duration_sum_ms"),
                    func.sum(OperationalMetricRollup.duration_le_1s).label("duration_le_1s"),
                    func.sum(OperationalMetricRollup.duration_le_5s).label("duration_le_5s"),
                    func.sum(OperationalMetricRollup.duration_le_15s).label("duration_le_15s"),
                    func.sum(OperationalMetricRollup.duration_le_60s).label("duration_le_60s"),
                    func.sum(OperationalMetricRollup.duration_le_300s).label("duration_le_300s"),
                    func.sum(OperationalMetricRollup.duration_le_900s).label("duration_le_900s"),
                    func.sum(OperationalMetricRollup.duration_le_inf).label("duration_le_inf"),
                )
                .group_by(*dimensions)
                .order_by(*dimensions)
                .all()
            )
        except Exception:
            if db is not None:
                db.rollback()
            yield counter
            yield histogram
            return
        finally:
            if db is not None:
                db.close()

        # Treat the database as untrusted at the reporting boundary. Historical
        # adapter versions and manually corrupted labels collapse to finite
        # ``other`` buckets. Rows that collapse to the same labels are summed
        # before emission so Prometheus never receives duplicate series.
        aggregates: dict[tuple[str, ...], dict[str, int]] = {}
        for row in rows:
            normalized = OperationalLabels.normalize(
                metric_name=row.metric_name,
                ats=row.ats,
                adapter_version=row.adapter_version,
                selector_version=row.selector_version,
                stage=row.stage,
                outcome=row.outcome,
                reason_code=row.reason_code,
                field_type=row.field_type,
                resolver=row.resolver,
                attachment_result=row.attachment_result,
                evidence_type=row.evidence_type,
            )
            labels = (
                normalized.metric_name,
                normalized.ats,
                normalized.adapter_version,
                normalized.selector_version,
                normalized.stage,
                normalized.outcome,
                normalized.reason_code,
                normalized.field_type,
                normalized.resolver,
                normalized.attachment_result,
                normalized.evidence_type,
            )
            aggregate = aggregates.setdefault(
                labels,
                {
                    "event_count": 0,
                    "duration_count": 0,
                    "duration_sum_ms": 0,
                    "duration_le_1s": 0,
                    "duration_le_5s": 0,
                    "duration_le_15s": 0,
                    "duration_le_60s": 0,
                    "duration_le_300s": 0,
                    "duration_le_900s": 0,
                    "duration_le_inf": 0,
                },
            )
            for name in aggregate:
                aggregate[name] += max(0, int(getattr(row, name, 0) or 0))

        for labels in sorted(aggregates):
            aggregate = aggregates[labels]
            counter.add_metric(list(labels), aggregate["event_count"])
            if aggregate["duration_count"]:
                histogram.add_metric(
                    list(labels),
                    [
                        ("1.0", aggregate["duration_le_1s"]),
                        ("5.0", aggregate["duration_le_5s"]),
                        ("15.0", aggregate["duration_le_15s"]),
                        ("60.0", aggregate["duration_le_60s"]),
                        ("300.0", aggregate["duration_le_300s"]),
                        ("900.0", aggregate["duration_le_900s"]),
                        ("+Inf", aggregate["duration_le_inf"]),
                    ],
                    aggregate["duration_sum_ms"] / 1000.0,
                )
        yield counter
        yield histogram


_REGISTRY_COLLECTOR_ATTR = "_job_agent_durable_operational_collector"


def register_durable_operational_collector(registry=None):
    """Register once per registry, including across module hot reloads."""

    if registry is None:
        try:
            from prometheus_client import REGISTRY
        except ImportError:  # pragma: no cover - dependency-light smoke
            return None
        registry = REGISTRY

    existing = getattr(registry, _REGISTRY_COLLECTOR_ATTR, None)
    if existing is not None:
        return existing

    collector = DurableOperationalCollector()
    try:
        registry.register(collector)
    except ValueError:
        owners = getattr(registry, "_names_to_collectors", {})
        existing = owners.get("job_agent_operational_events")
        if getattr(existing, "_registry_marker", None) != collector._registry_marker:
            raise
        collector = existing
    setattr(registry, _REGISTRY_COLLECTOR_ATTR, collector)
    return collector
