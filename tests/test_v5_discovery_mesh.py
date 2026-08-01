from __future__ import annotations

import asyncio
import base64
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from profile.cv_routing import CVDefinition, CVRoutingConfig, RoutingOverride
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.application_mutations import mark_job_terminally_skipped
from db.models import (
    Base,
    DiscoveryCursorState,
    DiscoveryRun,
    EmployerCatalogEntryRecord,
    Job,
    JobSourceOccurrenceRecord,
    JobStatus,
)
from discovery.catalog import (
    build_catalog_entry,
    catalog_entry_from_url,
    upsert_catalog_entries,
)
from discovery.contracts import (
    DiscoveredPosting,
    DiscoveryCursor,
    DiscoveryPage,
    DiscoverySourceDescriptor,
    JobSourceOccurrence,
    SearchIntentV1,
    stable_digest,
)
from discovery.generic_sources import fetch_generic_page, require_public_https_url
from discovery.gmail_alerts import fetch_gmail_alert_page, parse_job_alert_message
from discovery.http_client import DiscoveryFetchError, DiscoveryHttpClient
from discovery.locks import reconcile_stale_discovery_runs, try_discovery_lock
from discovery.mesh import _run_catalog_source
from discovery.persistence import (
    ingest_discovered_postings,
    mark_source_result,
    reconcile_source_snapshot,
    save_cursor,
    upsert_source_state,
)
from discovery.search_intents import (
    activate_search_intents,
    active_search_intents,
    derive_search_intents,
)
from discovery.settings import DiscoveryMeshSettings
from discovery.source_adapters import (
    descriptor_for,
    fetch_ashby_page,
    fetch_greenhouse_page,
    fetch_lever_page,
    fetch_smartrecruiters_page,
    source_key_for,
)
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData


