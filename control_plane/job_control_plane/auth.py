"""Opaque operator sessions with strict CSRF and same-origin enforcement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import OperatorSession

SESSION_COOKIE = "jaa_control_session"
CSRF_COOKIE = "jaa_control_csrf"
CSRF_HEADER = "x-csrf-token"


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def secret_digest(secret: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_operator_token(settings: Settings, candidate: str) -> bool:
    return hmac.compare_digest(
        secret_digest(settings.operator_token, candidate),
        secret_digest(settings.operator_token, settings.operator_token),
    )


def _authority(value: str, *, scheme: str | None = None) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        hostname = (parsed.hostname or "").lower()
        effective_scheme = parsed.scheme or scheme or ""
        port = parsed.port or (443 if effective_scheme == "https" else 80)
    except (UnicodeError, ValueError):
        return None
    return (hostname, port) if hostname else None


def require_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin", "")
    allowed = any(hmac.compare_digest(origin, value) for value in settings.operator_origins)
    origin_authority = _authority(origin)
    request_authority = _authority(
        request.headers.get("host", ""),
        scheme=urlsplit(origin).scheme,
    )
    if not allowed or origin_authority is None or origin_authority != request_authority:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ORIGIN_DENIED")


def create_operator_session(
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> tuple[OperatorSession, str, str]:
    created_at = (now or datetime.now(UTC)).astimezone(UTC)
    session_token = _token()
    csrf_token = _token()
    row = OperatorSession(
        session_token_digest=secret_digest(settings.session_secret, session_token),
        csrf_token_digest=secret_digest(settings.csrf_secret, csrf_token),
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=settings.session_ttl_seconds),
        last_seen_at=created_at,
    )
    db.add(row)
    db.flush()
    return row, session_token, csrf_token


def set_session_cookies(
    response: Response,
    settings: Settings,
    *,
    session_token: str,
    csrf_token: str,
) -> None:
    common = {
        "secure": settings.secure_cookies,
        "samesite": "strict",
        "path": "/",
        "max_age": settings.session_ttl_seconds,
    }
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        httponly=False,
        **common,
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        secure=settings.secure_cookies,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(
        CSRF_COOKIE,
        secure=settings.secure_cookies,
        httponly=False,
        samesite="strict",
        path="/",
    )


def load_operator_session(
    request: Request,
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> OperatorSession:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SESSION_REQUIRED")
    digest = secret_digest(settings.session_secret, token)
    row = db.scalar(select(OperatorSession).where(OperatorSession.session_token_digest == digest))
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    if row is None or row.revoked_at is not None or _as_utc(row.expires_at) <= checked_at:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SESSION_INVALID")
    row.last_seen_at = checked_at
    return row


def require_csrf(
    request: Request,
    session: OperatorSession,
    settings: Settings,
) -> None:
    require_origin(request, settings)
    header_token = request.headers.get(CSRF_HEADER, "")
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not header_token or not cookie_token or not hmac.compare_digest(header_token, cookie_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF_DENIED")
    actual = secret_digest(settings.csrf_secret, header_token)
    if not hmac.compare_digest(actual, session.csrf_token_digest):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF_DENIED")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "clear_session_cookies",
    "create_operator_session",
    "load_operator_session",
    "require_csrf",
    "require_origin",
    "secret_digest",
    "set_session_cookies",
    "verify_operator_token",
]
