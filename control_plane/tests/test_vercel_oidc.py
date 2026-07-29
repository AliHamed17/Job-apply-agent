from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from job_control_plane import app as app_module
from job_control_plane import vercel_oidc
from job_control_plane.app import create_app
from job_control_plane.config import (
    Settings,
    VercelIdentityTarget,
    build_identity_bundle_digest,
)
from job_control_plane.crypto import private_key_to_base64url, public_key_to_base64url
from job_control_plane.db import Base
from job_control_plane.vercel_oidc import (
    MAX_JSON_NESTING_DEPTH,
    MAX_JWKS_KEYS,
    MAX_TOKEN_AGE_SECONDS,
    MAX_TOKEN_BYTES,
    MAX_TOKEN_TTL_SECONDS,
    VERCEL_OIDC_GLOBAL_ISSUER,
    VERCEL_OIDC_HEADER,
    VercelJwksCache,
    VercelOidcDenialCode,
    VercelOidcVerificationError,
    VercelOidcVerifier,
    fetch_vercel_jwks,
)

NOW = 1_800_000_000
OWNER = "safe-team"
PROJECT = "job-apply-control"
PROJECT_ID = "prj_12345678abcdef"
SCOPE_ID = "team_12345678abcdef"
KID = "vercel-test-key-1"
IDENTITY_VERSION = UUID("00000000-0000-4000-8000-000000000999")
TARGET = VercelIdentityTarget(
    environment="production",
    project_id=PROJECT_ID,
    scope_id=SCOPE_ID,
)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _jwk(private_key: RSAPrivateKey, *, kid: str = KID) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    modulus = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    exponent = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "alg": "RS256",
        "e": _base64url(exponent),
        "kid": kid,
        "kty": "RSA",
        "n": _base64url(modulus),
        "use": "sig",
    }


def _claims(
    *,
    issuer: str = VERCEL_OIDC_GLOBAL_ISSUER,
    overrides: Mapping[str, object] | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "iss": issuer,
        "aud": f"https://vercel.com/{OWNER}",
        "sub": f"owner:{OWNER}:project:{PROJECT}:environment:production",
        "iat": NOW,
        "nbf": NOW,
        "exp": NOW + 3_600,
        "owner": OWNER,
        "owner_id": SCOPE_ID,
        "project": PROJECT,
        "project_id": PROJECT_ID,
        "environment": "production",
    }
    if overrides:
        values.update(overrides)
    return values


