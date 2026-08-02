from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_control_plane.app import create_app
from job_control_plane.config import Settings
from job_control_plane.db import Base, current_revision
from job_control_plane.models import OperatorSession, RunnerDevice
from job_control_plane.protocol import (
    AdapterCode,
    AttemptStage,
    CommandAckEnvelope,
    CommandAckPayload,
    CommandAckStatus,
    CommandPollEnvelope,
    CommandPollPayload,
    HeartbeatEnvelope,
    HeartbeatPayload,
    ReviewGrantEnvelope,
    ReviewGrantPayload,
    ReviewGrantRevocationEnvelope,
    ReviewGrantRevocationPayload,
    RunnerEventEnvelope,
    RunnerEventPayload,
    RunnerStatus,
)


def test_migration_refuses_missing_database_url(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    with pytest.raises(RuntimeError, match="CONTROL_DATABASE_URL is required"):
        command.upgrade(config, "head")

    assert list(tmp_path.iterdir()) == []


def _login_throttle_schema_snapshot(engine: Engine) -> dict[str, object]:
    inspector = inspect(engine)
    assert "control_login_throttle" in inspector.get_table_names()

    columns = {column["name"]: column for column in inspector.get_columns("control_login_throttle")}
    assert set(columns) == {
        "id",
        "window_started_at",
        "denial_count",
        "denial_audited_at",
    }
    assert columns["id"]["type"].length == 16
    assert columns["id"]["nullable"] is False
    assert columns["window_started_at"]["nullable"] is False
    assert columns["denial_count"]["nullable"] is False
    assert columns["denial_audited_at"]["nullable"] is True

    checks = {
        constraint["name"]: " ".join(constraint["sqltext"].split())
        for constraint in inspector.get_check_constraints("control_login_throttle")
    }
    assert set(checks) == {
        "ck_control_login_throttle_count",
        "ck_control_login_throttle_singleton",
    }
    assert checks["ck_control_login_throttle_count"] == ("denial_count >= 0 AND denial_count <= 8")
    assert checks["ck_control_login_throttle_singleton"] == ("id = 'operator_login'")

    primary_key = inspector.get_pk_constraint("control_login_throttle")
    assert primary_key["name"] == "pk_control_login_throttle"
    assert primary_key["constrained_columns"] == ["id"]

    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT id, window_started_at, denial_count, denial_audited_at "
                    "FROM control_login_throttle"
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["id"] == "operator_login"
    assert rows[0]["window_started_at"] is not None
    assert rows[0]["denial_count"] == 0
    assert rows[0]["denial_audited_at"] is None

    return {
        "columns": tuple(
            (
                name,
                str(column["type"]),
                column["nullable"],
                column["default"],
            )
            for name, column in columns.items()
        ),
        "checks": tuple(sorted(checks.items())),
        "primary_key": (
            primary_key["name"],
            tuple(primary_key["constrained_columns"]),
        ),
    }


def test_migration_upgrade_runtime_write_downgrade_round_trip(
    tmp_path: Path,
    monkeypatch,
    settings: Settings,
    sign_runner: Callable[..., Any],
) -> None:
    root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "migration.sqlite"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.chdir(root)
    monkeypatch.setenv("CONTROL_DATABASE_URL", database_url)
    config = Config(str(root / "alembic.ini"))

    command.upgrade(config, "0003_login_throttle")
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    assert current_revision(engine) == "0003_login_throttle"
    published_login_throttle = _login_throttle_schema_snapshot(engine)
    published_audit_indexes = {
        index["name"] for index in inspect(engine).get_indexes("control_operator_audit")
    }
    assert "ix_control_operator_audit_created_at" in published_audit_indexes
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    assert current_revision(engine) == "0006_runner_operations_summary"
    table_names = set(inspect(engine).get_table_names())
    assert {
        "control_runner_devices",
        "control_review_grants",
        "control_submission_commands",
        "control_runner_nonces",
        "control_runner_events",
        "control_operator_sessions",
        "control_operator_audit",
        "control_kill_switch_commands",
    }.issubset(table_names)
    assert "control_login_throttle" not in table_names
    review_grant_columns = {
        column["name"] for column in inspect(engine).get_columns("control_review_grants")
    }
    assert {"revoked_at", "revocation_envelope_digest"} <= review_grant_columns
    kill_command_columns = {
        column["name"] for column in inspect(engine).get_columns("control_kill_switch_commands")
    }
    assert "runner_boot_id" in kill_command_columns
    runner_columns = {
        column["name"] for column in inspect(engine).get_columns("control_runner_devices")
    }
    assert {
        "operations_digest",
        "policy_status",
        "policy_revision",
        "policy_expires_at",
        "policy_daily_remaining",
        "policy_hourly_remaining",
        "kill_switch_active",
        "pipeline_counters_json",
        "source_status_json",
        "adapter_status_json",
    } <= runner_columns
    migrated_kill_indexes = {
        index["name"] for index in inspect(engine).get_indexes("control_kill_switch_commands")
    }
    migrated_audit_indexes = {
        index["name"] for index in inspect(engine).get_indexes("control_operator_audit")
    }
    metadata_engine = create_engine("sqlite://")
    Base.metadata.create_all(metadata_engine)
    metadata_audit_indexes = {
        index["name"] for index in inspect(metadata_engine).get_indexes("control_operator_audit")
    }
    metadata_kill_indexes = {
        index["name"]
        for index in inspect(metadata_engine).get_indexes("control_kill_switch_commands")
    }
    assert migrated_audit_indexes == metadata_audit_indexes
    assert migrated_kill_indexes == metadata_kill_indexes
    assert migrated_audit_indexes == published_audit_indexes
    assert "ix_control_operator_audit_created_at" in migrated_audit_indexes
    metadata_engine.dispose()

    migrated_settings = replace(settings, database_url=database_url)
    app = create_app(migrated_settings, engine=engine)
    with TestClient(app, base_url=migrated_settings.public_origin) as client:
        login = client.post(
            "/auth/login",
            headers={"origin": migrated_settings.public_origin},
            json={"token": migrated_settings.operator_token},
        )
        assert login.status_code == 200  # SQLite autoincrement matches the ORM.
        heartbeat = sign_runner(
            HeartbeatEnvelope,
            HeartbeatPayload(
                boot_id=UUID("00000000-0000-0000-0000-000000000001"),
                release_digest="a" * 40,
                status=RunnerStatus.READY,
            ),
        )
        accepted = client.post(
            "/api/runner/heartbeat",
            json=heartbeat.model_dump(mode="json"),
        )
        assert accepted.status_code == 200  # Nonce BigInteger variant also works.
        with Session(engine) as db:
            device = db.execute(select(RunnerDevice)).scalar_one()
            assert device.policy_status == "unavailable"
            assert device.pipeline_counters_json == "{}"
            assert device.source_status_json == "[]"
            assert device.adapter_status_json == "[]"
    engine.dispose()

    command.downgrade(config, "0003_login_throttle")
    restored = create_engine(database_url)
    assert current_revision(restored) == "0003_login_throttle"
    assert _login_throttle_schema_snapshot(restored) == published_login_throttle
    restored_audit_indexes = {
        index["name"] for index in inspect(restored).get_indexes("control_operator_audit")
    }
    assert restored_audit_indexes == published_audit_indexes
    with restored.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM control_operator_audit")).scalar_one()
            >= 1
        )
    restored.dispose()

    command.downgrade(config, "0002_review_grant_revocations")
    intermediate = create_engine(database_url)
    assert current_revision(intermediate) == "0002_review_grant_revocations"
    assert "control_login_throttle" not in inspect(intermediate).get_table_names()
    intermediate_audit_indexes = {
        index["name"] for index in inspect(intermediate).get_indexes("control_operator_audit")
    }
    assert "ix_control_operator_audit_created_at" not in intermediate_audit_indexes
    with intermediate.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM control_operator_audit")).scalar_one()
            >= 1
        )
    intermediate.dispose()

    command.downgrade(config, "base")
    downgraded = create_engine(database_url)
    assert not any(name.startswith("control_") for name in inspect(downgraded).get_table_names())
    downgraded.dispose()


