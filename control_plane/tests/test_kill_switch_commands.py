from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from job_control_plane.config import Settings
from job_control_plane.crypto import verify_envelope
from job_control_plane.protocol import (
    RUNNER_AUDIENCE,
    CommandAckEnvelope,
    CommandAckPayload,
    CommandAckStatus,
    CommandPollEnvelope,
    CommandPollPayload,
    EnvelopePurpose,
    HeartbeatEnvelope,
    HeartbeatPayload,
    KillSwitchCommandEnvelope,
    RunnerStatus,
)


def _headers(settings: Settings, csrf: str) -> dict[str, str]:
    return {"origin": settings.public_origin, "x-csrf-token": csrf}


def _body(idempotency_key: UUID) -> dict[str, str]:
    return {
        "acknowledgement": "ACTIVATE_KILL_SWITCH",
        "client_idempotency_key": str(idempotency_key),
    }


def test_kill_switch_requires_operator_csrf_and_exact_activation_acknowledgement(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    heartbeat: HeartbeatEnvelope,
) -> None:
    del heartbeat
    key = uuid4()
    unauthenticated = client.post("/api/kill-switch", json=_body(key))
    assert unauthenticated.status_code == 403

    invalid = client.post(
        "/api/kill-switch",
        headers=_headers(settings, authenticated),
        json={
            "acknowledgement": "CLEAR_KILL_SWITCH",
            "client_idempotency_key": str(key),
        },
    )
    assert invalid.status_code == 422

    first = client.post(
        "/api/kill-switch",
        headers=_headers(settings, authenticated),
        json=_body(key),
    )
    assert first.status_code == 202
    assert first.json() == {
        "command_id": first.json()["command_id"],
        "status": "queued",
        "active_requested": True,
        "duplicate": False,
    }
    duplicate = client.post(
        "/api/kill-switch",
        headers=_headers(settings, authenticated),
        json=_body(key),
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["command_id"] == first.json()["command_id"]
    assert duplicate.json()["duplicate"] is True

    listing = client.get("/api/kill-switch/commands")
    assert listing.status_code == 200
    encoded = listing.text.casefold()
    assert "application_ref" not in encoded
    assert "email" not in encoded
    assert listing.json()["commands"][0]["active_requested"] is True


def test_signed_kill_switch_poll_ack_and_replay_are_bounded(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
    control_private_key: Ed25519PrivateKey,
) -> None:
    created = client.post(
        "/api/kill-switch",
        headers=_headers(settings, authenticated),
        json=_body(uuid4()),
    )
    command_id = UUID(created.json()["command_id"])
    poll = sign_runner(
        CommandPollEnvelope,
        CommandPollPayload(boot_id=heartbeat.payload.boot_id),
    )
    response = client.post(
        "/api/runner/kill-switch/poll",
        json=poll.model_dump(mode="json"),
    )
    assert response.status_code == 200
    [raw_command] = response.json()["commands"]
    envelope = KillSwitchCommandEnvelope.model_validate(raw_command)
    assert envelope.payload.command_id == command_id
    assert envelope.payload.boot_id == heartbeat.payload.boot_id
    assert envelope.payload.action == "activate_kill_switch"
    assert envelope.payload.reason_code == "REMOTE_OPERATOR_KILL"
    verify_envelope(
        envelope,
        control_private_key.public_key(),
        expected_purpose=EnvelopePurpose.CONTROL_KILL_COMMAND,
        expected_audience=RUNNER_AUDIENCE,
    )

    empty_poll = sign_runner(
        CommandPollEnvelope,
        CommandPollPayload(boot_id=heartbeat.payload.boot_id),
    )
    empty = client.post(
        "/api/runner/kill-switch/poll",
        json=empty_poll.model_dump(mode="json"),
    )
    assert empty.status_code == 200
    assert empty.json() == {"commands": []}

    acknowledgement = sign_runner(
        CommandAckEnvelope,
        CommandAckPayload(
            command_id=command_id,
            ack_status=CommandAckStatus.RECEIVED,
        ),
    )
    ack_url = f"/api/runner/kill-switch/{command_id}/ack"
    accepted = client.post(ack_url, json=acknowledgement.model_dump(mode="json"))
    assert accepted.status_code == 200
    assert accepted.json()["duplicate"] is False

    fresh_duplicate = sign_runner(
        CommandAckEnvelope,
        CommandAckPayload(
            command_id=command_id,
            ack_status=CommandAckStatus.RECEIVED,
        ),
    )
    duplicate = client.post(ack_url, json=fresh_duplicate.model_dump(mode="json"))
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    listing = client.get("/api/kill-switch/commands")
    assert listing.json()["commands"][0]["status"] == "acknowledged"


def test_kill_switch_poll_rejects_wrong_boot_binding(
    client: TestClient,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    poll = sign_runner(
        CommandPollEnvelope,
        CommandPollPayload(boot_id=uuid4()),
    )
    response = client.post(
        "/api/runner/kill-switch/poll",
        json=poll.model_dump(mode="json"),
    )
    assert response.status_code == 409
    assert response.json() == {"code": "RUNNER_BOOT_MISMATCH"}
    assert heartbeat.payload.boot_id != poll.payload.boot_id


def test_pre_restart_kill_command_is_not_delivered_to_replacement_runner(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    created = client.post(
        "/api/kill-switch",
        headers=_headers(settings, authenticated),
        json=_body(uuid4()),
    )
    assert created.status_code == 202

    replacement = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="b" * 40,
            status=RunnerStatus.READY,
        ),
    )
    assert replacement.payload.boot_id != heartbeat.payload.boot_id
    accepted = client.post(
        "/api/runner/heartbeat",
        json=replacement.model_dump(mode="json"),
    )
    assert accepted.status_code == 200

    poll = sign_runner(
        CommandPollEnvelope,
        CommandPollPayload(boot_id=replacement.payload.boot_id),
    )
    response = client.post(
        "/api/runner/kill-switch/poll",
        json=poll.model_dump(mode="json"),
    )
    assert response.status_code == 200
    assert response.json() == {"commands": []}
