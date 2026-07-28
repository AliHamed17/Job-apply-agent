from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from job_control_plane import services
from job_control_plane.app import create_app
from job_control_plane.auth import (
    INVALID_LOGIN_LIMIT,
    INVALID_LOGIN_WINDOW_SECONDS,
    SESSION_COOKIE,
    InvalidLoginLimiter,
)
from job_control_plane.config import Settings
from job_control_plane.models import OperatorAudit, OperatorSession
from job_control_plane.services import (
    OPERATOR_AUDIT_RETENTION,
    audit,
)


def test_only_liveness_and_login_bootstrap_are_usable_without_session(
    client: TestClient,
) -> None:
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert live.headers["cache-control"] == "no-store, max-age=0"
    assert live.headers["x-frame-options"] == "DENY"

    root = client.get("/")
    assert root.status_code == 200
    assert "Enter the operator token" in root.text
    assert "Send application" not in root.text

    assert client.get("/health/ready").status_code == 401
    assert client.get("/api/commands").status_code == 401
    assert client.get("/api/review-grants").status_code == 401
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_login_requires_exact_origin_and_sets_hardened_session(
    client: TestClient,
    settings: Settings,
) -> None:
    body = {"token": settings.operator_token}
    assert client.post("/auth/login", json=body).status_code == 403
    assert (
        client.post(
            "/auth/login",
            headers={"origin": "http://attacker.invalid"},
            json=body,
        ).status_code
        == 403
    )
    invalid = client.post(
        "/auth/login",
        headers={"origin": settings.public_origin},
        json={"token": "x" * 32},
    )
    assert invalid.status_code == 401
    assert settings.operator_token not in invalid.text

    response = client.post(
        "/auth/login",
        headers={"origin": settings.public_origin},
        json=body,
    )
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookies if "jaa_control_session=" in value)
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie
    opaque_value = session_cookie.split(";", 1)[0].split("=", 1)[1]
    token, proof = opaque_value.split(".")
    assert len(token) == 43
    assert len(proof) == 64
    assert settings.operator_token not in response.text

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Redacted command metadata only" in dashboard.text
    assert "Waiting for verification" in dashboard.text
    assert "last?.outcome === 'confirmed_submitted'" in dashboard.text
    assert "last?.evidence_type" in dashboard.text
    assert "last?.signature_verified === true" in dashboard.text
    assert "'confirmed'" in dashboard.text
    assert "Application not employer-confirmed" in dashboard.text


def test_invalid_login_uses_a_fixed_local_bucket_without_audit_rows(
    client: TestClient,
    settings: Settings,
) -> None:
    headers = {"origin": settings.public_origin}
    for _ in range(INVALID_LOGIN_LIMIT):
        response = client.post(
            "/auth/login",
            headers=headers,
            json={"token": "invalid-" + ("x" * 32)},
        )
        assert response.status_code == 401
        assert response.json() == {"code": "TOKEN_INVALID"}

    limited = client.post(
        "/auth/login",
        headers=headers,
        json={"token": "invalid-" + ("y" * 32)},
    )
    assert limited.status_code == 429
    assert limited.json() == {"code": "LOGIN_RATE_LIMITED"}
    repeated = client.post(
        "/auth/login",
        headers=headers,
        json={"token": "invalid-" + ("z" * 32)},
    )
    assert repeated.status_code == 429
    assert repeated.json() == {"code": "LOGIN_RATE_LIMITED"}

    factory = client.app.state.sessions
    with factory() as db:
        denied_audits = list(
            db.scalars(
                select(OperatorAudit).where(
                    OperatorAudit.action == "login",
                    OperatorAudit.result == "denied",
                )
            )
        )
        assert denied_audits == []
        assert db.scalar(select(OperatorSession)) is None


def test_valid_operator_token_bypasses_saturated_invalid_login_limiter(
    client: TestClient,
    settings: Settings,
) -> None:
    headers = {"origin": settings.public_origin}
    for _ in range(INVALID_LOGIN_LIMIT + 2):
        client.post(
            "/auth/login",
            headers=headers,
            json={"token": "not-the-token-" + ("q" * 32)},
        )

    valid = client.post(
        "/auth/login",
        headers=headers,
        json={"token": settings.operator_token},
    )
    assert valid.status_code == 200
    assert valid.json()["authenticated"] is True


def test_invalid_tokens_and_cookies_never_checkout_a_database_connection(
    client: TestClient,
    settings: Settings,
) -> None:
    engine = client.app.state.engine
    checkouts = 0

    def count_checkout(*_args) -> None:
        nonlocal checkouts
        checkouts += 1

    event.listen(engine, "checkout", count_checkout)
    try:
        headers = {"origin": settings.public_origin}
        for _ in range(INVALID_LOGIN_LIMIT + 4):
            response = client.post(
                "/auth/login",
                headers=headers,
                json={"token": "invalid-" + ("x" * 32)},
            )
            assert response.status_code in {401, 429}

        root_without_cookie = client.get("/")
        protected_without_cookie = client.get("/api/commands")
        assert root_without_cookie.status_code == 200
        assert "Enter the operator token" in root_without_cookie.text
        assert protected_without_cookie.status_code == 401

        for cookie in ("malformed", f"{'A' * 43}.{'0' * 64}"):
            client.cookies.set(SESSION_COOKIE, cookie)
            root = client.get("/")
            protected = client.get("/api/commands")
            assert root.status_code == 200
            assert "Enter the operator token" in root.text
            assert protected.status_code == 401
            assert protected.json()["code"] in {"SESSION_REQUIRED", "SESSION_INVALID"}
        client.cookies.delete(SESSION_COOKIE)
        assert checkouts == 0

        valid = client.post(
            "/auth/login",
            headers=headers,
            json={"token": settings.operator_token},
        )
        assert valid.status_code == 200
        assert checkouts > 0
    finally:
        event.remove(engine, "checkout", count_checkout)


