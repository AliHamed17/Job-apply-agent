"""Permitted generic feed and JSON-LD discovery with fail-closed robots checks."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from discovery.contracts import (
    DiscoveredPosting,
    DiscoveryCursor,
    DiscoveryPage,
    EmployerCatalogEntry,
    JobSourceOccurrence,
    SearchIntentV1,
    stable_digest,
)
from discovery.http_client import DiscoveryFetchError, DiscoveryHttpClient
from discovery.source_adapters import source_key_for
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData
from jobs.parsers.jsonld import parse_jsonld

_USER_AGENT = "JobApplyAgent/0.2"


async def require_public_https_url(url: str) -> str:
    """Reject credentials, non-HTTPS schemes, and private/reserved DNS targets."""

    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").casefold()
    except (ValueError, UnicodeError) as exc:
        raise DiscoveryFetchError("SOURCE_URL_UNSAFE") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DiscoveryFetchError("SOURCE_URL_UNSAFE")
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise DiscoveryFetchError("SOURCE_DNS_FAILED") from exc
    resolved = {item[4][0].split("%", 1)[0] for item in addresses}
    if not resolved or any(not ipaddress.ip_address(value).is_global for value in resolved):
        raise DiscoveryFetchError("SOURCE_ADDRESS_NOT_PUBLIC")
    return host


async def _robots_allowed(
    client: DiscoveryHttpClient,
    *,
    url: str,
    host: str,
) -> bool:
    parsed = urlsplit(url)
    robots_url = f"https://{parsed.netloc}/robots.txt"
    try:
        response = await client.get(
            robots_url,
            allowed_hosts=frozenset({host}),
        )
    except DiscoveryFetchError as exc:
        if exc.status_code == 404 or exc.reason_code == "SOURCE_TENANT_NOT_FOUND":
            return True
        raise DiscoveryFetchError("ROBOTS_UNAVAILABLE") from exc
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines())
    return parser.can_fetch(_USER_AGENT, url)


def _matches(job: JobData, intents: tuple[SearchIntentV1, ...]) -> bool:
    if not intents:
        return True
    content = f"{job.title} {job.description} {' '.join(job.keywords)}".casefold()
    return any(
        any(term.casefold() in job.title.casefold() for term in intent.titles)
        or sum(1 for skill in set(intent.skills) if skill.casefold() in content) >= 2
        for intent in intents
    )


def _posting(
    job: JobData,
    *,
    entry: EmployerCatalogEntry,
    observed_at: datetime,
) -> DiscoveredPosting:
    normalized_url = normalize_url(job.apply_url or job.source_url)
    normalized_hash = url_hash(normalized_url)
    external_id = normalized_hash
    source_key = source_key_for(entry)
    return DiscoveredPosting(
        job=job,
        occurrence=JobSourceOccurrence(
            occurrence_key=stable_digest(
                {
                    "source_key": source_key,
                    "catalog_key": entry.catalog_key,
                    "external_posting_id": external_id,
                }
            ),
            source_key=source_key,
            catalog_key=entry.catalog_key,
            external_posting_id=external_id,
            normalized_url=normalized_url,
            normalized_url_hash=normalized_hash,
            revision_digest=stable_digest(job.model_dump(mode="json")),
            observed_at=observed_at,
        ),
    )


def _occurrence_key(job: JobData, entry: EmployerCatalogEntry) -> str:
    normalized_url = normalize_url(job.apply_url or job.source_url)
    normalized_hash = url_hash(normalized_url)
    return stable_digest(
        {
            "source_key": source_key_for(entry),
            "catalog_key": entry.catalog_key,
            "external_posting_id": normalized_hash,
        }
    )


def _feed_links(content: str, *, base_url: str) -> list[str]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("SOURCE_PAYLOAD_INVALID") from exc
    links: list[str] = []
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1].casefold()
        candidate = ""
        if local == "loc":
            candidate = (element.text or "").strip()
        elif local == "link":
            candidate = str(element.attrib.get("href") or "").strip()
            if not candidate and (element.text or "").strip().startswith("http"):
                candidate = (element.text or "").strip()
        if candidate:
            links.append(urljoin(base_url, candidate))
    return list(dict.fromkeys(links))


async def fetch_generic_page(
    entry: EmployerCatalogEntry,
    cursor: DiscoveryCursor,
    client: DiscoveryHttpClient,
    intents: tuple[SearchIntentV1, ...],
    *,
    max_jobs: int,
) -> DiscoveryPage:
    """Fetch a configured JSON-LD page or same-host sitemap/feed page."""

    if entry.base_url is None:
        raise ValueError("SOURCE_BASE_URL_REQUIRED")
    base_url = str(entry.base_url)
    host = await require_public_https_url(base_url)
    if not await _robots_allowed(client, url=base_url, host=host):
        raise DiscoveryFetchError("ROBOTS_DISALLOWED")
    offset = int(cursor.cursor.get("offset") or 0)
    headers: dict[str, str] = {}
    if cursor.etag and offset == 0:
        headers["If-None-Match"] = cursor.etag
    if cursor.last_modified and offset == 0:
        headers["If-Modified-Since"] = cursor.last_modified
    response = await client.get(
        base_url,
        headers=headers,
        allowed_hosts=frozenset({host}),
    )
    if offset > 0:
        current_etag = response.headers.get("ETag")
        current_modified = response.headers.get("Last-Modified")
        if (
            cursor.etag
            and current_etag
            and cursor.etag != current_etag
            or cursor.last_modified
            and current_modified
            and cursor.last_modified != current_modified
        ):
            return DiscoveryPage(
                cursor=DiscoveryCursor(
                    source_key=cursor.source_key,
                    catalog_key=cursor.catalog_key,
                    cursor={"offset": 0},
                ),
                restart_snapshot=True,
            )
    next_cursor = DiscoveryCursor(
        source_key=cursor.source_key,
        catalog_key=cursor.catalog_key,
        cursor=cursor.cursor,
        etag=response.headers.get("ETag") or cursor.etag,
        last_modified=response.headers.get("Last-Modified") or cursor.last_modified,
        last_seen_posting_at=datetime.now(UTC),
    )
    if response.status_code == 304:
        return DiscoveryPage(cursor=next_cursor, complete_snapshot=True, not_modified=True)

    observed = datetime.now(UTC)
    if entry.ats == "generic_jsonld":
        jobs = parse_jsonld(response.text, base_url, require_explicit_url=True)
        window = jobs[offset : offset + max_jobs]
        jsonld_postings = tuple(
            _posting(job, entry=entry, observed_at=observed)
            for job in window
            if _matches(job, intents)
        )
        complete = offset + len(window) >= len(jobs)
        next_cursor = next_cursor.model_copy(
            update={"cursor": {"offset": 0 if complete else offset + len(window)}}
        )
        return DiscoveryPage(
            postings=jsonld_postings,
            snapshot_occurrence_keys=tuple(_occurrence_key(job, entry) for job in window),
            cursor=next_cursor,
            complete_snapshot=complete,
        )

    links = _feed_links(response.text, base_url=base_url)
    selected = links[offset : offset + max_jobs]
    postings: list[DiscoveredPosting] = []
    snapshot_keys: list[str] = []
    for url in selected:
        parsed = urlsplit(url)
        if (parsed.hostname or "").rstrip(".").casefold() != host:
            continue
        if not await _robots_allowed(client, url=url, host=host):
            continue
        page = await client.get(url, allowed_hosts=frozenset({host}))
        for job in parse_jsonld(page.text, url, require_explicit_url=True):
            snapshot_keys.append(_occurrence_key(job, entry))
            if _matches(job, intents):
                postings.append(_posting(job, entry=entry, observed_at=observed))
    complete = offset + len(selected) >= len(links)
    next_cursor = next_cursor.model_copy(
        update={"cursor": {"offset": 0 if complete else offset + len(selected)}}
    )
    return DiscoveryPage(
        postings=tuple(postings),
        snapshot_occurrence_keys=tuple(snapshot_keys),
        cursor=next_cursor,
        complete_snapshot=complete,
    )
