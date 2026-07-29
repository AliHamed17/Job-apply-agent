"""Fail-closed configuration for the isolated control plane."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from .crypto import (
    private_key_from_base64url,
    public_key_from_base64url,
    public_key_to_base64url,
)


class ConfigurationError(RuntimeError):
    """Raised when the control plane cannot start safely."""


_IDENTITY_ATTESTATION_CONTEXT = b"JobApplyAgent/control-identity-bundle/v2\0"
_IDENTITY_VALUE_NAMES = (
    "CONTROL_OPERATOR_TOKEN",
    "CONTROL_SESSION_SECRET",
    "CONTROL_CSRF_SECRET",
    "CONTROL_SIGNING_PRIVATE_KEY_B64",
    "CONTROL_SIGNING_KEY_ID",
    "CONTROL_RUNNER_PUBLIC_KEY_B64",
    "CONTROL_RUNNER_DEVICE_ID",
)
_VERCEL_PROJECT_ID_PATTERN = re.compile(r"prj_[A-Za-z0-9]{8,120}")
_VERCEL_SCOPE_ID_PATTERN = re.compile(r"team_[A-Za-z0-9]{8,120}")


@dataclass(frozen=True, slots=True)
class VercelIdentityTarget:
    """Expected Vercel deployment identity retained from the schema-v2 digest."""

    environment: str
    project_id: str
    scope_id: str


def _exact_vercel_id(value: str, *, pattern: re.Pattern[str], name: str) -> str:
    if not pattern.fullmatch(value):
        raise ConfigurationError(f"{name} must be an exact Vercel ID")
    return value


def build_identity_bundle_digest(
    values: Mapping[str, str],
    *,
    version_id: UUID,
    environment: str,
    project_id: str,
    scope_id: str,
) -> str:
    """Build the canonical all-or-nothing identity/target attestation."""

    if environment not in {"production", "preview"}:
        raise ConfigurationError("identity target environment is invalid")
    exact_project = _exact_vercel_id(
        project_id,
        pattern=_VERCEL_PROJECT_ID_PATTERN,
        name="identity project",
    )
    exact_scope = _exact_vercel_id(
        scope_id,
        pattern=_VERCEL_SCOPE_ID_PATTERN,
        name="identity scope",
    )
    identity_values: dict[str, str] = {}
    for name in _IDENTITY_VALUE_NAMES:
        value = values.get(name)
        if not isinstance(value, str) or not value:
            raise ConfigurationError(f"{name} is required for identity attestation")
        identity_values[name] = value
    canonical = json.dumps(
        {
            "identity": identity_values,
            "schema_version": 2,
            "target": {
                "environment": environment,
                "project_id": exact_project,
                "scope_id": exact_scope,
            },
            "version_id": str(version_id),
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    digest = hashlib.sha256(_IDENTITY_ATTESTATION_CONTEXT + canonical).hexdigest()
    return f"v2:{version_id}:{environment}:{exact_project}:{exact_scope}:{digest}"


def _verify_identity_bundle_digest(
    values: Mapping[str, str],
) -> tuple[str, VercelIdentityTarget]:
    supplied = _required(values, "CONTROL_IDENTITY_BUNDLE_DIGEST")
    parts = supplied.split(":")
    if len(parts) != 6 or parts[0] != "v2":
        raise ConfigurationError("CONTROL_IDENTITY_BUNDLE_DIGEST is invalid")
    _, raw_version, environment, project_id, scope_id, raw_digest = parts
    try:
        version_id = UUID(raw_version)
    except ValueError as exc:
        raise ConfigurationError("CONTROL_IDENTITY_BUNDLE_DIGEST is invalid") from exc
    if str(version_id) != raw_version or not re.fullmatch(r"[0-9a-f]{64}", raw_digest):
        raise ConfigurationError("CONTROL_IDENTITY_BUNDLE_DIGEST is invalid")
    runtime_environment = _required(values, "VERCEL_ENV").lower()
    runtime_project_id = _required(values, "VERCEL_PROJECT_ID")
    if environment != runtime_environment or project_id != runtime_project_id:
        raise ConfigurationError("control identity target does not match this deployment")
    expected = build_identity_bundle_digest(
        values,
        version_id=version_id,
        environment=environment,
        project_id=project_id,
        scope_id=scope_id,
    )
    if not secrets.compare_digest(expected, supplied):
        raise ConfigurationError("control identity bundle is incomplete or mixed")
    return supplied, VercelIdentityTarget(
        environment=environment,
        project_id=project_id,
        scope_id=scope_id,
    )


def normalize_database_url(database_url: str) -> str:
    """Select the bundled psycopg v3 driver for common PostgreSQL URLs."""

    parsed = urlsplit(database_url)
    if parsed.scheme in {"postgres", "postgresql"}:
        return parsed._replace(scheme="postgresql+psycopg").geturl()
    return database_url


def _vercel_deployment_origin(values: Mapping[str, str], *, required: bool) -> str | None:
    vercel_url = values.get("VERCEL_URL", "").strip()
    if not vercel_url:
        if required:
            raise ConfigurationError("VERCEL_URL is required")
        return None
    if (
        "://" in vercel_url
        or any(marker in vercel_url for marker in ("/", "@", "?", "#"))
        or not vercel_url.lower().endswith(".vercel.app")
    ):
        raise ConfigurationError("VERCEL_URL must be a bare deployment hostname")
    return f"https://{vercel_url}"


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _strong_secret(value: str, name: str) -> str:
    if len(value.encode("utf-8")) < 32:
        raise ConfigurationError(f"{name} must contain at least 32 bytes")
    lowered = value.lower()
    if any(marker in lowered for marker in ("changeme", "placeholder", "default-secret")):
        raise ConfigurationError(f"{name} contains a forbidden placeholder")
    if len(set(value)) < 12:
        raise ConfigurationError(f"{name} does not have enough character diversity")
    return value


def _validate_postgres_tls(database_url: str) -> None:
    parsed = urlsplit(database_url)
    if parsed.scheme != "postgresql+psycopg":
        raise ConfigurationError("production database must be PostgreSQL")
    if not parsed.hostname:
        raise ConfigurationError("production PostgreSQL must use a network host")
    query = parse_qs(parsed.query)
    sslmode = query.get("sslmode", [""])[-1].lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise ConfigurationError("production PostgreSQL must require TLS via sslmode")


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    app_env: str
    vercel_env: str
    database_url: str
    public_origin: str
    operator_token: str
    session_secret: str
    csrf_secret: str
    control_signing_private_key: str
    control_signing_key_id: UUID
    runner_device_id: UUID
    runner_verify_public_key: str
    identity_bundle_digest: str | None = None
    vercel_identity_target: VercelIdentityTarget | None = None
    deployment_origin: str | None = None
    session_ttl_seconds: int = 3_600
    runner_offline_seconds: int = 30
    docs_enabled: bool = False
    secure_cookies: bool = True
    test_dispatch_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_url", normalize_database_url(self.database_url))
        if self.vercel_env not in {"", "development", "production", "preview"}:
            raise ConfigurationError("VERCEL_ENV is invalid")
        if self.app_env == "production" and self.vercel_env not in {
            "production",
            "preview",
        }:
            raise ConfigurationError(
                "production control plane requires VERCEL_ENV=production or preview"
            )
        if self.vercel_env in {"production", "preview"} and self.app_env != "production":
            raise ConfigurationError("Vercel production and preview require APP_ENV=production")
        if self.vercel_env in {"production", "preview"}:
            target = self.vercel_identity_target
            if (
                target is None
                or target.environment != self.vercel_env
                or self.identity_bundle_digest is None
            ):
                raise ConfigurationError("Vercel runtime identity target is required")
        if self.app_env == "production" and not self.secure_cookies:
            raise ConfigurationError("production sessions require secure cookies")
        if self.app_env == "production":
            _validate_postgres_tls(self.database_url)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def dispatch_allowed(self) -> bool:
        """Preview deployments can never dispatch a command."""

        return (self.app_env == "production" and self.vercel_env == "production") or (
            self.app_env == "test" and self.test_dispatch_allowed
        )

    @property
    def requires_vercel_oidc(self) -> bool:
        """Production and Preview Vercel requests require platform OIDC."""

        return self.vercel_env in {"production", "preview"}

    @property
    def trusted_origins(self) -> tuple[str, ...]:
        origins = [self.public_origin]
        if self.deployment_origin and self.deployment_origin not in origins:
            origins.append(self.deployment_origin)
        return tuple(origins)

    @property
    def operator_origins(self) -> tuple[str, ...]:
        """Only the canonical origin may authenticate or mutate production."""

        return (self.public_origin,)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        app_env = values.get("APP_ENV", "development").strip().lower()
        if app_env not in {"development", "test", "production"}:
            raise ConfigurationError("APP_ENV must be development, test, or production")

        vercel_env = values.get("VERCEL_ENV", "").strip().lower()
        if vercel_env not in {"", "development", "production", "preview"}:
            raise ConfigurationError("VERCEL_ENV is invalid")
        if app_env == "production" and vercel_env not in {"production", "preview"}:
            raise ConfigurationError(
                "production control plane requires VERCEL_ENV=production or preview"
            )
        if vercel_env in {"production", "preview"} and app_env != "production":
            raise ConfigurationError("Vercel production and preview require APP_ENV=production")
        database_url = normalize_database_url(_required(values, "CONTROL_DATABASE_URL"))
        deployment_origin = (
            _vercel_deployment_origin(
                values,
                required=vercel_env == "preview",
            )
            if vercel_env in {"production", "preview"}
            else None
        )
        if vercel_env == "preview":
            assert deployment_origin is not None
            public_origin = deployment_origin
        else:
            public_origin = _required(values, "CONTROL_PUBLIC_ORIGIN").rstrip("/")
        try:
            parsed_origin = urlsplit(public_origin)
            origin_port = parsed_origin.port
        except ValueError as exc:
            raise ConfigurationError("CONTROL_PUBLIC_ORIGIN is invalid") from exc
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ConfigurationError("CONTROL_PUBLIC_ORIGIN must be a pure HTTP(S) origin")
        operator_token = _strong_secret(
            _required(values, "CONTROL_OPERATOR_TOKEN"), "CONTROL_OPERATOR_TOKEN"
        )
        session_secret = _strong_secret(
            _required(values, "CONTROL_SESSION_SECRET"), "CONTROL_SESSION_SECRET"
        )
        csrf_secret = _strong_secret(
            _required(values, "CONTROL_CSRF_SECRET"), "CONTROL_CSRF_SECRET"
        )
        signing_key = _required(values, "CONTROL_SIGNING_PRIVATE_KEY_B64")
        runner_key = _required(values, "CONTROL_RUNNER_PUBLIC_KEY_B64")
        secrets = {operator_token, session_secret, csrf_secret, signing_key, runner_key}
        if len(secrets) != 5:
            raise ConfigurationError("operator, session, CSRF, and signing keys must be distinct")

        try:
            control_private_key = private_key_from_base64url(signing_key)
            public_key_from_base64url(runner_key)
            control_key_id = UUID(_required(values, "CONTROL_SIGNING_KEY_ID"))
            runner_device_id = UUID(_required(values, "CONTROL_RUNNER_DEVICE_ID"))
        except ValueError as exc:
            raise ConfigurationError("invalid Ed25519 key or device identifier") from exc
        if public_key_to_base64url(control_private_key.public_key()) == runner_key:
            raise ConfigurationError("control and runner Ed25519 identities must differ")
        if control_key_id == runner_device_id:
            raise ConfigurationError("control and runner key identifiers must differ")

        identity_bundle_digest: str | None = None
        vercel_identity_target: VercelIdentityTarget | None = None
        if vercel_env in {"production", "preview"}:
            identity_bundle_digest, vercel_identity_target = _verify_identity_bundle_digest(values)

        if app_env == "production":
            _validate_postgres_tls(database_url)
            if not public_origin.startswith("https://"):
                raise ConfigurationError("production origin must use HTTPS")
            if origin_port not in {None, 443}:
                raise ConfigurationError("production origin must use the default HTTPS port")
            if values.get("CONTROL_ALLOW_PREVIEW_DISPATCH", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }:
                raise ConfigurationError("preview dispatch cannot be enabled")

        session_ttl = int(values.get("CONTROL_SESSION_TTL_SECONDS", "3600"))
        offline_seconds = int(values.get("CONTROL_RUNNER_OFFLINE_SECONDS", "30"))
        if not 300 <= session_ttl <= 86_400:
            raise ConfigurationError("session TTL must be between 300 and 86400 seconds")
        if not 10 <= offline_seconds <= 300:
            raise ConfigurationError("runner offline threshold must be between 10 and 300 seconds")

        return cls(
            app_env=app_env,
            vercel_env=vercel_env,
            database_url=database_url,
            public_origin=public_origin,
            operator_token=operator_token,
            session_secret=session_secret,
            csrf_secret=csrf_secret,
            control_signing_private_key=signing_key,
            control_signing_key_id=control_key_id,
            runner_device_id=runner_device_id,
            runner_verify_public_key=runner_key,
            identity_bundle_digest=identity_bundle_digest,
            vercel_identity_target=vercel_identity_target,
            deployment_origin=deployment_origin,
            session_ttl_seconds=session_ttl,
            runner_offline_seconds=offline_seconds,
            docs_enabled=app_env != "production",
            secure_cookies=app_env == "production",
        )


__all__ = [
    "ConfigurationError",
    "Settings",
    "VercelIdentityTarget",
    "build_identity_bundle_digest",
    "normalize_database_url",
]
