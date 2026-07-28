from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from job_control_plane.app import create_app
from job_control_plane.config import Settings
from job_control_plane.models import OperatorSession


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
