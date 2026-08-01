"""Authoritative discovery cursor, occurrence, and canonical-job persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from db.models import (
    Application,
    DiscoveryCursorState,
    DiscoveryRun,
    DiscoverySourceState,
    EmployerCatalogEntryRecord,
    Job,
    JobSourceOccurrenceRecord,
    JobStatus,
)
from discovery.contracts import (
    DiscoveredPosting,
    DiscoveryCursor,
    DiscoverySourceDescriptor,
)
from ingestion.url_utils import job_signature

_MUTABLE_JOB_STATUSES = (JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.SKIPPED)


@dataclass(frozen=True, slots=True)
class DiscoveryIngestStats:
    inserted: int = 0
    updated: int = 0
    duplicate: int = 0
    closed: int = 0
    queued: int = 0


def cursor_key(source_key: str, catalog_key: str | None) -> str:
    identity = f"{source_key}|{catalog_key or 'singleton'}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def upsert_source_state(
    db,
    descriptor: DiscoverySourceDescriptor,
    *,
    next_poll_at: datetime | None = None,
) -> DiscoverySourceState:
    row = (
        db.query(DiscoverySourceState)
        .filter(DiscoverySourceState.source_key == descriptor.source_key)
        .one_or_none()
    )
    values = {
        "source_type": descriptor.source_type,
        "descriptor_version": descriptor.semantic_version,
        "configuration_digest": descriptor.configuration_digest,
        "transport": descriptor.transport,
        "authentication_mode": descriptor.authentication_mode,
        "host": descriptor.host,
        "cadence_seconds": descriptor.cadence_seconds,
        "enabled": descriptor.enabled,
        "disabled_reason": descriptor.disabled_reason,
    }
    if row is None:
        row = DiscoverySourceState(
            source_key=descriptor.source_key,
            next_poll_at=next_poll_at,
            health_status="unknown" if descriptor.enabled else "disabled",
            **values,
        )
        db.add(row)
    else:
        reset_cursor = any(
            getattr(row, key) != values[key]
            for key in (
                "source_type",
                "descriptor_version",
                "configuration_digest",
                "transport",
                "authentication_mode",
                "host",
            )
        ) or (not bool(row.enabled) and descriptor.enabled)
        for key, value in values.items():
            setattr(row, key, value)
        if reset_cursor:
            db.query(DiscoveryCursorState).filter(
                DiscoveryCursorState.source_key == descriptor.source_key
            ).delete(synchronize_session=False)
            row.next_poll_at = None
            row.health_status = "unknown"
            row.last_error_code = None
        if not descriptor.enabled:
            row.health_status = "disabled"
        elif row.health_status == "disabled":
            row.health_status = "unknown"
        if next_poll_at is not None and row.next_poll_at is None:
            row.next_poll_at = next_poll_at
    db.commit()
    db.refresh(row)
    return row


def load_cursor(
    db,
    descriptor: DiscoverySourceDescriptor,
    *,
    catalog: EmployerCatalogEntryRecord | None,
) -> DiscoveryCursor:
    key = cursor_key(descriptor.source_key, catalog.catalog_key if catalog else None)
    row = (
        db.query(DiscoveryCursorState).filter(DiscoveryCursorState.cursor_key == key).one_or_none()
    )
    if row is None:
        return DiscoveryCursor(
            source_key=descriptor.source_key,
            catalog_key=catalog.catalog_key if catalog else None,
        )
    try:
        payload = json.loads(row.cursor_json)
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return DiscoveryCursor(
        source_key=descriptor.source_key,
        catalog_key=catalog.catalog_key if catalog else None,
        cursor=payload,
        etag=row.etag,
        last_modified=row.last_modified,
        last_seen_posting_at=row.last_seen_posting_at,
    )


def save_cursor(
    db,
    cursor: DiscoveryCursor,
    *,
    catalog: EmployerCatalogEntryRecord | None,
) -> None:
    key = cursor_key(cursor.source_key, cursor.catalog_key)
    row = (
        db.query(DiscoveryCursorState).filter(DiscoveryCursorState.cursor_key == key).one_or_none()
    )
    values = {
        "source_key": cursor.source_key,
        "catalog_entry_id": catalog.id if catalog else None,
        "cursor_json": json.dumps(
            cursor.cursor,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "etag": cursor.etag,
        "last_modified": cursor.last_modified,
        "last_seen_posting_at": (
            cursor.last_seen_posting_at.astimezone(UTC).replace(tzinfo=None)
            if cursor.last_seen_posting_at
            else None
        ),
    }
    if row is None:
        db.add(DiscoveryCursorState(cursor_key=key, **values))
    else:
        for field, value in values.items():
            setattr(row, field, value)
    db.commit()


def _catalog_by_key(db, catalog_key: str | None) -> EmployerCatalogEntryRecord | None:
    if not catalog_key:
        return None
    return (
        db.query(EmployerCatalogEntryRecord)
        .filter(EmployerCatalogEntryRecord.catalog_key == catalog_key)
        .one_or_none()
    )


def _create_job(posting: DiscoveredPosting) -> Job:
    job = posting.job
    return Job(
        extracted_url_id=None,
        title=job.title,
        company=job.company or "",
        location=job.location or "",
        employment_type=job.employment_type or "",
        seniority=job.seniority or "",
        description=job.description or "",
        requirements=job.requirements or "",
        apply_url=posting.occurrence.normalized_url,
        source_url=job.source_url or posting.occurrence.normalized_url,
        date_posted=job.date_posted or "",
        keywords=json.dumps(job.keywords, ensure_ascii=False),
        apply_url_hash=posting.occurrence.normalized_url_hash,
        job_signature=job_signature(job.title, job.company, job.location),
        status=JobStatus.EXTRACTED,
        discovery_source=posting.occurrence.source_key[:30],
        easy_apply=False,
    )


def _update_mutable_job(job: Job, posting: DiscoveredPosting) -> None:
    content = posting.job
    job.title = content.title
    job.company = content.company or ""
    job.location = content.location or ""
    job.employment_type = content.employment_type or ""
    job.seniority = content.seniority or ""
    job.description = content.description or ""
    job.requirements = content.requirements or ""
    job.apply_url = posting.occurrence.normalized_url
    job.source_url = content.source_url or posting.occurrence.normalized_url
    job.date_posted = content.date_posted or ""
    job.keywords = json.dumps(content.keywords, ensure_ascii=False)
    job.apply_url_hash = posting.occurrence.normalized_url_hash
    job.job_signature = job_signature(content.title, content.company, content.location)
    job.status = JobStatus.EXTRACTED
    job.score = None


def _job_content_quality(value) -> int:
    """Prefer richer source metadata without allowing sparse alerts to erase it."""

    return (
        min(len(str(value.description or "").strip()), 20_000)
        + min(len(str(value.requirements or "").strip()), 10_000)
        + 100 * bool(str(value.company or "").strip())
        + 100 * bool(str(value.location or "").strip())
        + 50 * bool(str(value.employment_type or "").strip())
        + 50 * bool(str(value.seniority or "").strip())
    )


def ingest_discovered_postings(
    db,
    postings: tuple[DiscoveredPosting, ...],
    *,
    tasks_always_eager: bool,
    preparation_ready: bool,
) -> DiscoveryIngestStats:
    """Upsert source occurrences and queue scoring only after one durable commit."""

    inserted = 0
    updated = 0
    duplicate = 0
    queued_ids: list[int] = []
    for posting in postings:
        occurrence = (
            db.query(JobSourceOccurrenceRecord)
            .filter(JobSourceOccurrenceRecord.occurrence_key == posting.occurrence.occurrence_key)
            .one_or_none()
        )
        observed_at = posting.occurrence.observed_at.astimezone(UTC).replace(tzinfo=None)
        if occurrence is not None:
            changed = occurrence.revision_digest != posting.occurrence.revision_digest
            reopened = not occurrence.active and occurrence.closed_at is not None
            occurrence.last_seen_at = observed_at
            occurrence.active = True
            occurrence.closed_at = None
            occurrence.normalized_url = posting.occurrence.normalized_url
            occurrence.normalized_url_hash = posting.occurrence.normalized_url_hash
            occurrence.revision_digest = posting.occurrence.revision_digest
            job = occurrence.job
            if (
                (changed or reopened)
                and job.application is None
                and job.status in _MUTABLE_JOB_STATUSES
                and job.terminal_skip_at is None
            ):
                _update_mutable_job(job, posting)
                updated += 1
                queued_ids.append(int(job.id))
            else:
                duplicate += 1
                if (
                    job.application is None
                    and job.status == JobStatus.EXTRACTED
                    and job.score is None
                ):
                    queued_ids.append(int(job.id))
            continue

        catalog = _catalog_by_key(db, posting.occurrence.catalog_key)
        existing_occurrence = None
        if catalog is not None and posting.occurrence.external_posting_id:
            existing_occurrence = (
                db.query(JobSourceOccurrenceRecord)
                .filter(
                    JobSourceOccurrenceRecord.catalog_entry_id == catalog.id,
                    JobSourceOccurrenceRecord.external_posting_id
                    == posting.occurrence.external_posting_id,
                )
                .order_by(JobSourceOccurrenceRecord.id)
                .first()
            )
        if existing_occurrence is None:
            existing_occurrence = (
                db.query(JobSourceOccurrenceRecord)
                .filter(
                    JobSourceOccurrenceRecord.normalized_url_hash
                    == posting.occurrence.normalized_url_hash
                )
                .order_by(JobSourceOccurrenceRecord.id)
                .first()
            )
        job = existing_occurrence.job if existing_occurrence is not None else None
        if job is None:
            job = (
                db.query(Job)
                .filter(Job.apply_url_hash == posting.occurrence.normalized_url_hash)
                .order_by(Job.id)
                .first()
            )
        if job is None:
            job = _create_job(posting)
            db.add(job)
            db.flush()
            inserted += 1
            queued_ids.append(int(job.id))
        else:
            duplicate += 1
            if (
                job.application is None
                and job.status in _MUTABLE_JOB_STATUSES
                and job.terminal_skip_at is None
            ):
                if _job_content_quality(posting.job) > _job_content_quality(job):
                    _update_mutable_job(job, posting)
                    updated += 1
                    queued_ids.append(int(job.id))
                elif job.status == JobStatus.EXTRACTED and job.score is None:
                    queued_ids.append(int(job.id))

        db.add(
            JobSourceOccurrenceRecord(
                occurrence_key=posting.occurrence.occurrence_key,
                job_id=job.id,
                source_key=posting.occurrence.source_key,
                catalog_entry_id=catalog.id if catalog else None,
                external_posting_id=posting.occurrence.external_posting_id,
                normalized_url=posting.occurrence.normalized_url,
                normalized_url_hash=posting.occurrence.normalized_url_hash,
                revision_digest=posting.occurrence.revision_digest,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                closed_at=None,
                active=True,
            )
        )
    db.commit()

    from worker.tasks import score_job_task  # noqa: PLC0415

    queued = 0
    for job_id in dict.fromkeys(queued_ids):
        if tasks_always_eager:
            score_job_task.apply(args=[job_id, preparation_ready])
        else:
            score_job_task.delay(job_id, preparation_ready)
        queued += 1
    return DiscoveryIngestStats(
        inserted=inserted,
        updated=updated,
        duplicate=duplicate,
        queued=queued,
    )


def mark_snapshot_occurrences_seen(
    db,
    *,
    source_key: str,
    catalog_entry_id: int | None,
    occurrence_keys: tuple[str, ...],
    observed_at: datetime,
) -> int:
    """Persist page-level presence, including postings filtered by search intent."""

    unique_keys = tuple(dict.fromkeys(occurrence_keys))
    if not unique_keys:
        return 0
    query = db.query(JobSourceOccurrenceRecord).filter(
        JobSourceOccurrenceRecord.source_key == source_key,
        JobSourceOccurrenceRecord.occurrence_key.in_(unique_keys),
    )
    if catalog_entry_id is not None:
        query = query.filter(JobSourceOccurrenceRecord.catalog_entry_id == catalog_entry_id)
    seen_at = observed_at.astimezone(UTC).replace(tzinfo=None)
    rows = query.all()
    for row in rows:
        row.last_seen_at = max(row.last_seen_at, seen_at)
        row.active = True
        row.closed_at = None
    db.commit()
    return len(rows)


def reconcile_source_snapshot(
    db,
    *,
    source_key: str,
    catalog_entry_id: int | None,
    seen_occurrence_keys: set[str] | None = None,
    snapshot_started_at: datetime | None = None,
    observed_at: datetime,
) -> int:
    """Close tracked occurrences absent from one complete source snapshot."""

    if (seen_occurrence_keys is None) == (snapshot_started_at is None):
        raise ValueError("provide exactly one snapshot reconciliation strategy")

    query = db.query(JobSourceOccurrenceRecord).filter(
        JobSourceOccurrenceRecord.source_key == source_key,
        JobSourceOccurrenceRecord.active.is_(True),
    )
    if catalog_entry_id is not None:
        query = query.filter(JobSourceOccurrenceRecord.catalog_entry_id == catalog_entry_id)
    if snapshot_started_at is not None:
        started_at = snapshot_started_at.astimezone(UTC).replace(tzinfo=None)
        query = query.filter(JobSourceOccurrenceRecord.last_seen_at < started_at)
    rows = query.all()
    closed = 0
    closed_at = observed_at.astimezone(UTC).replace(tzinfo=None)
    affected_job_ids: set[int] = set()
    for row in rows:
        if seen_occurrence_keys is not None and row.occurrence_key in seen_occurrence_keys:
            continue
        row.active = False
        row.closed_at = closed_at
        affected_job_ids.add(int(row.job_id))
        closed += 1
    db.flush()
    for job_id in affected_job_ids:
        has_active = (
            db.query(JobSourceOccurrenceRecord.id)
            .filter(
                JobSourceOccurrenceRecord.job_id == job_id,
                JobSourceOccurrenceRecord.active.is_(True),
            )
            .first()
            is not None
        )
        has_application = (
            db.query(Application.id).filter(Application.job_id == job_id).first() is not None
        )
        if not has_active and not has_application:
            job = db.get(Job, job_id)
            if job is not None and job.status in _MUTABLE_JOB_STATUSES:
                job.status = JobStatus.SKIPPED
    db.commit()
    return closed


def mark_source_result(
    db,
    source: DiscoverySourceState,
    *,
    success: bool,
    reason_code: str | None,
    retry_after_seconds: float | None = None,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    source.health_status = "healthy" if success else "degraded"
    source.last_error_code = None if success else reason_code
    if success:
        source.last_success_at = now
    delay = (
        max(source.cadence_seconds, int(retry_after_seconds))
        if retry_after_seconds is not None
        else source.cadence_seconds
    )
    jitter = (
        0
        if source.cadence_seconds <= 120
        else int(hashlib.sha256(source.source_key.encode("utf-8")).hexdigest()[:4], 16)
        % max(1, min(90, source.cadence_seconds // 5))
    )
    source.next_poll_at = now + timedelta(seconds=delay + jitter)
    db.commit()


def start_discovery_run(db, source_key: str) -> DiscoveryRun:
    run = DiscoveryRun(source=source_key[:64], status="running", inserted=0)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def finish_discovery_run(
    db,
    run: DiscoveryRun,
    *,
    status: str,
    inserted: int,
    updated: int = 0,
    duplicates: int = 0,
    closed: int = 0,
    reason_code: str | None = None,
) -> None:
    run.status = status
    run.inserted = inserted
    run.updated = updated
    run.duplicates = duplicates
    run.closed = closed
    run.reason_code = reason_code
    run.finished_at = datetime.now(UTC).replace(tzinfo=None)
    from core.operational_metrics import record_discovery_result  # noqa: PLC0415

    record_discovery_result(db, run, occurred_at=run.finished_at)
    db.commit()
