"""Polite discovery HTTP transport with host serialization and bounded retry."""

from __future__ import annotations

import asyncio
import email.utils
import ipaddress
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx


class DiscoveryFetchError(RuntimeError):
    """A source failed with a bounded, stable reason code."""

    def __init__(
        self,
        reason_code: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return max(0.0, (parsed - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class DiscoveryHttpClient:
    """One-request-per-host client honoring validators and Retry-After."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_attempts: int = 3,
        base_backoff_seconds: float = 0.5,
        max_response_bytes: int = 10 * 1024 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._max_attempts = max(1, max_attempts)
        self._base_backoff_seconds = max(0.0, base_backoff_seconds)
        self._max_response_bytes = max(1024, max_response_bytes)
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> DiscoveryHttpClient:
        return self

    async def __aexit__(self, *_args) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _send_bounded(
        self,
        url: str,
        *,
        params: dict[str, str | int | float | bool | None] | None,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        """Stream one response and retain at most the configured byte budget."""

        request = self._client.build_request(
            "GET",
            url,
            params=params,
            headers=headers,
            extensions=extensions,
        )
        response = await self._client.send(
            request,
            stream=True,
            # Discovery endpoints are canonical and fixed-host. Following a
            # redirect would bypass the host allowlist and DNS checks.
            follow_redirects=False,
        )
        try:
            if response.status_code == 304 or not 200 <= response.status_code < 300:
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=b"",
                    request=response.request,
                )

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > self._max_response_bytes:
                    raise DiscoveryFetchError("SOURCE_PAYLOAD_TOO_LARGE")

            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self._max_response_bytes:
                    raise DiscoveryFetchError("SOURCE_PAYLOAD_TOO_LARGE")
                chunks.append(chunk)

            # aiter_bytes yields decoded bytes. Remove transport encodings so
            # the reconstructed in-memory response is not decoded twice.
            bounded_headers = httpx.Headers(response.headers)
            for header in ("Content-Encoding", "Content-Length", "Transfer-Encoding"):
                bounded_headers.pop(header, None)
            return httpx.Response(
                response.status_code,
                headers=bounded_headers,
                content=b"".join(chunks),
                request=response.request,
            )
        finally:
            await response.aclose()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str | int | float | bool | None] | None = None,
        headers: dict[str, str] | None = None,
        allowed_hosts: frozenset[str] | None = None,
        connect_ip: str | None = None,
    ) -> httpx.Response:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").rstrip(".").casefold()
            port = parsed.port or 443
        except (ValueError, UnicodeError) as exc:
            raise DiscoveryFetchError("SOURCE_URL_UNSAFE") from exc
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise DiscoveryFetchError("SOURCE_URL_UNSAFE")
        if allowed_hosts is not None and host not in allowed_hosts:
            raise DiscoveryFetchError("SOURCE_HOST_NOT_ALLOWED")

        lock = self._host_locks.setdefault(host, asyncio.Lock())
        request_headers = {
            "Accept": "application/json, application/ld+json, application/xml, text/html;q=0.8",
            "Accept-Encoding": "identity",
            "User-Agent": "JobApplyAgent/0.2 (+private authorized discovery)",
            **(headers or {}),
        }
        request_url = url
        request_extensions: dict[str, object] | None = None
        if connect_ip is not None:
            try:
                pinned_address = ipaddress.ip_address(connect_ip.split("%", 1)[0])
            except ValueError as exc:
                raise DiscoveryFetchError("SOURCE_ADDRESS_NOT_PUBLIC") from exc
            if not pinned_address.is_global:
                raise DiscoveryFetchError("SOURCE_ADDRESS_NOT_PUBLIC")
            address_text = str(pinned_address)
            address_netloc = f"[{address_text}]" if pinned_address.version == 6 else address_text
            if port != 443:
                address_netloc = f"{address_netloc}:{port}"
            request_url = urlunsplit(
                (parsed.scheme, address_netloc, parsed.path, parsed.query, parsed.fragment)
            )
            host_header = f"[{host}]" if ":" in host else host
            if port != 443:
                host_header = f"{host_header}:{port}"
            # The TCP connection uses the already validated numeric address;
            # Host and SNI retain the configured origin for HTTP routing and
            # certificate verification. Closing the HTTP/1.1 connection also
            # prevents a shared-IP pool from crossing configured hostnames.
            request_headers["Host"] = host_header
            request_headers["Connection"] = "close"
            request_extensions = {"sni_hostname": host}
        last_status: int | None = None
        last_retry_after: float | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with lock:
                    response = await self._send_bounded(
                        request_url,
                        params=params,
                        headers=request_headers,
                        extensions=request_extensions,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self._max_attempts:
                    raise DiscoveryFetchError("SOURCE_TIMEOUT") from exc
                await asyncio.sleep(self._base_backoff_seconds * (2 ** (attempt - 1)))
                continue

            last_status = response.status_code
            last_retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if 300 <= response.status_code < 400 and response.status_code != 304:
                raise DiscoveryFetchError(
                    "SOURCE_REDIRECT_NOT_ALLOWED",
                    status_code=response.status_code,
                )
            if response.status_code in {408, 425, 429} or response.status_code >= 500:
                if attempt == self._max_attempts:
                    reason = (
                        "SOURCE_RATE_LIMITED"
                        if response.status_code == 429
                        else "SOURCE_TIMEOUT"
                        if response.status_code == 408
                        else "SOURCE_5XX"
                    )
                    raise DiscoveryFetchError(
                        reason,
                        status_code=response.status_code,
                        retry_after_seconds=last_retry_after,
                    )
                delay = (
                    last_retry_after
                    if last_retry_after is not None
                    else self._base_backoff_seconds * (2 ** (attempt - 1))
                )
                if delay > 60.0:
                    reason = "SOURCE_RATE_LIMITED" if response.status_code == 429 else "SOURCE_5XX"
                    raise DiscoveryFetchError(
                        reason,
                        status_code=response.status_code,
                        retry_after_seconds=last_retry_after,
                    )
                await asyncio.sleep(min(delay, 60.0))
                continue
            if response.status_code in {401, 403}:
                raise DiscoveryFetchError(
                    "SOURCE_AUTH_REQUIRED",
                    status_code=response.status_code,
                )
            if response.status_code == 404:
                raise DiscoveryFetchError("SOURCE_TENANT_NOT_FOUND", status_code=404)
            if response.status_code != 304:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise DiscoveryFetchError(
                        "SOURCE_HTTP_ERROR",
                        status_code=response.status_code,
                    ) from exc
            return response
        raise DiscoveryFetchError(
            "SOURCE_UNAVAILABLE",
            status_code=last_status,
            retry_after_seconds=last_retry_after,
        )
