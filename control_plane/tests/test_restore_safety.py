from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from job_control_plane.config import Settings
from job_control_plane.db import Base
from job_control_plane.models import (
    OperatorSession,
    ReviewGrant,
    RunnerDevice,
    RunnerEvent,
    SubmissionCommand,
)
from job_control_plane.protocol import ReviewGrantEnvelope
from job_control_plane.restore_safety import quarantine_restored_control_plane


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'control-restore.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _grant(device: RunnerDevice, *, now: datetime, sequence: int) -> ReviewGrant:
    return ReviewGrant(
        id=str(uuid4()),
        device_id=device.id,
        application_ref=str(uuid4()),
        application_revision=sequence,
        adapter="workday",
        adapter_version="2.0.3",
        form_fingerprint_digest=f"{sequence:x}".rjust(64, "a"),
        envelope_digest=f"{sequence:x}".rjust(64, "b"),
        reviewed_at=now,
        expires_at=now + timedelta(minutes=5),
        created_at=now,
    )


def _command(
    device: RunnerDevice,
    grant: ReviewGrant,
    *,
    now: datetime,
    sequence: int,
    status: str,
) -> SubmissionCommand:
    claimed = status == "claimed"
    return SubmissionCommand(
        id=str(uuid4()),
        grant_id=grant.id,
        device_id=device.id,
        application_ref=grant.application_ref,
        application_revision=sequence,
        adapter="workday",
        adapter_version="2.0.3",
        form_fingerprint_digest=grant.form_fingerprint_digest,
        client_idempotency_digest=f"{sequence:x}".rjust(64, "c"),
        status=status,
        signed_envelope_json="{}",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        claimed_at=now if claimed else None,
        claim_lease_expires_at=now + timedelta(seconds=15) if claimed else None,
        delivery_count=1 if claimed else 0,
        acknowledged_at=now if status in {"acknowledged", "running", "finished"} else None,
        ack_status="received" if status in {"acknowledged", "running", "finished"} else None,
        finished_at=now if status == "finished" else None,
    )