def test_operations_summary_migration_preserves_runner_identity_and_round_trips(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'operations-summary-migration.sqlite'}"
    monkeypatch.chdir(root)
    monkeypatch.setenv("CONTROL_DATABASE_URL", database_url)
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "0005_kill_switch_commands")
    engine = create_engine(database_url)
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO control_runner_devices ("
                "id, public_key_b64, active, created_at, last_seen_at, boot_id, "
                "release_digest, status"
                ") VALUES ("
                ":id, :public_key, true, :created_at, :last_seen_at, :boot_id, "
                ":release_digest, 'ready'"
                ")"
            ),
            {
                "id": "00000000-0000-4000-8000-000000000006",
                "public_key": "A" * 43,
                "created_at": now,
                "last_seen_at": now,
                "boot_id": "00000000-0000-4000-8000-000000000007",
                "release_digest": "a" * 64,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")
    upgraded = create_engine(database_url)
    assert current_revision(upgraded) == "0006_runner_operations_summary"
    with upgraded.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT id, release_digest, status, operations_digest, policy_status, "
                    "policy_revision, policy_daily_remaining, policy_hourly_remaining, "
                    "kill_switch_active, pipeline_counters_json, source_status_json, "
                    "adapter_status_json FROM control_runner_devices"
                )
            )
            .mappings()
            .one()
        )
    assert row["id"] == "00000000-0000-4000-8000-000000000006"
    assert row["release_digest"] == "a" * 64
    assert row["status"] == "ready"
    assert row["operations_digest"] is None
    assert row["policy_status"] == "unavailable"
    assert row["policy_revision"] == 0
    assert row["policy_daily_remaining"] == 0
    assert row["policy_hourly_remaining"] == 0
    assert bool(row["kill_switch_active"]) is False
    assert row["pipeline_counters_json"] == "{}"
    assert row["source_status_json"] == "[]"
    assert row["adapter_status_json"] == "[]"
    upgraded.dispose()

    command.downgrade(config, "0005_kill_switch_commands")
    downgraded = create_engine(database_url)
    assert current_revision(downgraded) == "0005_kill_switch_commands"
    columns = {item["name"] for item in inspect(downgraded).get_columns("control_runner_devices")}
    assert "operations_digest" not in columns
    assert "pipeline_counters_json" not in columns
    with downgraded.connect() as connection:
        preserved = (
            connection.execute(
                text("SELECT id, release_digest, status FROM control_runner_devices")
            )
            .mappings()
            .one()
        )
    assert preserved == {
        "id": "00000000-0000-4000-8000-000000000006",
        "release_digest": "a" * 64,
        "status": "ready",
    }
    downgraded.dispose()


