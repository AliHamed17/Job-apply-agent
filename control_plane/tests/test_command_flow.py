from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from job_control_plane.config import Settings
from job_control_plane.crypto import verify_envelope
from job_control_plane.models import RunnerDevice, RunnerNonce, SubmissionCommand
from job_control_plane.protocol import (
    RUNNER_AUDIENCE,
    AttemptOutcome,
    AttemptStage,
    CommandAckEnvelope,
    CommandAckPayload,
    CommandAckStatus,
    CommandPollEnvelope,
    CommandPollPayload,
    ControlCommandEnvelope,
    EnvelopePurpose,
    EvidenceType,
    HeartbeatEnvelope,
    HeartbeatPayload,
    ReasonCode,
    ReviewGrantEnvelope,
    ReviewGrantPayload,
    RunnerEventEnvelope,
    RunnerEventPayload,
    RunnerStatus,
)


def _send_body(
    grant: ReviewGrantEnvelope, *, idempotency_key: UUID | None = None
) -> dict[str, Any]:
    payload = grant.payload
    return {
        "grant_id": str(payload.grant_id),
        "application_ref": str(payload.application_ref),
        "application_revision": payload.application_revision,
        "form_fingerprint_digest": payload.form_fingerprint_digest,
        "acknowledgement": "SEND_APPLICATION",
        "client_idempotency_key": str(idempotency_key or uuid4()),
    }


def _send_headers(settings: Settings, csrf: str) -> dict[str, str]:
    return {
        "origin": settings.public_origin,
        "x-csrf-token": csrf,
    }


def _poll(
    client: TestClient,
    sign_runner: Callable[..., Any],
    heartbeat: HeartbeatEnvelope,
) -> dict[str, Any]:
    envelope = sign_runner(
        CommandPollEnvelope,
        CommandPollPayload(boot_id=heartbeat.payload.boot_id),
    )
    response = client.post(
        "/api/runner/commands/poll",
        json=envelope.model_dump(mode="json"),
    )
    assert response.status_code == 200
    return response.json()


def test_runner_replay_and_review_grant_idempotency(
    client: TestClient,
    sign_runner: Callable[..., Any],
    heartbeat: HeartbeatEnvelope,
    review_grant: ReviewGrantEnvelope,
) -> None:
    replay = client.post(
        "/api/runner/heartbeat",
        json=heartbeat.model_dump(mode="json"),
    )
    assert replay.status_code == 409
    assert replay.json() == {"code": "RUNNER_REPLAYED"}

    duplicate = sign_runner(
        ReviewGrantEnvelope,
        review_grant.payload,
        expires_at=review_grant.expires_at,
    )
    duplicate_response = client.post(
        "/api/runner/review-grants",
        json=duplicate.model_dump(mode="json"),
    )
    assert duplicate_response.status_code == 200
    assert duplicate_response.json()["duplicate"] is True

    conflicting_payload = ReviewGrantPayload(
        **{
            **review_grant.payload.model_dump(),
            "application_revision": review_grant.payload.application_revision + 1,
        }
    )
    conflict = sign_runner(
        ReviewGrantEnvelope,
        conflicting_payload,
        expires_at=review_grant.expires_at,
    )
    conflict_response = client.post(
        "/api/runner/review-grants",
        json=conflict.model_dump(mode="json"),
    )
    assert conflict_response.status_code == 409
    assert conflict_response.json() == {"code": "REVIEW_GRANT_CONFLICT"}