def _signed_token(
    private_key: RSAPrivateKey,
    *,
    claims: Mapping[str, object] | None = None,
    header: Mapping[str, object] | None = None,
    signing_key: RSAPrivateKey | None = None,
) -> str:
    protected: dict[str, object] = {"alg": "RS256", "kid": KID, "typ": "JWT"}
    if header:
        protected.update(header)
    encoded_header = _base64url(
        json.dumps(protected, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_claims = _base64url(
        json.dumps(
            dict(claims or _claims()),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = (signing_key or private_key).sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


def _raw_claims_token(raw_claims: str) -> str:
    header = _base64url(b'{"alg":"RS256","kid":"vercel-test-key-1","typ":"JWT"}')
    claims = _base64url(raw_claims.encode("utf-8"))
    signature = _base64url(b"not-a-valid-signature")
    token = f"{header}.{claims}.{signature}"
    assert len(token.encode("utf-8")) <= MAX_TOKEN_BYTES
    return token


def _verifier(
    private_key: RSAPrivateKey,
    *,
    target: VercelIdentityTarget = TARGET,
    fetches: list[str] | None = None,
) -> VercelOidcVerifier:
    observed = fetches if fetches is not None else []

    def fetch(issuer: str) -> Mapping[str, object]:
        observed.append(issuer)
        return {"keys": [_jwk(private_key)]}

    return VercelOidcVerifier(
        target,
        jwks_cache=VercelJwksCache(fetcher=fetch),
        wall_clock=lambda: float(NOW),
    )


@pytest.fixture(scope="module")
def rsa_private_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65_537, key_size=2_048)


def _production_settings() -> Settings:
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
    env["CONTROL_IDENTITY_BUNDLE_DIGEST"] = build_identity_bundle_digest(
        env,
        version_id=IDENTITY_VERSION,
        environment="production",
        project_id=PROJECT_ID,
        scope_id=SCOPE_ID,
    )
    return Settings.from_env(env)


def test_only_exact_global_issuer_tokens_verify(
    rsa_private_key: RSAPrivateKey,
) -> None:
    global_fetches: list[str] = []
    global_verifier = _verifier(rsa_private_key, fetches=global_fetches)
    global_claims = global_verifier.verify(_signed_token(rsa_private_key))
    assert global_claims.owner_id == SCOPE_ID
    assert global_claims.project_id == PROJECT_ID
    assert global_claims.environment == "production"
    global_verifier.verify(_signed_token(rsa_private_key))
    string_delimiters = _claims(overrides={"opaque": "[{" * (MAX_JSON_NESTING_DEPTH + 1)})
    global_verifier.verify(_signed_token(rsa_private_key, claims=string_delimiters))
    assert global_fetches == [VERCEL_OIDC_GLOBAL_ISSUER]

    team_issuer = f"{VERCEL_OIDC_GLOBAL_ISSUER}/{OWNER}"
    team_fetches: list[str] = []
    team_verifier = _verifier(rsa_private_key, fetches=team_fetches)
    with pytest.raises(VercelOidcVerificationError, match="issuer"):
        team_verifier.verify(_signed_token(rsa_private_key, claims=_claims(issuer=team_issuer)))
    assert team_fetches == []


def test_json_nesting_bound_is_exact_and_string_aware(
    rsa_private_key: RSAPrivateKey,
) -> None:
    def nested_lists(levels: int) -> object:
        value: object = 0
        for _index in range(levels):
            value = [value]
        return value

    verifier = _verifier(rsa_private_key)
    accepted_claims = _claims(
        overrides={
            "nested": nested_lists(MAX_JSON_NESTING_DEPTH - 1),
            "opaque": r"\"[{]}\\tail",
        }
    )
    verifier.verify(_signed_token(rsa_private_key, claims=accepted_claims))

    otherwise_valid_over_depth = _claims(overrides={"nested": nested_lists(MAX_JSON_NESTING_DEPTH)})
    with pytest.raises(VercelOidcVerificationError, match="JWT claims"):
        verifier.verify(_signed_token(rsa_private_key, claims=otherwise_valid_over_depth))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"aud": "https://vercel.com/other"}, "audience"),
        ({"sub": "owner:safe-team:project:other:environment:production"}, "subject"),
        ({"owner_id": "team_deadbeef12345678"}, "deployment target"),
        ({"project_id": "prj_deadbeef12345678"}, "deployment target"),
        (
            {
                "environment": "preview",
                "sub": f"owner:{OWNER}:project:{PROJECT}:environment:preview",
            },
            "deployment target",
        ),
        ({"iat": NOW + 31}, "issued-at"),
        ({"nbf": NOW + 31}, "not-before"),
        ({"exp": NOW - 31}, "expiry"),
        (
            {
                "iat": NOW - MAX_TOKEN_AGE_SECONDS - 31,
                "nbf": NOW - MAX_TOKEN_AGE_SECONDS - 31,
            },
            "token age",
        ),
        ({"exp": NOW + MAX_TOKEN_TTL_SECONDS + 1}, "token lifetime"),
        ({"nbf": NOW - 31}, "time ordering"),
        ({"iat": float(NOW)}, "iat claim"),
    ],
)
def test_authenticated_claim_mismatches_fail_closed(
    rsa_private_key: RSAPrivateKey,
    override: Mapping[str, object],
    message: str,
) -> None:
    verifier = _verifier(rsa_private_key)
    token = _signed_token(
        rsa_private_key,
        claims=_claims(overrides=override),
    )
    with pytest.raises(VercelOidcVerificationError, match=message):
        verifier.verify(token)


