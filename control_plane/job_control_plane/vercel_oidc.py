"""Fail-closed verification of Vercel's request-scoped OIDC identity."""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import math
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .config import VercelIdentityTarget

VERCEL_OIDC_HEADER = "x-vercel-oidc-token"
VERCEL_OIDC_GLOBAL_ISSUER = "https://oidc.vercel.com"
VERCEL_OIDC_HOST = "oidc.vercel.com"
MAX_TOKEN_BYTES = 16_384
MAX_JWKS_BYTES = 262_144
MAX_JWKS_KEYS = 16
MAX_JSON_NESTING_DEPTH = 32
MAX_CLOCK_SKEW_SECONDS = 30
MAX_TOKEN_AGE_SECONDS = 3_600
MAX_TOKEN_TTL_SECONDS = 12 * 60 * 60
DEFAULT_JWKS_CACHE_TTL_SECONDS = 3_600
DEFAULT_JWKS_REFRESH_COOLDOWN_SECONDS = 30
_OIDC_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_KID_PATTERN = re.compile(r"[A-Za-z0-9._~:/+=-]{1,200}")

JwksDocument = Mapping[str, object]
JwksFetcher = Callable[[str], JwksDocument]


class VercelOidcDenialCode(StrEnum):
    """Bounded, non-sensitive reason codes for server-side diagnostics."""

    BAD_ISSUER = "BAD_ISSUER"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    BAD_TARGET = "BAD_TARGET"
    BAD_TIME_AGE = "BAD_TIME_AGE"
    BAD_TIME_EXPIRED = "BAD_TIME_EXPIRED"
    BAD_TIME_IAT = "BAD_TIME_IAT"
    BAD_TIME_NBF = "BAD_TIME_NBF"
    BAD_TIME_ORDERING = "BAD_TIME_ORDERING"
    BAD_TIME_TTL = "BAD_TIME_TTL"
    BAD_TIME_TTL_ENV_FALLBACK = "BAD_TIME_TTL_ENV_FALLBACK"
    BAD_TIME_TTL_REQUEST = "BAD_TIME_TTL_REQUEST"
    HEADER_CARDINALITY = "HEADER_CARDINALITY"
    JWKS_UNAVAILABLE = "JWKS_UNAVAILABLE"
    MALFORMED_TOKEN = "MALFORMED_TOKEN"
    MISSING_HEADER = "MISSING_HEADER"
    VERIFIER_UNAVAILABLE = "VERIFIER_UNAVAILABLE"


