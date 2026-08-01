"""Private local key loading for signed autopilot policy revisions."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from dotenv import dotenv_values

DEFAULT_AUTOMATION_POLICY_KEY_PATH = ".job-agent/automation-policy-ed25519.pem"


class AutomationPolicyKeyError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class AutomationPolicySigningIdentity:
    key_id: UUID
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey


def configured_automation_policy_key_path() -> Path:
    """Resolve a private path without extending generic application settings."""

    direct = os.environ.get("AUTOMATION_POLICY_SIGNING_KEY_PATH", "").strip()
    if direct:
        return Path(direct)
    env_file = Path(os.environ.get("JOB_AGENT_ENV_FILE", ".env"))
    if env_file.is_file():
        configured = str(
            dotenv_values(env_file).get("AUTOMATION_POLICY_SIGNING_KEY_PATH") or ""
        ).strip()
        if configured:
            return Path(configured)
    return Path(DEFAULT_AUTOMATION_POLICY_KEY_PATH)


def _key_id(public_key: Ed25519PublicKey) -> UUID:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = bytearray(hashlib.sha256(raw).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(digest))


def load_automation_policy_signing_identity(
    path: str | Path | None = None,
) -> AutomationPolicySigningIdentity:
    candidate = Path(path) if path is not None else configured_automation_policy_key_path()
    try:
        if not candidate.is_file() or candidate.stat().st_size > 8_192:
            raise AutomationPolicyKeyError("AUTOMATION_POLICY_SIGNING_KEY_UNAVAILABLE")
        loaded = serialization.load_pem_private_key(candidate.read_bytes(), password=None)
    except AutomationPolicyKeyError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise AutomationPolicyKeyError("AUTOMATION_POLICY_SIGNING_KEY_INVALID") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise AutomationPolicyKeyError("AUTOMATION_POLICY_SIGNING_KEY_INVALID")
    public_key = loaded.public_key()
    return AutomationPolicySigningIdentity(
        key_id=_key_id(public_key),
        private_key=loaded,
        public_key=public_key,
    )


def generate_automation_policy_signing_key(path: str | Path) -> UUID:
    """Create one key without overwriting existing operator-owned material."""

    candidate = Path(path)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    encoded = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        with candidate.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        raise AutomationPolicyKeyError("AUTOMATION_POLICY_SIGNING_KEY_EXISTS") from exc
    try:
        os.chmod(candidate, 0o600)
    except OSError:
        # Windows ACL hardening is handled by the operator helper script.  The
        # library still never logs or returns private bytes.
        pass
    return _key_id(private_key.public_key())


__all__ = [
    "DEFAULT_AUTOMATION_POLICY_KEY_PATH",
    "AutomationPolicyKeyError",
    "AutomationPolicySigningIdentity",
    "configured_automation_policy_key_path",
    "generate_automation_policy_signing_key",
    "load_automation_policy_signing_identity",
]
