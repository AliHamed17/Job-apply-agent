"""Durability and privacy tests for signed redacted runner events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, ControlPlaneEventOutbox
from worker.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneClientConfig,
    ControlPlaneClientError,
)
from worker.control_plane_event_outbox import (
    ControlPlaneEventError,
    RedactedControlPlaneEvent,
    claim_control_plane_event,
    deliver_claimed_control_plane_event,
    enqueue_control_plane_event,
    new_event_ref,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.mark.asyncio
async def test_outbox_delivers_only_the_canonical_event_shape():
    db = _session()
    now = datetime.now(UTC)
    command_id = str(uuid4())
    row = enqueue_control_plane_event(
        db,
        RedactedControlPlaneEvent(
            event_id=new_event_ref(),
            command_id=command_id,
            sequence=1,
            stage="queued",
            occurred_at=now,
        ),
    )
    db.commit()
    claim = claim_control_plane_event(db, runner_id=str(uuid4()), now=now)
    assert claim is not None
    sent: list[dict[str, object]] = []

    def signer(purpose, payload):
        assert purpose == "runner_event"
        return {"signed": True, "payload": dict(payload)}

    async def sender(envelope):
        sent.append(dict(envelope))

    await deliver_claimed_control_plane_event(
        db,
        row_id=claim[0],
        claim_token=claim[1],
        signer=signer,
        sender=sender,
        now=now,
    )
    assert sent == [
        {
            "signed": True,
            "payload": {
                "event_id": row.event_ref,
                "command_id": command_id,
                "sequence": 1,
                "stage": "queued",
                "occurred_at": now.isoformat(),
            },
        }
    ]
    saved = db.get(ControlPlaneEventOutbox, row.id)
    assert saved.state == "sent"
    assert saved.sent_at is not None
    db.close()


@pytest.mark.asyncio
async def test_database_payload_tampering_is_never_sent():
    db = _session()
    now = datetime.now(UTC)
    row = enqueue_control_plane_event(
        db,
        RedactedControlPlaneEvent(
            event_id=new_event_ref(),
            command_id=str(uuid4()),
            sequence=1,
            stage="queued",
            occurred_at=now,
        ),
    )
    db.commit()
    row.payload_json = '{"email":"candidate@example.com"}'
    db.commit()
    claim = claim_control_plane_event(db, runner_id=str(uuid4()), now=now)
    assert claim is not None
    sent = False

    async def sender(_envelope):
        nonlocal sent
        sent = True

    with pytest.raises(ControlPlaneEventError, match="EVENT_PAYLOAD_CORRUPT"):
        await deliver_claimed_control_plane_event(
            db,
            row_id=claim[0],
            claim_token=claim[1],
            signer=lambda _purpose, payload: payload,
            sender=sender,
            now=now,
        )
    assert sent is False
    assert db.get(ControlPlaneEventOutbox, row.id).state == "pending"
    db.close()


@pytest.mark.asyncio
async def test_stale_claim_is_recovered_without_changing_terminal_evidence():
    db = _session()
    now = datetime.now(UTC)
    command_id = str(uuid4())
    event_id = new_event_ref()
    row = enqueue_control_plane_event(
        db,
        RedactedControlPlaneEvent(
            event_id=event_id,
            command_id=command_id,
            sequence=7,
            stage="finished",
            outcome="confirmed_submitted",
            evidence_type="schema_valid_receipt",
            evidence_digest="e" * 64,
            occurred_at=now,
        ),
    )
    db.commit()
    original_payload = row.payload_json
    original_digest = row.payload_digest
    abandoned = claim_control_plane_event(db, runner_id=str(uuid4()), now=now)
    assert abandoned is not None
    assert (
        claim_control_plane_event(
            db,
            runner_id=str(uuid4()),
            now=now + timedelta(seconds=29),
        )
        is None
    )

    recovered = claim_control_plane_event(
        db,
        runner_id=str(uuid4()),
        now=now + timedelta(seconds=30),
    )
    assert recovered is not None
    assert recovered[0] == abandoned[0] == row.id
    assert recovered[1] != abandoned[1]
    saved = db.get(ControlPlaneEventOutbox, row.id)
    assert saved.event_ref == event_id
    assert saved.payload_json == original_payload
    assert saved.payload_digest == original_digest
    assert saved.delivery_count == 2
    sent: list[dict[str, object]] = []

    await deliver_claimed_control_plane_event(
        db,
        row_id=recovered[0],
        claim_token=recovered[1],
        signer=lambda _purpose, payload: payload,
        sender=lambda envelope: _record(sent, envelope),
        now=now + timedelta(seconds=31),
    )
    assert sent == [
        {
            "event_id": event_id,
            "command_id": command_id,
            "sequence": 7,
            "stage": "finished",
            "outcome": "confirmed_submitted",
            "evidence_type": "schema_valid_receipt",
            "evidence_digest": "e" * 64,
            "occurred_at": now.isoformat(),
        }
    ]
    db.close()


@pytest.mark.asyncio
async def test_unaccepted_cloud_receipt_keeps_the_same_event_retryable():
    db = _session()
    now = datetime.now(UTC)
    row = enqueue_control_plane_event(
        db,
        RedactedControlPlaneEvent(
            event_id=new_event_ref(),
            command_id=str(uuid4()),
            sequence=1,
            stage="queued",
            occurred_at=now,
        ),
    )
    db.commit()
    original_payload = row.payload_json
    original_digest = row.payload_digest
    claim = claim_control_plane_event(db, runner_id=str(uuid4()), now=now)
    assert claim is not None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"accepted": False},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlPlaneClient(
        ControlPlaneClientConfig("https://control.example"),
        http=http,
    )
    with pytest.raises(ControlPlaneClientError, match="CONTROL_PLANE_RECEIPT_INVALID"):
        await deliver_claimed_control_plane_event(
            db,
            row_id=claim[0],
            claim_token=claim[1],
            signer=lambda _purpose, payload: {"payload": payload},
            sender=client.send_event,
            now=now,
        )
    saved = db.get(ControlPlaneEventOutbox, row.id)
    assert saved.state == "pending"
    assert saved.event_ref == row.event_ref
    assert saved.payload_json == original_payload
    assert saved.payload_digest == original_digest
    assert saved.sent_at is None
    assert saved.last_error_code == "CONTROL_PLANE_DELIVERY_FAILED"
    await http.aclose()
    db.close()


async def _record(destination, value):
    destination.append(dict(value))


def test_event_contract_rejects_free_text_and_inconsistent_terminal_states():
    now = datetime.now(UTC)
    with pytest.raises(ControlPlaneEventError, match="EVENT_REASON_INVALID"):
        RedactedControlPlaneEvent(
            event_id=new_event_ref(),
            command_id=str(uuid4()),
            sequence=1,
            stage="finished",
            outcome="needs_review",
            reason_code="candidate@example.com",
            occurred_at=now,
        )
    with pytest.raises(ControlPlaneEventError, match="EVENT_EVIDENCE_TYPE_INVALID"):
        RedactedControlPlaneEvent(
            event_id=new_event_ref(),
            command_id=str(uuid4()),
            sequence=1,
            stage="finished",
            outcome="confirmed_submitted",
            evidence_type="email",
            evidence_digest="e" * 64,
            occurred_at=now,
        )
    with pytest.raises(ControlPlaneEventError, match="EVENT_REASON_INVALID"):
        RedactedControlPlaneEvent(
            event_id=new_event_ref(),
            command_id=str(uuid4()),
            sequence=1,
            stage="finished",
            outcome="needs_review",
            reason_code="ALI_HAMED",
            occurred_at=now,
        )
