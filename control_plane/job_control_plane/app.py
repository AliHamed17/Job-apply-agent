"""FastAPI surface for the isolated, redacted Vercel control plane."""

import hashlib
import html
from collections.abc import Generator
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import (
    InvalidLoginLimiter,
    clear_session_cookies,
    create_operator_session,
    load_operator_session,
    require_csrf,
    require_origin,
    secret_digest,
    set_session_cookies,
    touch_operator_session,
    verify_operator_token,
)
from .config import Settings
from .db import (
    EXPECTED_SCHEMA_REVISION,
    Base,
    build_engine,
    build_session_factory,
    current_revision,
    database_is_responsive,
)
from .models import OperatorSession, ReviewGrant, RunnerDevice, SubmissionCommand
from .protocol import (
    PROTOCOL_VERSION,
    CommandAckEnvelope,
    CommandPollEnvelope,
    ControlCommandEnvelope,
    HeartbeatEnvelope,
    ReviewGrantEnvelope,
    ReviewGrantRevocationEnvelope,
    RunnerEventEnvelope,
)
from .services import (
    ControlPlaneError,
    acknowledge_command,
    as_utc,
    audit,
    create_command,
    poll_command,
    receive_heartbeat,
    receive_review_grant,
    receive_review_grant_revocation,
    receive_runner_event,
    require_current_schema,
    sha256_bytes,
    utc_now,
)
from .vercel_oidc import (
    VERCEL_OIDC_HEADER,
    VercelOidcVerificationError,
    VercelOidcVerifier,
)

CURRENT_SCHEMA_REVISION = EXPECTED_SCHEMA_REVISION


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(ApiModel):
    token: Annotated[str, StringConstraints(min_length=32, max_length=512)]


class LoginResponse(ApiModel):
    authenticated: Literal[True] = True
    csrf_token: str
    expires_at: datetime


class SendCommandRequest(ApiModel):
    grant_id: UUID
    application_ref: UUID
    application_revision: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    form_fingerprint_digest: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]
    acknowledgement: Literal["SEND_APPLICATION"]
    client_idempotency_key: UUID


class SendCommandResponse(ApiModel):
    command_id: UUID
    status: Literal[
        "queued",
        "claimed",
        "acknowledged",
        "running",
        "rejected",
        "finished",
    ]
    verified: Literal[False] = False
    duplicate: bool
    status_url: Annotated[
        str,
        StringConstraints(pattern=r"^/api/commands/[0-9a-f-]{36}$"),
    ]


class HeartbeatReceipt(ApiModel):
    accepted: Literal[True] = True
    device_id: UUID


class ReviewGrantReceipt(ApiModel):
    accepted: Literal[True] = True
    grant_id: UUID
    duplicate: bool


class CommandPollResponse(ApiModel):
    commands: Annotated[list[ControlCommandEnvelope], Field(max_length=1)]


class CommandAckReceipt(ApiModel):
    accepted: Literal[True] = True
    command_id: UUID
    duplicate: bool