def test_control_plane_restore_quarantine_revokes_authority_without_evidence(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    now = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    restored_at = now + timedelta(hours=1)
    with factory() as db:
        active_device = RunnerDevice(
            id=str(uuid4()),
            public_key_b64="a" * 43,
            active=True,
            created_at=now,
            last_seen_at=now,
            status="ready",
        )
        inactive_device = RunnerDevice(
            id=str(uuid4()),
            public_key_b64="b" * 43,
            active=False,
            created_at=now,
            last_seen_at=now,
            status="offline",
        )
        db.add_all([active_device, inactive_device])
        commands: dict[str, SubmissionCommand] = {}
        for sequence, status in enumerate(
            ("queued", "claimed", "acknowledged", "running", "finished"),
            start=1,
        ):
            grant = _grant(active_device, now=now, sequence=sequence)
            command = _command(
                active_device,
                grant,
                now=now,
                sequence=sequence,
                status=status,
            )
            db.add_all([grant, command])
            commands[status] = command
        event = RunnerEvent(
            id=str(uuid4()),
            device_id=active_device.id,
            command_id=commands["finished"].id,
            sequence=1,
            stage="finished",
            outcome="confirmed_submitted",
            reason_code=None,
            evidence_type="ats_visible_confirmation",
            evidence_digest="e" * 64,
            payload_digest="p" * 64,
            envelope_digest="q" * 64,
            occurred_at=now,
            received_at=now,
        )
        active_session = OperatorSession(
            session_token_digest="s" * 64,
            csrf_token_digest="c" * 64,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
        )
        revoked_session = OperatorSession(
            session_token_digest="t" * 64,
            csrf_token_digest="d" * 64,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
            revoked_at=now,
        )
        db.add_all([event, active_session, revoked_session])
        db.commit()
        event_snapshot = (
            event.stage,
            event.outcome,
            event.evidence_type,
            event.evidence_digest,
            event.payload_digest,
            event.envelope_digest,
        )

        summary = quarantine_restored_control_plane(db, now=restored_at)
        db.commit()

        assert summary.to_dict() == {
            "runner_devices_deactivated": 1,
            "operator_sessions_revoked": 1,
            "undelivered_commands_rejected": 2,
            "identity_rotation_required": True,
        }
        assert active_device.active is False
        assert inactive_device.active is False
        assert active_session.revoked_at == restored_at
        assert revoked_session.revoked_at == now
        assert commands["queued"].status == "rejected"
        assert commands["claimed"].status == "rejected"
        assert commands["claimed"].claim_lease_expires_at is None
        assert commands["acknowledged"].status == "acknowledged"
        assert commands["running"].status == "running"
        assert commands["finished"].status == "finished"
        assert db.scalar(select(RunnerEvent).where(RunnerEvent.id == event.id)) is event
        assert (
            event.stage,
            event.outcome,
            event.evidence_type,
            event.evidence_digest,
            event.payload_digest,
            event.envelope_digest,
        ) == event_snapshot
        assert db.query(RunnerEvent).count() == 1
        assert all(grant.revoked_at is None for grant in db.query(ReviewGrant))

        repeated = quarantine_restored_control_plane(
            db,
            now=restored_at + timedelta(minutes=1),
        )
        db.commit()
        assert repeated.to_dict() == {
            "runner_devices_deactivated": 0,
            "operator_sessions_revoked": 0,
            "undelivered_commands_rejected": 0,
            "identity_rotation_required": True,
        }


def test_control_plane_quarantine_script_dry_run_rolls_back(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "quarantine_restored_control_plane.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_quarantine_restored_control_plane_cli",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)

    factory = _factory(tmp_path)
    created_at = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    with factory() as db:
        device = RunnerDevice(
            id=str(uuid4()),
            public_key_b64="z" * 43,
            active=True,
            created_at=created_at,
            last_seen_at=created_at,
            status="ready",
        )
        db.add(device)
        db.commit()
        device_id = device.id

    class _DisposableEngine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(command.Settings, "from_env", lambda: object())
    monkeypatch.setattr(command, "build_engine", lambda _settings: _DisposableEngine())
    monkeypatch.setattr(command, "build_session_factory", lambda _engine: factory)

    assert command.main(["--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["committed"] is False
    assert result["control_plane_restore_quarantine"]["runner_devices_deactivated"] == 1

    with factory() as db:
        assert db.get(RunnerDevice, device_id).active is True


def test_restored_inactive_device_hides_send_without_forging_grant_revocation(
    client: TestClient,
    settings: Settings,
    authenticated: str,
    review_grant: ReviewGrantEnvelope,
) -> None:
    assert authenticated
    grant_id = str(review_grant.payload.grant_id)
    restored_at = datetime.now(UTC) + timedelta(minutes=1)
    factory = client.app.state.sessions

    listed_before_restore = client.get("/api/review-grants")
    assert listed_before_restore.status_code == 200
    row_before_restore = next(
        item for item in listed_before_restore.json()["grants"] if item["grant_id"] == grant_id
    )
    assert row_before_restore["eligible"] is True
    assert row_before_restore["eligibility_state"] == "eligible"
    dashboard_before_restore = client.get("/")
    marker = f"<td><code>{grant_id}</code></td>"
    start = dashboard_before_restore.text.index(marker)
    active_row = dashboard_before_restore.text[
        start : dashboard_before_restore.text.index("</tr>", start)
    ]
    assert "Send application</button>" in active_row

    with factory.begin() as db:
        grant = db.get(ReviewGrant, grant_id)
        device = db.get(RunnerDevice, str(settings.runner_device_id))
        assert grant is not None
        assert device is not None and device.active is True
        assert grant.revoked_at is None
        summary = quarantine_restored_control_plane(db, now=restored_at)

    assert summary.runner_devices_deactivated == 1
    assert summary.operator_sessions_revoked == 1

    client.cookies.clear()
    login = client.post(
        "/auth/login",
        headers={"origin": settings.public_origin},
        json={"token": settings.operator_token},
    )
    assert login.status_code == 200

    listed = client.get("/api/review-grants")
    assert listed.status_code == 200
    row = next(item for item in listed.json()["grants"] if item["grant_id"] == grant_id)
    assert row["eligible"] is False
    assert row["eligibility_state"] == "runner_disabled"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    start = dashboard.text.index(marker)
    rendered_row = dashboard.text[start : dashboard.text.index("</tr>", start)]
    assert "<td>runner_disabled</td>" in rendered_row
    assert "Send application" not in rendered_row

    denied_send = client.post(
        "/api/send",
        headers={
            "origin": settings.public_origin,
            "x-csrf-token": login.json()["csrf_token"],
        },
        json={
            "grant_id": grant_id,
            "application_ref": str(review_grant.payload.application_ref),
            "application_revision": review_grant.payload.application_revision,
            "form_fingerprint_digest": review_grant.payload.form_fingerprint_digest,
            "acknowledgement": "SEND_APPLICATION",
            "client_idempotency_key": str(uuid4()),
        },
    )
    assert denied_send.status_code == 409
    assert denied_send.json() == {"code": "RUNNER_OFFLINE"}

    with factory() as db:
        grant = db.get(ReviewGrant, grant_id)
        assert grant is not None
        assert grant.revoked_at is None
        assert grant.revocation_envelope_digest is None