def _factory(tmp_path, name="mesh.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _routing(count: int = 12) -> CVRoutingConfig:
    cvs = [
        CVDefinition(
            id=f"cv-{index:02d}",
            file=f"cv-{index:02d}.pdf",
            title_terms=[f"Role {index}", f"Engineer {index}"],
            skills=["Python", f"Skill {index}"],
            seniority=["mid", "senior"],
        )
        for index in range(count)
    ]
    overrides = (
        [RoutingOverride(cv_id="cv-03", title_contains=["Machine Learning Engineer"])]
        if count > 3
        else []
    )
    return CVRoutingConfig(cvs=cvs, overrides=overrides)


def _intent() -> SearchIntentV1:
    return derive_search_intents(_routing(1), profile_locations=["Tel Aviv"])[0]


def _entry(ats: str, *, region: str = "global"):
    return build_catalog_entry(
        company_name="Example",
        ats=ats,
        tenant_key="example",
        region=region,
        base_url={
            "greenhouse": "https://boards.greenhouse.io/example",
            "lever": "https://jobs.lever.co/example",
            "ashby": "https://jobs.ashbyhq.com/example",
            "smartrecruiters": "https://careers.smartrecruiters.com/example",
        }.get(ats),
    )


def _cursor(entry) -> DiscoveryCursor:
    return DiscoveryCursor(
        source_key=source_key_for(entry),
        catalog_key=entry.catalog_key,
    )


def _mock_client(handler):
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return raw, DiscoveryHttpClient(timeout_seconds=2, client=raw)


def _posting(
    *,
    source_key: str,
    occurrence_key: str,
    url: str,
    revision: str,
    title: str = "Machine Learning Engineer",
    description: str = "Python ML systems",
    catalog_key: str | None = None,
    external_posting_id: str | None = None,
) -> DiscoveredPosting:
    normalized = normalize_url(url)
    return DiscoveredPosting(
        job=JobData(
            title=title,
            company="Example",
            location="Tel Aviv",
            description=description,
            apply_url=normalized,
            source_url=normalized,
            keywords=["Python", "ML"],
        ),
        occurrence=JobSourceOccurrence(
            occurrence_key=stable_digest(occurrence_key),
            source_key=source_key,
            catalog_key=catalog_key,
            external_posting_id=external_posting_id or occurrence_key,
            normalized_url=normalized,
            normalized_url_hash=url_hash(normalized),
            revision_digest=stable_digest(revision),
            observed_at=datetime.now(UTC),
        ),
    )


def test_source_descriptor_requires_reason_exactly_when_disabled():
    base = {
        "source_key": "example",
        "source_type": "remotive",
        "semantic_version": "1.0.0",
        "transport": "public_api",
        "authentication_mode": "none",
        "host": "example.test",
        "cadence_seconds": 600,
        "supports_cursor": False,
        "supports_conditional_requests": False,
        "tenant_scoped": False,
    }
    assert DiscoverySourceDescriptor(**base).enabled is True
    with pytest.raises(ValidationError):
        DiscoverySourceDescriptor(**base, enabled=False)
    disabled = DiscoverySourceDescriptor(
        **base,
        enabled=False,
        disabled_reason="OPERATOR_DISABLED",
    )
    assert disabled.disabled_reason == "OPERATOR_DISABLED"


def test_discovery_settings_enforce_source_cadence_and_payload_bounds():
    settings = DiscoveryMeshSettings(_env_file=None)
    assert settings.discovery_scheduler_interval_seconds == 60
    assert settings.discovery_poll_interval_seconds == 600
    assert settings.public_discovery_interval_h == 6
    with pytest.raises(ValidationError):
        DiscoveryMeshSettings(_env_file=None, discovery_poll_interval_seconds=599)
    with pytest.raises(ValidationError):
        DiscoveryMeshSettings(_env_file=None, discovery_http_max_response_bytes=512)


def test_search_intents_cover_all_twelve_cvs_and_default_geography():
    first = derive_search_intents(_routing(), profile_locations=["Haifa", "Israel"])
    second = derive_search_intents(_routing(), profile_locations=["Haifa", "Israel"])

    assert first == second
    assert len(first) == 12
    assert {intent.cv_id for intent in first} == {f"cv-{index:02d}" for index in range(12)}
    assert all("Israel" in intent.locations for intent in first)
    assert all("Worldwide Remote" in intent.locations for intent in first)
    assert (
        "Machine Learning Engineer"
        in next(intent for intent in first if intent.cv_id == "cv-03").titles
    )


def test_search_intent_activation_is_immutable_and_idempotent(tmp_path):
    engine, factory = _factory(tmp_path, "intents.db")
    db = factory()
    intents = derive_search_intents(_routing(2), profile_locations=["Israel"])

    first = activate_search_intents(db, intents)
    same = activate_search_intents(db, intents)
    source = upsert_source_state(
        db,
        DiscoverySourceDescriptor(
            source_key="remotive",
            source_type="remotive",
            semantic_version="1.0.0",
            transport="public_api",
            authentication_mode="none",
            host="remotive.com",
            cadence_seconds=21_600,
            supports_cursor=False,
            supports_conditional_requests=False,
            tenant_scoped=False,
        ),
    )
    save_cursor(
        db,
        DiscoveryCursor(source_key="remotive", cursor={"offset": 10}),
        catalog=None,
    )
    source.next_poll_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    db.commit()
    changed = activate_search_intents(
        db,
        derive_search_intents(_routing(3), profile_locations=["Israel"]),
    )

    assert first.id == same.id
    assert changed.version == first.version + 1
    version, loaded = active_search_intents(db)
    assert version == changed.version
    assert len(loaded) == 3
    assert db.query(DiscoveryCursorState).count() == 0
    db.refresh(source)
    assert source.next_poll_at is None
    assert source.health_status == "unknown"
    db.close()
    engine.dispose()


@pytest.mark.parametrize(
    ("url", "ats", "tenant", "region"),
    [
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse", "acme", "global"),
        ("https://jobs.lever.co/acme/abc", "lever", "acme", "global"),
        ("https://jobs.eu.lever.co/acme/abc", "lever", "acme", "eu"),
        ("https://jobs.ashbyhq.com/acme/abc", "ashby", "acme", "global"),
        (
            "https://careers.smartrecruiters.com/acme/job",
            "smartrecruiters",
            "acme",
            "global",
        ),
    ],
)
def test_catalog_learns_tenant_scoped_identifiers(url, ats, tenant, region):
    entry = catalog_entry_from_url(url)
    assert entry is not None
    assert (entry.ats, entry.tenant_key, entry.region) == (ats, tenant, region)
    assert len(source_key_for(entry)) == 64
    assert tenant.casefold() not in source_key_for(entry).casefold()


async def test_http_transport_honors_retry_after_then_recovers(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    raw, client = _mock_client(handler)
    sleep = MagicMock()

    async def no_sleep(delay):
        sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    response = await client.get(
        "https://api.lever.co/v0/postings/example",
        allowed_hosts=frozenset({"api.lever.co"}),
    )
    await raw.aclose()

    assert response.json() == {"ok": True}
    assert calls == 2
    sleep.assert_called_once_with(0.0)


async def test_http_transport_defers_long_retry_after_to_scheduler(monkeypatch):
    async def handler(request):
        return httpx.Response(429, headers={"Retry-After": "3600"}, request=request)

    raw, client = _mock_client(handler)
    sleep = MagicMock()

    async def no_sleep(delay):
        sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(DiscoveryFetchError, match="SOURCE_RATE_LIMITED") as failure:
        await client.get("https://api.lever.co/v0/postings/example")
    await raw.aclose()

    assert failure.value.retry_after_seconds == 3600
    sleep.assert_not_called()


async def test_http_transport_retries_timeout_then_reports_stable_reason(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture timeout", request=request)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DiscoveryHttpClient(
        timeout_seconds=2,
        max_attempts=2,
        base_backoff_seconds=0,
        client=raw,
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(DiscoveryFetchError, match="SOURCE_TIMEOUT"):
        await client.get("https://api.lever.co/v0/postings/example")
    await raw.aclose()
    assert calls == 2


async def test_http_transport_serializes_requests_per_host():
    active = 0
    maximum = 0
    gate = asyncio.Event()

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 1:
            gate.set()
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={}, request=request)

    raw, client = _mock_client(handler)
    first = asyncio.create_task(client.get("https://api.lever.co/a"))
    await gate.wait()
    second = asyncio.create_task(client.get("https://api.lever.co/b"))
    await asyncio.gather(first, second)
    await raw.aclose()
    assert maximum == 1


async def test_http_transport_rejects_allowlist_bypassing_redirects():
    async def handler(request):
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/private"},
            request=request,
        )

    raw, client = _mock_client(handler)
    with pytest.raises(DiscoveryFetchError, match="SOURCE_REDIRECT_NOT_ALLOWED"):
        await client.get(
            "https://api.lever.co/v0/postings/example",
            allowed_hosts=frozenset({"api.lever.co"}),
        )
    await raw.aclose()


async def test_http_transport_rejects_oversized_payloads():
    async def handler(request):
        return httpx.Response(200, content=b"x" * 2048, request=request)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DiscoveryHttpClient(
        timeout_seconds=2,
        max_response_bytes=1024,
        client=raw,
    )
    with pytest.raises(DiscoveryFetchError, match="SOURCE_PAYLOAD_TOO_LARGE"):
        await client.get("https://api.lever.co/v0/postings/example")
    await raw.aclose()


async def test_http_transport_stops_streaming_at_payload_limit():
    class ChunkedStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.yielded = 0

        async def __aiter__(self):
            for _ in range(4):
                self.yielded += 1
                yield b"x" * 700

    stream = ChunkedStream()

    async def handler(request):
        return httpx.Response(200, stream=stream, request=request)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DiscoveryHttpClient(
        timeout_seconds=2,
        max_response_bytes=1024,
        client=raw,
    )
    with pytest.raises(DiscoveryFetchError, match="SOURCE_PAYLOAD_TOO_LARGE"):
        await client.get("https://api.lever.co/v0/postings/example")
    await raw.aclose()
    assert stream.yielded == 2


async def test_greenhouse_conditional_snapshot_tracks_unmatched_ids():
    async def handler(request):
        assert request.headers["if-none-match"] == '"old"'
        return httpx.Response(
            200,
            headers={"ETag": '"new"'},
            json={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Role 0",
                        "absolute_url": "https://boards.greenhouse.io/example/jobs/1",
                        "location": {"name": "Tel Aviv"},
                        "content": "<p>Python Skill 0</p>",
                    },
                    {
                        "id": 2,
                        "title": "Account Executive",
                        "absolute_url": "https://boards.greenhouse.io/example/jobs/2",
                        "location": {"name": "Tel Aviv"},
                        "content": "Sales",
                    },
                ]
            },
            request=request,
        )

    entry = _entry("greenhouse")
    cursor = _cursor(entry).model_copy(update={"etag": '"old"'})
    raw, client = _mock_client(handler)
    page = await fetch_greenhouse_page(entry, cursor, client, (_intent(),), max_jobs=10)
    await raw.aclose()

    assert len(page.postings) == 1
    assert len(page.snapshot_occurrence_keys) == 2
    assert page.complete_snapshot is True
    assert page.cursor.etag == '"new"'