def test_stale_schema_blocks_every_authority_path_before_state_changes(
    tmp_path: Path,
    monkeypatch,
    settings: Settings,
    sign_runner: Callable[..., Any],
) -> None:
    root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'stale-schema.sqlite'}"
    monkeypatch.setenv("CONTROL_DATABASE_URL", database_url)
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "0002_review_grant_revocations")

    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    stale_settings = replace(settings, database_url=database_url)
    app = create_app(stale_settings, engine=engine)
    now = datetime.now(UTC)
    boot_id = uuid4()
    grant_id = uuid4()
    application_ref = uuid4()
    command_id = uuid4()
    heartbeat = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=boot_id,
            release_digest="a" * 40,
            status=RunnerStatus.READY,
        ),
    )
    grant = sign_runner(
        ReviewGrantEnvelope,
        ReviewGrantPayload(
            grant_id=grant_id,
            application_ref=application_ref,
            application_revision=1,
            adapter=AdapterCode.WORKDAY,
            adapter_version="2.0.0",
            form_fingerprint_digest="b" * 64,
            reviewed_at=now,
        ),
    )
    revocation = sign_runner(
        ReviewGrantRevocationEnvelope,
        ReviewGrantRevocationPayload(
            grant_id=grant_id,
            application_ref=application_ref,
            application_revision=1,
            adapter=AdapterCode.WORKDAY,
            adapter_version="2.0.0",
            form_fingerprint_digest="b" * 64,
            reviewed_at=now,
            grant_expires_at=now + timedelta(minutes=5),
            revoked_at=now + timedelta(seconds=1),
        ),
    )
    poll = sign_runner(
        CommandPollEnvelope,
        CommandPollPayload(boot_id=boot_id),
    )
    ack = sign_runner(
        CommandAckEnvelope,
        CommandAckPayload(
            command_id=command_id,
            ack_status=CommandAckStatus.RECEIVED,
        ),
    )
    event = sign_runner(
        RunnerEventEnvelope,
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=command_id,
            sequence=1,
            stage=AttemptStage.QUEUED,
            occurred_at=now,
        ),
    )
    schema_blocked_requests = [
        (
            "POST",
            "/auth/login",
            {"token": stale_settings.operator_token},
        ),
        ("POST", "/api/runner/heartbeat", heartbeat.model_dump(mode="json")),
        ("POST", "/api/runner/review-grants", grant.model_dump(mode="json")),
        (
            "POST",
            "/api/runner/review-grant-revocations",
            revocation.model_dump(mode="json"),
        ),
        ("POST", "/api/runner/commands/poll", poll.model_dump(mode="json")),
        (
            "POST",
            f"/api/runner/commands/{command_id}/ack",
            ack.model_dump(mode="json"),
        ),
        ("POST", "/api/runner/events", event.model_dump(mode="json")),
    ]

    with TestClient(app, base_url=stale_settings.public_origin) as client:
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ok"}
        checkouts = 0

        def count_checkout(*_args) -> None:
            nonlocal checkouts
            checkouts += 1

        sqlalchemy_event.listen(engine, "checkout", count_checkout)
        try:
            invalid = client.post(
                "/auth/login",
                headers={"origin": stale_settings.public_origin},
                json={"token": "invalid-" + ("x" * 32)},
            )
        finally:
            sqlalchemy_event.remove(engine, "checkout", count_checkout)
        assert invalid.status_code == 401
        assert invalid.json() == {"code": "TOKEN_INVALID"}
        assert checkouts == 0
        root = client.get("/")
        assert root.status_code == 200
        assert "Enter the operator token" in root.text
        for method, path in (
            ("GET", "/health/ready"),
            ("GET", "/api/review-grants"),
            ("GET", "/api/commands"),
            ("POST", "/auth/logout"),
            ("POST", "/api/send"),
        ):
            response = client.request(method, path, json={})
            assert response.status_code == 401, path
        for method, path, payload in schema_blocked_requests:
            response = client.request(
                method,
                path,
                headers={"origin": stale_settings.public_origin},
                json=payload,
            )
            assert response.status_code == 503, path
            assert response.json() == {"code": "SCHEMA_NOT_CURRENT"}, path
            assert "set-cookie" not in response.headers

    with engine.connect() as connection:
        for table_name in (
            "control_operator_sessions",
            "control_operator_audit",
            "control_runner_devices",
            "control_review_grants",
            "control_submission_commands",
            "control_runner_nonces",
            "control_runner_events",
        ):
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one() == 0
    engine.dispose()


