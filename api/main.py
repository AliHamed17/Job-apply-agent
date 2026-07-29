"""FastAPI application — main entry point with auth, rate limiting, and CORS."""

from __future__ import annotations

import hmac
import os
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

try:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
except ImportError:  # pragma: no cover - exercised in dependency-light smokes
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    def generate_latest() -> bytes:
        """Return a valid, discoverable placeholder exposition."""
        metric_names = (
            "job_agent_http_requests_total",
            "job_agent_http_request_duration_seconds",
            "job_agent_pipeline_duration_seconds",
            "job_agent_failures_total",
            "job_agent_retries_total",
            "job_agent_governor_denials_total",
            "job_agent_queue_depth",
            "job_agent_queue_snapshot_available",
            "job_agent_challenge_trips_total",
            "job_agent_outbound_results_total",
            "job_agent_selector_failures_total",
            "job_agent_operational_events_total",
            "job_agent_operational_duration_seconds",
        )
        return "".join(
            f"# HELP {name} Metric unavailable in this minimal environment.\n"
            f"# TYPE {name} untyped\n"
            for name in metric_names
        ).encode()


from api.routes.ab_testing import router as ab_testing_router
from api.routes.analytics import router as analytics_router
from api.routes.applications import router as applications_router
from api.routes.ats import router as ats_router
from api.routes.audit import router as audit_router
from api.routes.batch_apply import router as batch_apply_router
from api.routes.batch_rescore import router as batch_rescore_router
from api.routes.command_center import router as command_center_router
from api.routes.control import router as control_router
from api.routes.culture_fit import router as culture_fit_router
from api.routes.cv_routing import router as cv_routing_router
from api.routes.dashboard import router as dashboard_router
from api.routes.digest import router as digest_router
from api.routes.dispatch import router as dispatch_router
from api.routes.dry_run import router as dry_run_router
from api.routes.export import router as export_router
from api.routes.feedback import router as feedback_router
from api.routes.followup import router as followup_router
from api.routes.health_inspector import router as health_inspector_router
from api.routes.interview_prep import router as interview_prep_router
from api.routes.interview_simulate import router as interview_simulate_router
from api.routes.jobs import router as jobs_router
from api.routes.operations import router as operations_router
from api.routes.outreach import router as outreach_router
from api.routes.profile import router as profile_router
from api.routes.realign import router as realign_router
from api.routes.runtime import router as runtime_router
from api.routes.salary import router as salary_router
from api.routes.skill_gaps import router as skill_gaps_router
from api.routes.spotlight import router as spotlight_router
from api.routes.stream import router as stream_router
from api.routes.webhook import ingest_router
from api.routes.webhook import router as webhook_router
from api.routes.whatsapp_ingest import router as whatsapp_ingest_router
from api.routes.widgets import router as widgets_router
from core.config import get_settings
from core.logging import new_correlation_id, setup_logging
from core.metrics import (
    HTTP_LATENCY,
    HTTP_REQUESTS,
    normalize_http_method,
    refresh_authoritative_metrics,
)
from core.operations import rate_limit_allowed, readiness_report
from core.runtime_identity import (
    compute_source_digest,
    compute_ui_asset_digest,
    get_runtime_identity,
)
from core.single_instance import acquire_dashboard_instance_lock
from db.session import init_db

# Setup structured logging
setup_logging()
logger = structlog.get_logger(__name__)

_PUBLIC_READ_PATHS = frozenset(
    {
        "/",
        "/health",
        "/health/live",
        "/health/ready",
        "/metrics",
        "/openapi.json",
        "/favicon.ico",
    }
)
_PUBLIC_READ_PREFIXES = ("/static", "/docs", "/redoc")


def _is_public_read(request: Request) -> bool:
    """Return whether a dependency probe or static UI request is read-only."""
    if request.method not in {"GET", "HEAD"}:
        return False
    path = request.url.path
    return path in _PUBLIC_READ_PATHS or path.startswith(_PUBLIC_READ_PREFIXES)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.validate_runtime()
    identity = get_runtime_identity()
    instance_lock = acquire_dashboard_instance_lock(
        app_env=settings.app_env,
        boot_id=identity.boot_id,
    )
    try:
        init_db()
        logger.info(
            "app_started",
            draft_only=settings.draft_only,
            auto_apply=settings.auto_apply,
            build_sha=identity.build_sha,
            boot_id=identity.boot_id,
        )
        yield
    finally:
        if instance_lock is not None:
            instance_lock.release()