async def test_greenhouse_not_modified_and_malformed_payload_are_explicit():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(304, request=request)
        return httpx.Response(200, json=[], request=request)

    entry = _entry("greenhouse")
    raw, client = _mock_client(handler)
    unchanged = await fetch_greenhouse_page(
        entry,
        _cursor(entry),
        client,
        (_intent(),),
        max_jobs=10,
    )
    with pytest.raises(ValueError, match="SOURCE_PAYLOAD_INVALID"):
        await fetch_greenhouse_page(
            entry,
            _cursor(entry),
            client,
            (_intent(),),
            max_jobs=10,
        )
    await raw.aclose()

    assert unchanged.not_modified is True
    assert unchanged.complete_snapshot is True


async def test_greenhouse_locally_pages_complete_snapshot_without_losing_rows():
    conditional_headers: list[str | None] = []

    async def handler(request):
        conditional_headers.append(request.headers.get("If-None-Match"))
        return httpx.Response(
            200,
            headers={"ETag": '"snapshot"'},
            json={
                "jobs": [
                    {
                        "id": job_id,
                        "title": "Role 0",
                        "absolute_url": (f"https://boards.greenhouse.io/example/jobs/{job_id}"),
                        "content": "Python Skill 0",
                    }
                    for job_id in (1, 2)
                ]
            },
            request=request,
        )

    entry = _entry("greenhouse")
    raw, client = _mock_client(handler)
    first = await fetch_greenhouse_page(
        entry,
        _cursor(entry),
        client,
        (_intent(),),
        max_jobs=1,
    )
    second = await fetch_greenhouse_page(
        entry,
        first.cursor,
        client,
        (_intent(),),
        max_jobs=1,
    )
    await raw.aclose()

    assert conditional_headers == [None, None]
    assert first.complete_snapshot is False
    assert second.complete_snapshot is True
    assert len(set(first.snapshot_occurrence_keys + second.snapshot_occurrence_keys)) == 2