def test_future_schema_is_fail_closed_before_login_or_runner_state(
    client: TestClient,
    settings: Settings,
    sign_runner: Callable[..., Any],
) -> None:
    engine = client.app.state.engine
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = 'future_schema_revision'")
        )

    checkouts = 0

    def count_checkout(*_args) -> None:
        nonlocal checkouts
        checkouts += 1

    sqlalchemy_event.listen(engine, "checkout", count_checkout)
    try:
        invalid = client.post(
            "/auth/login",
            headers={"origin": settings.public_origin},
            json={"token": "invalid-" + ("x" * 32)},
        )
    finally:
        sqlalchemy_event.remove(engine, "checkout", count_checkout)
    assert invalid.status_code == 401
    assert invalid.json() == {"code": "TOKEN_INVALID"}
    assert checkouts == 0
    valid = client.post(
        "/auth/login",
        headers={"origin": settings.public_origin},
        json={"token": settings.operator_token},
    )
    assert valid.status_code == 503
    assert valid.json() == {"code": "SCHEMA_NOT_CURRENT"}

    heartbeat = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="a" * 40,
            status=RunnerStatus.READY,
        ),
    )
    runner = client.post(
        "/api/runner/heartbeat",
        json=heartbeat.model_dump(mode="json"),
    )
    assert runner.status_code == 503
    assert runner.json() == {"code": "SCHEMA_NOT_CURRENT"}
    assert client.get("/").status_code == 200
    assert client.get("/health/ready").status_code == 401
    assert client.get("/health/live").status_code == 200

    with engine.connect() as connection:
        for table_name in (
            "control_operator_sessions",
            "control_operator_audit",
            "control_runner_devices",
            "control_runner_nonces",
        ):
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one() == 0