@pytest.mark.parametrize(
    ("ttl_seconds", "expected_code"),
    [
        (0, VercelOidcDenialCode.BAD_TIME_ORDERING),
        (
            MAX_TOKEN_TTL_SECONDS + 1,
            VercelOidcDenialCode.BAD_TIME_TTL,
        ),
    ],
)
def test_token_lifetime_diagnostics_are_bounded(
    rsa_private_key: RSAPrivateKey,
    ttl_seconds: int,
    expected_code: VercelOidcDenialCode,
) -> None:
    verifier = _verifier(rsa_private_key)
    token = _signed_token(
        rsa_private_key,
        claims=_claims(overrides={"exp": NOW + ttl_seconds}),
    )

    with pytest.raises(VercelOidcVerificationError) as error:
        verifier.verify(token)

    assert error.value.code is expected_code


@pytest.mark.parametrize(
    "claims",
    [
        _claims(issuer="https://attacker.invalid"),
        _claims(issuer=f"{VERCEL_OIDC_GLOBAL_ISSUER}/{OWNER}"),
        _claims(
            issuer=f"{VERCEL_OIDC_GLOBAL_ISSUER}/other-team",
            overrides={"owner": OWNER},
        ),
        _claims(overrides={"owner": "unsafe/team"}),
    ],
)
def test_untrusted_issuer_shapes_never_fetch_jwks(
    rsa_private_key: RSAPrivateKey,
    claims: Mapping[str, object],
) -> None:
    fetches: list[str] = []
    verifier = _verifier(rsa_private_key, fetches=fetches)
    with pytest.raises(VercelOidcVerificationError, match="issuer|owner"):
        verifier.verify(_signed_token(rsa_private_key, claims=claims))
    assert fetches == []


def test_algorithm_key_id_signature_and_jwk_are_verified(
    rsa_private_key: RSAPrivateKey,
) -> None:
    with pytest.raises(VercelOidcVerificationError, match="algorithm"):
        _verifier(rsa_private_key).verify(_signed_token(rsa_private_key, header={"alg": "HS256"}))
    with pytest.raises(VercelOidcVerificationError, match="key identifier"):
        _verifier(rsa_private_key).verify(_signed_token(rsa_private_key, header={"kid": ""}))

    other_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    with pytest.raises(VercelOidcVerificationError, match="signature"):
        _verifier(rsa_private_key).verify(_signed_token(rsa_private_key, signing_key=other_key))

    unknown_kid_cache = VercelJwksCache(
        fetcher=lambda _issuer: {"keys": [_jwk(rsa_private_key, kid="other-key")]}
    )
    with pytest.raises(VercelOidcVerificationError, match="unknown"):
        VercelOidcVerifier(
            TARGET,
            jwks_cache=unknown_kid_cache,
            wall_clock=lambda: float(NOW),
        ).verify(_signed_token(rsa_private_key))

    weak_key = rsa.generate_private_key(public_exponent=65_537, key_size=1_024)
    weak_cache = VercelJwksCache(fetcher=lambda _issuer: {"keys": [_jwk(weak_key)]})
    with pytest.raises(VercelOidcVerificationError, match="modulus"):
        VercelOidcVerifier(
            TARGET,
            jwks_cache=weak_cache,
            wall_clock=lambda: float(NOW),
        ).verify(_signed_token(rsa_private_key))


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        "a.b.c.d",
        "!!.e30.signature",
        f"{_base64url(b'[]')}.{_base64url(b'{}')}.c2ln",
        "x" * (MAX_TOKEN_BYTES + 1),
    ],
)
def test_malformed_or_oversized_tokens_fail_without_reflection(
    rsa_private_key: RSAPrivateKey,
    token: str,
) -> None:
    with pytest.raises(VercelOidcVerificationError) as error:
        _verifier(rsa_private_key).verify(token)
    if token:
        assert token not in str(error.value)


