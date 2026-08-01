"""Strict outbound-only HTTPS client for the hosted redacted control plane."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx

MAX_CONTROL_PLANE_BODY_BYTES = 64 * 1024


class ControlPlaneClientError(RuntimeError):
    """A bounded network/protocol failure that never echoes remote content."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True, repr=False)
class ControlPlaneClientConfig:
    """Non-secret network configuration safe for a runner JSON file."""

    base_url: str
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
            hostname = parsed.hostname
        except (UnicodeError, ValueError) as exc:
            raise ControlPlaneClientError("CONTROL_PLANE_ORIGIN_INVALID") from exc
        if (
            parsed.scheme != "https"
            or not hostname
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ControlPlaneClientError("CONTROL_PLANE_ORIGIN_INVALID")
        if not 0.1 <= self.connect_timeout_seconds <= 30:
            raise ControlPlaneClientError("CONTROL_PLANE_TIMEOUT_INVALID")
        if not 0.1 <= self.request_timeout_seconds <= 60:
            raise ControlPlaneClientError("CONTROL_PLANE_TIMEOUT_INVALID")

    def __repr__(self) -> str:
        scheme, host, port = _origin(self.base_url)
        return f"ControlPlaneClientConfig(origin={f'{scheme}://{host}:{port}'!r})"


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, (parsed.hostname or "").lower(), port


def _json_without_duplicate_keys(raw: bytes) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError("duplicate key")
            decoded[key] = value
        return decoded

    return json.loads(
        raw,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )


def _envelope_identifier(
    envelope: Mapping[str, object],
    *,
    field: str,
    nested_payload: bool,
) -> str:
    source: object = envelope.get("payload") if nested_payload else envelope
    if not isinstance(source, Mapping):
        raise ControlPlaneClientError("CONTROL_PLANE_ENVELOPE_INVALID")
    value = source.get(field)
    try:
        identifier = str(UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ControlPlaneClientError("CONTROL_PLANE_ENVELOPE_INVALID") from exc
    if str(value).lower() != identifier:
        raise ControlPlaneClientError("CONTROL_PLANE_ENVELOPE_INVALID")
    return identifier


def _require_mutation_receipt(
    decoded: Mapping[str, object] | None,
    *,
    identifier_field: str,
    expected_identifier: str,
    includes_duplicate: bool,
) -> None:
    expected_fields = {"accepted", identifier_field}
    if includes_duplicate:
        expected_fields.add("duplicate")
    if (
        decoded is None
        or set(decoded) != expected_fields
        or decoded.get("accepted") is not True
        or decoded.get(identifier_field) != expected_identifier
        or (includes_duplicate and not isinstance(decoded.get("duplicate"), bool))
    ):
        raise ControlPlaneClientError("CONTROL_PLANE_RECEIPT_INVALID")


class ControlPlaneClient:
    """One-origin client; redirects and cross-origin destinations are rejected."""

    def __init__(
        self,
        config: ControlPlaneClientConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ):
        self._config = config
        self._base_url = config.base_url.rstrip("/") + "/"
        self._allowed_origin = _origin(self._base_url)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.request_timeout_seconds,
                connect=config.connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    def _endpoint(self, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
        ):
            raise ControlPlaneClientError("CONTROL_PLANE_PATH_INVALID")
        resolved = urljoin(self._base_url, path.lstrip("/"))
        if _origin(resolved) != self._allowed_origin:
            raise ControlPlaneClientError("CONTROL_PLANE_ORIGIN_CHANGED")
        return resolved

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> ControlPlaneClient:
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.close()

    async def _post(
        self,
        path: str,
        envelope: Mapping[str, object],
        *,
        allow_empty: bool,
    ) -> Mapping[str, object] | None:
        try:
            raw = json.dumps(
                dict(envelope),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except (TypeError, ValueError) as exc:
            raise ControlPlaneClientError("CONTROL_PLANE_ENVELOPE_INVALID") from exc
        if len(raw) > MAX_CONTROL_PLANE_BODY_BYTES:
            raise ControlPlaneClientError("CONTROL_PLANE_ENVELOPE_TOO_LARGE")

        try:
            response = await self._http.post(
                self._endpoint(path),
                content=raw,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                },
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ControlPlaneClientError("CONTROL_PLANE_UNAVAILABLE") from exc
        if _origin(str(response.url)) != self._allowed_origin:
            raise ControlPlaneClientError("CONTROL_PLANE_ORIGIN_CHANGED")
        if 300 <= response.status_code < 400:
            raise ControlPlaneClientError("CONTROL_PLANE_REDIRECT_REJECTED")
        if response.status_code == 204 and allow_empty:
            return None
        if not 200 <= response.status_code < 300:
            raise ControlPlaneClientError("CONTROL_PLANE_REQUEST_REJECTED")
        content = response.content
        if not content and allow_empty:
            return None
        if (
            not content
            or len(content) > MAX_CONTROL_PLANE_BODY_BYTES
            or "application/json" not in response.headers.get("content-type", "").lower()
        ):
            raise ControlPlaneClientError("CONTROL_PLANE_RESPONSE_INVALID")
        try:
            decoded = _json_without_duplicate_keys(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ControlPlaneClientError("CONTROL_PLANE_RESPONSE_INVALID") from exc
        if not isinstance(decoded, Mapping):
            raise ControlPlaneClientError("CONTROL_PLANE_RESPONSE_INVALID")
        return decoded

    async def send_heartbeat(self, envelope: Mapping[str, object]) -> None:
        expected = _envelope_identifier(
            envelope,
            field="key_id",
            nested_payload=False,
        )
        receipt = await self._post("/api/runner/heartbeat", envelope, allow_empty=False)
        _require_mutation_receipt(
            receipt,
            identifier_field="device_id",
            expected_identifier=expected,
            includes_duplicate=False,
        )

    async def publish_review_grant(self, envelope: Mapping[str, object]) -> None:
        expected = _envelope_identifier(
            envelope,
            field="grant_id",
            nested_payload=True,
        )
        receipt = await self._post(
            "/api/runner/review-grants",
            envelope,
            allow_empty=False,
        )
        _require_mutation_receipt(
            receipt,
            identifier_field="grant_id",
            expected_identifier=expected,
            includes_duplicate=True,
        )

    async def revoke_review_grant(self, envelope: Mapping[str, object]) -> None:
        expected = _envelope_identifier(
            envelope,
            field="grant_id",
            nested_payload=True,
        )
        receipt = await self._post(
            "/api/runner/review-grant-revocations",
            envelope,
            allow_empty=False,
        )
        _require_mutation_receipt(
            receipt,
            identifier_field="grant_id",
            expected_identifier=expected,
            includes_duplicate=True,
        )

    async def poll_command(
        self,
        envelope: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        return await self._post("/api/runner/commands/poll", envelope, allow_empty=False)

    async def poll_kill_switch_command(
        self,
        envelope: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        return await self._post(
            "/api/runner/kill-switch/poll",
            envelope,
            allow_empty=False,
        )

    async def acknowledge_command(
        self,
        command_id: str,
        envelope: Mapping[str, object],
    ) -> None:
        try:
            canonical_command_id = str(UUID(command_id))
        except (TypeError, ValueError) as exc:
            raise ControlPlaneClientError("CONTROL_COMMAND_ID_INVALID") from exc
        if command_id.lower() != canonical_command_id:
            raise ControlPlaneClientError("CONTROL_COMMAND_ID_INVALID")
        receipt = await self._post(
            f"/api/runner/commands/{command_id}/ack",
            envelope,
            allow_empty=False,
        )
        _require_mutation_receipt(
            receipt,
            identifier_field="command_id",
            expected_identifier=canonical_command_id,
            includes_duplicate=True,
        )

    async def acknowledge_kill_switch_command(
        self,
        command_id: str,
        envelope: Mapping[str, object],
    ) -> None:
        try:
            canonical_command_id = str(UUID(command_id))
        except (TypeError, ValueError) as exc:
            raise ControlPlaneClientError("KILL_SWITCH_COMMAND_ID_INVALID") from exc
        if command_id.lower() != canonical_command_id:
            raise ControlPlaneClientError("KILL_SWITCH_COMMAND_ID_INVALID")
        receipt = await self._post(
            f"/api/runner/kill-switch/{command_id}/ack",
            envelope,
            allow_empty=False,
        )
        _require_mutation_receipt(
            receipt,
            identifier_field="command_id",
            expected_identifier=canonical_command_id,
            includes_duplicate=True,
        )

    async def send_event(self, envelope: Mapping[str, object]) -> None:
        expected = _envelope_identifier(
            envelope,
            field="event_id",
            nested_payload=True,
        )
        receipt = await self._post("/api/runner/events", envelope, allow_empty=False)
        _require_mutation_receipt(
            receipt,
            identifier_field="event_id",
            expected_identifier=expected,
            includes_duplicate=True,
        )
