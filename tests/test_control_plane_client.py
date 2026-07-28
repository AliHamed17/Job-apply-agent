"""Network-boundary tests for the outbound-only control-plane client."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from worker.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneClientConfig,
    ControlPlaneClientError,
)


def test_client_requires_an_exact_credential_free_https_origin():
    for value in (
        "http://control.example",
        "https://user:password@control.example",
        "https://control.example?redirect=https://evil.example",
        "https://control.example/#fragment",
        "https://control.example/private-token",
        "https://control.example:444",
        "https://control.example:not-a-port",
        "https://control.example:99999",
    ):
        with pytest.raises(ControlPlaneClientError, match="CONTROL_PLANE_ORIGIN_INVALID"):
            ControlPlaneClientConfig(value)
    assert ControlPlaneClientConfig("https://control.example:443").base_url.endswith(":443")


@pytest.mark.asyncio
async def test_client_never_follows_redirects_or_accepts_cross_origin_paths():
    requests: list[httpx.Request] = []
    device_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://evil.example/collect"},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlPlaneClient(
        ControlPlaneClientConfig("https://control.example"),
        http=http,
    )
    with pytest.raises(ControlPlaneClientError, match="CONTROL_PLANE_REDIRECT_REJECTED"):
        await client.send_heartbeat({"key_id": device_id})
    with pytest.raises(ControlPlaneClientError, match="CONTROL_PLANE_PATH_INVALID"):
        client._endpoint("//evil.example/collect")
    assert len(requests) == 1
    assert requests[0].url.host == "control.example"
    await http.aclose()


@pytest.mark.asyncio
async def test_poll_is_bounded_json_and_uses_no_bearer_authorization():
    command = {"protocol_version": "jaa-control.v1", "signature": "x" * 86}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://control.example/api/runner/commands/poll")
        assert "authorization" not in request.headers
        assert request.headers["cache-control"] == "no-store"
        return httpx.Response(
            200,
            content=json.dumps({"commands": [command]}).encode(),
            headers={"Content-Type": "application/json"},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlPlaneClient(
        ControlPlaneClientConfig("https://control.example"),
        http=http,
    )
    assert await client.poll_command({"signed": "poll"}) == {"commands": [command]}
    await http.aclose()


@pytest.mark.asyncio
async def test_ack_path_is_bound_to_a_canonical_command_uuid():
    seen: list[str] = []
    command_id = "09aac6df-df84-419c-a7a4-558acc709382"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "accepted": True,
                    "command_id": command_id,
                    "duplicate": False,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlPlaneClient(
        ControlPlaneClientConfig("https://control.example"),
        http=http,
    )
    await client.acknowledge_command(command_id, {"signed": "ack"})
    assert seen == [f"/api/runner/commands/{command_id}/ack"]
    with pytest.raises(ControlPlaneClientError, match="CONTROL_COMMAND_ID_INVALID"):
        await client.acknowledge_command("../private", {"signed": "ack"})
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"accepted": False},
        {
            "accepted": True,
            "event_id": "00000000-0000-4000-8000-000000000000",
            "duplicate": False,
        },
    ],
)
async def test_mutation_receipts_must_be_accepted_and_bound_to_the_event(body):
    event_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlPlaneClient(
        ControlPlaneClientConfig("https://control.example"),
        http=http,
    )
    with pytest.raises(ControlPlaneClientError, match="CONTROL_PLANE_RECEIPT_INVALID"):
        await client.send_event({"payload": {"event_id": event_id}})
    await http.aclose()


@pytest.mark.asyncio
async def test_each_mutation_route_requires_its_exact_receipt_identifier():
    device_id = str(uuid4())
    grant_id = str(uuid4())
    command_id = str(uuid4())
    event_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        receipts = {
            "/api/runner/heartbeat": {
                "accepted": True,
                "device_id": device_id,
            },
            "/api/runner/review-grants": {
                "accepted": True,
                "grant_id": grant_id,
                "duplicate": False,
            },
            f"/api/runner/commands/{command_id}/ack": {
                "accepted": True,
                "command_id": command_id,
                "duplicate": True,
            },
            "/api/runner/events": {
                "accepted": True,
                "event_id": event_id,
                "duplicate": False,
            },
        }
        return httpx.Response(
            200,
            json=receipts[request.url.path],
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlPlaneClient(
        ControlPlaneClientConfig("https://control.example"),
        http=http,
    )
    await client.send_heartbeat({"key_id": device_id})
    await client.publish_review_grant({"payload": {"grant_id": grant_id}})
    await client.acknowledge_command(command_id, {"payload": {"command_id": command_id}})
    await client.send_event({"payload": {"event_id": event_id}})
    await http.aclose()