def test_unsigned_runner_requests_are_database_free(
    client: TestClient,
    sign_runner: Callable[..., Any],
) -> None:
    engine = client.app.state.engine
    heartbeat = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="a" * 40,
            status=RunnerStatus.READY,
        ),
    )
    tampered = heartbeat.model_copy(update={"signature": "A" * 86})
    checkouts = 0

    def count_checkout(*_args) -> None:
        nonlocal checkouts
        checkouts += 1

    sqlalchemy_event.listen(engine, "checkout", count_checkout)
    try:
        malformed = client.post("/api/runner/heartbeat", json={})
        unsigned = client.post(
            "/api/runner/heartbeat",
            json=tampered.model_dump(mode="json"),
        )
        assert malformed.status_code == 422
        assert unsigned.status_code == 401
        assert unsigned.json() == {"code": "RUNNER_SIGNATURE_INVALID"}
        assert checkouts == 0

        signed = client.post(
            "/api/runner/heartbeat",
            json=heartbeat.model_dump(mode="json"),
        )
        assert signed.status_code == 200
        assert checkouts > 0
    finally:
        sqlalchemy_event.remove(engine, "checkout", count_checkout)


@pytest.mark.parametrize(
    "additional_revision",
    ["0002_review_grant_revocations", "future_schema_revision"],
)
def test_multi_head_schema_is_fail_closed_without_authority_state(
    client: TestClient,
    settings: Settings,
    sign_runner: Callable[..., Any],
    additional_revision: str,
) -> None:
    engine = client.app.state.engine
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": additional_revision},
        )

    valid = client.post(
        "/auth/login",
        headers={"origin": settings.public_origin},
        json={"token": settings.operator_token},
    )
    assert valid.status_code == 503
    assert valid.json() == {"code": "SCHEMA_NOT_CURRENT"}
    assert "set-cookie" not in valid.headers
    checkouts = 0

    def count_checkout(*_args) -> None:
        nonlocal checkouts
        checkouts += 1

    sqlalchemy_event.listen(engine, "checkout", count_checkout)
    try:
        invalid = client.post(
            "/auth/login",
            headers={"origin": settings.public_origin},
            json={"token": "invalid-" + ("x" * 32)},
        )
    finally:
        sqlalchemy_event.remove(engine, "checkout", count_checkout)
    assert invalid.status_code == 401
    assert invalid.json() == {"code": "TOKEN_INVALID"}
    assert checkouts == 0

    heartbeat = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="a" * 40,
            status=RunnerStatus.READY,
        ),
    )
    runner = client.post(
        "/api/runner/heartbeat",
        json=heartbeat.model_dump(mode="json"),
    )
    assert runner.status_code == 503
    assert runner.json() == {"code": "SCHEMA_NOT_CURRENT"}
    assert client.get("/").status_code == 200
    assert client.get("/health/ready").status_code == 401
    assert client.get("/health/live").status_code == 200

    with engine.connect() as connection:
        for table_name in (
            "control_operator_sessions",
            "control_operator_audit",
            "control_runner_devices",
            "control_runner_nonces",
        ):
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one() == 0


@pytest.mark.parametrize(
    ("mode", "other_revision"),
    [
        ("single", "0002_review_grant_revocations"),
        ("single", "future_schema_revision"),
        ("multi", "0002_review_grant_revocations"),
        ("multi", "future_schema_revision"),
    ],
)
def test_valid_session_on_noncurrent_schema_is_503_without_touching_session(
    client: TestClient,
    authenticated: str,
    mode: str,
    other_revision: str,
) -> None:
    assert authenticated
    engine = client.app.state.engine
    factory = client.app.state.sessions
    with factory() as db:
        session = db.scalar(select(OperatorSession))
        assert session is not None
        last_seen_at = session.last_seen_at
    with engine.connect() as connection:
        audit_count = connection.execute(
            text("SELECT COUNT(*) FROM control_operator_audit")
        ).scalar_one()
    with engine.begin() as connection:
        if mode == "single":
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": other_revision},
            )
        else:
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": other_revision},
            )

    protected = client.get("/api/commands")
    dashboard = client.get("/")
    assert protected.status_code == 503
    assert protected.json() == {"code": "SCHEMA_NOT_CURRENT"}
    assert dashboard.status_code == 503
    assert dashboard.json() == {"code": "SCHEMA_NOT_CURRENT"}
    assert client.get("/health/live").status_code == 200

    with factory() as db:
        session = db.scalar(select(OperatorSession))
        assert session is not None
        assert session.last_seen_at == last_seen_at
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM control_operator_audit")).scalar_one()
            == audit_count
        )


