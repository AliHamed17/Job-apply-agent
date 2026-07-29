from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from job_control_plane.config import (
    ConfigurationError,
    Settings,
    build_identity_bundle_digest,
)
from job_control_plane.crypto import (
    ProtocolVerificationError,
    private_key_to_base64url,
    public_key_to_base64url,
    sign_envelope,
    verify_envelope,
)
from job_control_plane.protocol import (
    CONTROL_AUDIENCE,
    AttemptOutcome,
    AttemptStage,
    EnvelopePurpose,
    HeartbeatEnvelope,
    HeartbeatPayload,
    RunnerEventPayload,
    RunnerStatus,
)

IDENTITY_VERSION = UUID("00000000-0000-4000-8000-000000000123")
PROJECT_ID = "prj_12345678abcdef"
SCOPE_ID = "team_12345678abcdef"


def _refresh_identity_digest(env: dict[str, str]) -> None:
    env["CONTROL_IDENTITY_BUNDLE_DIGEST"] = build_identity_bundle_digest(
        env,
        version_id=IDENTITY_VERSION,
        environment=env["VERCEL_ENV"],
        project_id=env["VERCEL_PROJECT_ID"],
        scope_id=SCOPE_ID,
    )


def _production_env() -> dict[str, str]:
    control = Ed25519PrivateKey.generate()
    runner = Ed25519PrivateKey.generate()
    env = {
        "APP_ENV": "production",
        "VERCEL_ENV": "production",
        "VERCEL_PROJECT_ID": PROJECT_ID,
        "CONTROL_DATABASE_URL": "postgresql+psycopg://db/control?sslmode=verify-full",
        "CONTROL_PUBLIC_ORIGIN": "https://control.example",
        "CONTROL_OPERATOR_TOKEN": "operator-ABCDEF0123456789-" + ("o" * 20),
        "CONTROL_SESSION_SECRET": "session-GHIJKL9876543210-" + ("s" * 20),
        "CONTROL_CSRF_SECRET": "csrf-MNOPQR1357902468-" + ("c" * 20),
        "CONTROL_SIGNING_PRIVATE_KEY_B64": private_key_to_base64url(control),
        "CONTROL_SIGNING_KEY_ID": str(uuid4()),
        "CONTROL_RUNNER_PUBLIC_KEY_B64": public_key_to_base64url(runner.public_key()),
        "CONTROL_RUNNER_DEVICE_ID": str(uuid4()),
    }
    _refresh_identity_digest(env)
    return env


def test_production_settings_require_tls_distinct_secrets_and_exact_origin() -> None:
    env = _production_env()
    env["VERCEL_URL"] = "job-agent-staged-a1b2c3.vercel.app"
    settings = Settings.from_env(env)
    assert settings.dispatch_allowed is True
    assert settings.public_origin == "https://control.example"
    assert settings.trusted_origins == (
        "https://control.example",
        "https://job-agent-staged-a1b2c3.vercel.app",
    )
    assert settings.operator_origins == ("https://control.example",)

    for unsafe_url in (
        "sqlite:///control.db",
        "postgresql://db/control",
        "postgresql://db/control?sslmode=disable",
        "postgresql:///control?sslmode=require",
    ):
        bad = dict(env, CONTROL_DATABASE_URL=unsafe_url)
        with pytest.raises(ConfigurationError):
            Settings.from_env(bad)

    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(env, CONTROL_SESSION_SECRET=env["CONTROL_CSRF_SECRET"]))
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(env, CONTROL_OPERATOR_TOKEN="changeme-" + ("x" * 40)))
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(env, CONTROL_OPERATOR_TOKEN="x" * 40))
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(env, CONTROL_PUBLIC_ORIGIN="https://control.example/path"))
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(env, CONTROL_PUBLIC_ORIGIN="https://user@control.example"))
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(env, CONTROL_PUBLIC_ORIGIN="https://control.example:bad"))
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(env, CONTROL_PUBLIC_ORIGIN="https://control.example:8443"))
    assert "operator-" not in repr(settings)
    assert "postgresql" not in repr(settings)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_common_postgres_urls_select_bundled_psycopg3_driver(scheme: str) -> None:
    env = _production_env()
    env["CONTROL_DATABASE_URL"] = f"{scheme}://user:password@db.example/control?sslmode=verify-full"

    settings = Settings.from_env(env)

    assert settings.database_url == (
        "postgresql+psycopg://user:password@db.example/control?sslmode=verify-full"
    )


def test_unbundled_psycopg2_url_is_rejected() -> None:
    env = _production_env()
    env["CONTROL_DATABASE_URL"] = (
        "postgresql+psycopg2://user:password@db.example/control?sslmode=verify-full"
    )

    with pytest.raises(ConfigurationError, match="production database"):
        Settings.from_env(env)


