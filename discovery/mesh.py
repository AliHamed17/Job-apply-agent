"""Always-on, tenant-scoped discovery mesh orchestration."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from db.models import DiscoverySourceState, EmployerCatalogEntryRecord
from discovery.catalog import (
    catalog_entry_from_url,
    load_catalog,
    upsert_catalog_entries,
)
from discovery.contracts import (
    DiscoveredPosting,
    DiscoveryCursor,
    DiscoveryPage,
    DiscoverySourceDescriptor,
    EmployerCatalogEntry,
    JobSourceOccurrence,
    SearchIntentV1,
    stable_digest,
)
from discovery.generic_sources import fetch_generic_page
from discovery.gmail_alerts import fetch_gmail_alert_page
from discovery.http_client import DiscoveryFetchError, DiscoveryHttpClient
from discovery.locks import reconcile_stale_discovery_runs, try_discovery_lock
from discovery.persistence import (
    DiscoveryIngestStats,
    finish_discovery_run,
    ingest_discovered_postings,
    load_cursor,
    mark_snapshot_occurrences_seen,
    mark_source_result,
    reconcile_source_snapshot,
    save_cursor,
    start_discovery_run,
    upsert_source_state,
)
from discovery.public_sources import fetch_remotive_jobs
from discovery.search_intents import active_search_intents
from discovery.source_adapters import descriptor_for, fetch_catalog_page, source_key_for
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData

logger = structlog.get_logger(__name__)

_SNAPSHOT_STARTED_KEY = "snapshot_started_at"
_SNAPSHOT_PENDING_KEY = "snapshot_pending_reconciliation"

LINKEDIN_DESCRIPTOR = DiscoverySourceDescriptor(
    source_key="linkedin_partner",
    source_type="linkedin_partner",
    semantic_version="1.0.0",
    configuration_digest=stable_digest({"source": "linkedin_partner", "enabled": False}),
    transport="partner_api",
    authentication_mode="partner",
    host="linkedin.com",
    cadence_seconds=600,
    supports_cursor=True,
    supports_conditional_requests=True,
    tenant_scoped=False,
    enabled=False,
    disabled_reason="WRITTEN_PARTNER_ACCESS_REQUIRED",
)


def remotive_descriptor(settings) -> DiscoverySourceDescriptor:
    enabled = bool(settings.public_discovery_enabled)
    return DiscoverySourceDescriptor(
        source_key="remotive",
        source_type="remotive",
        semantic_version="1.0.0",
        configuration_digest=stable_digest(
            {
                "source": "remotive",
                "endpoint": "https://remotive.com/api/remote-jobs",
            }
        ),
        transport="public_api",
        authentication_mode="none",
        host="remotive.com",
        cadence_seconds=max(21_600, int(settings.public_discovery_interval_h) * 3600),
        supports_cursor=False,
        supports_conditional_requests=False,
        tenant_scoped=False,
        enabled=enabled,
        disabled_reason=None if enabled else "PUBLIC_DISCOVERY_DISABLED",
    )


def gmail_descriptor(settings) -> DiscoverySourceDescriptor:
    enabled = bool(settings.gmail_alert_enabled)
    return DiscoverySourceDescriptor(
        source_key="gmail_alert",
        source_type="gmail_alert",
        semantic_version="1.0.0",
        configuration_digest=stable_digest(
            {
                "source": "gmail_alert",
                "label": settings.gmail_alert_label,
            }
        ),
        transport="oauth_mailbox",
        authentication_mode="oauth_local",
        host="gmail.googleapis.com",
        cadence_seconds=60,
        supports_cursor=True,
        supports_conditional_requests=False,
        tenant_scoped=False,
        enabled=enabled,
        disabled_reason=None if enabled else "GMAIL_ALERTS_DISABLED",
    )


def _catalog_contract(row: EmployerCatalogEntryRecord) -> EmployerCatalogEntry:
    return EmployerCatalogEntry(
        catalog_key=row.catalog_key,
        company_name=row.company_name,
        ats=row.ats,
        tenant_key=row.tenant_key,
        region=row.region,
        base_url=row.base_url,
        enabled=bool(row.enabled),
        discovered_via=row.discovered_via,
    )


def _remotive_postings(
    jobs: list[JobData],
    *,
    observed_at: datetime,
) -> tuple[DiscoveredPosting, ...]:
    result: list[DiscoveredPosting] = []
    for job in jobs:
        normalized_url = normalize_url(job.apply_url or job.source_url)
        normalized_hash = url_hash(normalized_url)
        result.append(
            DiscoveredPosting(
                job=job,
                occurrence=JobSourceOccurrence(
                    occurrence_key=stable_digest(
                        {
                            "source_key": "remotive",
                            "external_posting_id": normalized_hash,
                        }
                    ),
                    source_key="remotive",
                    external_posting_id=normalized_hash,
                    normalized_url=normalized_url,
                    normalized_url_hash=normalized_hash,
                    revision_digest=stable_digest(job.model_dump(mode="json")),
                    observed_at=observed_at,
                ),
            )
        )
    return tuple(result)


def _learn_alert_catalog(db, postings: tuple[DiscoveredPosting, ...]) -> None:
    entries: dict[str, EmployerCatalogEntry] = {}
    for posting in postings:
        entry = catalog_entry_from_url(posting.occurrence.normalized_url)
        if entry is not None:
            entries[entry.catalog_key] = entry
    if entries:
        upsert_catalog_entries(db, tuple(entries.values()))


def synchronize_discovery_configuration(db, settings) -> list[DiscoverySourceState]:
    """Import local catalog entries and persist every enabled/disabled descriptor."""

    configured = load_catalog(settings.employer_catalog_path)
    if configured:
        upsert_catalog_entries(db, configured)
    states = [
        upsert_source_state(db, remotive_descriptor(settings)),
        upsert_source_state(db, gmail_descriptor(settings)),
        upsert_source_state(db, LINKEDIN_DESCRIPTOR),
    ]
    catalog_rows = (
        db.query(EmployerCatalogEntryRecord)
        .order_by(EmployerCatalogEntryRecord.ats, EmployerCatalogEntryRecord.tenant_key)
        .all()
    )
    for row in catalog_rows:
        states.append(
            upsert_source_state(
                db,
                descriptor_for(
                    _catalog_contract(row),
                    cadence_seconds=settings.discovery_poll_interval_seconds,
                ),
            )
        )
    return states


def _source_due(source: DiscoverySourceState, *, now: datetime, force: bool) -> bool:
    return bool(
        source.enabled and (force or source.next_poll_at is None or source.next_poll_at <= now)
    )


def _snapshot_started_at(cursor: DiscoveryCursor) -> datetime | None:
    raw = str(cursor.cursor.get(_SNAPSHOT_STARTED_KEY) or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _with_snapshot_state(
    cursor: DiscoveryCursor,
    *,
    started_at: datetime,
    pending_reconciliation: bool,
) -> DiscoveryCursor:
    values = dict(cursor.cursor)
    values[_SNAPSHOT_STARTED_KEY] = started_at.astimezone(UTC).isoformat()
    values[_SNAPSHOT_PENDING_KEY] = pending_reconciliation
    return cursor.model_copy(update={"cursor": values})


def _without_snapshot_state(cursor: DiscoveryCursor) -> DiscoveryCursor:
    values = dict(cursor.cursor)
    values.pop(_SNAPSHOT_STARTED_KEY, None)
    values.pop(_SNAPSHOT_PENDING_KEY, None)
    return cursor.model_copy(update={"cursor": values})


async def _run_catalog_source(
    db,
    *,
    settings,
    source: DiscoverySourceState,
    catalog: EmployerCatalogEntryRecord,
    intents: tuple[SearchIntentV1, ...],
    preparation_ready: bool,
) -> DiscoveryIngestStats:
    entry = _catalog_contract(catalog)
    descriptor = descriptor_for(
        entry,
        cadence_seconds=settings.discovery_poll_interval_seconds,
    )
    cursor = load_cursor(db, descriptor, catalog=catalog)
    inserted = 0
    updated = 0
    duplicates = 0
    closed = 0
    complete = False
    snapshot_started = _snapshot_started_at(cursor)
    if bool(cursor.cursor.get(_SNAPSHOT_PENDING_KEY)):
        if snapshot_started is not None:
            closed += reconcile_source_snapshot(
                db,
                source_key=source.source_key,
                catalog_entry_id=int(catalog.id),
                snapshot_started_at=snapshot_started,
                observed_at=datetime.now(UTC),
            )
        cursor = _without_snapshot_state(cursor)
        save_cursor(db, cursor, catalog=catalog)
        snapshot_started = None
    if int(cursor.cursor.get("offset") or 0) == 0 and snapshot_started is None:
        snapshot_started = datetime.now(UTC)
    async with DiscoveryHttpClient(
        timeout_seconds=settings.public_discovery_timeout_s,
        max_attempts=settings.discovery_http_max_attempts,
        max_response_bytes=settings.discovery_http_max_response_bytes,
    ) as client:
        for _ in range(settings.discovery_max_pages_per_run):
            if entry.ats in {"generic_jsonld", "generic_feed"}:
                page = await fetch_generic_page(
                    entry,
                    cursor,
                    client,
                    intents,
                    max_jobs=settings.public_discovery_max_jobs,
                )
            else:
                page = await fetch_catalog_page(
                    entry,
                    cursor,
                    client,
                    intents,
                    max_jobs=settings.public_discovery_max_jobs,
                )
            if page.restart_snapshot:
                snapshot_started = datetime.now(UTC)
                cursor = _with_snapshot_state(
                    page.cursor,
                    started_at=snapshot_started,
                    pending_reconciliation=False,
                )
                save_cursor(db, cursor, catalog=catalog)
                continue
            stats = ingest_discovered_postings(
                db,
                page.postings,
                tasks_always_eager=settings.tasks_always_eager,
                preparation_ready=preparation_ready,
            )
            inserted += stats.inserted
            updated += stats.updated
            duplicates += stats.duplicate
            mark_snapshot_occurrences_seen(
                db,
                source_key=source.source_key,
                catalog_entry_id=int(catalog.id),
                occurrence_keys=page.snapshot_occurrence_keys,
                observed_at=datetime.now(UTC),
            )
            if page.not_modified:
                cursor = _without_snapshot_state(page.cursor)
            elif snapshot_started is not None:
                cursor = _with_snapshot_state(
                    page.cursor,
                    started_at=snapshot_started,
                    pending_reconciliation=page.complete_snapshot,
                )
            else:
                # A legacy resumed cursor has no trustworthy origin watermark.
                # Finish it without reconciliation; the next origin scan will
                # create a durable snapshot watermark.
                cursor = _without_snapshot_state(page.cursor)
            save_cursor(db, cursor, catalog=catalog)
            complete = page.complete_snapshot
            if page.not_modified or complete or not descriptor.supports_cursor:
                break
    if complete and snapshot_started is not None and not page.not_modified:
        closed += reconcile_source_snapshot(
            db,
            source_key=source.source_key,
            catalog_entry_id=int(catalog.id),
            snapshot_started_at=snapshot_started,
            observed_at=datetime.now(UTC),
        )
        cursor = _without_snapshot_state(cursor)
        save_cursor(db, cursor, catalog=catalog)
    return DiscoveryIngestStats(
        inserted=inserted,
        updated=updated,
        duplicate=duplicates,
        closed=closed,
    )


async def _run_singleton_source(
    db,
    *,
    settings,
    source: DiscoverySourceState,
    profile,
    intents: tuple[SearchIntentV1, ...],
    preparation_ready: bool,
) -> DiscoveryIngestStats:
    descriptor = (
        remotive_descriptor(settings)
        if source.source_key == "remotive"
        else gmail_descriptor(settings)
    )
    cursor = load_cursor(db, descriptor, catalog=None)
    if source.source_key == "remotive":
        jobs = await fetch_remotive_jobs(profile, settings, intents=intents)
        page = DiscoveryPage(
            postings=_remotive_postings(jobs, observed_at=datetime.now(UTC)),
            cursor=cursor,
            complete_snapshot=False,
        )
        stats = ingest_discovered_postings(
            db,
            page.postings,
            tasks_always_eager=settings.tasks_always_eager,
            preparation_ready=preparation_ready,
        )
        save_cursor(db, page.cursor, catalog=None)
        return stats
    if source.source_key != "gmail_alert":
        raise ValueError("SOURCE_ADAPTER_NOT_IMPLEMENTED")

    inserted = 0
    updated = 0
    duplicates = 0
    queued = 0
    remaining = int(settings.gmail_alert_max_messages)
    while remaining > 0:
        page_size = min(100, remaining)
        page = await fetch_gmail_alert_page(
            oauth_path=settings.gmail_oauth_token_path,
            label=settings.gmail_alert_label,
            cursor=cursor,
            intents=intents,
            max_messages=page_size,
        )
        _learn_alert_catalog(db, page.postings)
        page_stats = ingest_discovered_postings(
            db,
            page.postings,
            tasks_always_eager=settings.tasks_always_eager,
            preparation_ready=preparation_ready,
        )
        inserted += page_stats.inserted
        updated += page_stats.updated
        duplicates += page_stats.duplicate
        queued += page_stats.queued
        cursor = page.cursor
        save_cursor(db, cursor, catalog=None)
        remaining -= page_size
        if not str(cursor.cursor.get("page_token") or ""):
            break
    return DiscoveryIngestStats(
        inserted=inserted,
        updated=updated,
        duplicate=duplicates,
        queued=queued,
    )


async def run_discovery_mesh(
    db,
    *,
    settings,
    profile,
    preparation_ready: bool,
    force: bool = False,
    source_filter: str | None = None,
) -> dict[str, int]:
    """Run all due sources once under a cross-process advisory lock."""

    with try_discovery_lock(db, "v5-discovery-mesh") as acquired:
        if not acquired:
            return {
                "inserted": 0,
                "updated": 0,
                "duplicates": 0,
                "closed": 0,
                "skipped_overlap": 1,
            }
        reconcile_stale_discovery_runs(
            db,
            stale_after_seconds=settings.discovery_stale_run_seconds,
        )
        synchronize_discovery_configuration(db, settings)
        _, intents = active_search_intents(db)
        if not intents:
            raise ValueError("SEARCH_INTENT_NOT_ACTIVATED")
        now = datetime.now(UTC).replace(tzinfo=None)
        query = db.query(DiscoverySourceState).order_by(DiscoverySourceState.source_key)
        if source_filter:
            query = query.filter(DiscoverySourceState.source_key == source_filter)
        sources = query.all()
        totals = {
            "inserted": 0,
            "updated": 0,
            "duplicates": 0,
            "closed": 0,
            "skipped_overlap": 0,
        }
        for source in sources:
            if not _source_due(source, now=now, force=force):
                continue
            run = start_discovery_run(db, source.source_key)
            try:
                if source.source_type in {
                    "greenhouse",
                    "lever",
                    "ashby",
                    "smartrecruiters",
                    "generic_jsonld",
                    "generic_feed",
                }:
                    catalog = next(
                        (
                            row
                            for row in db.query(EmployerCatalogEntryRecord).all()
                            if source_key_for(_catalog_contract(row)) == source.source_key
                        ),
                        None,
                    )
                    if catalog is None:
                        raise ValueError("SOURCE_CATALOG_ENTRY_MISSING")
                    stats = await _run_catalog_source(
                        db,
                        settings=settings,
                        source=source,
                        catalog=catalog,
                        intents=intents,
                        preparation_ready=preparation_ready,
                    )
                else:
                    stats = await _run_singleton_source(
                        db,
                        settings=settings,
                        source=source,
                        profile=profile,
                        intents=intents,
                        preparation_ready=preparation_ready,
                    )
                totals["inserted"] += stats.inserted
                totals["updated"] += stats.updated
                totals["duplicates"] += stats.duplicate
                totals["closed"] += stats.closed
                finish_discovery_run(
                    db,
                    run,
                    status="success",
                    inserted=stats.inserted,
                    updated=stats.updated,
                    duplicates=stats.duplicate,
                    closed=stats.closed,
                )
                mark_source_result(db, source, success=True, reason_code=None)
            except Exception as exc:
                db.rollback()
                reason_code = (
                    exc.reason_code
                    if isinstance(exc, DiscoveryFetchError)
                    else str(exc)
                    if str(exc).isupper() and len(str(exc)) <= 64
                    else "SOURCE_UNAVAILABLE"
                )
                retry_after = (
                    exc.retry_after_seconds if isinstance(exc, DiscoveryFetchError) else None
                )
                finish_discovery_run(
                    db,
                    run,
                    status="failed",
                    inserted=0,
                    reason_code=reason_code,
                )
                mark_source_result(
                    db,
                    source,
                    success=False,
                    reason_code=reason_code,
                    retry_after_seconds=retry_after,
                )
                logger.warning(
                    "discovery_source_failed",
                    source_key=source.source_key,
                    reason_code=reason_code,
                )
        return totals
