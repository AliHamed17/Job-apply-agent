"""Read-only Gmail-label alert ingestion with local OAuth state."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from discovery.catalog import catalog_entry_from_url
from discovery.contracts import (
    DiscoveredPosting,
    DiscoveryCursor,
    DiscoveryPage,
    JobSourceOccurrence,
    SearchIntentV1,
    stable_digest,
)
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData

_GMAIL_API_HOST = "gmail.googleapis.com"
_TOKEN_HOST = "oauth2.googleapis.com"
_ALERT_HOST_SUFFIXES = (
    "linkedin.com",
    "drushim.co.il",
    "alljobs.co.il",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
)
_GENERIC_ANCHOR_LABELS = frozenset(
    {
        "apply",
        "apply now",
        "view",
        "view job",
        "learn more",
        "ראה משרה",
        "הגש מועמדות",
    }
)
_LINKEDIN_ID = re.compile(r"/jobs/(?:view|collections/recommended)/(\d+)")


def _body_parts(message) -> tuple[str, str]:
    plain: list[str] = []
    rich: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeError):
            continue
        if not isinstance(value, str):
            continue
        (rich if content_type == "text/html" else plain).append(value)
    return "\n".join(plain), "\n".join(rich)


def _supported_alert_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").casefold()
    except (ValueError, UnicodeError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and any(host == suffix or host.endswith("." + suffix) for suffix in _ALERT_HOST_SUFFIXES)
    )


def _provider(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    if host.endswith("linkedin.com"):
        return "linkedin_alert"
    if host.endswith("drushim.co.il"):
        return "drushim_alert"
    if host.endswith("alljobs.co.il"):
        return "alljobs_alert"
    entry = catalog_entry_from_url(url)
    return f"{entry.ats}_alert" if entry else "job_alert"


def _external_id(url: str) -> str:
    match = _LINKEDIN_ID.search(url)
    if match:
        return match.group(1)
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[-1].casefold() in {"application", "apply"}:
        parts.pop()
    return parts[-1] if parts else url_hash(url)[:24]


def _matches_intents(title: str, intents: tuple[SearchIntentV1, ...]) -> bool:
    if not intents:
        return True
    lowered = title.casefold()
    return any(any(term.casefold() in lowered for term in intent.titles) for intent in intents)


def parse_job_alert_message(
    raw_message: bytes,
    *,
    message_id: str,
    internal_date_ms: int,
    source_key: str,
    intents: tuple[SearchIntentV1, ...],
) -> tuple[DiscoveredPosting, ...]:
    """Extract validated job anchors without retaining mailbox content."""

    if len(raw_message) > 2_000_000:
        raise ValueError("ALERT_MESSAGE_TOO_LARGE")
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    plain, rich = _body_parts(message)
    soup = BeautifulSoup(rich, "html.parser")
    candidates: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        url = str(anchor.get("href") or "").strip()
        title = " ".join(anchor.get_text(" ").split()).strip()
        if (
            _supported_alert_url(url)
            and len(title) >= 3
            and title.casefold() not in _GENERIC_ANCHOR_LABELS
        ):
            candidates.append((title, url))
    if not candidates and plain:
        # Plain-text messages are accepted only when the line itself carries
        # both a meaningful title and a validated direct URL.
        for line in plain.splitlines():
            match = re.search(r"(https://\S+)", line)
            if not match:
                continue
            url = match.group(1).rstrip(".,;:!?)")
            title = " ".join(line[: match.start()].strip(" -:|\t").split())
            if _supported_alert_url(url) and len(title) >= 3:
                candidates.append((title, url))

    observed_at = datetime.fromtimestamp(internal_date_ms / 1000, tz=UTC)
    result: list[DiscoveredPosting] = []
    seen: set[str] = set()
    for title, raw_url in candidates:
        normalized_url = normalize_url(raw_url)
        normalized_hash = url_hash(normalized_url)
        if normalized_hash in seen or not _matches_intents(title, intents):
            continue
        seen.add(normalized_hash)
        learned = catalog_entry_from_url(normalized_url)
        external_posting_id = _external_id(normalized_url)
        occurrence_key = stable_digest(
            {
                "source_key": source_key,
                "message_id": message_id,
                "url_hash": normalized_hash,
            }
        )
        job = JobData(
            title=title,
            company=learned.company_name if learned else "",
            apply_url=normalized_url,
            source_url=normalized_url,
            date_posted=observed_at.isoformat(),
            keywords=[_provider(normalized_url)],
        )
        result.append(
            DiscoveredPosting(
                job=job,
                occurrence=JobSourceOccurrence(
                    occurrence_key=occurrence_key,
                    source_key=source_key,
                    catalog_key=learned.catalog_key if learned else None,
                    external_posting_id=external_posting_id,
                    normalized_url=normalized_url,
                    normalized_url_hash=normalized_hash,
                    revision_digest=stable_digest(job.model_dump(mode="json")),
                    observed_at=observed_at,
                ),
            )
        )
    return tuple(result)


def _decode_raw(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise ValueError("ALERT_MESSAGE_INVALID") from exc


def _load_oauth_state(path: str | Path) -> dict[str, object]:
    resolved = Path(path)
    if not resolved.is_file():
        raise ValueError("GMAIL_OAUTH_NOT_CONFIGURED")
    if resolved.stat().st_size > 65_536:
        raise ValueError("GMAIL_OAUTH_INVALID")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("GMAIL_OAUTH_INVALID") from exc
    if not isinstance(payload, dict):
        raise ValueError("GMAIL_OAUTH_INVALID")
    return payload


async def _access_token(state: dict[str, object], client: httpx.AsyncClient) -> str:
    token = str(state.get("access_token") or "").strip()
    expires_at = float(state.get("expires_at") or 0)
    if token and expires_at > datetime.now(UTC).timestamp() + 60:
        return token
    refresh_fields = {
        "client_id": str(state.get("client_id") or "").strip(),
        "client_secret": str(state.get("client_secret") or "").strip(),
        "refresh_token": str(state.get("refresh_token") or "").strip(),
    }
    if not all(refresh_fields.values()):
        raise ValueError("GMAIL_OAUTH_REFRESH_REQUIRED")
    response = await client.post(
        f"https://{_TOKEN_HOST}/token",
        data={**refresh_fields, "grant_type": "refresh_token"},
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        raise ValueError("GMAIL_OAUTH_REFRESH_FAILED")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("GMAIL_OAUTH_REFRESH_FAILED") from exc
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise ValueError("GMAIL_OAUTH_REFRESH_FAILED")
    return token


async def fetch_gmail_alert_page(
    *,
    oauth_path: str | Path,
    label: str,
    cursor: DiscoveryCursor,
    intents: tuple[SearchIntentV1, ...],
    max_messages: int,
    client: httpx.AsyncClient | None = None,
) -> DiscoveryPage:
    """Fetch one Gmail API page and return only parsed job metadata."""

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        token = await _access_token(_load_oauth_state(oauth_path), client)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        last_internal = int(cursor.cursor.get("last_internal_date_ms") or 0)
        page_token = str(cursor.cursor.get("page_token") or "").strip()
        clean_label = " ".join(label.split()).strip()
        if not clean_label or any(ord(character) < 32 for character in clean_label):
            raise ValueError("GMAIL_ALERT_LABEL_INVALID")
        escaped_label = clean_label.replace("\\", "\\\\").replace('"', '\\"')
        query = f'label:"{escaped_label}"'
        if last_internal:
            query += f" after:{max(0, last_internal // 1000 - 1)}"
        params: dict[str, object] = {
            "q": query,
            "maxResults": min(100, max_messages),
        }
        if page_token:
            params["pageToken"] = page_token
        response = await client.get(
            f"https://{_GMAIL_API_HOST}/gmail/v1/users/me/messages",
            params=params,
            headers=headers,
        )
        if response.status_code != 200:
            raise ValueError("GMAIL_ALERT_LIST_FAILED")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("GMAIL_ALERT_LIST_INVALID") from exc
        if not isinstance(payload, dict):
            raise ValueError("GMAIL_ALERT_LIST_INVALID")
        rows = payload.get("messages", [])
        if not isinstance(rows, list):
            raise ValueError("GMAIL_ALERT_LIST_INVALID")
        postings: list[DiscoveredPosting] = []
        max_internal = int(cursor.cursor.get("max_internal_date_ms") or last_internal)
        for row in rows:
            message_id = str(row.get("id") or "").strip()
            if not message_id:
                continue
            detail = await client.get(
                f"https://{_GMAIL_API_HOST}/gmail/v1/users/me/messages/{message_id}",
                params={"format": "raw"},
                headers=headers,
            )
            if detail.status_code != 200:
                continue
            try:
                message_payload = detail.json()
            except ValueError as exc:
                raise ValueError("GMAIL_ALERT_MESSAGE_INVALID") from exc
            if not isinstance(message_payload, dict):
                raise ValueError("GMAIL_ALERT_MESSAGE_INVALID")
            internal_date = int(message_payload.get("internalDate") or 0)
            max_internal = max(max_internal, internal_date)
            if internal_date <= last_internal:
                continue
            postings.extend(
                parse_job_alert_message(
                    _decode_raw(str(message_payload.get("raw") or "")),
                    message_id=message_id,
                    internal_date_ms=internal_date,
                    source_key=cursor.source_key,
                    intents=intents,
                )
            )
        next_page = str(payload.get("nextPageToken") or "").strip()
        cursor_values: dict[str, str | int | float | bool | None]
        if next_page:
            cursor_values = {
                "last_internal_date_ms": last_internal,
                "max_internal_date_ms": max_internal,
                "page_token": next_page,
            }
        else:
            cursor_values = {
                "last_internal_date_ms": max_internal,
                "max_internal_date_ms": max_internal,
                "page_token": "",
            }
        return DiscoveryPage(
            postings=tuple(postings),
            cursor=DiscoveryCursor(
                source_key=cursor.source_key,
                cursor=cursor_values,
                last_seen_posting_at=(
                    datetime.fromtimestamp(max_internal / 1000, tz=UTC)
                    if max_internal
                    else cursor.last_seen_posting_at
                ),
            ),
            complete_snapshot=False,
        )
    finally:
        if owns_client:
            await client.aclose()
