"""Private outbound-polling runner for the hosted redacted control plane."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.config import (
    JOB_AGENT_ENV_FILE,
    Settings,
    get_settings,
)
from core.control_plane_review_permits import (
    ControlPlaneReviewGrantError,
    ReviewGrantProjection,
    ReviewGrantRevocationProjection,
    claim_review_grant_projection,
    claim_review_grant_revocation,
    consume_control_plane_review_grant,
    load_claimed_review_grant_projection,
    load_claimed_review_grant_revocation,
    mark_review_grant_projected,
    mark_review_grant_revocation_delivered,
    release_review_grant_projection,
    release_review_grant_revocation,
    validate_control_plane_review_grant,
)
from core.operations import readiness_report
from core.runtime_identity import build_runtime_capabilities, get_runtime_identity
from core.submission_service import (
    ClientReleaseIdentity,
    DescriptorResolver,
    SessionChecker,
    SubmissionCommandRequest,
    create_submission_commands,
)
from db.models import (
    ControlPlaneCommandReceipt,
    SubmissionCommand,
)
from db.session import (
    create_engine_for_settings,
    create_session_factory_for_engine,
    get_session_factory,
)
from submitters.platforms import adapter_for_url
from worker.control_plane_client import ControlPlaneClient, ControlPlaneClientConfig
from worker.control_plane_event_outbox import (
    RedactedControlPlaneEvent,
    claim_control_plane_event,
    deliver_claimed_control_plane_event,
    enqueue_control_plane_event,
    transition_event_ref,
)

HEARTBEAT_INTERVAL_SECONDS = 10
RUNNER_OFFLINE_AFTER_SECONDS = 30
MAX_COMMAND_LIFETIME_SECONDS = 300
MAX_REVOCATIONS_PER_CYCLE = 25
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ControlPlaneRunnerError(RuntimeError):
    """A stable runner rejection that never echoes envelope or private data."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _aware(value).replace(tzinfo=None)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _resolve_clock(
    *,
    now: datetime | None,
    clock: Callable[[], datetime] | None,
) -> Callable[[], datetime]:
    if now is not None and clock is not None:
        raise ControlPlaneRunnerError("CONTROL_CLOCK_INVALID")
    if now is not None:
        return lambda: now
    return clock or _utc_now