def test_nonce_retention_prunes_expired_rows_without_weakening_live_replay(
    client: TestClient,
    settings: Settings,
    sign_runner: Callable[..., Any],
    heartbeat: HeartbeatEnvelope,
) -> None:
    factory = client.app.state.sessions
    expired_nonce = str(uuid4())
    with factory.begin() as db:
        db.add(
            RunnerNonce(
                device_id=str(settings.runner_device_id),
                purpose=EnvelopePurpose.RUNNER_COMMAND_POLL.value,
                nonce=expired_nonce,
                issued_at=datetime.now(UTC) - timedelta(minutes=21),
                expires_at=datetime.now(UTC) - timedelta(minutes=20),
                seen_at=datetime.now(UTC) - timedelta(minutes=21),
            )
        )

    fresh = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="c" * 40,
            status=RunnerStatus.READY,
        ),
    )
    assert (
        client.post("/api/runner/heartbeat", json=fresh.model_dump(mode="json")).status_code == 200
    )
    with factory() as db:
        assert (
            db.query(RunnerNonce).filter(RunnerNonce.nonce == expired_nonce).one_or_none() is None
        )
    replay = client.post(
        "/api/runner/heartbeat",
        json=heartbeat.model_dump(mode="json"),
    )
    assert replay.status_code == 409
    assert replay.json() == {"code": "RUNNER_REPLAYED"}


def test_one_grant_one_command_and_client_idempotency(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
) -> None:
    idempotency_key = uuid4()
    body = _send_body(review_grant, idempotency_key=idempotency_key)
    first = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=body,
    )
    assert first.status_code == 202
    assert first.json()["verified"] is False
    assert first.json()["status"] == "queued"
    assert first.json()["duplicate"] is False

    duplicate = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=body,
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["command_id"] == first.json()["command_id"]
    assert duplicate.json()["duplicate"] is True

    second_key = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    assert second_key.status_code == 409
    assert second_key.json() == {"code": "REVIEW_GRANT_CONSUMED"}


def test_lost_poll_and_ack_redeliver_same_command_safely(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
    control_private_key: Ed25519PrivateKey,
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    command_id = UUID(sent.json()["command_id"])

    first_poll = _poll(client, sign_runner, heartbeat)
    assert len(first_poll["commands"]) == 1
    first_envelope_json = first_poll["commands"][0]
    command_envelope = ControlCommandEnvelope.model_validate(first_envelope_json)
    assert command_envelope.payload.command_id == command_id
    verify_envelope(
        command_envelope,
        control_private_key.public_key(),
        expected_purpose=EnvelopePurpose.CONTROL_COMMAND,
        expected_audience=RUNNER_AUDIENCE,
    )

    assert _poll(client, sign_runner, heartbeat)["commands"] == []

    factory = client.app.state.sessions
    with factory.begin() as db:
        row = db.get(SubmissionCommand, str(command_id))
        assert row is not None
        row.claim_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    redelivery = _poll(client, sign_runner, heartbeat)
    assert redelivery["commands"] == [first_envelope_json]

    ack_payload = CommandAckPayload(
        command_id=command_id,
        ack_status=CommandAckStatus.RECEIVED,
    )
    ack = sign_runner(CommandAckEnvelope, ack_payload)
    ack_url = f"/api/runner/commands/{command_id}/ack"
    accepted = client.post(ack_url, json=ack.model_dump(mode="json"))
    assert accepted.status_code == 200
    assert accepted.json()["duplicate"] is False

    replay = client.post(ack_url, json=ack.model_dump(mode="json"))
    assert replay.status_code == 409
    assert replay.json() == {"code": "RUNNER_REPLAYED"}

    fresh_ack = sign_runner(CommandAckEnvelope, ack_payload)
    duplicate = client.post(ack_url, json=fresh_ack.model_dump(mode="json"))
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    with factory() as db:
        row = db.get(SubmissionCommand, str(command_id))
        assert row is not None
        assert row.delivery_count == 2
        assert row.status == "acknowledged"


def test_delayed_ack_and_events_report_after_command_expiry(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    command_id = UUID(sent.json()["command_id"])
    assert len(_poll(client, sign_runner, heartbeat)["commands"]) == 1

    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    factory = client.app.state.sessions
    with factory.begin() as db:
        row = db.get(SubmissionCommand, str(command_id))
        assert row is not None
        row.claimed_at = expired_at - timedelta(seconds=1)
        row.expires_at = expired_at

    ack_payload = CommandAckPayload(
        command_id=command_id,
        ack_status=CommandAckStatus.RECEIVED,
    )
    ack_url = f"/api/runner/commands/{command_id}/ack"
    delayed_ack = sign_runner(CommandAckEnvelope, ack_payload)
    accepted = client.post(ack_url, json=delayed_ack.model_dump(mode="json"))
    assert accepted.status_code == 200
    assert accepted.json()["duplicate"] is False

    queued = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=1,
            stage=AttemptStage.QUEUED,
            occurred_at=expired_at - timedelta(milliseconds=500),
        ),
    )
    assert client.post("/api/runner/events", json=queued.model_dump(mode="json")).status_code == 200

    finished = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=2,
            stage=AttemptStage.FINISHED,
            outcome=AttemptOutcome.CONFIRMED_SUBMITTED,
            evidence_type=EvidenceType.ATS_VISIBLE_CONFIRMATION,
            evidence_digest="d" * 64,
            occurred_at=datetime.now(UTC),
        ),
    )
    assert (
        client.post("/api/runner/events", json=finished.model_dump(mode="json")).status_code == 200
    )

    replay = client.post(ack_url, json=delayed_ack.model_dump(mode="json"))
    assert replay.status_code == 409
    assert replay.json() == {"code": "RUNNER_REPLAYED"}
    duplicate_ack = sign_runner(CommandAckEnvelope, ack_payload)
    duplicate = client.post(ack_url, json=duplicate_ack.model_dump(mode="json"))
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    status_json = client.get(f"/api/commands/{command_id}").json()
    assert status_json["status"] == "finished"
    assert status_json["events"][-1]["outcome"] == "confirmed_submitted"