def test_preview_uses_vercel_host_and_cannot_dispatch() -> None:
    env = _production_env()
    env.update(
        {
            "VERCEL_ENV": "preview",
            "VERCEL_URL": "job-agent-git-safe-preview.vercel.app",
        }
    )
    env.pop("CONTROL_PUBLIC_ORIGIN")
    _refresh_identity_digest(env)
    settings = Settings.from_env(env)
    assert settings.public_origin == "https://job-agent-git-safe-preview.vercel.app"
    assert settings.dispatch_allowed is False

    for invalid in (
        "https://preview.vercel.app",
        "preview.vercel.app/path",
        "preview.example.com",
        "u@host",
    ):
        with pytest.raises(ConfigurationError):
            Settings.from_env(dict(env, VERCEL_URL=invalid))


def test_vercel_runtime_cannot_fall_back_to_development_security() -> None:
    env = _production_env()
    env.pop("APP_ENV")
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(_production_env(), APP_ENV="development"))


def test_vercel_identity_attestation_rejects_partial_or_cross_target_updates() -> None:
    env = _production_env()
    Settings.from_env(env)

    with pytest.raises(ConfigurationError, match="incomplete or mixed"):
        Settings.from_env(
            dict(
                env,
                CONTROL_SESSION_SECRET="new-session-ABCDEF0123456789-" + ("n" * 24),
            )
        )
    with pytest.raises(ConfigurationError, match="target"):
        Settings.from_env(dict(env, VERCEL_PROJECT_ID="prj_deadbeef12345678"))
    with pytest.raises(ConfigurationError, match="target"):
        Settings.from_env(dict(env, VERCEL_ENV="preview", VERCEL_URL="safe.vercel.app"))
    with pytest.raises(ConfigurationError, match="CONTROL_IDENTITY_BUNDLE_DIGEST"):
        Settings.from_env(
            {key: value for key, value in env.items() if key != "CONTROL_IDENTITY_BUNDLE_DIGEST"}
        )


def test_ed25519_envelope_verifies_purpose_audience_time_and_signature() -> None:
    private_key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    unsigned = HeartbeatEnvelope(
        key_id=uuid4(),
        audience=CONTROL_AUDIENCE,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
        nonce=uuid4(),
        payload=HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="a" * 40,
            status=RunnerStatus.READY,
        ),
    )
    envelope = HeartbeatEnvelope.model_validate(sign_envelope(unsigned, private_key).model_dump())
    verify_envelope(
        envelope,
        private_key.public_key(),
        expected_purpose=EnvelopePurpose.RUNNER_HEARTBEAT,
        expected_audience=CONTROL_AUDIENCE,
        now=now,
    )

    with pytest.raises(ProtocolVerificationError):
        verify_envelope(
            envelope,
            private_key.public_key(),
            expected_purpose=EnvelopePurpose.RUNNER_EVENT,
            expected_audience=CONTROL_AUDIENCE,
            now=now,
        )
    with pytest.raises(ProtocolVerificationError):
        verify_envelope(
            envelope,
            private_key.public_key(),
            expected_purpose=EnvelopePurpose.RUNNER_HEARTBEAT,
            expected_audience="wrong-audience",
            now=now,
        )
    with pytest.raises(ProtocolVerificationError):
        verify_envelope(
            envelope,
            private_key.public_key(),
            expected_purpose=EnvelopePurpose.RUNNER_HEARTBEAT,
            expected_audience=CONTROL_AUDIENCE,
            now=now + timedelta(minutes=6),
        )
    with pytest.raises(ProtocolVerificationError):
        verify_envelope(
            envelope,
            private_key.public_key(),
            expected_purpose=EnvelopePurpose.RUNNER_HEARTBEAT,
            expected_audience=CONTROL_AUDIENCE,
            now=envelope.expires_at,
        )
    with pytest.raises(ProtocolVerificationError):
        verify_envelope(
            envelope.model_copy(update={"audience": "tampered"}),
            private_key.public_key(),
            expected_purpose=EnvelopePurpose.RUNNER_HEARTBEAT,
            expected_audience="tampered",
            now=now,
        )


def test_protocol_models_forbid_extra_or_unverified_success() -> None:
    with pytest.raises(ValidationError):
        HeartbeatPayload.model_validate(
            {
                "boot_id": str(uuid4()),
                "release_digest": "a" * 40,
                "status": "ready",
                "job_url": "https://forbidden.example",
            }
        )
    with pytest.raises(ValidationError):
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=uuid4(),
            stage=AttemptStage.FINISHED,
            outcome=AttemptOutcome.CONFIRMED_SUBMITTED,
            occurred_at=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=uuid4(),
            sequence=1,
            stage=AttemptStage.FINISHED,
            outcome=AttemptOutcome.CONFIRMED_SUBMITTED,
            reason_code="FINAL_ACTION_UNCONFIRMED",
            evidence_type="ats_visible_confirmation",
            evidence_digest="e" * 64,
            occurred_at=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        RunnerEventPayload(
            event_id=uuid4(),
            command_id=uuid4(),
            stage=AttemptStage.INSPECTING,
            outcome=AttemptOutcome.NEEDS_REVIEW,
            occurred_at=datetime.now(UTC),
        )
