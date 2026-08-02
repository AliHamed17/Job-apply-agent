from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import job_control_plane.app as app_module
from job_control_plane.config import Settings
from job_control_plane.crypto import verify_envelope
from job_control_plane.models import ReviewGrant, RunnerDevice, RunnerNonce, SubmissionCommand
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
    ReviewGrantRevocationEnvelope,
    ReviewGrantRevocationPayload,
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


def _revocation_payload(
    grant: ReviewGrantEnvelope,
    *,
    revoked_at: datetime | None = None,
    application_revision: int | None = None,
) -> ReviewGrantRevocationPayload:
    payload = grant.payload
    return ReviewGrantRevocationPayload(
        grant_id=payload.grant_id,
        application_ref=payload.application_ref,
        application_revision=application_revision or payload.application_revision,
        adapter=payload.adapter,
        adapter_version=payload.adapter_version,
        form_fingerprint_digest=payload.form_fingerprint_digest,
        reviewed_at=payload.reviewed_at,
        grant_expires_at=grant.expires_at,
        revoked_at=revoked_at or datetime.now(UTC),
    )


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


def test_signed_revocation_is_idempotent_and_removes_send_authority(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    sign_runner: Callable[..., Any],
    review_grant: ReviewGrantEnvelope,
) -> None:
    revoked_at = datetime.now(UTC)
    payload = _revocation_payload(review_grant, revoked_at=revoked_at)
    envelope = sign_runner(
        ReviewGrantRevocationEnvelope,
        payload,
        issued_at=revoked_at,
    )
    response = client.post(
        "/api/runner/review-grant-revocations",
        json=envelope.model_dump(mode="json"),
    )
    assert response.status_code == 200
    assert response.json()["duplicate"] is False

    retry = sign_runner(
        ReviewGrantRevocationEnvelope,
        payload,
        issued_at=revoked_at + timedelta(seconds=1),
    )
    replay = client.post(
        "/api/runner/review-grant-revocations",
        json=retry.model_dump(mode="json"),
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True

    denied = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    assert denied.status_code == 409
    assert denied.json() == {"code": "REVIEW_GRANT_REVOKED"}

    grants = client.get("/api/review-grants").json()["grants"]
    row = next(item for item in grants if item["grant_id"] == str(payload.grant_id))
    assert row["eligible"] is False
    assert row["revoked_at"] is not None
    assert "revoked" in client.get("/").text
    with client.app.state.sessions() as db:
        stored = db.get(ReviewGrant, str(payload.grant_id))
        assert stored is not None
        assert stored.revocation_envelope_digest is not None


def test_dashboard_grant_expiry_boundary_controls_state_and_send_action(
    client: TestClient,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
    monkeypatch,
) -> None:
    assert authenticated
    checked_at = datetime.now(UTC).replace(microsecond=0)
    expired_id = str(review_grant.payload.grant_id)
    boundary_id = str(uuid4())
    live_id = str(uuid4())
    used_id = str(uuid4())
    revoked_id = str(uuid4())

    factory = client.app.state.sessions
    with factory.begin() as db:
        source = db.get(ReviewGrant, expired_id)
        assert source is not None
        source.expires_at = checked_at - timedelta(seconds=1)

        def grant(
            identifier: str,
            *,
            expires_at: datetime,
            consumed_at: datetime | None = None,
            revoked_at: datetime | None = None,
        ) -> ReviewGrant:
            return ReviewGrant(
                id=identifier,
                device_id=source.device_id,
                application_ref=str(uuid4()),
                application_revision=source.application_revision,
                adapter=source.adapter,
                adapter_version=source.adapter_version,
                form_fingerprint_digest=source.form_fingerprint_digest,
                envelope_digest=identifier.replace("-", "").ljust(64, "0"),
                reviewed_at=checked_at - timedelta(minutes=1),
                expires_at=expires_at,
                created_at=checked_at - timedelta(minutes=1),
                consumed_at=consumed_at,
                revoked_at=revoked_at,
                revocation_envelope_digest="f" * 64 if revoked_at is not None else None,
            )

        db.add_all(
            [
                grant(boundary_id, expires_at=checked_at),
                grant(live_id, expires_at=checked_at + timedelta(microseconds=1)),
                grant(
                    used_id,
                    expires_at=checked_at - timedelta(seconds=1),
                    consumed_at=checked_at - timedelta(seconds=2),
                ),
                grant(
                    revoked_id,
                    expires_at=checked_at - timedelta(seconds=1),
                    revoked_at=checked_at - timedelta(seconds=2),
                ),
            ]
        )

    monkeypatch.setattr(app_module, "utc_now", lambda: checked_at)
    dashboard = client.get("/")
    assert dashboard.status_code == 200

    def rendered_row(identifier: str) -> str:
        marker = f"<td><code>{identifier}</code></td>"
        start = dashboard.text.index(marker)
        return dashboard.text[start : dashboard.text.index("</tr>", start)]

    for identifier in (expired_id, boundary_id):
        row = rendered_row(identifier)
        assert "<td>expired</td>" in row
        assert "Send application" not in row

    live_row = rendered_row(live_id)
    assert "<td>eligible</td>" in live_row
    assert "Send application</button>" in live_row
    assert f"data-grant='{live_id}'" in live_row

    used_row = rendered_row(used_id)
    assert "<td>used</td>" in used_row
    assert "Send application" not in used_row
    revoked_row = rendered_row(revoked_id)
    assert "<td>revoked</td>" in revoked_row
    assert "Send application" not in revoked_row
    assert dashboard.text.count("Send application</button>") == 1

    listed = client.get("/api/review-grants")
    assert listed.status_code == 200
    assert {row["grant_id"] for row in listed.json()["grants"]} == {live_id}
    assert listed.json()["grants"][0]["eligible"] is True


def test_revocation_tombstone_wins_over_delayed_grant_projection(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    sign_runner: Callable[..., Any],
) -> None:
    now = datetime.now(UTC)
    grant_payload = ReviewGrantPayload(
        grant_id=uuid4(),
        application_ref=uuid4(),
        application_revision=8,
        adapter="greenhouse",
        adapter_version="1.0.0",
        form_fingerprint_digest="d" * 64,
        reviewed_at=now,
    )
    delayed_grant = sign_runner(
        ReviewGrantEnvelope,
        grant_payload,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    revocation = sign_runner(
        ReviewGrantRevocationEnvelope,
        _revocation_payload(
            delayed_grant,
            revoked_at=now + timedelta(seconds=1),
        ),
        issued_at=now + timedelta(seconds=1),
    )

    tombstoned = client.post(
        "/api/runner/review-grant-revocations",
        json=revocation.model_dump(mode="json"),
    )
    assert tombstoned.status_code == 200
    assert tombstoned.json()["duplicate"] is False

    delayed = client.post(
        "/api/runner/review-grants",
        json=delayed_grant.model_dump(mode="json"),
    )
    assert delayed.status_code == 200
    assert delayed.json()["duplicate"] is True

    denied = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(delayed_grant),
    )
    assert denied.status_code == 409
    assert denied.json() == {"code": "REVIEW_GRANT_REVOKED"}


def test_revocation_requires_the_exact_original_grant_binding(
    client: TestClient,
    sign_runner: Callable[..., Any],
    review_grant: ReviewGrantEnvelope,
) -> None:
    now = datetime.now(UTC)
    conflict = sign_runner(
        ReviewGrantRevocationEnvelope,
        _revocation_payload(
            review_grant,
            revoked_at=now,
            application_revision=review_grant.payload.application_revision + 1,
        ),
        issued_at=now,
    )
    response = client.post(
        "/api/runner/review-grant-revocations",
        json=conflict.model_dump(mode="json"),
    )
    assert response.status_code == 409
    assert response.json() == {"code": "REVIEW_GRANT_REVOCATION_CONFLICT"}


def test_revocation_cancels_a_stale_command_before_runner_delivery(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
    review_grant: ReviewGrantEnvelope,
) -> None:
    body = _send_body(review_grant)
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=body,
    )
    assert sent.status_code == 202
    command_id = sent.json()["command_id"]
    now = datetime.now(UTC)
    revocation = sign_runner(
        ReviewGrantRevocationEnvelope,
        _revocation_payload(review_grant, revoked_at=now),
        issued_at=now,
    )
    assert (
        client.post(
            "/api/runner/review-grant-revocations",
            json=revocation.model_dump(mode="json"),
        ).status_code
        == 200
    )

    assert _poll(client, sign_runner, heartbeat)["commands"] == []
    status = client.get(f"/api/commands/{command_id}").json()
    assert status["status"] == "rejected"
    assert status["finished_at"] is not None

    idempotent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=body,
    )
    assert idempotent.status_code == 202
    assert idempotent.json()["duplicate"] is True
    assert idempotent.json()["status"] == "rejected"


