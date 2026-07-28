"""Opaque operator sessions with strict CSRF and same-origin enforcement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import OperatorSession

SESSION_COOKIE = "jaa_control_session"
CSRF_COOKIE = "jaa_control_csrf"
CSRF_HEADER = "x-csrf-token"
INVALID_LOGIN_LIMIT = 8
INVALID_LOGIN_WINDOW_SECONDS = 300.0
_SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SESSION_PROOF_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class InvalidLoginLimiter:
    """One fixed-size, process-local denial bucket with no request keys."""

    def __init__(
        self,
        *,
        limit: int = INVALID_LOGIN_LIMIT,
        window_seconds: float = INVALID_LOGIN_WINDOW_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("invalid login limiter bounds")
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._denials: deque[float] = deque(maxlen=limit)
        self._lock = Lock()

    def record_denial(self) -> bool:
        with self._lock:
            now = self._clock()
            cutoff = now - self._window_seconds
            while self._denials and self._denials[0] <= cutoff:
                self._denials.popleft()
            if len(self._denials) >= self._limit:
                return True
            self._denials.append(now)
            return False


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def secret_digest(secret: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_operator_token(settings: Settings, candidate: str) -> bool:
    return hmac.compare_digest(
        secret_digest(settings.operator_token, candidate),
        secret_digest(settings.operator_token, settings.operator_token),
    )


def _session_record_digest(settings: Settings, token: str) -> str:
    return secret_digest(settings.session_secret, f"session-record:{token}")


def _session_cookie_value(settings: Settings, token: str) -> str:
    proof = secret_digest(settings.session_secret, f"session-cookie:{token}")
    return f"{token}.{proof}"


def _verified_session_digest(request: Request, settings: Settings) -> str:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    parts = cookie.split(".")
    if (
        len(parts) != 2
        or _SESSION_TOKEN_PATTERN.fullmatch(parts[0]) is None
        or _SESSION_PROOF_PATTERN.fullmatch(parts[1]) is None
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SESSION_REQUIRED")
    token, supplied_proof = parts
    expected_proof = secret_digest(settings.session_secret, f"session-cookie:{token}")
    if not hmac.compare_digest(supplied_proof, expected_proof):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SESSION_INVALID")
    return _session_record_digest(settings, token)


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
        session_token_digest=_session_record_digest(settings, session_token),
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
        _session_cookie_value(settings, session_token),
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
    digest = _verified_session_digest(request, settings)
    row = db.scalar(select(OperatorSession).where(OperatorSession.session_token_digest == digest))
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    if row is None or row.revoked_at is not None or _as_utc(row.expires_at) <= checked_at:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SESSION_INVALID")
    return row


def touch_operator_session(
    session: OperatorSession,
    *,
    now: datetime | None = None,
) -> None:
    session.last_seen_at = (now or datetime.now(UTC)).astimezone(UTC)


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
    "INVALID_LOGIN_LIMIT",
    "INVALID_LOGIN_WINDOW_SECONDS",
    "InvalidLoginLimiter",
    "SESSION_COOKIE",
    "clear_session_cookies",
    "create_operator_session",
    "load_operator_session",
    "require_csrf",
    "require_origin",
    "secret_digest",
    "set_session_cookies",
    "touch_operator_session",
    "verify_operator_token",
]
