"""Public, tenant-scoped ATS discovery adapters."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

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
from discovery.http_client import DiscoveryHttpClient
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData

_GREENHOUSE_HOSTS = frozenset({"boards-api.greenhouse.io"})
_LEVER_HOSTS = frozenset({"api.lever.co", "api.eu.lever.co"})
_ASHBY_HOSTS = frozenset({"api.ashbyhq.com"})
_SMARTRECRUITERS_HOSTS = frozenset({"api.smartrecruiters.com"})


def source_key_for(entry: EmployerCatalogEntry) -> str:
    # Keep the durable key opaque, bounded, and independent of tenant names.
    # ``DiscoveryRun.source`` is limited to 64 characters and Vercel/metrics
    # must not receive tenant identifiers, so use the complete SHA-256 digest.
    return stable_digest(
        {
            "source_type": entry.ats,
            "catalog_key": entry.catalog_key,
        }
    )


def descriptor_for(
    entry: EmployerCatalogEntry, *, cadence_seconds: int
) -> DiscoverySourceDescriptor:
    hosts = {
        "greenhouse": "boards-api.greenhouse.io",
        "lever": "api.eu.lever.co" if entry.region == "eu" else "api.lever.co",
        "ashby": "api.ashbyhq.com",
        "smartrecruiters": "api.smartrecruiters.com",
        "generic_jsonld": str(entry.base_url.host) if entry.base_url else "unconfigured",
        "generic_feed": str(entry.base_url.host) if entry.base_url else "unconfigured",
    }
    return DiscoverySourceDescriptor(
        source_key=source_key_for(entry),
        source_type=entry.ats,
        semantic_version=("1.1.0" if entry.ats in {"lever", "smartrecruiters"} else "1.0.0"),
        configuration_digest=stable_digest(
            {
                "ats": entry.ats,
                "tenant_key": entry.tenant_key,
                "region": entry.region,
                "base_url": str(entry.base_url or ""),
            }
        ),
        transport="public_api"
        if entry.ats
        in {
            "greenhouse",
            "lever",
            "ashby",
            "smartrecruiters",
        }
        else "permitted_web",
        authentication_mode="none"
        if entry.ats
        in {
            "greenhouse",
            "lever",
            "ashby",
            "smartrecruiters",
        }
        else "operator_configured",
        host=hosts[entry.ats],
        cadence_seconds=cadence_seconds,
        supports_cursor=entry.ats
        in {
            "greenhouse",
            "lever",
            "ashby",
            "smartrecruiters",
            "generic_jsonld",
            "generic_feed",
        },
        supports_conditional_requests=entry.ats in {"greenhouse", "ashby", "generic_jsonld"},
        tenant_scoped=True,
        enabled=entry.enabled,
        disabled_reason=None if entry.enabled else "CATALOG_ENTRY_DISABLED",
    )


def _text(value: object) -> str:
    return " ".join(
        BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ").split()
    )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _location(parts: list[object]) -> str:
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _matches_intents(job: JobData, intents: tuple[SearchIntentV1, ...]) -> bool:
    """Broad discovery filter; calibrated fit remains a later, stricter gate."""

    if not intents:
        return True
    title = job.title.casefold()
    content = f"{job.title} {job.description} {' '.join(job.keywords)}".casefold()
    for intent in intents:
        if any(term.casefold() in title for term in intent.titles):
            return True
        skill_hits = sum(1 for skill in set(intent.skills) if skill.casefold() in content)
        if skill_hits >= 2:
            return True
    return False


def _posting(
    job: JobData,
    *,
    entry: EmployerCatalogEntry,
    external_posting_id: str,
    observed_at: datetime,
    revision_hint: object = None,
) -> DiscoveredPosting:
    normalized_url = normalize_url(job.apply_url or job.source_url)
    normalized_hash = url_hash(normalized_url)
    source_key = source_key_for(entry)
    occurrence_key = _occurrence_key(entry, external_posting_id)
    revision_digest = stable_digest(
        {
            "job": job.model_dump(mode="json"),
            "revision_hint": revision_hint,
        }
    )
    return DiscoveredPosting(
        job=job,
        occurrence=JobSourceOccurrence(
            occurrence_key=occurrence_key,
            source_key=source_key,
            catalog_key=entry.catalog_key,
            external_posting_id=external_posting_id,
            normalized_url=normalized_url,
            normalized_url_hash=normalized_hash,
            revision_digest=revision_digest,
            observed_at=observed_at,
        ),
    )


def _occurrence_key(entry: EmployerCatalogEntry, external_posting_id: str) -> str:
    return stable_digest(
        {
            "source_key": source_key_for(entry),
            "catalog_key": entry.catalog_key,
            "external_posting_id": external_posting_id,
        }
    )


def _conditional_headers(cursor: DiscoveryCursor) -> dict[str, str]:
    headers: dict[str, str] = {}
    if cursor.etag:
        headers["If-None-Match"] = cursor.etag
    if cursor.last_modified:
        headers["If-Modified-Since"] = cursor.last_modified
    return headers


def _snapshot_changed(cursor: DiscoveryCursor, response, *, offset: int) -> bool:
    if offset == 0:
        return False
    current_etag = response.headers.get("ETag")
    current_modified = response.headers.get("Last-Modified")
    return bool(
        (cursor.etag and current_etag and cursor.etag != current_etag)
        or (cursor.last_modified and current_modified and cursor.last_modified != current_modified)
    )


def _restart_page(cursor: DiscoveryCursor) -> DiscoveryPage:
    return DiscoveryPage(
        cursor=DiscoveryCursor(
            source_key=cursor.source_key,
            catalog_key=cursor.catalog_key,
            cursor={"offset": 0},
        ),
        restart_snapshot=True,
    )


def _next_cursor(
    cursor: DiscoveryCursor,
    response,
    *,
    values: dict[str, str | int | float | bool | None] | None = None,
    capture_validators: bool = True,
) -> DiscoveryCursor:
    return DiscoveryCursor(
        source_key=cursor.source_key,
        catalog_key=cursor.catalog_key,
        cursor=values or cursor.cursor,
        etag=(response.headers.get("ETag") or cursor.etag) if capture_validators else None,
        last_modified=(
            response.headers.get("Last-Modified") or cursor.last_modified
            if capture_validators
            else None
        ),
        last_seen_posting_at=datetime.now(UTC),
    )


async def fetch_greenhouse_page(
    entry: EmployerCatalogEntry,
    cursor: DiscoveryCursor,
    client: DiscoveryHttpClient,
    intents: tuple[SearchIntentV1, ...],
    *,
    max_jobs: int,
) -> DiscoveryPage:
    offset = int(cursor.cursor.get("offset") or 0)
    url = f"https://boards-api.greenhouse.io/v1/boards/{entry.tenant_key}/jobs"
    response = await client.get(
        url,
        params={"content": "true"},
        headers=_conditional_headers(cursor) if offset == 0 else {},
        allowed_hosts=_GREENHOUSE_HOSTS,
    )
    if _snapshot_changed(cursor, response, offset=offset):
        return _restart_page(cursor)
    next_cursor = _next_cursor(cursor, response, values={"offset": offset})
    if response.status_code == 304:
        return DiscoveryPage(cursor=next_cursor, complete_snapshot=True, not_modified=True)
    try:
        rows = response.json().get("jobs", [])
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("SOURCE_PAYLOAD_INVALID") from exc
    if not isinstance(rows, list):
        raise ValueError("SOURCE_PAYLOAD_INVALID")
    observed = datetime.now(UTC)
    postings: list[DiscoveredPosting] = []
    snapshot_keys: list[str] = []
    window = rows[offset : offset + max_jobs]
    for row in window:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        apply_url = str(row.get("absolute_url") or "").strip()
        if not job_id or not title or not apply_url:
            continue
        snapshot_keys.append(_occurrence_key(entry, job_id))
        job = JobData(
            title=title,
            company=entry.company_name,
            location=str(_mapping(row.get("location")).get("name") or "").strip(),
            description=_text(row.get("content")),
            apply_url=apply_url,
            source_url=apply_url,
            date_posted=str(row.get("first_published") or row.get("updated_at") or ""),
            keywords=[
                str(_mapping(item).get("name") or "").strip()
                for key in ("departments", "offices")
                for item in (row.get(key) if isinstance(row.get(key), list) else [])
                if str(_mapping(item).get("name") or "").strip()
            ],
        )
        if _matches_intents(job, intents):
            postings.append(
                _posting(
                    job,
                    entry=entry,
                    external_posting_id=job_id,
                    observed_at=observed,
                    revision_hint=row.get("updated_at"),
                )
            )
    complete = offset + len(window) >= len(rows)
    next_cursor = next_cursor.model_copy(
        update={"cursor": {"offset": 0 if complete else offset + len(window)}}
    )
    return DiscoveryPage(
        postings=tuple(postings),
        snapshot_occurrence_keys=tuple(snapshot_keys),
        cursor=next_cursor,
        complete_snapshot=complete,
    )


async def fetch_lever_page(
    entry: EmployerCatalogEntry,
    cursor: DiscoveryCursor,
    client: DiscoveryHttpClient,
    intents: tuple[SearchIntentV1, ...],
    *,
    max_jobs: int,
) -> DiscoveryPage:
    host = "api.eu.lever.co" if entry.region == "eu" else "api.lever.co"
    offset = int(cursor.cursor.get("offset") or 0)
    limit = min(100, max_jobs)
    response = await client.get(
        f"https://{host}/v0/postings/{entry.tenant_key}",
        params={"mode": "json", "skip": offset, "limit": limit},
        allowed_hosts=_LEVER_HOSTS,
    )
    # Lever validators describe an individual paginated response, not a
    # collection-wide snapshot. Comparing page 0's validator with page 1's
    # would restart forever, while a page-0 304 could hide later-page changes.
    # Scan all pages unconditionally and never persist page validators.
    next_cursor = _next_cursor(
        cursor,
        response,
        values={"offset": offset + limit},
        capture_validators=False,
    )
    if response.status_code == 304:
        raise ValueError("SOURCE_UNEXPECTED_NOT_MODIFIED")
    try:
        rows = response.json()
    except ValueError as exc:
        raise ValueError("SOURCE_PAYLOAD_INVALID") from exc
    if not isinstance(rows, list):
        raise ValueError("SOURCE_PAYLOAD_INVALID")
    observed = datetime.now(UTC)
    postings: list[DiscoveredPosting] = []
    snapshot_keys: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or "").strip()
        title = str(row.get("text") or "").strip()
        apply_url = str(row.get("applyUrl") or row.get("hostedUrl") or "").strip()
        if not job_id or not title or not apply_url:
            continue
        snapshot_keys.append(_occurrence_key(entry, job_id))
        categories = _mapping(row.get("categories"))
        lists = row.get("lists") if isinstance(row.get("lists"), list) else []
        description = " ".join(
            value
            for value in (
                _text(row.get("descriptionPlain") or row.get("description")),
                *(_text(item.get("content")) for item in lists if isinstance(item, dict)),
                _text(row.get("additionalPlain") or row.get("additional")),
            )
            if value
        )
        job = JobData(
            title=title,
            company=entry.company_name,
            location=str(categories.get("location") or "").strip(),
            employment_type=str(categories.get("commitment") or "").strip(),
            seniority=str(categories.get("level") or "").strip(),
            description=description,
            apply_url=apply_url,
            source_url=str(row.get("hostedUrl") or apply_url),
            date_posted=str(row.get("createdAt") or ""),
            keywords=[
                str(categories.get(key) or "").strip()
                for key in ("team", "department", "level", "commitment", "location")
                if str(categories.get(key) or "").strip()
            ],
        )
        if _matches_intents(job, intents):
            postings.append(
                _posting(
                    job,
                    entry=entry,
                    external_posting_id=job_id,
                    observed_at=observed,
                    revision_hint=row.get("updatedAt") or row.get("createdAt"),
                )
            )
    complete = len(rows) < limit
    if complete:
        next_cursor = next_cursor.model_copy(update={"cursor": {"offset": 0}})
    return DiscoveryPage(
        postings=tuple(postings),
        snapshot_occurrence_keys=tuple(snapshot_keys),
        cursor=next_cursor,
        complete_snapshot=complete,
    )


async def fetch_ashby_page(
    entry: EmployerCatalogEntry,
    cursor: DiscoveryCursor,
    client: DiscoveryHttpClient,
    intents: tuple[SearchIntentV1, ...],
    *,
    max_jobs: int,
) -> DiscoveryPage:
    offset = int(cursor.cursor.get("offset") or 0)
    response = await client.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{entry.tenant_key}",
        headers=_conditional_headers(cursor) if offset == 0 else {},
        allowed_hosts=_ASHBY_HOSTS,
    )
    if _snapshot_changed(cursor, response, offset=offset):
        return _restart_page(cursor)
    next_cursor = _next_cursor(cursor, response, values={"offset": offset})
    if response.status_code == 304:
        return DiscoveryPage(cursor=next_cursor, complete_snapshot=True, not_modified=True)
    try:
        payload = response.json()
        rows = payload.get("jobs", [])
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("SOURCE_PAYLOAD_INVALID") from exc
    if not isinstance(rows, list):
        raise ValueError("SOURCE_PAYLOAD_INVALID")
    observed = datetime.now(UTC)
    postings: list[DiscoveredPosting] = []
    snapshot_keys: list[str] = []
    listed_rows = [
        row for row in rows if isinstance(row, dict) and row.get("isListed") is not False
    ]
    window = listed_rows[offset : offset + max_jobs]
    for row in window:
        apply_url = str(row.get("applyUrl") or row.get("jobUrl") or "").strip()
        job_url = str(row.get("jobUrl") or "").strip()
        title = str(row.get("title") or "").strip()
        id_url = job_url or apply_url
        path_parts = [part for part in id_url.rstrip("/").split("/") if part]
        if path_parts and path_parts[-1].casefold() in {"application", "apply"}:
            path_parts.pop()
        job_id = path_parts[-1] if path_parts else ""
        if not job_id or not title or not apply_url:
            continue
        snapshot_keys.append(_occurrence_key(entry, job_id))
        secondary = [
            str(_mapping(item).get("location") or "").strip()
            for item in (
                row.get("secondaryLocations")
                if isinstance(row.get("secondaryLocations"), list)
                else []
            )
            if str(_mapping(item).get("location") or "").strip()
        ]
        job = JobData(
            title=title,
            company=entry.company_name,
            location=_location([row.get("location"), *secondary]),
            employment_type=str(row.get("employmentType") or "").strip(),
            description=_text(row.get("descriptionPlain") or row.get("descriptionHtml")),
            apply_url=apply_url,
            source_url=str(row.get("jobUrl") or apply_url),
            date_posted=str(row.get("publishedAt") or ""),
            keywords=[
                str(row.get(key) or "").strip()
                for key in ("department", "team", "workplaceType")
                if str(row.get(key) or "").strip()
            ],
        )
        if _matches_intents(job, intents):
            postings.append(
                _posting(
                    job,
                    entry=entry,
                    external_posting_id=job_id,
                    observed_at=observed,
                    revision_hint=row.get("publishedAt"),
                )
            )
    complete = offset + len(window) >= len(listed_rows)
    next_cursor = next_cursor.model_copy(
        update={"cursor": {"offset": 0 if complete else offset + len(window)}}
    )
    return DiscoveryPage(
        postings=tuple(postings),
        snapshot_occurrence_keys=tuple(snapshot_keys),
        cursor=next_cursor,
        complete_snapshot=complete,
    )


async def fetch_smartrecruiters_page(
    entry: EmployerCatalogEntry,
    cursor: DiscoveryCursor,
    client: DiscoveryHttpClient,
    intents: tuple[SearchIntentV1, ...],
    *,
    max_jobs: int,
) -> DiscoveryPage:
    offset = int(cursor.cursor.get("offset") or 0)
    limit = min(100, max_jobs)
    response = await client.get(
        f"https://api.smartrecruiters.com/v1/companies/{entry.tenant_key}/postings",
        params={"offset": offset, "limit": limit},
        allowed_hosts=_SMARTRECRUITERS_HOSTS,
    )
    # SmartRecruiters also emits page-level validators. Treating those as a
    # collection identity causes false drift on every page transition.
    next_cursor = _next_cursor(
        cursor,
        response,
        values={"offset": offset + limit},
        capture_validators=False,
    )
    if response.status_code == 304:
        raise ValueError("SOURCE_UNEXPECTED_NOT_MODIFIED")
    try:
        payload: dict[str, Any] = response.json()
        rows = payload.get("content", [])
        total = int(payload.get("totalFound") or len(rows))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("SOURCE_PAYLOAD_INVALID") from exc
    if not isinstance(rows, list):
        raise ValueError("SOURCE_PAYLOAD_INVALID")
    observed = datetime.now(UTC)
    postings: list[DiscoveredPosting] = []
    snapshot_keys: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or row.get("uuid") or "").strip()
        title = str(row.get("name") or "").strip()
        if not job_id or not title:
            continue
        snapshot_keys.append(_occurrence_key(entry, job_id))
        location = _mapping(row.get("location"))
        candidate_url = f"https://jobs.smartrecruiters.com/{entry.tenant_key}/{job_id}"
        job = JobData(
            title=title,
            company=str(_mapping(row.get("company")).get("name") or entry.company_name),
            location=_location(
                [location.get("city"), location.get("region"), location.get("country")]
            ),
            employment_type=str(_mapping(row.get("typeOfEmployment")).get("label") or ""),
            seniority=str(_mapping(row.get("experienceLevel")).get("label") or ""),
            description="",
            apply_url=candidate_url,
            source_url=candidate_url,
            date_posted=str(row.get("releasedDate") or ""),
            keywords=[
                str(_mapping(row.get(key)).get("label") or "").strip()
                for key in ("department", "function", "industry")
                if str(_mapping(row.get(key)).get("label") or "").strip()
            ],
        )
        if _matches_intents(job, intents):
            postings.append(
                _posting(
                    job,
                    entry=entry,
                    external_posting_id=job_id,
                    observed_at=observed,
                    revision_hint=row.get("releasedDate"),
                )
            )
    complete = offset + len(rows) >= total
    if complete:
        next_cursor = next_cursor.model_copy(update={"cursor": {"offset": 0}})
    return DiscoveryPage(
        postings=tuple(postings),
        snapshot_occurrence_keys=tuple(snapshot_keys),
        cursor=next_cursor,
        complete_snapshot=complete,
    )


async def fetch_catalog_page(
    entry: EmployerCatalogEntry,
    cursor: DiscoveryCursor,
    client: DiscoveryHttpClient,
    intents: tuple[SearchIntentV1, ...],
    *,
    max_jobs: int,
) -> DiscoveryPage:
    adapters = {
        "greenhouse": fetch_greenhouse_page,
        "lever": fetch_lever_page,
        "ashby": fetch_ashby_page,
        "smartrecruiters": fetch_smartrecruiters_page,
    }
    adapter = adapters.get(entry.ats)
    if adapter is None:
        raise ValueError("SOURCE_ADAPTER_NOT_IMPLEMENTED")
    return await adapter(entry, cursor, client, intents, max_jobs=max_jobs)