# ── App creation ─────────────────────────────────────────
app = FastAPI(
    title="AI Job Apply Agent",
    description="Monitor WhatsApp for job links, extract postings, and draft/submit applications",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS (configurable per environment) ──────────────────
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Redis-backed rate limiting, consistent across API processes."""
    # Signed webhooks can arrive in bursts. Public probes and static dashboard
    # assets must remain observable when Redis is the dependency that is down.
    if request.url.path.startswith("/webhook") or _is_public_read(request):
        return await call_next(request)

    peer_ip = request.client.host if request.client else "unknown"
    client_ip = peer_ip
    if peer_ip in settings.trusted_proxy_list:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            client_ip = forwarded.split(",", 1)[0].strip()
    if client_ip == "127.0.0.1":
        return await call_next(request)
    try:
        allowed = rate_limit_allowed(client_ip, settings.rate_limit_requests_per_minute, settings)
    except Exception:
        logger.exception("rate_limit_backend_unavailable")
        if settings.app_env == "production":
            return JSONResponse(status_code=503, content={"detail": "Service unavailable"})
        allowed = True
    if not allowed:
        logger.warning("rate_limited", client=client_ip)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
        )

    return await call_next(request)


# ── Static Asset Cache Control ────────────────────────────
@app.middleware("http")
async def no_cache_static_middleware(request: Request, call_next):
    """Force revalidation on static assets so dashboard edits show up
    immediately instead of being served from a stale browser cache."""
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ── API Token Auth Middleware ────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Bearer token authentication for API endpoints.

    Exempt: signed webhook, liveness, metrics, and API documentation.
    """
    if request.url.path == "/webhook/whatsapp" or _is_public_read(request):
        return await call_next(request)

    if settings.app_env == "development" and settings.operator_auth_is_placeholder:
        prepare_only = (
            settings.dry_run or settings.draft_only or not settings.portal_final_submit_enabled
        )
        if prepare_only:
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "OPERATOR_AUTH_REQUIRED",
                    "message": "Configure a strong operator API secret before live sending.",
                }
            },
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid Authorization header"},
        )

    token = auth_header.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, settings.secret_key):
        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid API token"},
        )

    return await call_next(request)


# ── Correlation ID Middleware ────────────────────────────
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Attach a correlation ID to every request for log tracing."""
    import structlog.contextvars

    correlation_id = request.headers.get("X-Correlation-ID", new_correlation_id())
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    structlog.contextvars.unbind_contextvars("correlation_id")
    return response


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    route_label = getattr(route, "path", "unmatched")
    status_class = f"{response.status_code // 100}xx"
    HTTP_REQUESTS.labels(
        route_label,
        normalize_http_method(request.method),
        status_class,
    ).inc()
    HTTP_LATENCY.labels(route_label).observe(time.perf_counter() - started)
    return response


# ── Register routes ──────────────────────────────────────
app.include_router(webhook_router)
app.include_router(ingest_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(applications_router, prefix="/api")
app.include_router(ats_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(operations_router, prefix="/api")
app.include_router(feedback_router, prefix="/api")
app.include_router(profile_router)
app.include_router(control_router)
app.include_router(cv_routing_router, prefix="/api")
app.include_router(realign_router, prefix="/api")
app.include_router(runtime_router, prefix="/api")
app.include_router(interview_prep_router, prefix="/api")
app.include_router(widgets_router, prefix="/api")
app.include_router(export_router, prefix="/api")
app.include_router(outreach_router, prefix="/api")
app.include_router(batch_rescore_router, prefix="/api")
app.include_router(digest_router, prefix="/api")
app.include_router(health_inspector_router, prefix="/api")
app.include_router(interview_simulate_router, prefix="/api")
app.include_router(batch_apply_router, prefix="/api")
app.include_router(dry_run_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(followup_router, prefix="/api")
app.include_router(ab_testing_router, prefix="/api")
app.include_router(culture_fit_router, prefix="/api")
app.include_router(stream_router, prefix="/api")
app.include_router(command_center_router, prefix="/api")
app.include_router(dispatch_router, prefix="/api")
app.include_router(spotlight_router, prefix="/api")
app.include_router(salary_router, prefix="/api")
app.include_router(skill_gaps_router, prefix="/api")
app.include_router(whatsapp_ingest_router, prefix="/api")


# ── Static and Templates ─────────────────────────────────
static_dir = os.path.join(os.path.dirname(__file__), "static")
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)


# ── Health + Metrics ─────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/health/live")
async def health_live():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/health/ready")
async def health_ready():
    report = await run_in_threadpool(readiness_report, settings)
    return JSONResponse(report, status_code=200 if report["status"] == "ready" else 503)


@app.get("/")
async def serve_dashboard(request: Request):
    """Serve the main dashboard UI."""
    identity = get_runtime_identity()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "runtime_build_sha": identity.build_sha,
            "runtime_ui_asset_digest": compute_ui_asset_digest(),
            "runtime_source_digest": compute_source_digest(),
            "runtime_protocol_version": identity.protocol_version,
            "runtime_boot_id": identity.boot_id,
            "runtime_started_at": identity.started_at.isoformat().replace("+00:00", "Z"),
        },
    )


@app.get("/metrics")
async def metrics():
    """Prometheus exposition with bounded labels and no personal data."""

    def _snapshot() -> bytes:
        refresh_authoritative_metrics()
        return generate_latest()

    payload = await run_in_threadpool(_snapshot)
    return Response(payload, media_type=CONTENT_TYPE_LATEST)