def test_operator_audit_retention_and_hard_cap_are_enforced(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(services, "OPERATOR_AUDIT_HARD_CAP", 3)
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    factory = client.app.state.sessions
    with factory.begin() as db:
        for index in range(5):
            audit(
                db,
                action="test",
                result="accepted",
                request_digest=f"{index:064x}",
                now=started_at + timedelta(seconds=index),
            )

    with factory() as db:
        retained = list(
            db.scalars(
                select(OperatorAudit).order_by(
                    OperatorAudit.created_at,
                    OperatorAudit.id,
                )
            )
        )
        assert [row.request_digest for row in retained] == [
            f"{index:064x}" for index in range(2, 5)
        ]

    after_retention = started_at + OPERATOR_AUDIT_RETENTION + timedelta(minutes=1)
    with factory.begin() as db:
        audit(
            db,
            action="test",
            result="accepted",
            request_digest="f" * 64,
            now=after_retention,
        )
    with factory() as db:
        retained = list(db.scalars(select(OperatorAudit)))
        assert [row.request_digest for row in retained] == ["f" * 64]


def test_invalid_login_limiter_reopens_after_the_fixed_window() -> None:
    clock = [100.0]
    limiter = InvalidLoginLimiter(
        limit=2,
        window_seconds=INVALID_LOGIN_WINDOW_SECONDS,
        clock=lambda: clock[0],
    )
    assert limiter.record_denial() is False
    assert limiter.record_denial() is False
    assert limiter.record_denial() is True
    clock[0] += INVALID_LOGIN_WINDOW_SECONDS
    assert limiter.record_denial() is False


def test_exact_staged_deployment_host_allows_liveness_but_not_operator_access(
    client: TestClient,
    settings: Settings,
) -> None:
    deployment_origin = "https://job-agent-staged-a1b2c3.vercel.app"
    staged_settings = replace(settings, deployment_origin=deployment_origin)
    staged_app = create_app(staged_settings, engine=client.app.state.engine)

    with TestClient(staged_app, base_url=deployment_origin) as staged:
        assert staged.get("/health/live").status_code == 200
        final_origin_on_staged_host = staged.post(
            "/auth/login",
            headers={"origin": settings.public_origin},
            json={"token": settings.operator_token},
        )
        assert final_origin_on_staged_host.status_code == 403
        staged_origin = staged.post(
            "/auth/login",
            headers={"origin": deployment_origin},
            json={"token": settings.operator_token},
        )
        assert staged_origin.status_code == 403

    untrusted = TestClient(staged_app, base_url="https://other.vercel.app")
    assert untrusted.get("/health/live").status_code == 400


def test_operator_mutations_require_csrf_cookie_header_and_origin(
    client: TestClient,
    settings: Settings,
    authenticated: str,
) -> None:
    body = {
        "grant_id": "00000000-0000-0000-0000-000000000001",
        "application_ref": "00000000-0000-0000-0000-000000000002",
        "application_revision": 1,
        "form_fingerprint_digest": "a" * 64,
        "acknowledgement": "SEND_APPLICATION",
        "client_idempotency_key": "00000000-0000-0000-0000-000000000003",
    }
    assert client.post("/api/send", json=body).status_code == 403
    assert (
        client.post(
            "/api/send",
            headers={"origin": "http://attacker.invalid", "x-csrf-token": authenticated},
            json=body,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/send",
            headers={"origin": settings.public_origin, "x-csrf-token": "wrong"},
            json=body,
        ).status_code
        == 403
    )
    valid_csrf = client.post(
        "/api/send",
        headers={
            "origin": settings.public_origin,
            "x-csrf-token": authenticated,
        },
        json=body,
    )
    assert valid_csrf.status_code == 404
    assert valid_csrf.json() == {"code": "REVIEW_GRANT_NOT_FOUND"}


def test_logout_revokes_session(
    client: TestClient,
    settings: Settings,
    authenticated: str,
) -> None:
    response = client.post(
        "/auth/logout",
        headers={
            "origin": settings.public_origin,
            "x-csrf-token": authenticated,
        },
    )
    assert response.status_code == 200
    assert client.get("/api/commands").status_code == 401


def test_expired_server_session_is_rejected(
    client: TestClient,
    authenticated: str,
) -> None:
    factory = client.app.state.sessions
    with factory.begin() as db:
        row = db.scalar(select(OperatorSession))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/commands").status_code == 401