def test_jwks_cache_bounds_refreshes_and_prevents_fetch_storms(
    rsa_private_key: RSAPrivateKey,
) -> None:
    clock = [100.0]
    fetches: list[str] = []
    second_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)

    def fetch(issuer: str) -> Mapping[str, object]:
        fetches.append(issuer)
        key = rsa_private_key if len(fetches) == 1 else second_key
        kid = KID if len(fetches) == 1 else "rotated-key"
        return {"keys": [_jwk(key, kid=kid)]}

    cache = VercelJwksCache(
        fetcher=fetch,
        clock=lambda: clock[0],
        ttl_seconds=60,
        refresh_cooldown_seconds=10,
    )
    assert cache.get_key(issuer=VERCEL_OIDC_GLOBAL_ISSUER, kid=KID)
    assert cache.get_key(issuer=VERCEL_OIDC_GLOBAL_ISSUER, kid=KID)
    assert len(fetches) == 1

    with pytest.raises(VercelOidcVerificationError, match="rate limited"):
        cache.get_key(issuer=VERCEL_OIDC_GLOBAL_ISSUER, kid="rotated-key")
    assert len(fetches) == 1

    clock[0] += 10
    assert cache.get_key(
        issuer=VERCEL_OIDC_GLOBAL_ISSUER,
        kid="rotated-key",
    )
    assert len(fetches) == 2

    with pytest.raises(VercelOidcVerificationError, match="issuer"):
        cache.get_key(
            issuer=f"{VERCEL_OIDC_GLOBAL_ISSUER}/untrusted-team",
            kid="rotated-key",
        )
    assert cache.size == 1
    assert len(fetches) == 2


def test_jwks_document_key_count_and_duplicates_are_rejected(
    rsa_private_key: RSAPrivateKey,
) -> None:
    too_many = VercelJwksCache(
        fetcher=lambda _issuer: {
            "keys": [
                _jwk(rsa_private_key, kid=f"key-{index}") for index in range(MAX_JWKS_KEYS + 1)
            ]
        }
    )
    with pytest.raises(VercelOidcVerificationError, match="key set"):
        too_many.get_key(issuer=VERCEL_OIDC_GLOBAL_ISSUER, kid=KID)

    duplicate = VercelJwksCache(
        fetcher=lambda _issuer: {"keys": [_jwk(rsa_private_key), _jwk(rsa_private_key)]}
    )
    with pytest.raises(VercelOidcVerificationError, match="identifier"):
        duplicate.get_key(issuer=VERCEL_OIDC_GLOBAL_ISSUER, kid=KID)

    for field, hostile_value, message in (
        ("alg", [], "algorithm"),
        ("use", {}, "use"),
    ):
        hostile_key: dict[str, object] = dict(_jwk(rsa_private_key))
        hostile_key[field] = hostile_value
        malformed = VercelJwksCache(fetcher=lambda _issuer, key=hostile_key: {"keys": [key]})
        with pytest.raises(VercelOidcVerificationError, match=message):
            malformed.get_key(issuer=VERCEL_OIDC_GLOBAL_ISSUER, kid=KID)


def test_jwks_transport_uses_only_fixed_vercel_host_and_rejects_redirects(
    rsa_private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, int, str]] = []
    response_payload = [json.dumps({"keys": [_jwk(rsa_private_key)]}).encode("utf-8")]
    response_status = [200]

    class FakeResponse:
        @property
        def status(self) -> int:
            return response_status[0]

        def getheader(self, name: str) -> str | None:
            if name == "Content-Length":
                return str(len(response_payload[0]))
            return None

        def read(self, amount: int) -> bytes:
            assert amount == vercel_oidc.MAX_JWKS_BYTES + 1
            return response_payload[0]

    class FakeConnection:
        def __init__(
            self,
            host: str,
            *,
            port: int,
            timeout: int,
            context: object,
        ) -> None:
            assert timeout == 3
            assert context is not None
            requests.append((host, port, ""))

        def request(
            self,
            method: str,
            path: str,
            *,
            headers: Mapping[str, str],
        ) -> None:
            assert method == "GET"
            assert headers["Accept"] == "application/json"
            host, port, _unused = requests[-1]
            requests[-1] = (host, port, path)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            return None

    monkeypatch.setattr(vercel_oidc.http.client, "HTTPSConnection", FakeConnection)
    fetched = fetch_vercel_jwks(VERCEL_OIDC_GLOBAL_ISSUER)
    assert fetched["keys"]
    assert requests == [
        (
            "oidc.vercel.com",
            443,
            "/.well-known/jwks",
        )
    ]

    response_status[0] = 302
    with pytest.raises(VercelOidcVerificationError, match="unavailable"):
        fetch_vercel_jwks(VERCEL_OIDC_GLOBAL_ISSUER)
    assert requests[-1] == ("oidc.vercel.com", 443, "/.well-known/jwks")

    with pytest.raises(VercelOidcVerificationError, match="issuer"):
        fetch_vercel_jwks("https://attacker.invalid/jwks")
    with pytest.raises(VercelOidcVerificationError, match="issuer"):
        fetch_vercel_jwks(f"{VERCEL_OIDC_GLOBAL_ISSUER}/{OWNER}")

    response_status[0] = 200
    for malformed_json in (
        b'{"too_large":' + (b"9" * 5_000) + b"}",
        b'{"too_deep":'
        + (b"[" * MAX_JSON_NESTING_DEPTH)
        + b"0"
        + (b"]" * MAX_JSON_NESTING_DEPTH)
        + b"}",
        b'{"not_finite":1e9999}',
    ):
        response_payload[0] = malformed_json
        with pytest.raises(VercelOidcVerificationError, match="JWKS response"):
            fetch_vercel_jwks(VERCEL_OIDC_GLOBAL_ISSUER)


