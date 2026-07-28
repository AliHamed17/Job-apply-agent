from __future__ import annotations

# ruff: noqa: E402
import sys
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_PLANE_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_ROOT))

from job_control_plane.app import create_app
from job_control_plane.config import Settings
from job_control_plane.crypto import (
    private_key_to_base64url,
    public_key_to_base64url,
    sign_envelope,
)
from job_control_plane.protocol import (
    CONTROL_AUDIENCE,
    AdapterCode,
    HeartbeatEnvelope,
    HeartbeatPayload,
    ReviewGrantEnvelope,
    ReviewGrantPayload,
    RunnerStatus,
    SignedEnvelope,
    StrictProtocolModel,
)

EnvelopeT = TypeVar("EnvelopeT", bound=SignedEnvelope[Any])


@pytest.fixture
def runner_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def control_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def settings(
    tmp_path: Path,
    runner_private_key: Ed25519PrivateKey,
    control_private_key: Ed25519PrivateKey,
) -> Settings:
    return Settings(
        app_env="test",
        vercel_env="",
        database_url=f"sqlite:///{tmp_path / 'control-plane.sqlite'}",
        public_origin="http://testserver",
        operator_token="operator-token-" + ("o" * 48),
        session_secret="session-secret-" + ("s" * 48),
        csrf_secret="csrf-secret-" + ("c" * 48),
        control_signing_private_key=private_key_to_base64url(control_private_key),
        control_signing_key_id=uuid4(),
        runner_device_id=uuid4(),
        runner_verify_public_key=public_key_to_base64url(runner_private_key.public_key()),
        secure_cookies=False,
        test_dispatch_allowed=True,
    )


@pytest.fixture
def client(settings: Settings, monkeypatch) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("CONTROL_DATABASE_URL", settings.database_url)
    command.upgrade(Config(str(CONTROL_PLANE_ROOT / "alembic.ini")), "head")
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    app = create_app(settings, engine=engine)
    with TestClient(app, base_url=settings.public_origin) as test_client:
        yield test_client
    engine.dispose()


@pytest.fixture
def sign_runner(
    settings: Settings,
    runner_private_key: Ed25519PrivateKey,
) -> Callable[..., Any]:
    def build(
        envelope_type: type[EnvelopeT],
        payload: StrictProtocolModel,
        *,
        issued_at: datetime | None = None,
        expires_at: datetime | None = None,
        nonce: UUID | None = None,
        audience: str = CONTROL_AUDIENCE,
    ) -> EnvelopeT:
        now = issued_at or datetime.now(UTC)
        unsigned = envelope_type(
            key_id=settings.runner_device_id,
            audience=audience,
            issued_at=now,
            expires_at=expires_at or now + timedelta(seconds=60),
            nonce=nonce or uuid4(),
            payload=payload,
        )
        return envelope_type.model_validate(
            sign_envelope(unsigned, runner_private_key).model_dump()
        )

    return build


@pytest.fixture
def authenticated(client: TestClient, settings: Settings) -> str:
    response = client.post(
        "/auth/login",
        headers={"origin": settings.public_origin},
        json={"token": settings.operator_token},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


@pytest.fixture
def heartbeat(
    client: TestClient,
    sign_runner: Callable[..., Any],
) -> HeartbeatEnvelope:
    envelope = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="a" * 40,
            status=RunnerStatus.READY,
        ),
    )
    response = client.post("/api/runner/heartbeat", json=envelope.model_dump(mode="json"))
    assert response.status_code == 200
    return envelope


@pytest.fixture
def review_grant(
    client: TestClient,
    sign_runner: Callable[..., Any],
    heartbeat: HeartbeatEnvelope,
) -> ReviewGrantEnvelope:
    now = datetime.now(UTC)
    envelope = sign_runner(
        ReviewGrantEnvelope,
        ReviewGrantPayload(
            grant_id=uuid4(),
            application_ref=uuid4(),
            application_revision=3,
            adapter=AdapterCode.WORKDAY,
            adapter_version="2.0.0",
            form_fingerprint_digest="b" * 64,
            reviewed_at=now,
        ),
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    response = client.post(
        "/api/runner/review-grants",
        json=envelope.model_dump(mode="json"),
    )
    assert response.status_code == 200
    return envelope