async def test_lever_cursor_paginates_without_losing_offset():
    offsets: list[int] = []

    async def handler(request):
        offset = int(request.url.params["skip"])
        offsets.append(offset)
        count = 2 if offset == 0 else 1
        rows = [
            {
                "id": f"id-{offset + index}",
                "text": "Role 0",
                "applyUrl": f"https://jobs.lever.co/example/id-{offset + index}/apply",
                "hostedUrl": f"https://jobs.lever.co/example/id-{offset + index}",
                "descriptionPlain": "Python Skill 0",
                "categories": {"location": "Israel"},
            }
            for index in range(count)
        ]
        return httpx.Response(200, json=rows, request=request)

    entry = _entry("lever")
    raw, client = _mock_client(handler)
    first = await fetch_lever_page(entry, _cursor(entry), client, (_intent(),), max_jobs=2)
    second = await fetch_lever_page(entry, first.cursor, client, (_intent(),), max_jobs=2)
    await raw.aclose()

    assert first.complete_snapshot is False
    assert second.complete_snapshot is True
    assert offsets == [0, 2]
    assert second.cursor.cursor["offset"] == 0
    assert len(first.snapshot_occurrence_keys) + len(second.snapshot_occurrence_keys) == 3


async def test_changed_etag_restarts_paginated_snapshot_from_origin():
    async def handler(request):
        return httpx.Response(
            200,
            headers={"ETag": '"new"'},
            json=[],
            request=request,
        )

    entry = _entry("lever")
    cursor = _cursor(entry).model_copy(update={"cursor": {"offset": 100}, "etag": '"old"'})
    raw, client = _mock_client(handler)
    page = await fetch_lever_page(
        entry,
        cursor,
        client,
        (_intent(),),
        max_jobs=100,
    )
    await raw.aclose()

    assert page.restart_snapshot is True
    assert page.cursor.cursor == {"offset": 0}
    assert page.cursor.etag is None


async def test_ashby_and_smartrecruiters_public_shapes():
    async def ashby_handler(request):
        return httpx.Response(
            200,
            json={
                "apiVersion": "1",
                "jobs": [
                    {
                        "title": "Role 0",
                        "location": "Remote - EMEA",
                        "isListed": True,
                        "isRemote": True,
                        "descriptionPlain": "Python Skill 0",
                        "jobUrl": "https://jobs.ashbyhq.com/example/abc",
                        "applyUrl": "https://jobs.ashbyhq.com/example/abc/application",
                    }
                ],
            },
            request=request,
        )

    ashby = _entry("ashby")
    raw, client = _mock_client(ashby_handler)
    ashby_page = await fetch_ashby_page(
        ashby,
        _cursor(ashby),
        client,
        (_intent(),),
        max_jobs=10,
    )
    await raw.aclose()

    async def smart_handler(request):
        assert request.url.params["offset"] == "0"
        return httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {
                        "id": "7498",
                        "name": "Role 0",
                        "company": {"name": "Example"},
                        "location": {"city": "Tel Aviv", "country": "il", "remote": True},
                        "function": {"label": "Python Skill 0"},
                    }
                ],
            },
            request=request,
        )

    smart = _entry("smartrecruiters")
    raw, client = _mock_client(smart_handler)
    smart_page = await fetch_smartrecruiters_page(
        smart,
        _cursor(smart),
        client,
        (_intent(),),
        max_jobs=10,
    )
    await raw.aclose()

    assert ashby_page.postings[0].job.location == "Remote - EMEA"
    assert smart_page.postings[0].job.apply_url.endswith("/example/7498")
    assert smart_page.complete_snapshot is True


def test_gmail_alert_parser_keeps_message_and_identity_content_out_of_result():
    private_marker = "private.person@example.com"
    raw = f"""From: alerts@example.test
To: {private_marker}
Subject: Jobs for you
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body>
<p>{private_marker}</p>
<a href="https://jobs.lever.co/acme/job-123">Machine Learning Engineer</a>
<a href="https://evil.example/job">Machine Learning Engineer</a>
</body></html>
""".encode()
    postings = parse_job_alert_message(
        raw,
        message_id="gmail-message-1",
        internal_date_ms=1_784_800_000_000,
        source_key="gmail_alert",
        intents=(),
    )

    assert len(postings) == 1
    serialized = postings[0].model_dump_json()
    assert private_marker not in serialized
    assert "evil.example" not in serialized
    assert postings[0].occurrence.catalog_key is not None