def test_first_durable_event_recovers_a_completely_lost_ack_after_expiry(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    command_id = UUID(sent.json()["command_id"])
    assert len(_poll(client, sign_runner, heartbeat)["commands"]) == 1

    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    factory = client.app.state.sessions
    with factory.begin() as db:
        row = db.get(SubmissionCommand, str(command_id))
        assert row is not None
        row.claimed_at = expired_at - timedelta(seconds=1)
        row.expires_at = expired_at

    queued = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=1,
            stage=AttemptStage.QUEUED,
            occurred_at=expired_at - timedelta(milliseconds=500),
        ),
    )
    recovered = client.post("/api/runner/events", json=queued.model_dump(mode="json"))
    assert recovered.status_code == 200

    with factory() as db:
        row = db.get(SubmissionCommand, str(command_id))
        assert row is not None
        assert row.ack_status == CommandAckStatus.RECEIVED.value
        assert row.acknowledged_at is not None
        assert row.status == "running"

    finished = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=2,
            stage=AttemptStage.FINISHED,
            outcome=AttemptOutcome.UNKNOWN,
            reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED,
            occurred_at=datetime.now(UTC),
        ),
    )
    assert (
        client.post("/api/runner/events", json=finished.model_dump(mode="json")).status_code == 200
    )
    status_json = client.get(f"/api/commands/{command_id}").json()
    assert status_json["status"] == "finished"
    assert status_json["events"][-1]["outcome"] == "unknown"