class RunnerEventReceipt(ApiModel):
    accepted: Literal[True] = True
    event_id: UUID
    duplicate: bool


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    initialize_schema: bool = False,
    oidc_verifier: VercelOidcVerifier | None = None,
) -> FastAPI:
    runtime = settings or Settings.from_env()
    database = engine or build_engine(runtime)
    sessions = build_session_factory(database)
    request_identity_verifier = oidc_verifier
    if runtime.requires_vercel_oidc and request_identity_verifier is None:
        identity_target = runtime.vercel_identity_target
        if identity_target is None:
            raise RuntimeError("Vercel runtime identity target is unavailable")
        request_identity_verifier = VercelOidcVerifier(identity_target)
    if initialize_schema:
        Base.metadata.create_all(database)

    app = FastAPI(
        title="Job Apply Control Plane",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = runtime
    app.state.engine = database
    app.state.sessions = sessions
    app.state.vercel_oidc_verifier = request_identity_verifier
    invalid_login_limiter = InvalidLoginLimiter()
    app.state.invalid_login_limiter = invalid_login_limiter

    allowed_hosts = [
        host
        for origin in runtime.trusted_origins
        if (host := urlsplit(origin).hostname) is not None
    ]
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        public_liveness = request.url.path == "/health/live" and request.method in {
            "GET",
            "HEAD",
        }
        content_length = request.headers.get("content-length")
        request_too_large = bool(
            content_length and content_length.isdigit() and int(content_length) > 65_536
        )
        oidc_denied = False
        if runtime.requires_vercel_oidc and not public_liveness:
            oidc_headers = request.headers.getlist(VERCEL_OIDC_HEADER)
            if len(oidc_headers) != 1 or request_identity_verifier is None:
                oidc_denied = True
            else:
                try:
                    await run_in_threadpool(
                        request_identity_verifier.verify,
                        oidc_headers[0],
                    )
                except VercelOidcVerificationError:
                    oidc_denied = True
        response: Response
        if oidc_denied:
            response = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"code": "VERCEL_OIDC_ATTESTATION_FAILED"},
            )
        elif request_too_large:
            response = JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"code": "REQUEST_TOO_LARGE"},
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if runtime.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    def get_db() -> Generator[Session, None, None]:
        db = sessions()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    db_dep = Annotated[Session, Depends(get_db)]

    def operator_session(request: Request, db: db_dep) -> OperatorSession:
        operator = load_operator_session(request, db, runtime)
        require_current_schema(db)
        touch_operator_session(operator)
        return operator

    operator_dep = Annotated[OperatorSession, Depends(operator_session)]

    @app.exception_handler(ControlPlaneError)
    async def control_error(_request: Request, exc: ControlPlaneError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"code": "REQUEST_INVALID"})

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "REQUEST_DENIED"
        return JSONResponse(status_code=exc.status_code, content={"code": detail})

    @app.get("/health/live", include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/auth/login", response_model=LoginResponse, include_in_schema=False)
    def login(request: Request, body: LoginRequest, db: db_dep) -> Response:
        require_origin(request, runtime)
        token_valid = verify_operator_token(runtime, body.token)
        if not token_valid:
            if invalid_login_limiter.record_denial():
                raise HTTPException(status_code=429, detail="LOGIN_RATE_LIMITED")
            raise HTTPException(status_code=401, detail="TOKEN_INVALID")
        require_current_schema(db)
        request_digest = secret_digest(runtime.operator_token, "operator-login")
        row, session_token, csrf_token = create_operator_session(db, runtime)
        audit(
            db,
            action="login",
            result="accepted",
            request_digest=request_digest,
            target_type="session",
        )
        response = JSONResponse(
            content=LoginResponse(
                csrf_token=csrf_token,
                expires_at=row.expires_at,
            ).model_dump(mode="json")
        )
        set_session_cookies(
            response,
            runtime,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        return response

    @app.post("/auth/logout", include_in_schema=False)
    def logout(
        request: Request,
        db: db_dep,
        operator: operator_dep,
    ) -> Response:
        require_csrf(request, operator, runtime)
        operator.revoked_at = utc_now()
        audit(
            db,
            action="logout",
            result="accepted",
            request_digest=sha256_bytes(b"operator-logout"),
            target_type="session",
        )
        response = JSONResponse(content={"revoked": True})
        clear_session_cookies(response, runtime)
        return response

    @app.get("/health/ready", include_in_schema=False)
    def readiness(db: db_dep, _operator: operator_dep) -> Response:
        now = utc_now()
        device = db.get(RunnerDevice, str(runtime.runner_device_id))
        migration_current = current_revision(database) == CURRENT_SCHEMA_REVISION
        runner_online = bool(
            device
            and device.active
            and device.status == "ready"
            and device.last_seen_at
            and now - as_utc(device.last_seen_at)
            <= timedelta(seconds=runtime.runner_offline_seconds)
        )
        checks = {
            "database": database_is_responsive(database),
            "migration": migration_current,
            "runner": runner_online,
            "dispatch": runtime.dispatch_allowed,
        }
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "degraded",
                "checks": checks,
                "protocol_version": PROTOCOL_VERSION,
            },
        )

    @app.get("/api/review-grants", include_in_schema=False)
    def list_grants(db: db_dep, _operator: operator_dep) -> dict[str, list[dict[str, object]]]:
        now = utc_now()
        configured_device = db.get(RunnerDevice, str(runtime.runner_device_id))
        rows = db.scalars(
            select(ReviewGrant)
            .where(ReviewGrant.expires_at > now)
            .order_by(ReviewGrant.created_at.desc())
            .limit(100)
        ).all()
        grants: list[dict[str, object]] = []
        for row in rows:
            eligibility_state = _review_grant_state(
                row,
                configured_device=configured_device,
                settings=runtime,
                now=now,
            )
            grants.append(
                {
                    "grant_id": row.id,
                    "application_ref": row.application_ref,
                    "application_revision": row.application_revision,
                    "adapter": row.adapter,
                    "adapter_version": row.adapter_version,
                    "form_fingerprint_digest": row.form_fingerprint_digest,
                    "expires_at": row.expires_at,
                    "revoked_at": row.revoked_at,
                    "eligibility_state": eligibility_state,
                    "eligible": eligibility_state == "eligible",
                }
            )
        return {"grants": grants}

    @app.get("/api/commands", include_in_schema=False)
    def list_commands(db: db_dep, _operator: operator_dep) -> dict[str, list[dict[str, object]]]:
        rows = db.scalars(
            select(SubmissionCommand).order_by(SubmissionCommand.created_at.desc()).limit(100)
        ).all()
        return {"commands": [_command_view(row) for row in rows]}

    @app.get("/api/commands/{command_id}", include_in_schema=False)
    def command_status(command_id: UUID, db: db_dep, _operator: operator_dep) -> dict[str, object]:
        row = db.get(SubmissionCommand, str(command_id))
        if row is None:
            raise HTTPException(status_code=404, detail="COMMAND_NOT_FOUND")
        result = _command_view(row)
        result["events"] = [
            {
                "event_id": event.id,
                "sequence": event.sequence,
                "stage": event.stage,
                "outcome": event.outcome,
                "reason_code": event.reason_code,
                "evidence_type": event.evidence_type,
                "occurred_at": event.occurred_at,
                "signature_verified": True,
            }
            for event in sorted(row.events, key=lambda item: item.received_at)
        ]
        return result

    @app.post(
        "/api/send",
        status_code=202,
        response_model=SendCommandResponse,
        include_in_schema=False,
    )
    def send_application(
        request: Request,
        body: SendCommandRequest,
        db: db_dep,
        operator: operator_dep,
    ) -> dict[str, object]:
        require_csrf(request, operator, runtime)
        request_digest = hashlib.sha256(body.model_dump_json().encode("utf-8")).hexdigest()
        try:
            result = create_command(
                db,
                runtime,
                grant_id=body.grant_id,
                application_ref=body.application_ref,
                application_revision=body.application_revision,
                form_fingerprint_digest=body.form_fingerprint_digest,
                client_idempotency_key=body.client_idempotency_key,
            )
        except ControlPlaneError as exc:
            audit(
                db,
                action="send",
                result="denied",
                request_digest=request_digest,
                target_type="grant",
                target_id=str(body.grant_id),
            )
            db.commit()
            raise exc
        audit(
            db,
            action="send",
            result="accepted",
            request_digest=request_digest,
            target_type="command",
            target_id=result.command.id,
        )
        return {
            "command_id": result.command.id,
            "status": result.command.status,
            "verified": False,
            "duplicate": result.duplicate,
            "status_url": f"/api/commands/{result.command.id}",
        }

    @app.post(
        "/api/runner/heartbeat",
        response_model=HeartbeatReceipt,
        include_in_schema=False,
    )
    def runner_heartbeat(body: HeartbeatEnvelope, db: db_dep) -> dict[str, object]:
        receipt = receive_heartbeat(db, runtime, body)
        return {"accepted": True, "device_id": receipt.identifier}

    @app.post(
        "/api/runner/review-grants",
        response_model=ReviewGrantReceipt,
        include_in_schema=False,
    )
    def runner_review_grant(body: ReviewGrantEnvelope, db: db_dep) -> dict[str, object]:
        receipt = receive_review_grant(db, runtime, body)
        return {
            "accepted": True,
            "grant_id": receipt.identifier,
            "duplicate": receipt.duplicate,
        }

    @app.post(
        "/api/runner/review-grant-revocations",
        response_model=ReviewGrantReceipt,
        include_in_schema=False,
    )
    def runner_review_grant_revocation(
        body: ReviewGrantRevocationEnvelope,
        db: db_dep,
    ) -> dict[str, object]:
        receipt = receive_review_grant_revocation(db, runtime, body)
        return {
            "accepted": True,
            "grant_id": receipt.identifier,
            "duplicate": receipt.duplicate,
        }

    @app.post(
        "/api/runner/commands/poll",
        response_model=CommandPollResponse,
        include_in_schema=False,
    )
    def runner_poll(body: CommandPollEnvelope, db: db_dep) -> dict[str, object]:
        commands = poll_command(db, runtime, body)
        return {
            "commands": [command.model_dump(mode="json") for command in commands],
        }

    @app.post(
        "/api/runner/commands/{command_id}/ack",
        response_model=CommandAckReceipt,
        include_in_schema=False,
    )
    def runner_ack(
        command_id: UUID,
        body: CommandAckEnvelope,
        db: db_dep,
    ) -> dict[str, object]:
        receipt = acknowledge_command(
            db,
            runtime,
            body,
            path_command_id=command_id,
        )
        return {
            "accepted": True,
            "command_id": receipt.identifier,
            "duplicate": receipt.duplicate,
        }

    @app.post(
        "/api/runner/events",
        response_model=RunnerEventReceipt,
        include_in_schema=False,
    )
    def runner_event(body: RunnerEventEnvelope, db: db_dep) -> dict[str, object]:
        receipt = receive_runner_event(db, runtime, body)
        return {
            "accepted": True,
            "event_id": receipt.identifier,
            "duplicate": receipt.duplicate,
        }

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, db: db_dep) -> HTMLResponse:
        try:
            operator = load_operator_session(request, db, runtime)
        except HTTPException:
            return HTMLResponse(_login_html())
        require_current_schema(db)
        touch_operator_session(operator)
        now = utc_now()
        grants = db.scalars(
            select(ReviewGrant).order_by(ReviewGrant.created_at.desc()).limit(50)
        ).all()
        commands = db.scalars(
            select(SubmissionCommand).order_by(SubmissionCommand.created_at.desc()).limit(50)
        ).all()
        configured_device = db.get(RunnerDevice, str(runtime.runner_device_id))
        grant_states = {
            row.id: _review_grant_state(
                row,
                configured_device=configured_device,
                settings=runtime,
                now=now,
            )
            for row in grants
        }
        return HTMLResponse(
            _dashboard_html(
                grants=grants,
                commands=commands,
                grant_states=grant_states,
            )
        )

    return app