def test_vercel_middleware_requires_attestation_before_database_access(
    rsa_private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        app_module.LOGGER,
        "warning",
        lambda *arguments: log_calls.append(arguments),
    )
    settings = _production_settings()
    fetches: list[str] = []
    verifier = _verifier(rsa_private_key, fetches=fetches)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(settings, engine=engine, oidc_verifier=verifier)
    checkouts = 0

    def count_checkout(*_args) -> None:
        nonlocal checkouts
        checkouts += 1

    event.listen(engine, "checkout", count_checkout)
    try:
        with TestClient(app, base_url=settings.public_origin) as client:
            live = client.get("/health/live")
            assert live.status_code == 200
            assert live.json() == {"status": "ok"}
            assert (
                client.get(
                    "/health/live",
                    headers={VERCEL_OIDC_HEADER: "malformed"},
                ).status_code
                == 200
            )

            denied = client.get("/")
            assert denied.status_code == 401
            assert denied.json() == {"code": "VERCEL_OIDC_ATTESTATION_FAILED"}
            assert checkouts == 0

            malformed = client.get(
                "/",
                headers={VERCEL_OIDC_HEADER: "not-a-jwt"},
            )
            assert malformed.status_code == 401
            assert malformed.json() == {"code": "VERCEL_OIDC_ATTESTATION_FAILED"}
            assert checkouts == 0

            token = _signed_token(rsa_private_key)
            login_shell = client.get(
                "/",
                headers={VERCEL_OIDC_HEADER: token},
            )
            assert login_shell.status_code == 200
            assert "Enter the operator token" in login_shell.text
            assert fetches == [VERCEL_OIDC_GLOBAL_ISSUER]

            protected = client.get(
                "/api/commands",
                headers={VERCEL_OIDC_HEADER: token},
            )
            assert protected.status_code == 401
            assert protected.json() == {"code": "SESSION_REQUIRED"}

            cross_scope = _signed_token(
                rsa_private_key,
                claims=_claims(overrides={"owner_id": "team_deadbeef12345678"}),
            )
            refused_scope = client.get(
                "/",
                headers={VERCEL_OIDC_HEADER: cross_scope},
            )
            assert refused_scope.status_code == 401
            assert refused_scope.json() == {"code": "VERCEL_OIDC_ATTESTATION_FAILED"}
            assert log_calls == [
                ("vercel_oidc_attestation_denied code=%s", "MISSING_HEADER"),
                ("vercel_oidc_attestation_denied code=%s", "MALFORMED_TOKEN"),
                ("vercel_oidc_attestation_denied code=%s", "BAD_TARGET"),
            ]
            assert "not-a-jwt" not in repr(log_calls)
    finally:
        event.remove(engine, "checkout", count_checkout)
        engine.dispose()