@pytest.mark.parametrize(
    ("fixture_name", "provider"),
    [
        ("linkedin-alert.eml", "linkedin_alert"),
        ("drushim-alert.eml", "drushim_alert"),
        ("alljobs-alert.eml", "alljobs_alert"),
    ],
)
def test_sanitized_job_alert_fixtures_are_parsed(fixture_name, provider):
    raw = (Path(__file__).parent / "fixtures" / "discovery" / fixture_name).read_bytes()
    postings = parse_job_alert_message(
        raw,
        message_id=stable_digest(fixture_name),
        internal_date_ms=1_784_800_000_000,
        source_key="gmail_alert",
        intents=(),
    )

    assert len(postings) == 1
    assert postings[0].job.keywords == [provider]
    assert "candidate@example.test" not in postings[0].model_dump_json()


async def test_gmail_detail_failure_does_not_advance_page_checkpoint(tmp_path):
    encoded = base64.urlsafe_b64encode(
        b"""From: alerts@example.test
To: candidate@example.test
Subject: Jobs
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<a href="https://jobs.lever.co/example/newer">Machine Learning Engineer</a>
"""
    ).decode()
    requested_details: list[str] = []

    async def handler(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={"messages": [{"id": "newer"}, {"id": "older"}]},
                request=request,
            )
        message_id = request.url.path.rsplit("/", 1)[-1]
        requested_details.append(message_id)
        if message_id == "newer":
            return httpx.Response(
                200,
                json={"internalDate": "1784800000000", "raw": encoded},
                request=request,
            )
        return httpx.Response(503, request=request)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with (
        patch(
            "discovery.gmail_alerts._load_oauth_state",
            return_value={
                "access_token": "fixture-local-token",
                "expires_at": datetime.now(UTC).timestamp() + 3600,
            },
        ),
        pytest.raises(ValueError, match="GMAIL_ALERT_MESSAGE_FETCH_FAILED"),
    ):
        await fetch_gmail_alert_page(
            oauth_path=tmp_path / "unused-local-oauth.json",
            label="JobApplyAgent",
            cursor=DiscoveryCursor(source_key="gmail_alert"),
            intents=(),
            max_messages=10,
            client=raw,
        )
    await raw.aclose()

    assert requested_details == ["newer", "older"]


async def test_generic_source_rejects_loopback_dns(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(DiscoveryFetchError, match="SOURCE_ADDRESS_NOT_PUBLIC"):
        await require_public_https_url("https://example.test/jobs")


async def test_generic_feed_pagination_does_not_send_first_page_validator(
    monkeypatch,
):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )
    sitemap_requests: list[str | None] = []

    async def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if request.url.path == "/jobs.xml":
            sitemap_requests.append(request.headers.get("If-None-Match"))
            return httpx.Response(
                200,
                headers={"ETag": '"snapshot-1"'},
                text=(
                    "<urlset>"
                    "<url><loc>https://careers.example.test/jobs/1</loc></url>"
                    "<url><loc>https://careers.example.test/jobs/2</loc></url>"
                    "</urlset>"
                ),
                request=request,
            )
        job_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            text=(
                '<script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"JobPosting",'
                f'"title":"Role 0","description":"Python Skill 0",'
                f'"url":"https://careers.example.test/jobs/{job_id}",'
                '"hiringOrganization":{"name":"Example"}}'
                "</script>"
            ),
            request=request,
        )

    entry = build_catalog_entry(
        company_name="Example",
        ats="generic_feed",
        tenant_key="example-feed",
        base_url="https://careers.example.test/jobs.xml",
    )
    raw, client = _mock_client(handler)
    first = await fetch_generic_page(
        entry,
        _cursor(entry),
        client,
        (_intent(),),
        max_jobs=1,
    )
    second = await fetch_generic_page(
        entry,
        first.cursor,
        client,
        (_intent(),),
        max_jobs=1,
    )
    await raw.aclose()

    assert sitemap_requests == [None, None]
    assert first.complete_snapshot is False
    assert second.complete_snapshot is True
    assert len(first.postings) == len(second.postings) == 1