def test_claimed_command_accepts_only_a_late_rejected_ack_after_revocation(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    heartbeat: HeartbeatEnvelope,
    sign_runner: Callable[..., Any],
    review_grant: ReviewGrantEnvelope,
) -> None:
    sent = client.post(
        "/api/send",
        headers=_send_headers(settings, authenticated),
        json=_send_body(review_grant),
    )
    command_id = UUID(sent.json()["command_id"])
    assert len(_poll(client, sign_runner, heartbeat)["commands"]) == 1

    now = datetime.now(UTC)
    revocation = sign_runner(
        ReviewGrantRevocationEnvelope,
        _revocation_payload(review_grant, revoked_at=now),
        issued_at=now,
    )
    assert (
        client.post(
            "/api/runner/review-grant-revocations",
            json=revocation.model_dump(mode="json"),
        ).status_code
        == 200
    )
    rejected_ack = sign_runner(
        CommandAckEnvelope,
        CommandAckPayload(
            command_id=command_id,
            ack_status=CommandAckStatus.REJECTED,
        ),
    )
    accepted = client.post(
        f"/api/runner/commands/{command_id}/ack",
        json=rejected_ack.model_dump(mode="json"),
    )
    assert accepted.status_code == 200
    assert accepted.json()["duplicate"] is False
    assert client.get(f"/api/commands/{command_id}").json()["ack_status"] == "rejected"


def test_revocation_requires_a_paired_lowercase_sha256_audit_digest(
    client: TestClient,
    review_grant: ReviewGrantEnvelope,
) -> None:
    factory = client.app.state.sessions
    with factory() as db:
        row = db.get(ReviewGrant, str(review_grant.payload.grant_id))
        assert row is not None
        row.revoked_at = datetime.now(UTC)
        row.revocation_envelope_digest = "A" * 64
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


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
    assert status_json["events"][-1]["evidence_digest"] == "e" * 64


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