def _uuid(value: object, reason: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ControlPlaneRunnerError(reason) from exc
    if str(parsed) != str(value):
        raise ControlPlaneRunnerError(reason)
    return str(parsed)


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedControlCommand:
    """Canonical verified command normalized for the local admission boundary."""

    command_id: str
    grant_id: str
    application_ref: str
    application_revision: int
    adapter: str
    adapter_version: str
    form_fingerprint_digest: str
    delivery_nonce: str
    issued_at: datetime
    expires_at: datetime
    envelope_digest: str

    def __post_init__(self) -> None:
        _uuid(self.command_id, "CONTROL_COMMAND_ID_INVALID")
        _uuid(self.grant_id, "CONTROL_GRANT_ID_INVALID")
        _uuid(self.application_ref, "CONTROL_APPLICATION_REF_INVALID")
        _uuid(self.delivery_nonce, "CONTROL_NONCE_INVALID")
        if self.application_revision < 1:
            raise ControlPlaneRunnerError("CONTROL_REVISION_INVALID")
        if not _SAFE_TOKEN_RE.fullmatch(self.adapter) or not _SAFE_TOKEN_RE.fullmatch(
            self.adapter_version
        ):
            raise ControlPlaneRunnerError("CONTROL_ADAPTER_INVALID")
        if not _SHA256_RE.fullmatch(self.form_fingerprint_digest):
            raise ControlPlaneRunnerError("CONTROL_FINGERPRINT_INVALID")
        if not _SHA256_RE.fullmatch(self.envelope_digest):
            raise ControlPlaneRunnerError("CONTROL_ENVELOPE_DIGEST_INVALID")
        issued_at = _aware(self.issued_at)
        expires_at = _aware(self.expires_at)
        if expires_at <= issued_at or expires_at - issued_at > timedelta(
            seconds=MAX_COMMAND_LIFETIME_SECONDS
        ):
            raise ControlPlaneRunnerError("CONTROL_COMMAND_LIFETIME_INVALID")

    def __repr__(self) -> str:
        return (
            "VerifiedControlCommand("
            f"command_id={self.command_id!r}, grant_id={self.grant_id!r}, "
            f"adapter={self.adapter!r}, adapter_version={self.adapter_version!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AcceptedControlCommand:
    remote_command_ref: str
    remote_attempt_ref: str
    application_id: int
    attempt_id: int
    submission_command_id: int
    replayed: bool

    def __repr__(self) -> str:
        return (
            "AcceptedControlCommand("
            f"remote_command_ref={self.remote_command_ref!r}, "
            f"remote_attempt_ref={self.remote_attempt_ref!r}, "
            f"attempt_id={self.attempt_id}, replayed={self.replayed})"
        )


def _capabilities(
    settings: Settings,
    *,
    engine=None,
) -> Mapping[str, object]:
    return build_runtime_capabilities(
        settings,
        readiness_report(settings, engine=engine)
        if engine is not None
        else readiness_report(settings),
    )


def _client_release(capabilities: Mapping[str, object]) -> ClientReleaseIdentity:
    release = capabilities.get("release")
    if not isinstance(release, Mapping):
        raise ControlPlaneRunnerError("RUNTIME_NOT_READY")
    values = {
        field: str(release.get(field) or "")
        for field in (
            "build_sha",
            "ui_asset_digest",
            "source_digest",
            "protocol_version",
            "boot_id",
        )
    }
    if any(not value for value in values.values()):
        raise ControlPlaneRunnerError("RUNTIME_NOT_READY")
    return ClientReleaseIdentity(**values)


def _runner_release(capabilities: Mapping[str, object]) -> str:
    release = capabilities.get("release")
    value = str(release.get("release_id") or "") if isinstance(release, Mapping) else ""
    if not _SHA256_RE.fullmatch(value):
        raise ControlPlaneRunnerError("RUNTIME_NOT_READY")
    return value


def _find_command_replay(
    db,
    command: VerifiedControlCommand,
) -> AcceptedControlCommand | None:
    receipt = (
        db.query(ControlPlaneCommandReceipt)
        .filter(ControlPlaneCommandReceipt.remote_command_ref == command.command_id)
        .one_or_none()
    )
    if receipt is None:
        return None
    if not hmac.compare_digest(receipt.envelope_digest, command.envelope_digest):
        raise ControlPlaneRunnerError("CONTROL_COMMAND_CONFLICT")
    submission_command = (
        db.query(SubmissionCommand)
        .filter(SubmissionCommand.idempotency_key == receipt.client_idempotency_key)
        .one_or_none()
    )
    if submission_command is None:
        raise ControlPlaneRunnerError("CONTROL_RECEIPT_INCONSISTENT")
    attempt = submission_command.attempt
    if attempt.application_id != receipt.review_grant.application_id:
        raise ControlPlaneRunnerError("CONTROL_RECEIPT_INCONSISTENT")
    return AcceptedControlCommand(
        remote_command_ref=receipt.remote_command_ref,
        remote_attempt_ref=receipt.remote_attempt_ref,
        application_id=attempt.application_id,
        attempt_id=attempt.id,
        submission_command_id=submission_command.id,
        replayed=True,
    )


def accept_control_plane_command(
    db,
    command: VerifiedControlCommand,
    *,
    settings: Settings | None = None,
    capabilities: Mapping[str, object] | None = None,
    descriptor_resolver: DescriptorResolver = adapter_for_url,
    session_checker: SessionChecker | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AcceptedControlCommand:
    """Atomically consume local authority using a fixed test time or live clock."""

    read_clock = _resolve_clock(now=now, clock=clock)
    timestamp = _aware(read_clock())
    protocol_clock_skew = _crypto_module().MAX_CLOCK_SKEW
    if command.issued_at > timestamp + protocol_clock_skew or command.expires_at <= timestamp:
        raise ControlPlaneRunnerError("CONTROL_COMMAND_EXPIRED")
    replay = _find_command_replay(db, command)
    if replay is not None:
        return replay

    resolved_settings = settings or get_settings()
    resolved_capabilities = capabilities or _capabilities(resolved_settings)
    runner_release = _runner_release(resolved_capabilities)
    # Readiness/model checks can be slow. Refresh the wall clock before
    # consuming any remote authority so a near-expiry command cannot cross its
    # deadline while those checks run.
    timestamp = _aware(read_clock())
    if command.expires_at <= timestamp:
        raise ControlPlaneRunnerError("CONTROL_COMMAND_EXPIRED")
    try:
        grant = validate_control_plane_review_grant(
            db,
            review_grant_ref=command.grant_id,
            remote_application_ref=command.application_ref,
            runner_release=runner_release,
            now=timestamp,
        )
    except ControlPlaneReviewGrantError as exc:
        # A concurrent duplicate may have waited for the first transaction's
        # grant lock. Re-read the atomic receipt before reporting a replay.
        if exc.reason_code == "REVIEW_GRANT_REPLAYED":
            replay = _find_command_replay(db, command)
            if replay is not None:
                return replay
        raise ControlPlaneRunnerError(exc.reason_code) from exc

    bindings = (
        (grant.grant_ref, command.grant_id, "CONTROL_GRANT_CHANGED"),
        (
            grant.application_ref.remote_ref,
            command.application_ref,
            "CONTROL_APPLICATION_CHANGED",
        ),
        (
            grant.application_revision,
            command.application_revision,
            "APPLICATION_REVISION_CHANGED",
        ),
        (grant.adapter_name, command.adapter, "ADAPTER_VERSION_CHANGED"),
        (grant.adapter_version, command.adapter_version, "ADAPTER_VERSION_CHANGED"),
        (
            grant.form_plan_fingerprint,
            command.form_fingerprint_digest,
            "FORM_CHANGED",
        ),
    )
    for expected, observed, reason_code in bindings:
        if not hmac.compare_digest(str(expected), str(observed)):
            raise ControlPlaneRunnerError(reason_code)
    authority_expires_at = min(
        _aware(command.expires_at),
        _aware(grant.expires_at),
    )
    # Grant validation and database locking may themselves take time. The
    # irreversible authority consumption below uses one last exact deadline
    # check rather than the earlier validation timestamp.
    timestamp = _aware(read_clock())
    if authority_expires_at <= timestamp:
        raise ControlPlaneRunnerError("CONTROL_COMMAND_EXPIRED")

    client_idempotency_key = f"control-plane:{command.command_id}"
    remote_attempt_ref = str(uuid4())
    receipt = ControlPlaneCommandReceipt(
        remote_command_ref=command.command_id,
        remote_attempt_ref=remote_attempt_ref,
        review_grant_id=grant.id,
        delivery_nonce_hash=hashlib.sha256(command.delivery_nonce.encode("ascii")).hexdigest(),
        envelope_digest=command.envelope_digest,
        client_idempotency_key=client_idempotency_key,
        accepted_at=_naive(timestamp),
    )
    db.add(receipt)
    consume_control_plane_review_grant(
        grant,
        remote_command_ref=command.command_id,
        now=timestamp,
    )
    enqueue_control_plane_event(
        db,
        RedactedControlPlaneEvent(
            event_id=transition_event_ref(command.command_id, 1),
            command_id=command.command_id,
            sequence=1,
            stage="queued",
            occurred_at=timestamp,
        ),
    )
    request = SubmissionCommandRequest(
        application_id=grant.application_id,
        client_idempotency_key=client_idempotency_key,
        application_revision=grant.application_revision,
        form_plan_id=grant.form_plan.plan_id,
        client_release=_client_release(resolved_capabilities),
        authority_expires_at=authority_expires_at,
    )
    kwargs: dict[str, Any] = {
        "settings": resolved_settings,
        "capabilities": resolved_capabilities,
        "descriptor_resolver": descriptor_resolver,
        "now": _naive(timestamp),
    }
    if session_checker is not None:
        kwargs["session_checker"] = session_checker
    try:
        [created] = create_submission_commands(db, [request], **kwargs)
    except Exception:
        # create_submission_commands rolls back the complete session. Explicitly
        # repeat this invariant in case its implementation changes.
        db.rollback()
        raise
    return AcceptedControlCommand(
        remote_command_ref=command.command_id,
        remote_attempt_ref=remote_attempt_ref,
        application_id=created.application_id,
        attempt_id=created.attempt_id,
        submission_command_id=created.command_id,
        replayed=created.replayed,
    )


def wake_control_plane_submission_command(accepted: AcceptedControlCommand) -> bool:
    """Best-effort post-commit wake; durable database state stays authoritative."""

    if accepted.replayed:
        return False
    try:
        from worker.submission_commands import execute_submission_command_task

        execute_submission_command_task.delay(accepted.submission_command_id)
    except Exception:
        return False
    return True


@dataclass(frozen=True, slots=True, repr=False)
class RunnerConfig:
    """Only paths, public identifiers, URLs, and bounded timing values."""

    control_plane_url: str
    device_id: str
    control_signing_key_id: str
    control_plane_audience: str
    private_key_path: Path
    control_plane_public_key_path: Path
    runtime_env_path: Path | None = None
    poll_interval_seconds: float = 10.0
    heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS
    offline_after_seconds: int = RUNNER_OFFLINE_AFTER_SECONDS

    def __post_init__(self) -> None:
        _uuid(self.device_id, "RUNNER_DEVICE_ID_INVALID")
        _uuid(self.control_signing_key_id, "CONTROL_SIGNING_KEY_ID_INVALID")
        if hmac.compare_digest(self.device_id, self.control_signing_key_id):
            raise ControlPlaneRunnerError("CONTROL_SIGNING_KEY_ID_INVALID")
        if not _SAFE_TOKEN_RE.fullmatch(self.control_plane_audience):
            raise ControlPlaneRunnerError("CONTROL_PLANE_AUDIENCE_INVALID")
        for path in (self.private_key_path, self.control_plane_public_key_path):
            if not path.is_absolute():
                raise ControlPlaneRunnerError("RUNNER_KEY_PATH_NOT_ABSOLUTE")
        project_root = Path(__file__).resolve().parents[1]
        if self.private_key_path.resolve().is_relative_to(project_root):
            raise ControlPlaneRunnerError("RUNNER_PRIVATE_KEY_PATH_NOT_EXTERNAL")
        if self.runtime_env_path is not None:
            if not self.runtime_env_path.is_absolute():
                raise ControlPlaneRunnerError("RUNNER_ENV_PATH_NOT_ABSOLUTE")
            if self.runtime_env_path.resolve().is_relative_to(project_root):
                raise ControlPlaneRunnerError("RUNNER_ENV_PATH_NOT_EXTERNAL")
        if not 5 <= self.poll_interval_seconds <= HEARTBEAT_INTERVAL_SECONDS:
            raise ControlPlaneRunnerError("RUNNER_POLL_INTERVAL_INVALID")
        if self.heartbeat_interval_seconds != HEARTBEAT_INTERVAL_SECONDS:
            raise ControlPlaneRunnerError("RUNNER_HEARTBEAT_INTERVAL_INVALID")
        if self.offline_after_seconds != RUNNER_OFFLINE_AFTER_SECONDS:
            raise ControlPlaneRunnerError("RUNNER_OFFLINE_THRESHOLD_INVALID")
        ControlPlaneClientConfig(self.control_plane_url)

    def __repr__(self) -> str:
        return (
            "RunnerConfig("
            f"control_plane_url={self.control_plane_url!r}, "
            f"device_id={self.device_id!r})"
        )


def _validated_runtime_env(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        env_size = path.stat().st_size
    except OSError as exc:
        raise ControlPlaneRunnerError("RUNNER_ENV_UNAVAILABLE") from exc
    if not path.is_file() or not 0 < env_size <= 64 * 1024:
        raise ControlPlaneRunnerError("RUNNER_ENV_INVALID")
    return path


def activate_runner_runtime(path: Path | None) -> Settings:
    """Activate one validated, authoritative settings source for this process."""

    runtime_env_path = _validated_runtime_env(path)
    if runtime_env_path is None:
        raise ControlPlaneRunnerError("RUNNER_ENV_REQUIRED")
    previous_selector = os.environ.get(JOB_AGENT_ENV_FILE)
    try:
        os.environ[JOB_AGENT_ENV_FILE] = str(runtime_env_path)
        get_settings.cache_clear()
        settings = get_settings()
        settings.validate_runtime()
    except (OSError, ValueError):
        if previous_selector is None:
            os.environ.pop(JOB_AGENT_ENV_FILE, None)
        else:
            os.environ[JOB_AGENT_ENV_FILE] = previous_selector
        get_settings.cache_clear()
        # Do not include validation input or file contents in runner output.
        raise ControlPlaneRunnerError("RUNNER_ENV_UNSAFE") from None

    # The validated instance remains cached for every subsequent global
    # settings consumer in this process; later file edits cannot split the
    # runner's engine/session/readiness configuration.
    return settings


def load_runner_config(path: Path) -> RunnerConfig:
    """Load an absolute non-secret JSON config; key bytes stay in external files."""

    if not path.is_absolute():
        raise ControlPlaneRunnerError("RUNNER_CONFIG_PATH_NOT_ABSOLUTE")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ControlPlaneRunnerError("RUNNER_CONFIG_UNAVAILABLE") from exc
    if len(raw) > 16 * 1024:
        raise ControlPlaneRunnerError("RUNNER_CONFIG_INVALID")
    try:
        values = json.loads(
            raw,
            object_pairs_hook=lambda pairs: _reject_duplicate_keys(pairs),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ControlPlaneRunnerError("RUNNER_CONFIG_INVALID") from exc
    if not isinstance(values, dict):
        raise ControlPlaneRunnerError("RUNNER_CONFIG_INVALID")
    allowed = {
        "control_plane_url",
        "device_id",
        "control_signing_key_id",
        "control_plane_audience",
        "private_key_path",
        "control_plane_public_key_path",
        "runtime_env_path",
        "poll_interval_seconds",
        "heartbeat_interval_seconds",
        "offline_after_seconds",
    }
    if set(values) - allowed or not {
        "control_plane_url",
        "device_id",
        "control_signing_key_id",
        "control_plane_audience",
        "private_key_path",
        "control_plane_public_key_path",
        "runtime_env_path",
    }.issubset(values):
        raise ControlPlaneRunnerError("RUNNER_CONFIG_INVALID")
    forbidden = ("secret", "token", "password", "private_key_pem", "key_material")
    if any(any(marker in key.lower() for marker in forbidden) for key in values):
        # ``private_key_path`` is explicitly the one allowed non-secret pointer.
        unexpected = set(values).intersection(
            {"secret", "token", "password", "private_key_pem", "key_material"}
        )
        if unexpected:
            raise ControlPlaneRunnerError("RUNNER_CONFIG_CONTAINS_SECRET")
    try:
        config = RunnerConfig(
            control_plane_url=str(values["control_plane_url"]),
            device_id=str(values["device_id"]),
            control_signing_key_id=str(values["control_signing_key_id"]),
            control_plane_audience=str(values["control_plane_audience"]),
            private_key_path=Path(str(values["private_key_path"])),
            control_plane_public_key_path=Path(str(values["control_plane_public_key_path"])),
            runtime_env_path=(
                Path(str(values["runtime_env_path"]))
                if values.get("runtime_env_path") is not None
                else None
            ),
            poll_interval_seconds=float(values.get("poll_interval_seconds", 10.0)),
            heartbeat_interval_seconds=int(
                values.get("heartbeat_interval_seconds", HEARTBEAT_INTERVAL_SECONDS)
            ),
            offline_after_seconds=int(
                values.get("offline_after_seconds", RUNNER_OFFLINE_AFTER_SECONDS)
            ),
        )
        _validated_runtime_env(config.runtime_env_path)
        return config
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ControlPlaneRunnerError):
            raise
        raise ControlPlaneRunnerError("RUNNER_CONFIG_INVALID") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_private_path(path: Path) -> bytes:
    """Default key loader hook; callers may replace it with a DPAPI loader."""

    try:
        if not path.is_file():
            raise OSError
        raw = path.read_bytes()
    except OSError as exc:
        raise ControlPlaneRunnerError("RUNNER_KEY_UNAVAILABLE") from exc
    if not 32 <= len(raw) <= 16 * 1024:
        raise ControlPlaneRunnerError("RUNNER_KEY_INVALID")
    return raw


def _protocol_module():
    try:
        from control_plane.job_control_plane import protocol
    except ImportError as exc:
        raise ControlPlaneRunnerError("CONTROL_PROTOCOL_UNAVAILABLE") from exc
    return protocol


def _crypto_module():
    try:
        from control_plane.job_control_plane import crypto
    except ImportError as exc:
        raise ControlPlaneRunnerError("CONTROL_PROTOCOL_UNAVAILABLE") from exc
    return crypto


class ControlPlaneRunner:
    """Single private runner. It creates no inbound listener."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        client: ControlPlaneClient | None = None,
        key_loader: Callable[[Path], bytes] = _read_private_path,
        settings: Settings | None = None,
        session_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.config = config
        self.client = client or ControlPlaneClient(
            ControlPlaneClientConfig(config.control_plane_url)
        )
        self._owns_client = client is None
        if settings is None:
            self._settings = activate_runner_runtime(config.runtime_env_path)
        else:
            try:
                settings.validate_runtime()
            except ValueError:
                raise ControlPlaneRunnerError("RUNNER_ENV_UNSAFE") from None
            self._settings = settings
        try:
            self._database_engine = create_engine_for_settings(self._settings)
            self._session_factory = session_factory or create_session_factory_for_engine(
                self._database_engine
            )
        except Exception:
            # Driver/configuration failures are bounded and never echo a URL.
            raise ControlPlaneRunnerError("RUNNER_DATABASE_CONFIG_INVALID") from None
        self._clock = clock
        self._protocol = _protocol_module()
        self._crypto = _crypto_module()
        if config.control_plane_audience != self._protocol.CONTROL_AUDIENCE:
            raise ControlPlaneRunnerError("CONTROL_PLANE_AUDIENCE_INVALID")
        self._private_key = self._crypto.private_key_from_base64url(
            self._key_text(key_loader(config.private_key_path))
        )
        self._control_plane_public_key = self._crypto.public_key_from_base64url(
            self._key_text(key_loader(config.control_plane_public_key_path))
        )
        self._last_heartbeat: datetime | None = None
        self._stop = asyncio.Event()
        self._boot_id = str(uuid4())

    @staticmethod
    def _key_text(raw: bytes) -> str:
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ControlPlaneRunnerError("RUNNER_KEY_INVALID") from exc
        if not value or "\n" in value or "\r" in value:
            raise ControlPlaneRunnerError("RUNNER_KEY_INVALID")
        return value

    def stop(self) -> None:
        self._stop.set()

    def _open_database_session(self):
        factory = getattr(self, "_session_factory", None)
        if factory is None:
            # Compatibility for narrowly constructed test doubles. A real
            # runner always owns the factory created in ``__init__``.
            factory = get_session_factory()
        return factory()

    def _runtime_readiness(self) -> Mapping[str, object]:
        engine = getattr(self, "_database_engine", None)
        if engine is None:
            return readiness_report(self._settings)
        return readiness_report(self._settings, engine=engine)

    def _runtime_capabilities(self) -> Mapping[str, object]:
        engine = getattr(self, "_database_engine", None)
        if engine is None:
            return _capabilities(self._settings)
        return _capabilities(self._settings, engine=engine)

    def _signed_envelope(
        self,
        *,
        purpose: object,
        payload: Mapping[str, object],
        now: datetime,
        expires_at: datetime | None = None,
    ) -> Mapping[str, object]:
        envelope_classes = {
            self._protocol.EnvelopePurpose.RUNNER_HEARTBEAT: (self._protocol.HeartbeatEnvelope),
            self._protocol.EnvelopePurpose.RUNNER_REVIEW_GRANT: (
                self._protocol.ReviewGrantEnvelope
            ),
            self._protocol.EnvelopePurpose.RUNNER_REVIEW_GRANT_REVOCATION: (
                self._protocol.ReviewGrantRevocationEnvelope
            ),
            self._protocol.EnvelopePurpose.RUNNER_COMMAND_POLL: (
                self._protocol.CommandPollEnvelope
            ),
            self._protocol.EnvelopePurpose.RUNNER_COMMAND_ACK: (self._protocol.CommandAckEnvelope),
            self._protocol.EnvelopePurpose.RUNNER_EVENT: self._protocol.RunnerEventEnvelope,
        }
        envelope_class = envelope_classes.get(purpose)
        if envelope_class is None:
            raise ControlPlaneRunnerError("CONTROL_ENVELOPE_PURPOSE_INVALID")
        envelope_expiry = _aware(expires_at or (now + timedelta(seconds=60)))
        if envelope_expiry <= now or envelope_expiry - now > timedelta(
            seconds=MAX_COMMAND_LIFETIME_SECONDS
        ):
            raise ControlPlaneRunnerError("CONTROL_ENVELOPE_LIFETIME_INVALID")
        values = {
            "protocol_version": self._protocol.PROTOCOL_VERSION,
            "key_id": self.config.device_id,
            "purpose": str(purpose),
            "audience": self.config.control_plane_audience,
            "issued_at": now.isoformat(),
            "expires_at": envelope_expiry.isoformat(),
            "nonce": str(uuid4()),
            "payload": dict(payload),
        }
        try:
            encoded = json.dumps(
                values,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            unsigned = envelope_class.model_validate_json(encoded)
            signed = self._crypto.sign_envelope(unsigned, self._private_key)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneRunnerError("CONTROL_ENVELOPE_INVALID") from exc
        return signed.model_dump(mode="json")

    def _verify_command(
        self,
        envelope_data: Mapping[str, object],
        *,
        now: datetime,
    ) -> VerifiedControlCommand:
        try:
            encoded = json.dumps(
                dict(envelope_data),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            envelope = self._protocol.ControlCommandEnvelope.model_validate_json(encoded)
            if not hmac.compare_digest(
                str(envelope.key_id),
                self.config.control_signing_key_id,
            ):
                raise ControlPlaneRunnerError("CONTROL_SIGNING_KEY_ID_MISMATCH")
            self._crypto.verify_envelope(
                envelope,
                self._control_plane_public_key,
                expected_purpose=self._protocol.EnvelopePurpose.CONTROL_COMMAND,
                expected_audience=self._protocol.RUNNER_AUDIENCE,
                now=now,
            )
            canonical = self._protocol.canonical_envelope_bytes(envelope)
            payload = envelope.payload
            return VerifiedControlCommand(
                command_id=str(payload.command_id),
                grant_id=str(payload.grant_id),
                application_ref=str(payload.application_ref),
                application_revision=payload.application_revision,
                adapter=str(payload.adapter),
                adapter_version=str(payload.adapter_version),
                form_fingerprint_digest=str(payload.form_fingerprint_digest),
                delivery_nonce=str(envelope.nonce),
                issued_at=envelope.issued_at,
                expires_at=envelope.expires_at,
                envelope_digest=hashlib.sha256(canonical).hexdigest(),
            )
        except ControlPlaneRunnerError:
            raise
        except Exception as exc:
            raise ControlPlaneRunnerError("CONTROL_COMMAND_INVALID") from exc

    async def _heartbeat(self, now: datetime) -> None:
        identity = get_runtime_identity()
        readiness = self._runtime_readiness()
        status = "ready" if readiness.get("status") == "ready" else "degraded"
        payload = {
            "boot_id": self._boot_id,
            "release_digest": identity.release_id,
            "status": status,
        }
        envelope = self._signed_envelope(
            purpose=self._protocol.EnvelopePurpose.RUNNER_HEARTBEAT,
            payload=payload,
            now=now,
        )
        await self.client.send_heartbeat(envelope)
        self._last_heartbeat = now

    async def publish_review_grant(self, grant: ReviewGrantProjection) -> None:
        """Publish only the canonical redacted projection of a local grant."""

        now = datetime.now(UTC)
        envelope = self._signed_envelope(
            purpose=self._protocol.EnvelopePurpose.RUNNER_REVIEW_GRANT,
            payload=grant.to_wire(),
            now=now,
            expires_at=_aware(grant.expires_at),
        )
        await self.client.publish_review_grant(envelope)

    async def _publish_one_review_grant(self) -> bool:
        db = self._open_database_session()
        try:
            claim = claim_review_grant_projection(
                db,
                runner_id=self.config.device_id,
            )
        finally:
            db.close()
        if claim is None:
            return False
        grant_id, claim_token = claim
        db = self._open_database_session()
        try:
            projection = load_claimed_review_grant_projection(
                db,
                grant_id=grant_id,
                claim_token=claim_token,
                runner_release=get_runtime_identity().release_id,
            )
        except Exception:
            db.rollback()
            release_review_grant_projection(
                db,
                grant_id=grant_id,
                claim_token=claim_token,
            )
            raise
        finally:
            db.close()
        try:
            await self.publish_review_grant(projection)
        except Exception:
            db = self._open_database_session()
            try:
                release_review_grant_projection(
                    db,
                    grant_id=grant_id,
                    claim_token=claim_token,
                )
            finally:
                db.close()
            raise
        db = self._open_database_session()
        try:
            mark_review_grant_projected(
                db,
                grant_id=grant_id,
                claim_token=claim_token,
            )
        finally:
            db.close()
        return True

    async def publish_review_grant_revocation(
        self,
        revocation: ReviewGrantRevocationProjection,
    ) -> None:
        """Publish a signed redacted tombstone over the outbound-only client."""

        now = datetime.now(UTC)
        envelope = self._signed_envelope(
            purpose=self._protocol.EnvelopePurpose.RUNNER_REVIEW_GRANT_REVOCATION,
            payload=revocation.to_wire(),
            now=now,
            expires_at=min(
                _aware(revocation.grant_expires_at),
                now + timedelta(seconds=60),
            ),
        )
        await self.client.revoke_review_grant(envelope)

    async def _publish_one_review_grant_revocation(self) -> bool:
        db = self._open_database_session()
        try:
            claim = claim_review_grant_revocation(
                db,
                runner_id=self.config.device_id,
            )
        finally:
            db.close()
        if claim is None:
            return False
        grant_id, claim_token = claim
        db = self._open_database_session()
        try:
            revocation = load_claimed_review_grant_revocation(
                db,
                grant_id=grant_id,
                claim_token=claim_token,
            )
        except Exception:
            db.rollback()
            release_review_grant_revocation(
                db,
                grant_id=grant_id,
                claim_token=claim_token,
            )
            raise
        finally:
            db.close()
        try:
            await self.publish_review_grant_revocation(revocation)
        except Exception:
            db = self._open_database_session()
            try:
                release_review_grant_revocation(
                    db,
                    grant_id=grant_id,
                    claim_token=claim_token,
                )
            finally:
                db.close()
            raise
        db = self._open_database_session()
        try:
            mark_review_grant_revocation_delivered(
                db,
                grant_id=grant_id,
                claim_token=claim_token,
            )
        finally:
            db.close()
        return True

    async def _poll(self, now: datetime) -> Mapping[str, object] | None:
        envelope = self._signed_envelope(
            purpose=self._protocol.EnvelopePurpose.RUNNER_COMMAND_POLL,
            payload={"boot_id": self._boot_id, "max_commands": 1},
            now=now,
        )
        response = await self.client.poll_command(envelope)
        if response is None:
            return None
        commands = response.get("commands")
        if not isinstance(commands, list) or len(commands) > 1:
            raise ControlPlaneRunnerError("CONTROL_POLL_RESPONSE_INVALID")
        if not commands:
            return None
        if not isinstance(commands[0], Mapping):
            raise ControlPlaneRunnerError("CONTROL_POLL_RESPONSE_INVALID")
        return commands[0]

    def _admit(self, command: VerifiedControlCommand) -> AcceptedControlCommand:
        db = self._open_database_session()
        try:
            return accept_control_plane_command(
                db,
                command,
                settings=self._settings,
                capabilities=self._runtime_capabilities(),
                clock=self._clock,
            )
        finally:
            db.close()

    async def _ack(
        self,
        command_id: str,
        *,
        status: str,
        now: datetime,
    ) -> None:
        envelope = self._signed_envelope(
            purpose=self._protocol.EnvelopePurpose.RUNNER_COMMAND_ACK,
            payload={"command_id": command_id, "ack_status": status},
            now=now,
        )
        await self.client.acknowledge_command(command_id, envelope)

    def _event_signer(
        self,
        _purpose: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._signed_envelope(
            purpose=self._protocol.EnvelopePurpose.RUNNER_EVENT,
            payload=payload,
            now=datetime.now(UTC),
        )

    async def _drain_one_event(self) -> None:
        db = self._open_database_session()
        try:
            claimed = claim_control_plane_event(
                db,
                runner_id=self.config.device_id,
            )
            if claimed is None:
                return
            row_id, claim_token = claimed
            await deliver_claimed_control_plane_event(
                db,
                row_id=row_id,
                claim_token=claim_token,
                signer=self._event_signer,
                sender=self.client.send_event,
            )
        finally:
            db.close()

    async def run_once(self) -> str:
        now = datetime.now(UTC)
        if (
            self._last_heartbeat is None
            or (now - self._last_heartbeat).total_seconds()
            >= self.config.heartbeat_interval_seconds
        ):
            await self._heartbeat(now)
        revocations_sent = 0
        while (
            revocations_sent < MAX_REVOCATIONS_PER_CYCLE
            and await self._publish_one_review_grant_revocation()
        ):
            revocations_sent += 1
        if revocations_sent == MAX_REVOCATIONS_PER_CYCLE:
            return "revocations_draining"
        await self._publish_one_review_grant()
        # Grant/revocation publication may involve several network round trips.
        # Sign the poll with a fresh timestamp so its one-minute envelope cannot
        # expire merely because earlier outbound maintenance was slow.
        poll_time = datetime.now(UTC)
        envelope = await self._poll(poll_time)
        if envelope is None:
            await self._drain_one_event()
            return "idle"
        verification_time = datetime.now(UTC)
        command = self._verify_command(envelope, now=verification_time)
        try:
            accepted = await asyncio.to_thread(self._admit, command)
        except Exception:
            await self._ack(command.command_id, status="rejected", now=datetime.now(UTC))
            raise
        wake_control_plane_submission_command(accepted)
        await self._ack(command.command_id, status="received", now=datetime.now(UTC))
        await self._drain_one_event()
        return "replayed" if accepted.replayed else "accepted"

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except Exception:
                    # Fail closed, remain outbound-only, and retry after the
                    # configured interval. Never print envelope/private data.
                    pass
                try:
                    now = datetime.now(UTC)
                    heartbeat_remaining = (
                        self.config.heartbeat_interval_seconds
                        if self._last_heartbeat is None
                        else self.config.heartbeat_interval_seconds
                        - (now - self._last_heartbeat).total_seconds()
                    )
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=max(
                            0.1,
                            min(
                                self.config.poll_interval_seconds,
                                heartbeat_remaining,
                            ),
                        ),
                    )
                except TimeoutError:
                    continue
        finally:
            if self._owns_client:
                await self.client.close()
            self._database_engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m worker.control_plane_runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "once", "status"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--config",
            required=True,
            type=Path,
            help="Absolute path to non-secret runner JSON configuration",
        )
    return parser


async def _run_cli(command: str, config_path: Path) -> int:
    config = load_runner_config(config_path)
    if command == "status":
        # Status validates configuration and external key-file availability
        # without exposing key bytes or contacting the control plane.
        activate_runner_runtime(config.runtime_env_path)
        _read_private_path(config.private_key_path)
        _read_private_path(config.control_plane_public_key_path)
        print("configured")  # noqa: T201 - intentional bounded CLI output
        return 0
    runner = ControlPlaneRunner(config)
    if command == "once":
        try:
            result = await runner.run_once()
        finally:
            if runner._owns_client:
                await runner.client.close()
            runner._database_engine.dispose()
        print(result)  # noqa: T201 - intentional bounded CLI output
        return 0

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, runner.stop)
        except (NotImplementedError, RuntimeError):
            # Windows Proactor loops do not implement signal handlers.
            pass
    await runner.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.config.is_absolute():
        raise ControlPlaneRunnerError("RUNNER_CONFIG_PATH_NOT_ABSOLUTE")
    if any(marker in " ".join(sys.argv).lower() for marker in ("private-key=", "secret=")):
        raise ControlPlaneRunnerError("SECRET_IN_CLI_ARGUMENTS")
    return asyncio.run(_run_cli(args.command, args.config))


if __name__ == "__main__":
    raise SystemExit(main())
