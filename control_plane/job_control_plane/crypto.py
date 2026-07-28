"""Purpose-separated Ed25519 envelope signing and verification."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .protocol import EnvelopePurpose, SignedEnvelope, StrictProtocolModel, canonical_unsigned_bytes

MAX_ENVELOPE_TTL = timedelta(minutes=5)
MAX_CLOCK_SKEW = timedelta(seconds=30)


class ProtocolVerificationError(ValueError):
    """Raised for any invalid or expired signed protocol message."""


def _decode_base64url(value: str, *, expected_length: int) -> bytes:
    try:
        padded = value + ("=" * (-len(value) % 4))
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64url key material") from exc
    if len(decoded) != expected_length:
        raise ValueError("invalid key material length")
    return decoded


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def private_key_from_base64url(value: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_decode_base64url(value, expected_length=32))


def public_key_from_base64url(value: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_decode_base64url(value, expected_length=32))


def public_key_to_base64url(public_key: Ed25519PublicKey) -> str:
    return _encode_base64url(public_key.public_bytes_raw())


def private_key_to_base64url(private_key: Ed25519PrivateKey) -> str:
    return _encode_base64url(private_key.private_bytes_raw())


def sign_envelope(
    envelope: SignedEnvelope[StrictProtocolModel],
    private_key: Ed25519PrivateKey,
) -> SignedEnvelope[StrictProtocolModel]:
    """Sign an otherwise complete envelope without mutating it."""

    unsigned = envelope.model_copy(update={"signature": ""})
    signature = private_key.sign(canonical_unsigned_bytes(unsigned))
    return unsigned.model_copy(update={"signature": _encode_base64url(signature)})


def verify_envelope(
    envelope: SignedEnvelope[StrictProtocolModel],
    public_key: Ed25519PublicKey,
    *,
    expected_purpose: EnvelopePurpose,
    expected_audience: str,
    now: datetime | None = None,
) -> None:
    """Verify signature, purpose, audience, lifetime, and clock bounds.

    Replay/nonce consumption is intentionally transactional and belongs in the
    database service after this cryptographic check succeeds.
    """

    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    issued_at = envelope.issued_at.astimezone(UTC)
    expires_at = envelope.expires_at.astimezone(UTC)

    if envelope.purpose is not expected_purpose:
        raise ProtocolVerificationError("purpose mismatch")
    if envelope.audience != expected_audience:
        raise ProtocolVerificationError("audience mismatch")
    if issued_at > checked_at + MAX_CLOCK_SKEW:
        raise ProtocolVerificationError("issued-at is in the future")
    if expires_at <= issued_at:
        raise ProtocolVerificationError("invalid expiry ordering")
    if expires_at - issued_at > MAX_ENVELOPE_TTL:
        raise ProtocolVerificationError("envelope lifetime exceeds five minutes")
    if expires_at <= checked_at:
        raise ProtocolVerificationError("envelope expired")
    if not envelope.signature:
        raise ProtocolVerificationError("signature missing")

    try:
        signature = _decode_base64url(envelope.signature, expected_length=64)
        unsigned = envelope.model_copy(update={"signature": ""})
        public_key.verify(signature, canonical_unsigned_bytes(unsigned))
    except (InvalidSignature, ValueError) as exc:
        raise ProtocolVerificationError("signature invalid") from exc


__all__ = [
    "MAX_CLOCK_SKEW",
    "MAX_ENVELOPE_TTL",
    "ProtocolVerificationError",
    "private_key_from_base64url",
    "private_key_to_base64url",
    "public_key_from_base64url",
    "public_key_to_base64url",
    "sign_envelope",
    "verify_envelope",
]