def test_revocation_downgrade_preserves_noneligibility(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "revocation-downgrade.sqlite"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.chdir(root)
    monkeypatch.setenv("CONTROL_DATABASE_URL", database_url)
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")

    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    now = datetime.now(UTC)
    device_id = "00000000-0000-4000-8000-000000000001"
    grant_id = "00000000-0000-4000-8000-000000000002"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO control_runner_devices ("
                "id, public_key_b64, active, created_at"
                ") VALUES (:id, :public_key, :active, :created_at)"
            ),
            {
                "id": device_id,
                "public_key": "a" * 43,
                "active": True,
                "created_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO control_review_grants ("
                "id, device_id, application_ref, application_revision, "
                "adapter, adapter_version, form_fingerprint_digest, envelope_digest, "
                "reviewed_at, expires_at, created_at, consumed_at, "
                "revoked_at, revocation_envelope_digest"
                ") VALUES ("
                ":id, :device_id, :application_ref, 1, "
                "'greenhouse', '1.0.0', :fingerprint, :envelope_digest, "
                ":reviewed_at, :expires_at, :created_at, NULL, "
                ":revoked_at, :revocation_digest"
                ")"
            ),
            {
                "id": grant_id,
                "device_id": device_id,
                "application_ref": "00000000-0000-4000-8000-000000000003",
                "fingerprint": "b" * 64,
                "envelope_digest": "c" * 64,
                "reviewed_at": now,
                "expires_at": now + timedelta(minutes=5),
                "created_at": now,
                "revoked_at": now + timedelta(seconds=1),
                "revocation_digest": "d" * 64,
            },
        )
    engine.dispose()

    command.downgrade(config, "0001_control_plane")
    downgraded = create_engine(database_url, connect_args={"check_same_thread": False})
    assert current_revision(downgraded) == "0001_control_plane"
    columns = {
        column["name"] for column in inspect(downgraded).get_columns("control_review_grants")
    }
    assert "revoked_at" not in columns
    with downgraded.connect() as connection:
        row = connection.execute(
            text("SELECT consumed_at FROM control_review_grants WHERE id = :grant_id"),
            {"grant_id": grant_id},
        ).one()
        eligible_count = connection.scalar(
            text(
                "SELECT count(*) FROM control_review_grants "
                "WHERE id = :grant_id AND consumed_at IS NULL AND expires_at > :now"
            ),
            {"grant_id": grant_id, "now": now},
        )
    assert row.consumed_at is not None
    assert eligible_count == 0
    downgraded.dispose()


def test_cloud_models_have_no_candidate_or_document_fields() -> None:
    forbidden = {
        "email",
        "phone",
        "name",
        "job_url",
        "url",
        "cv",
        "resume",
        "answer",
        "question",
        "cover_letter",
        "cookie",
        "candidate",
        "nationality",
        "gender",
    }
    columns = {column.name for table in Base.metadata.sorted_tables for column in table.columns}
    assert not (columns & forbidden)
    assert all(
        not any(token in column for token in forbidden - {"name", "url", "cv"})
        for column in columns
    )


def test_control_plane_has_no_parent_application_imports() -> None:
    root = Path(__file__).resolve().parents[1]
    banned = {
        "api",
        "bridge",
        "core",
        "db",
        "llm",
        "profile",
        "submitters",
        "worker",
    }
    violations: list[str] = []
    for path in (root / "job_control_plane").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            if roots & banned:
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []


def test_project_manifest_and_vercel_ignore_cover_private_material() -> None:
    root = Path(__file__).resolve().parents[1]
    allowed = {
        ".gitignore",
        ".vercelignore",
        "MIGRATIONS.md",
        "README.md",
        "alembic.ini",
        "api",
        "job_control_plane",
        "migrations",
        "pyproject.toml",
        "requirements.txt",
        "tests",
        "vercel.json",
    }
    present = {
        path.name
        for path in root.iterdir()
        if path.name not in {".pytest_cache", ".ruff_cache", "__pycache__"}
    }
    assert present <= allowed

    ignore = (root / ".vercelignore").read_text(encoding="utf-8").lower()
    for marker in (
        ".env",
        ".vercel",
        "sqlite",
        "*.pdf",
        "*.docx",
        "*.key",
        "*.pem",
        "user_profile",
        "cv_routing",
        "resume",
        "profile-data",
        "runtime-data",
        "uploads",
        "session-data",
        "browser-data",
        "browser-state",
        "device-state",
        ".linkedin_profile",
        ".portal_profiles",
        "*secret*",
        "*token*",
        "*credential*",
    ):
        assert marker in ignore
