from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from job_control_plane.app import create_app
from job_control_plane.config import Settings
from job_control_plane.db import Base, current_revision
from job_control_plane.protocol import HeartbeatEnvelope, HeartbeatPayload, RunnerStatus


def test_migration_refuses_missing_database_url(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    with pytest.raises(RuntimeError, match="CONTROL_DATABASE_URL is required"):
        command.upgrade(config, "head")

    assert list(tmp_path.iterdir()) == []


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

    command.upgrade(config, "head")
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    assert current_revision(engine) == "0003_login_throttle"
    table_names = set(inspect(engine).get_table_names())
    assert {
        "control_runner_devices",
        "control_review_grants",
        "control_submission_commands",
        "control_runner_nonces",
        "control_runner_events",
        "control_operator_sessions",
        "control_operator_audit",
        "control_login_throttle",
    }.issubset(table_names)
    review_grant_columns = {
        column["name"] for column in inspect(engine).get_columns("control_review_grants")
    }
    assert {"revoked_at", "revocation_envelope_digest"} <= review_grant_columns
    throttle_columns = {
        column["name"] for column in inspect(engine).get_columns("control_login_throttle")
    }
    assert {
        "id",
        "window_started_at",
        "denial_count",
        "denial_audited_at",
    } == throttle_columns
    with engine.connect() as connection:
        throttle = connection.execute(
            text("SELECT id, denial_count FROM control_login_throttle WHERE id = 'operator_login'")
        ).one()
        assert throttle == ("operator_login", 0)

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
    engine.dispose()

    command.downgrade(config, "0002_review_grant_revocations")
    intermediate = create_engine(database_url)
    assert current_revision(intermediate) == "0002_review_grant_revocations"
    assert "control_login_throttle" not in inspect(intermediate).get_table_names()
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