def test_cross_source_dedup_preserves_both_occurrences(tmp_path):
    engine, factory = _factory(tmp_path, "dedup.db")
    db = factory()
    first = _posting(
        source_key="greenhouse:one",
        occurrence_key="greenhouse-1",
        url="https://careers.example.test/jobs/42?utm_source=one",
        revision="r1",
    )
    second = _posting(
        source_key="gmail_alert",
        occurrence_key="gmail-1",
        url="https://careers.example.test/jobs/42?utm_source=two",
        revision="r1",
    )
    with patch("worker.tasks.score_job_task") as score:
        first_stats = ingest_discovered_postings(
            db,
            (first,),
            tasks_always_eager=False,
            preparation_ready=False,
        )
        second_stats = ingest_discovered_postings(
            db,
            (second,),
            tasks_always_eager=False,
            preparation_ready=False,
        )

    assert first_stats.inserted == 1
    assert second_stats.inserted == 0
    assert db.query(Job).count() == 1
    assert db.query(JobSourceOccurrenceRecord).count() == 2
    assert score.delay.call_count == 2
    db.close()
    engine.dispose()


def test_richer_ats_occurrence_upgrades_sparse_alert_job(tmp_path):
    engine, factory = _factory(tmp_path, "richer-occurrence.db")
    db = factory()
    alert = _posting(
        source_key="gmail_alert",
        occurrence_key="alert-1",
        url="https://jobs.lever.co/example/job-42",
        revision="alert",
        description="",
    )
    ats = _posting(
        source_key="lever-source",
        occurrence_key="lever-42",
        url="https://jobs.lever.co/example/job-42",
        revision="ats",
        description="Detailed Python and machine-learning responsibilities.",
    )
    with patch("worker.tasks.score_job_task"):
        ingest_discovered_postings(
            db,
            (alert,),
            tasks_always_eager=False,
            preparation_ready=False,
        )
        stats = ingest_discovered_postings(
            db,
            (ats,),
            tasks_always_eager=False,
            preparation_ready=False,
        )

    assert db.query(Job).count() == 1
    assert db.query(JobSourceOccurrenceRecord).count() == 2
    assert stats.updated == 1
    assert db.query(Job).one().description.startswith("Detailed Python")
    db.close()
    engine.dispose()


def test_catalog_posting_id_deduplicates_hosted_and_apply_urls(tmp_path):
    engine, factory = _factory(tmp_path, "posting-id-dedup.db")
    db = factory()
    entry = _entry("lever")
    upsert_catalog_entries(db, (entry,))
    alert = _posting(
        source_key="gmail_alert",
        occurrence_key="gmail-message-job-42",
        url="https://jobs.lever.co/example/job-42",
        revision="alert",
        catalog_key=entry.catalog_key,
        external_posting_id="job-42",
    )
    feed = _posting(
        source_key=source_key_for(entry),
        occurrence_key="lever-job-42",
        url="https://jobs.lever.co/example/job-42/apply",
        revision="feed",
        catalog_key=entry.catalog_key,
        external_posting_id="job-42",
    )
    with patch("worker.tasks.score_job_task"):
        ingest_discovered_postings(
            db,
            (alert, feed),
            tasks_always_eager=False,
            preparation_ready=False,
        )

    assert db.query(Job).count() == 1
    assert db.query(JobSourceOccurrenceRecord).count() == 2
    db.close()
    engine.dispose()


def test_distinct_urls_with_same_title_company_location_remain_distinct(tmp_path):
    engine, factory = _factory(tmp_path, "distinct-requisitions.db")
    db = factory()
    first = _posting(
        source_key="greenhouse-one",
        occurrence_key="req-100",
        url="https://boards.greenhouse.io/example/jobs/100",
        revision="r1",
    )
    second = _posting(
        source_key="greenhouse-one",
        occurrence_key="req-200",
        url="https://boards.greenhouse.io/example/jobs/200",
        revision="r1",
    )
    with patch("worker.tasks.score_job_task"):
        ingest_discovered_postings(
            db,
            (first, second),
            tasks_always_eager=False,
            preparation_ready=False,
        )

    assert db.query(Job).count() == 2
    assert db.query(JobSourceOccurrenceRecord).count() == 2
    db.close()
    engine.dispose()


def test_mail_alert_source_is_due_each_minute_without_jitter(tmp_path):
    engine, factory = _factory(tmp_path, "gmail-cadence.db")
    db = factory()
    descriptor = DiscoverySourceDescriptor(
        source_key="gmail_alert",
        source_type="gmail_alert",
        semantic_version="1.0.0",
        transport="oauth_mailbox",
        authentication_mode="oauth_local",
        host="gmail.googleapis.com",
        cadence_seconds=60,
        supports_cursor=True,
        supports_conditional_requests=False,
        tenant_scoped=False,
    )
    source = upsert_source_state(db, descriptor)
    before = datetime.now(UTC).replace(tzinfo=None)
    mark_source_result(db, source, success=True, reason_code=None)
    delay = (source.next_poll_at - before).total_seconds()
    assert 59 <= delay <= 62
    db.close()
    engine.dispose()