def test_duplicate_oidc_headers_are_rejected(
    rsa_private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        app_module.LOGGER,
        "warning",
        lambda *arguments: log_calls.append(arguments),
    )
    settings = _production_settings()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(
        settings,
        engine=engine,
        oidc_verifier=_verifier(rsa_private_key),
    )
    token = _signed_token(rsa_private_key)
    try:
        with TestClient(app, base_url=settings.public_origin) as client:
            response = client.get(
                "/",
                headers=[
                    (VERCEL_OIDC_HEADER, token),
                    (VERCEL_OIDC_HEADER, token),
                ],
            )
            assert response.status_code == 401
            assert response.json() == {"code": "VERCEL_OIDC_ATTESTATION_FAILED"}
            assert log_calls == [("vercel_oidc_attestation_denied code=%s", "HEADER_CARDINALITY")]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("matches_environment", "expected_code"),
    [
        (True, "BAD_TIME_TTL_ENV_FALLBACK"),
        (False, "BAD_TIME_TTL_REQUEST"),
    ],
)
def test_overlong_token_source_is_classified_without_logging_values(
    rsa_private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    matches_environment: bool,
    expected_code: str,
) -> None:
    log_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        app_module.LOGGER,
        "warning",
        lambda *arguments: log_calls.append(arguments),
    )
    token = _signed_token(
        rsa_private_key,
        claims=_claims(overrides={"exp": NOW + MAX_TOKEN_TTL_SECONDS + 1}),
    )
    monkeypatch.setenv(
        "VERCEL_OIDC_TOKEN",
        token if matches_environment else "different-runtime-token",
    )
    settings = _production_settings()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(
        settings,
        engine=engine,
        oidc_verifier=_verifier(rsa_private_key),
    )
    try:
        with TestClient(app, base_url=settings.public_origin) as client:
            response = client.get(
                "/",
                headers={VERCEL_OIDC_HEADER: token},
            )
            assert response.status_code == 401
            assert response.json() == {"code": "VERCEL_OIDC_ATTESTATION_FAILED"}
            assert log_calls == [("vercel_oidc_attestation_denied code=%s", expected_code)]
            assert token not in repr(log_calls)
    finally:
        engine.dispose()


def test_verification_errors_expose_only_bounded_diagnostic_codes() -> None:
    error = VercelOidcVerificationError("never log this token-like detail")

    assert error.code is VercelOidcDenialCode.MALFORMED_TOKEN
    assert frozenset(code.value for code in VercelOidcDenialCode) == {
        "BAD_ISSUER",
        "BAD_SIGNATURE",
        "BAD_TARGET",
        "BAD_TIME_AGE",
        "BAD_TIME_EXPIRED",
        "BAD_TIME_IAT",
        "BAD_TIME_NBF",
        "BAD_TIME_ORDERING",
        "BAD_TIME_TTL",
        "BAD_TIME_TTL_ENV_FALLBACK",
        "BAD_TIME_TTL_REQUEST",
        "HEADER_CARDINALITY",
        "JWKS_UNAVAILABLE",
        "MALFORMED_TOKEN",
        "MISSING_HEADER",
        "VERIFIER_UNAVAILABLE",
    }


@pytest.mark.parametrize(
    "raw_claims",
    [
        '{"too_large":' + ("9" * 5_000) + "}",
        '{"too_deep":'
        + ("[" * MAX_JSON_NESTING_DEPTH)
        + "0"
        + ("]" * MAX_JSON_NESTING_DEPTH)
        + "}",
        '{"not_finite":1e9999}',
    ],
)
def test_malformed_json_edges_return_generic_denial_before_jwks_or_database(
    rsa_private_key: RSAPrivateKey,
    raw_claims: str,
) -> None:
    settings = _production_settings()
    fetches: list[str] = []
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(
        settings,
        engine=engine,
        oidc_verifier=_verifier(rsa_private_key, fetches=fetches),
    )
    checkouts = 0

    def count_checkout(*_args) -> None:
        nonlocal checkouts
        checkouts += 1

    event.listen(engine, "checkout", count_checkout)
    try:
        with TestClient(app, base_url=settings.public_origin) as client:
            response = client.get(
                "/",
                headers={VERCEL_OIDC_HEADER: _raw_claims_token(raw_claims)},
            )
            assert response.status_code == 401
            assert response.json() == {"code": "VERCEL_OIDC_ATTESTATION_FAILED"}
            assert fetches == []
            assert checkouts == 0
    finally:
        event.remove(engine, "checkout", count_checkout)
        engine.dispose()


def test_test_and_local_settings_do_not_require_vercel_oidc(
    settings: Settings,
    client: TestClient,
) -> None:
    assert settings.requires_vercel_oidc is False
    response = client.get("/")
    assert response.status_code == 200
    assert "Enter the operator token" in response.text