def _login_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Job Apply Control Plane</title>
<style>
body{font:16px system-ui;display:grid;min-height:90vh;place-items:center;color:#172033}
main{width:min(92vw,420px);padding:2rem;border:1px solid #d8deea;border-radius:12px}
label,input,button{display:block;width:100%;box-sizing:border-box} input,button{padding:.7rem}
button{margin-top:1rem} #result{min-height:1.5rem;color:#a11}
</style></head><body><main>
<h1>Private control plane</h1>
<p>Enter the operator token to start a short, secure browser session.</p>
<form id="login"><label>Operator token<input id="token" type="password"
autocomplete="current-password" minlength="32" required></label>
<button type="submit">Sign in</button></form><p id="result" role="alert"></p>
</main><script>
document.getElementById('login').addEventListener('submit', async (event) => {
  event.preventDefault();
  const token = document.getElementById('token');
  const response = await fetch('/auth/login', {
    method:'POST', headers:{'content-type':'application/json'},
    body:JSON.stringify({token:token.value})
  });
  token.value = '';
  if (response.ok) location.replace('/');
  else document.getElementById('result').textContent = 'Sign-in was not accepted.';
});
</script></body></html>"""


def _command_view(row: SubmissionCommand) -> dict[str, object]:
    effective_status = row.status
    if effective_status in {"queued", "claimed"} and as_utc(row.expires_at) <= utc_now():
        effective_status = "expired"
    return {
        "command_id": row.id,
        "grant_id": row.grant_id,
        "application_ref": row.application_ref,
        "application_revision": row.application_revision,
        "adapter": row.adapter,
        "adapter_version": row.adapter_version,
        "status": effective_status,
        "delivery_count": row.delivery_count,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "ack_status": row.ack_status,
        "finished_at": row.finished_at,
    }


def _review_grant_state(
    grant: ReviewGrant,
    *,
    configured_device: RunnerDevice | None,
    settings: Settings,
    now: datetime,
) -> str:
    """Return the display/send state without mutating restored grant history."""

    if grant.revoked_at is not None:
        return "revoked"
    if grant.consumed_at is not None:
        return "used"
    if as_utc(grant.expires_at) <= now:
        return "expired"
    if not settings.dispatch_allowed:
        return "dispatch_disabled"
    if (
        configured_device is None
        or configured_device.id != str(settings.runner_device_id)
        or grant.device_id != configured_device.id
        or not configured_device.active
    ):
        return "runner_disabled"
    if (
        configured_device.status != "ready"
        or not configured_device.boot_id
        or configured_device.last_seen_at is None
        or now - as_utc(configured_device.last_seen_at)
        > timedelta(seconds=settings.runner_offline_seconds)
    ):
        return "runner_offline"
    return "eligible"


def _dashboard_html(
    *,
    grants: list[ReviewGrant],
    commands: list[SubmissionCommand],
    grant_states: dict[str, str],
) -> str:
    grant_rows_parts: list[str] = []
    for row in grants:
        state = grant_states[row.id]
        grant_rows_parts.append(
            "<tr>"
            f"<td><code>{html.escape(row.id)}</code></td>"
            f"<td>{html.escape(row.adapter)} {html.escape(row.adapter_version)}</td>"
            f"<td><code>{html.escape(row.application_ref)}</code> r{row.application_revision}</td>"
            f"<td>{state}</td>"
            "<td>"
            + (
                (
                    "<button class='send' "
                    f"data-grant='{html.escape(row.id)}' "
                    f"data-ref='{html.escape(row.application_ref)}' "
                    f"data-revision='{row.application_revision}' "
                    f"data-fingerprint='{html.escape(row.form_fingerprint_digest)}'>"
                    "Send application</button>"
                )
                if state == "eligible"
                else ""
            )
            + "</td></tr>"
        )
    grant_rows = "".join(grant_rows_parts)
    command_rows = "".join(
        (
            "<tr>"
            f"<td><code>{html.escape(row.id)}</code></td>"
            f"<td>{html.escape(row.adapter)}</td>"
            f"<td>{html.escape(row.status)}</td>"
            f"<td>{html.escape(row.created_at.isoformat())}</td>"
            "</tr>"
        )
        for row in commands
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Apply Control Plane</title>
<style>
body{{font:15px system-ui;margin:2rem;max-width:1100px;color:#172033}}
h1,h2{{margin-top:1.5rem}} table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #d8deea;padding:.65rem;text-align:left}}
code{{font-size:.8rem}} button{{padding:.45rem .7rem}} #result{{min-height:1.5rem}}
#result.neutral{{color:#2459a9}} #result.warning{{color:#9a4f00}}
#result.confirmed{{color:#08783e;font-weight:650}}
</style></head><body>
<h1>Job Apply Control Plane</h1>
<p>Redacted command metadata only. Private application content stays on the runner.</p>
<p id="result" role="status"></p>
<h2>Local review grants</h2>
<table><thead><tr><th>Grant</th><th>Adapter</th><th>Opaque application</th>
<th>State</th><th>Explicit action</th></tr></thead><tbody>{grant_rows}</tbody></table>
<h2>Commands</h2>
<table><thead><tr><th>Command</th><th>Adapter</th><th>State</th><th>Created</th>
</tr></thead><tbody>{command_rows}</tbody></table>
<script>
const cookie = (name) => document.cookie.split('; ').find(v => v.startsWith(name + '='))
  ?.split('=').slice(1).join('=') || '';
const result = document.getElementById('result');
const terminalStatuses = new Set(['finished', 'rejected', 'expired']);
const show = (text, kind) => {{ result.textContent = text; result.className = kind; }};
const pollCommand = async (statusUrl, remaining = 150) => {{
  if (remaining <= 0) return show('Verification timed out. Review the command status.', 'warning');
  const response = await fetch(statusUrl, {{cache:'no-store'}});
  if (!response.ok) return show('Status unavailable. Review is required.', 'warning');
  const command = await response.json();
  const last = command.events?.at(-1);
  if (command.status === 'finished' && last?.outcome === 'confirmed_submitted'
      && last?.evidence_type && last?.signature_verified === true) {{
    return show('Employer evidence verified: application submitted.', 'confirmed');
  }}
  if (terminalStatuses.has(command.status)) {{
    const outcome = last?.outcome || command.status;
    return show(`Application not employer-confirmed: ${{outcome}}.`, 'warning');
  }}
  show(`Application ${{command.status}}; waiting for signed runner evidence…`, 'neutral');
  setTimeout(() => pollCommand(statusUrl, remaining - 1), 2000);
}};
for (const button of document.querySelectorAll('button.send')) {{
  button.addEventListener('click', async () => {{
    button.disabled = true;
    const response = await fetch('/api/send', {{
      method: 'POST',
      headers: {{'content-type':'application/json','x-csrf-token':cookie('jaa_control_csrf')}},
      body: JSON.stringify({{
        grant_id: button.dataset.grant,
        application_ref: button.dataset.ref,
        application_revision: Number(button.dataset.revision),
        form_fingerprint_digest: button.dataset.fingerprint,
        acknowledgement: 'SEND_APPLICATION',
        client_idempotency_key: crypto.randomUUID()
      }})
    }});
    const data = await response.json();
    if (response.ok) {{
      show(`Command queued: ${{data.command_id}}. Waiting for verification…`, 'neutral');
      pollCommand(data.status_url);
    }} else {{
      show(`Not sent: ${{data.code}}`, 'warning');
      button.disabled = false;
    }}
  }});
}}
</script></body></html>"""


__all__ = ["CURRENT_SCHEMA_REVISION", "create_app"]