def test_degraded_source_recovers_after_next_success(tmp_path):
    engine, factory = _factory(tmp_path, "source-recovery.db")
    db = factory()
    descriptor = DiscoverySourceDescriptor(
        source_key="remotive",
        source_type="remotive",
        semantic_version="1.0.0",
        transport="public_api",
        authentication_mode="none",
        host="remotive.com",
        cadence_seconds=21_600,
        supports_cursor=False,
        supports_conditional_requests=False,
        tenant_scoped=False,
    )
    source = upsert_source_state(db, descriptor)
    mark_source_result(
        db,
        source,
        success=False,
        reason_code="SOURCE_TIMEOUT",
        retry_after_seconds=30,
    )
    assert (source.health_status, source.last_error_code) == (
        "degraded",
        "SOURCE_TIMEOUT",
    )

    mark_source_result(db, source, success=True, reason_code=None)
    assert source.health_status == "healthy"
    assert source.last_error_code is None
    assert source.last_success_at is not None
    db.close()
    engine.dispose()


def test_source_version_change_resets_cursor_and_forces_full_poll(tmp_path):
    engine, factory = _factory(tmp_path, "source-version-reset.db")
    db = factory()
    descriptor = DiscoverySourceDescriptor(
        source_key="remotive",
        source_type="remotive",
        semantic_version="1.0.0",
        transport="public_api",
        authentication_mode="none",
        host="remotive.com",
        cadence_seconds=21_600,
        supports_cursor=False,
        supports_conditional_requests=False,
        tenant_scoped=False,
    )
    source = upsert_source_state(db, descriptor)
    save_cursor(
        db,
        DiscoveryCursor(source_key="remotive", cursor={"offset": 100}),
        catalog=None,
    )
    source.next_poll_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    db.commit()

    upgraded = upsert_source_state(
        db,
        descriptor.model_copy(update={"semantic_version": "2.0.0"}),
    )

    assert db.query(DiscoveryCursorState).count() == 0
    assert upgraded.descriptor_version == "2.0.0"
    assert upgraded.next_poll_at is None
    assert upgraded.health_status == "unknown"
    db.close()
    engine.dispose()


def test_posting_revision_rescores_mutable_job_and_closure_is_explicit(tmp_path):
    engine, factory = _factory(tmp_path, "revision.db")
    db = factory()
    original = _posting(
        source_key="lever:one",
        occurrence_key="lever-1",
        url="https://jobs.example.test/1",
        revision="r1",
        description="Original",
    )
    changed = _posting(
        source_key="lever:one",
        occurrence_key="lever-1",
        url="https://jobs.example.test/1",
        revision="r2",
        description="Changed",
    )
    with patch("worker.tasks.score_job_task"):
        ingest_discovered_postings(
            db,
            (original,),
            tasks_always_eager=False,
            preparation_ready=False,
        )
        stats = ingest_discovered_postings(
            db,
            (changed,),
            tasks_always_eager=False,
            preparation_ready=False,
        )
    assert stats.updated == 1
    assert db.query(Job).one().description == "Changed"

    closed = reconcile_source_snapshot(
        db,
        source_key="lever:one",
        catalog_entry_id=None,
        seen_occurrence_keys=set(),
        observed_at=datetime.now(UTC),
    )
    assert closed == 1
    assert db.query(JobSourceOccurrenceRecord).one().active is False
    assert db.query(Job).one().status == JobStatus.SKIPPED
    assert db.query(Job).one().terminal_skip_at is None

    with patch("worker.tasks.score_job_task"):
        reopened = ingest_discovered_postings(
            db,
            (changed,),
            tasks_always_eager=False,
            preparation_ready=False,
        )
    assert reopened.updated == 1
    assert db.query(JobSourceOccurrenceRecord).one().active is True
    assert db.query(Job).one().status == JobStatus.EXTRACTED
    db.close()
    engine.dispose()


def test_operator_terminal_skip_survives_source_revision(tmp_path):
    engine, factory = _factory(tmp_path, "terminal-skip.db")
    db = factory()
    original = _posting(
        source_key="lever:one",
        occurrence_key="lever-terminal",
        url="https://jobs.example.test/terminal",
        revision="r1",
        description="Original",
    )
    changed = _posting(
        source_key="lever:one",
        occurrence_key="lever-terminal",
        url="https://jobs.example.test/terminal",
        revision="r2",
        description="Changed",
    )
    with patch("worker.tasks.score_job_task") as score:
        ingest_discovered_postings(
            db,
            (original,),
            tasks_always_eager=False,
            preparation_ready=False,
        )
        score.reset_mock()
        job = db.query(Job).one()
        mark_job_terminally_skipped(job)
        db.commit()

        stats = ingest_discovered_postings(
            db,
            (changed,),
            tasks_always_eager=False,
            preparation_ready=False,
        )

    job = db.query(Job).one()
    assert stats.updated == 0
    assert stats.queued == 0
    assert score.delay.call_count == 0
    assert job.status == JobStatus.SKIPPED
    assert job.terminal_skip_at is not None
    assert job.description == "Original"
    db.close()
    engine.dispose()