def test_expiry_does_not_make_an_unclaimed_or_late_claim_acknowledgeable(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    command_id = UUID(sent.json()["command_id"])
    ack_payload = CommandAckPayload(
        command_id=command_id,
        ack_status=CommandAckStatus.RECEIVED,
    )
    ack_url = f"/api/runner/commands/{command_id}/ack"

    unclaimed_ack = sign_runner(CommandAckEnvelope, ack_payload)
    denied = client.post(ack_url, json=unclaimed_ack.model_dump(mode="json"))
    assert denied.status_code == 409
    assert denied.json() == {"code": "COMMAND_NOT_CLAIMED"}

    assert len(_poll(client, sign_runner, heartbeat)["commands"]) == 1
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    factory = client.app.state.sessions
    with factory.begin() as db:
        row = db.get(SubmissionCommand, str(command_id))
        assert row is not None
        row.expires_at = expired_at
        row.claimed_at = expired_at + timedelta(milliseconds=1)

    late_claim_ack = sign_runner(CommandAckEnvelope, ack_payload)
    denied = client.post(ack_url, json=late_claim_ack.model_dump(mode="json"))
    assert denied.status_code == 409
    assert denied.json() == {"code": "COMMAND_CLAIM_INVALID"}


def test_event_sequence_allows_only_precommit_reset_and_requires_employer_evidence(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    command_id = UUID(sent.json()["command_id"])
    _poll(client, sign_runner, heartbeat)
    ack = sign_runner(
        CommandAckEnvelope,
        CommandAckPayload(
            command_id=command_id,
            ack_status=CommandAckStatus.RECEIVED,
        ),
    )
    assert (
        client.post(
            f"/api/runner/commands/{command_id}/ack",
            json=ack.model_dump(mode="json"),
        ).status_code
        == 200
    )

    def send_event(payload: RunnerEventPayload):
        envelope = sign_runner(RunnerEventEnvelope, payload)
        return client.post("/api/runner/events", json=envelope.model_dump(mode="json"))

    first_payload = RunnerEventPayload(
        event_id=uuid4(),
        command_id=command_id,
        sequence=1,
        stage=AttemptStage.QUEUED,
        occurred_at=datetime.now(UTC),
    )
    first = send_event(first_payload)
    assert first.status_code == 200

    inspecting_payload = RunnerEventPayload(
        event_id=uuid4(),
        command_id=command_id,
        sequence=2,
        stage=AttemptStage.INSPECTING,
        occurred_at=datetime.now(UTC),
    )
    inspecting = send_event(inspecting_payload)
    assert inspecting.status_code == 200

    reset = send_event(
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=3,
            stage=AttemptStage.QUEUED,
            reason_code=ReasonCode.RUNTIME_NOT_READY,
            occurred_at=datetime.now(UTC),
        )
    )
    assert reset.status_code == 200

    committing = send_event(
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=4,
            stage=AttemptStage.COMMITTING,
            occurred_at=datetime.now(UTC),
        )
    )
    assert committing.status_code == 200

    unsafe_reset = send_event(
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=5,
            stage=AttemptStage.QUEUED,
            occurred_at=datetime.now(UTC),
        )
    )
    assert unsafe_reset.status_code == 409
    assert unsafe_reset.json() == {"code": "EVENT_STAGE_REGRESSION"}

    finished_payload = RunnerEventPayload(
        event_id=uuid4(),
        command_id=command_id,
        sequence=5,
        stage=AttemptStage.FINISHED,
        outcome=AttemptOutcome.CONFIRMED_SUBMITTED,
        evidence_type=EvidenceType.ATS_VISIBLE_CONFIRMATION,
        evidence_digest="e" * 64,
        occurred_at=datetime.now(UTC),
    )
    finished = send_event(finished_payload)
    assert finished.status_code == 200

    duplicate_envelope = sign_runner(RunnerEventEnvelope, finished_payload)
    duplicate = client.post(
        "/api/runner/events",
        json=duplicate_envelope.model_dump(mode="json"),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    status_response = client.get(f"/api/commands/{command_id}")
    assert status_response.status_code == 200
    status_json = status_response.json()
    assert status_json["status"] == "finished"
    assert status_json["events"][-1]["outcome"] == "confirmed_submitted"
    assert "evidence_digest" not in status_json["events"]  # digest stays audit-only


def test_bad_runner_signature_and_ack_path_binding_fail_closed(
    client: TestClient,
    sign_runner: Callable[..., Any],
    heartbeat: HeartbeatEnvelope,
) -> None:
    tampered = heartbeat.model_copy(update={"signature": "A" * 86})
    response = client.post(
        "/api/runner/heartbeat",
        json=tampered.model_dump(mode="json"),
    )
    assert response.status_code == 401
    assert response.json() == {"code": "RUNNER_SIGNATURE_INVALID"}

    payload = CommandAckPayload(
        command_id=uuid4(),
        ack_status=CommandAckStatus.RECEIVED,
    )
    envelope = sign_runner(CommandAckEnvelope, payload)
    wrong_path = client.post(
        f"/api/runner/commands/{uuid4()}/ack",
        json=envelope.model_dump(mode="json"),
    )
    assert wrong_path.status_code == 409
    assert wrong_path.json() == {"code": "COMMAND_BINDING_MISMATCH"}


def test_poll_requires_a_current_ready_heartbeat(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    assert sent.status_code == 202
    factory = client.app.state.sessions
    with factory.begin() as db:
        device = db.get(RunnerDevice, str(settings.runner_device_id))
        assert device is not None
        device.last_seen_at = datetime.now(UTC) - timedelta(minutes=1)

    poll = sign_runner(
        CommandPollEnvelope,
        CommandPollPayload(boot_id=heartbeat.payload.boot_id),
    )
    denied = client.post(
        "/api/runner/commands/poll",
        json=poll.model_dump(mode="json"),
    )
    assert denied.status_code == 409
    assert denied.json() == {"code": "RUNNER_OFFLINE"}
    replay = client.post(
        "/api/runner/commands/poll",
        json=poll.model_dump(mode="json"),
    )
    assert replay.status_code == 409
    assert replay.json() == {"code": "RUNNER_REPLAYED"}


def test_unknown_terminal_accepts_one_non_green_operator_reconciliation(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    command_id = UUID(sent.json()["command_id"])
    _poll(client, sign_runner, heartbeat)
    ack = sign_runner(
        CommandAckEnvelope,
        CommandAckPayload(
            command_id=command_id,
            ack_status=CommandAckStatus.RECEIVED,
        ),
    )
    assert (
        client.post(
            f"/api/runner/commands/{command_id}/ack",
            json=ack.model_dump(mode="json"),
        ).status_code
        == 200
    )

    queued = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=1,
            stage=AttemptStage.QUEUED,
            occurred_at=datetime.now(UTC),
        ),
    )
    assert client.post("/api/runner/events", json=queued.model_dump(mode="json")).status_code == 200

    unknown = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=2,
            stage=AttemptStage.FINISHED,
            outcome=AttemptOutcome.UNKNOWN,
            reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED,
            occurred_at=datetime.now(UTC),
        ),
    )
    assert (
        client.post("/api/runner/events", json=unknown.model_dump(mode="json")).status_code == 200
    )
    before = client.get(f"/api/commands/{command_id}").json()["finished_at"]

    reconciled = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=3,
            stage=AttemptStage.FINISHED,
            outcome=AttemptOutcome.OPERATOR_CONFIRMED,
            occurred_at=datetime.now(UTC),
        ),
    )
    accepted = client.post(
        "/api/runner/events",
        json=reconciled.model_dump(mode="json"),
    )
    assert accepted.status_code == 200

    second_reconciliation = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=4,
            stage=AttemptStage.FINISHED,
            outcome=AttemptOutcome.FAILED_BEFORE_COMMIT,
            occurred_at=datetime.now(UTC),
        ),
    )
    denied = client.post(
        "/api/runner/events",
        json=second_reconciliation.model_dump(mode="json"),
    )
    assert denied.status_code == 409
    assert denied.json() == {"code": "COMMAND_ALREADY_FINISHED"}

    status_json = client.get(f"/api/commands/{command_id}").json()
    assert status_json["finished_at"] == before
    assert [event["outcome"] for event in status_json["events"]] == [
        None,
        "unknown",
        "operator_confirmed",
    ]
    assert status_json["events"][-1]["evidence_type"] is None