class VercelOidcVerificationError(ValueError):
    """Raised without reflecting token or key material."""

    def __init__(
        self,
        message: str,
        *,
        code: VercelOidcDenialCode = VercelOidcDenialCode.MALFORMED_TOKEN,
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class VercelOidcClaims:
    """Authenticated Vercel deployment identity."""

    issuer: str
    owner: str
    owner_id: str
    project: str
    project_id: str
    environment: str


@dataclass(slots=True)
class _JwksEntry:
    keys: dict[str, RSAPublicKey]
    expires_at: float


def _decode_base64url(value: str, *, name: str, max_bytes: int) -> bytes:
    if not value:
        raise VercelOidcVerificationError(f"{name} is invalid")
    try:
        encoded = value.encode("ascii")
        padded = encoded + (b"=" * (-len(encoded) % 4))
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise VercelOidcVerificationError(f"{name} is invalid") from exc
    if not decoded or len(decoded) > max_bytes:
        raise VercelOidcVerificationError(f"{name} is invalid")
    return decoded


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VercelOidcVerificationError("JSON contains duplicate members")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise VercelOidcVerificationError("JSON contains a non-finite value")


def _bounded_json_integer(value: str) -> int:
    if len(value.lstrip("-")) > 20:
        raise VercelOidcVerificationError("JSON integer is too large")
    return int(value)


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise VercelOidcVerificationError("JSON float is non-finite")
    return parsed


def _assert_json_nesting_depth(value: str) -> None:
    """Reject deeply nested JSON without relying on interpreter recursion limits."""

    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise VercelOidcVerificationError("JSON nesting is too deep")
        elif character in "]}":
            depth -= 1
    # Structural mismatches are normalized by json.loads below.


def _decode_json_object(segment: str, *, name: str, max_bytes: int) -> dict[str, object]:
    raw = _decode_base64url(segment, name=name, max_bytes=max_bytes)
    try:
        decoded = raw.decode("utf-8")
        _assert_json_nesting_depth(decoded)
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
            parse_int=_bounded_json_integer,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise VercelOidcVerificationError(f"{name} is invalid") from exc
    if not isinstance(value, dict):
        raise VercelOidcVerificationError(f"{name} is invalid")
    return value


def _jwks_path_for_issuer(issuer: str) -> str:
    if issuer != VERCEL_OIDC_GLOBAL_ISSUER:
        raise VercelOidcVerificationError(
            "issuer is invalid",
            code=VercelOidcDenialCode.BAD_ISSUER,
        )
    return "/.well-known/jwks"


def fetch_vercel_jwks(issuer: str) -> JwksDocument:
    """Fetch a bounded JWKS from Vercel's fixed OIDC host without redirects."""

    path = _jwks_path_for_issuer(issuer)
    connection = http.client.HTTPSConnection(
        VERCEL_OIDC_HOST,
        port=443,
        timeout=3,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "User-Agent": "job-apply-control-plane/1",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise VercelOidcVerificationError(
                "JWKS endpoint is unavailable",
                code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
            )
        raw_length = response.getheader("Content-Length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise VercelOidcVerificationError(
                    "JWKS response is invalid",
                    code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
                ) from exc
            if content_length < 0 or content_length > MAX_JWKS_BYTES:
                raise VercelOidcVerificationError(
                    "JWKS response is invalid",
                    code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
                )
        raw = response.read(MAX_JWKS_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise VercelOidcVerificationError(
            "JWKS endpoint is unavailable",
            code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
        ) from exc
    finally:
        connection.close()
    if len(raw) > MAX_JWKS_BYTES:
        raise VercelOidcVerificationError(
            "JWKS response is invalid",
            code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
        )
    try:
        decoded = raw.decode("utf-8")
        _assert_json_nesting_depth(decoded)
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
            parse_int=_bounded_json_integer,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise VercelOidcVerificationError(
            "JWKS response is invalid",
            code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
        ) from exc
    if not isinstance(value, dict):
        raise VercelOidcVerificationError(
            "JWKS response is invalid",
            code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
        )
    return value


def _jwk_integer(value: object, *, name: str, max_bytes: int) -> int:
    if not isinstance(value, str):
        raise VercelOidcVerificationError("JWKS key is invalid")
    decoded = _decode_base64url(value, name=name, max_bytes=max_bytes)
    return int.from_bytes(decoded, "big")


def _parse_jwks(document: JwksDocument) -> dict[str, RSAPublicKey]:
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= MAX_JWKS_KEYS:
        raise VercelOidcVerificationError("JWKS key set is invalid")
    keys: dict[str, RSAPublicKey] = {}
    for item in raw_keys:
        if not isinstance(item, Mapping):
            raise VercelOidcVerificationError("JWKS key is invalid")
        if item.get("kty") != "RSA":
            continue
        algorithm = item.get("alg")
        key_use = item.get("use")
        if algorithm is not None and not isinstance(algorithm, str):
            raise VercelOidcVerificationError("JWKS key algorithm is invalid")
        if key_use is not None and not isinstance(key_use, str):
            raise VercelOidcVerificationError("JWKS key use is invalid")
        if algorithm not in (None, "RS256") or key_use not in (None, "sig"):
            continue
        key_ops = item.get("key_ops")
        if key_ops is not None and (
            not isinstance(key_ops, list)
            or not all(isinstance(operation, str) for operation in key_ops)
            or "verify" not in key_ops
        ):
            continue
        kid = item.get("kid")
        if not isinstance(kid, str) or not _KID_PATTERN.fullmatch(kid) or kid in keys:
            raise VercelOidcVerificationError("JWKS key identifier is invalid")
        modulus = _jwk_integer(item.get("n"), name="JWK modulus", max_bytes=1_024)
        exponent = _jwk_integer(item.get("e"), name="JWK exponent", max_bytes=8)
        if not 2_048 <= modulus.bit_length() <= 8_192:
            raise VercelOidcVerificationError("JWKS RSA modulus is invalid")
        if exponent < 3 or exponent > 0xFFFFFFFF or exponent % 2 == 0:
            raise VercelOidcVerificationError("JWKS RSA exponent is invalid")
        try:
            keys[kid] = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        except ValueError as exc:
            raise VercelOidcVerificationError("JWKS RSA key is invalid") from exc
    if not keys:
        raise VercelOidcVerificationError("JWKS has no RS256 verification keys")
    return keys


class VercelJwksCache:
    """Thread-safe, size/TTL/rate-bounded cache for Vercel RSA keys."""

    def __init__(
        self,
        *,
        fetcher: JwksFetcher = fetch_vercel_jwks,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: int = DEFAULT_JWKS_CACHE_TTL_SECONDS,
        refresh_cooldown_seconds: int = DEFAULT_JWKS_REFRESH_COOLDOWN_SECONDS,
    ) -> None:
        if not 60 <= ttl_seconds <= 21_600:
            raise ValueError("JWKS cache TTL is outside the supported bounds")
        if not 1 <= refresh_cooldown_seconds <= 300:
            raise ValueError("JWKS refresh cooldown is outside the supported bounds")
        self._fetcher = fetcher
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._refresh_cooldown_seconds = refresh_cooldown_seconds
        self._entry: _JwksEntry | None = None
        self._next_network_refresh_at = 0.0
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        with self._lock:
            return int(self._entry is not None)

    def get_key(self, *, issuer: str, kid: str) -> RSAPublicKey:
        _jwks_path_for_issuer(issuer)
        with self._lock:
            now = self._clock()
            entry = self._entry
            if entry is not None and entry.expires_at > now and kid in entry.keys:
                return entry.keys[kid]
            if now < self._next_network_refresh_at:
                raise VercelOidcVerificationError(
                    "JWKS refresh is rate limited",
                    code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
                )

            self._next_network_refresh_at = now + self._refresh_cooldown_seconds
            try:
                keys = _parse_jwks(self._fetcher(issuer))
            except VercelOidcVerificationError as exc:
                raise VercelOidcVerificationError(
                    str(exc),
                    code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
                ) from exc
            self._entry = _JwksEntry(
                keys=keys,
                expires_at=now + self._ttl_seconds,
            )
            key = keys.get(kid)
            if key is None:
                raise VercelOidcVerificationError(
                    "JWT key identifier is unknown",
                    code=VercelOidcDenialCode.JWKS_UNAVAILABLE,
                )
            return key


def _required_string(claims: Mapping[str, object], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise VercelOidcVerificationError(f"{name} claim is invalid")
    return value


def _required_timestamp(claims: Mapping[str, object], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53:
        raise VercelOidcVerificationError(f"{name} claim is invalid")
    return value


class VercelOidcVerifier:
    """Verify Vercel OIDC signature and exact schema-v2 deployment scope."""

    def __init__(
        self,
        target: VercelIdentityTarget,
        *,
        jwks_cache: VercelJwksCache | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._target = target
        self._jwks_cache = jwks_cache or VercelJwksCache()
        self._wall_clock = wall_clock

    def _lookup_issuer(self, claims: Mapping[str, object]) -> tuple[str, str]:
        owner = _required_string(claims, "owner")
        issuer = _required_string(claims, "iss")
        if not _OIDC_NAME_PATTERN.fullmatch(owner):
            raise VercelOidcVerificationError("owner claim is invalid")
        if issuer != VERCEL_OIDC_GLOBAL_ISSUER:
            raise VercelOidcVerificationError(
                "issuer claim is invalid",
                code=VercelOidcDenialCode.BAD_ISSUER,
            )
        return issuer, owner

    def _validate_authenticated_claims(
        self,
        claims: Mapping[str, object],
        *,
        issuer: str,
        owner: str,
    ) -> VercelOidcClaims:
        audience = _required_string(claims, "aud")
        subject = _required_string(claims, "sub")
        owner_id = _required_string(claims, "owner_id")
        project = _required_string(claims, "project")
        project_id = _required_string(claims, "project_id")
        environment = _required_string(claims, "environment")
        if not _OIDC_NAME_PATTERN.fullmatch(project):
            raise VercelOidcVerificationError("project claim is invalid")
        if audience != f"https://vercel.com/{owner}":
            raise VercelOidcVerificationError(
                "audience claim is invalid",
                code=VercelOidcDenialCode.BAD_TARGET,
            )
        expected_subject = f"owner:{owner}:project:{project}:environment:{environment}"
        if subject != expected_subject:
            raise VercelOidcVerificationError(
                "subject claim is invalid",
                code=VercelOidcDenialCode.BAD_TARGET,
            )
        if (
            owner_id != self._target.scope_id
            or project_id != self._target.project_id
            or environment != self._target.environment
        ):
            raise VercelOidcVerificationError(
                "deployment target claim is invalid",
                code=VercelOidcDenialCode.BAD_TARGET,
            )

        issued_at = _required_timestamp(claims, "iat")
        not_before = _required_timestamp(claims, "nbf")
        expires_at = _required_timestamp(claims, "exp")
        now = self._wall_clock()
        if issued_at > now + MAX_CLOCK_SKEW_SECONDS:
            raise VercelOidcVerificationError(
                "issued-at claim is invalid",
                code=VercelOidcDenialCode.BAD_TIME_IAT,
            )
        if not_before > now + MAX_CLOCK_SKEW_SECONDS:
            raise VercelOidcVerificationError(
                "not-before claim is invalid",
                code=VercelOidcDenialCode.BAD_TIME_NBF,
            )
        if expires_at <= now - MAX_CLOCK_SKEW_SECONDS:
            raise VercelOidcVerificationError(
                "expiry claim is invalid",
                code=VercelOidcDenialCode.BAD_TIME_EXPIRED,
            )
        if issued_at < now - MAX_TOKEN_AGE_SECONDS - MAX_CLOCK_SKEW_SECONDS:
            raise VercelOidcVerificationError(
                "token age is invalid",
                code=VercelOidcDenialCode.BAD_TIME_AGE,
            )
        if expires_at <= issued_at:
            raise VercelOidcVerificationError(
                "token lifetime is invalid",
                code=VercelOidcDenialCode.BAD_TIME_ORDERING,
            )
        token_ttl = expires_at - issued_at
        if token_ttl > MAX_TOKEN_TTL_SECONDS:
            raise VercelOidcVerificationError(
                "token lifetime is invalid",
                code=VercelOidcDenialCode.BAD_TIME_TTL,
            )
        if not issued_at - MAX_CLOCK_SKEW_SECONDS <= not_before < expires_at:
            raise VercelOidcVerificationError(
                "token time ordering is invalid",
                code=VercelOidcDenialCode.BAD_TIME_ORDERING,
            )

        return VercelOidcClaims(
            issuer=issuer,
            owner=owner,
            owner_id=owner_id,
            project=project,
            project_id=project_id,
            environment=environment,
        )

    def verify(self, token: str) -> VercelOidcClaims:
        if not isinstance(token, str) or not 1 <= len(token.encode("utf-8")) <= MAX_TOKEN_BYTES:
            raise VercelOidcVerificationError("OIDC token is invalid")
        segments = token.split(".")
        if len(segments) != 3:
            raise VercelOidcVerificationError("OIDC token is invalid")
        encoded_header, encoded_claims, encoded_signature = segments
        header = _decode_json_object(
            encoded_header,
            name="JWT header",
            max_bytes=4_096,
        )
        claims = _decode_json_object(
            encoded_claims,
            name="JWT claims",
            max_bytes=12_288,
        )
        if header.get("alg") != "RS256":
            raise VercelOidcVerificationError("JWT algorithm is invalid")
        token_type = header.get("typ")
        if not isinstance(token_type, str) or token_type.upper() != "JWT":
            raise VercelOidcVerificationError("JWT type is invalid")
        kid = header.get("kid")
        if not isinstance(kid, str) or not _KID_PATTERN.fullmatch(kid):
            raise VercelOidcVerificationError("JWT key identifier is invalid")
        signature = _decode_base64url(
            encoded_signature,
            name="JWT signature",
            max_bytes=1_024,
        )

        issuer, owner = self._lookup_issuer(claims)
        public_key = self._jwks_cache.get_key(issuer=issuer, kid=kid)
        signed = f"{encoded_header}.{encoded_claims}".encode("ascii")
        try:
            public_key.verify(
                signature,
                signed,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise VercelOidcVerificationError(
                "JWT signature is invalid",
                code=VercelOidcDenialCode.BAD_SIGNATURE,
            ) from exc

        authenticated = self._validate_authenticated_claims(
            claims,
            issuer=issuer,
            owner=owner,
        )
        return authenticated


__all__ = [
    "MAX_CLOCK_SKEW_SECONDS",
    "MAX_JSON_NESTING_DEPTH",
    "MAX_JWKS_BYTES",
    "MAX_JWKS_KEYS",
    "MAX_TOKEN_AGE_SECONDS",
    "MAX_TOKEN_BYTES",
    "MAX_TOKEN_TTL_SECONDS",
    "VERCEL_OIDC_HEADER",
    "VercelOidcDenialCode",
    "VercelJwksCache",
    "VercelOidcClaims",
    "VercelOidcVerificationError",
    "VercelOidcVerifier",
    "fetch_vercel_jwks",
]