async def test_resumed_paginated_scan_cannot_close_unseen_first_page(tmp_path):
    engine, factory = _factory(tmp_path, "resumed-pagination.db")
    db = factory()
    entry = _entry("lever")
    upsert_catalog_entries(db, (entry,))
    catalog = db.query(EmployerCatalogEntryRecord).one()
    descriptor = descriptor_for(entry, cadence_seconds=600)
    source = upsert_source_state(db, descriptor)
    save_cursor(
        db,
        DiscoveryCursor(
            source_key=descriptor.source_key,
            catalog_key=entry.catalog_key,
            cursor={"offset": 100},
        ),
        catalog=catalog,
    )
    existing = _posting(
        source_key=descriptor.source_key,
        occurrence_key="first-page-requisition",
        url="https://jobs.lever.co/example/first-page-requisition",
        revision="r1",
    )
    with patch("worker.tasks.score_job_task"):
        ingest_discovered_postings(
            db,
            (existing,),
            tasks_always_eager=False,
            preparation_ready=False,
        )
    occurrence = db.query(JobSourceOccurrenceRecord).one()
    occurrence.catalog_entry_id = catalog.id
    db.commit()

    async def empty_final_page(*_args, **_kwargs):
        return DiscoveryPage(
            cursor=DiscoveryCursor(
                source_key=descriptor.source_key,
                catalog_key=entry.catalog_key,
                cursor={"offset": 0},
            ),
            complete_snapshot=True,
        )

    settings = SimpleNamespace(
        discovery_poll_interval_seconds=600,
        public_discovery_timeout_s=2.0,
        discovery_http_max_attempts=1,
        discovery_http_max_response_bytes=1024 * 1024,
        discovery_max_pages_per_run=2,
        public_discovery_max_jobs=100,
        tasks_always_eager=False,
    )
    with patch("discovery.mesh.fetch_catalog_page", new=empty_final_page):
        resumed = await _run_catalog_source(
            db,
            settings=settings,
            source=source,
            catalog=catalog,
            intents=(_intent(),),
            preparation_ready=False,
        )

    assert resumed.closed == 0
    assert db.query(JobSourceOccurrenceRecord).one().active is True

    save_cursor(
        db,
        DiscoveryCursor(
            source_key=descriptor.source_key,
            catalog_key=entry.catalog_key,
            cursor={"offset": 0},
        ),
        catalog=catalog,
    )
    with patch("discovery.mesh.fetch_catalog_page", new=empty_final_page):
        complete = await _run_catalog_source(
            db,
            settings=settings,
            source=source,
            catalog=catalog,
            intents=(_intent(),),
            preparation_ready=False,
        )

    assert complete.closed == 1
    assert db.query(JobSourceOccurrenceRecord).one().active is False
    db.close()
    engine.dispose()


def test_local_advisory_lock_prevents_overlap(tmp_path):
    engine, factory = _factory(tmp_path, "lock.db")
    first = factory()
    second = factory()
    with try_discovery_lock(first, "mesh") as first_acquired:
        with try_discovery_lock(second, "mesh") as second_acquired:
            assert first_acquired is True
            assert second_acquired is False
    first.close()
    second.close()
    engine.dispose()


def test_stale_running_discovery_is_reconciled(tmp_path):
    engine, factory = _factory(tmp_path, "stale.db")
    db = factory()
    db.add(
        DiscoveryRun(
            source="greenhouse:old",
            status="running",
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2),
        )
    )
    db.commit()

    assert reconcile_stale_discovery_runs(db, stale_after_seconds=1800) == 1
    run = db.query(DiscoveryRun).one()
    assert (run.status, run.reason_code) == ("failed", "STALE_RUN_RECOVERED")
    db.close()
    engine.dispose()


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)
def test_postgres_advisory_lock_prevents_overlapping_mesh_runs():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    acquired = threading.Event()
    release = threading.Event()
    result: list[bool] = []

    def hold_first_lock():
        db = factory()
        try:
            with try_discovery_lock(db, "v5-discovery-mesh-test") as owned:
                assert owned is True
                acquired.set()
                assert release.wait(timeout=10)
        finally:
            db.close()

    thread = threading.Thread(target=hold_first_lock)
    thread.start()
    assert acquired.wait(timeout=10)
    second = factory()
    try:
        with try_discovery_lock(second, "v5-discovery-mesh-test") as owned:
            result.append(owned)
    finally:
        second.close()
        release.set()
        thread.join(timeout=10)
        engine.dispose()

    assert result == [False]
    assert thread.is_alive() is False
