"""FastAPI application — main entry point with auth, rate limiting, and CORS."""

from __future__ import annotations

import hmac
import os
import time
from functools import lru_cache
from collections import defaultdict
from contextlib import asynccontextmanager

import redis
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.routes.applications import router as applications_router
from api.routes.dashboard import router as dashboard_router
from api.routes.jobs import router as jobs_router
from api.routes.webhook import (
    get_webhook_metrics_payload,
    get_webhook_metrics_snapshot,
)
from api.routes.webhook import (
    router as webhook_router,
)
from core.config import get_settings
from core.logging import new_correlation_id, setup_logging
from db.session import init_db

# Setup structured logging
setup_logging()
logger = structlog.get_logger(__name__)

# ── App creation ─────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize runtime dependencies and validate configuration on startup."""
    config_errors = settings.validate_runtime_config()
    if config_errors:
        raise RuntimeError("; ".join(config_errors))

    _rate_limit_store.clear()
    init_db()
    logger.info("app_started", draft_only=settings.draft_only, auto_apply=settings.auto_apply)
    yield


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
    allow_origins=settings.cors_allowed_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.trusted_host_list,
)


# ── Rate Limiting Middleware ─────────────────────────────
_rate_limit_store: dict[str, list[float]] = defaultdict(list)


@lru_cache
def _get_redis_client():
    """Best-effort Redis client for shared runtime state (rate limits/metrics)."""
    try:
        client = redis.from_url(settings.redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
        client.ping()
        return client
    except Exception:
        return None


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Simple in-memory rate limiter per client IP."""
    # Skip rate limiting for webhook (Meta sends bursts)
    if request.url.path.startswith("/webhook"):
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    max_requests = settings.rate_limit_requests_per_minute

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            bucket = int(now // 60)
            key = f"rate_limit:{client_ip}:{bucket}"
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, 120)

            if count > max_requests:
                logger.warning("rate_limited", client=client_ip, backend="redis")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again later."},
                )

            return await call_next(request)
        except Exception:
            pass

    # Fallback: in-memory rate limiting
    window = 60.0
    _rate_limit_store[client_ip] = [
        t for t in _rate_limit_store[client_ip] if now - t < window
    ]

    if len(_rate_limit_store[client_ip]) >= max_requests:
        logger.warning("rate_limited", client=client_ip, backend="memory")
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
        )

    _rate_limit_store[client_ip].append(now)
    return await call_next(request)


def _is_auth_exempt_path(path: str) -> bool:
    """Return True only for explicitly exempt endpoints."""
    exact_exempt = {"/webhook/whatsapp", "/health", "/openapi.json", "/docs", "/redoc"}
    if path in exact_exempt:
        return True

    docs_prefixes = ("/docs/", "/redoc/")
    return path.startswith(docs_prefixes)




# ── API Token Auth Middleware ────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Bearer token authentication for API endpoints.

    Exempt: /webhook (uses its own verification), /health, /docs, /openapi.json
    """
    if _is_auth_exempt_path(request.url.path):
        return await call_next(request)

    # Optional insecure bypass for local/dev only when explicitly enabled
    if (
        settings.secret_key == "change-me"
        and not settings.is_production
        and settings.allow_insecure_auth_bypass
    ):
        return await call_next(request)

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




@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Attach baseline HTTP security headers to every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
    )
    return response


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


# ── Register routes ──────────────────────────────────────
app.include_router(webhook_router)
app.include_router(jobs_router, prefix="/api")
app.include_router(applications_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")


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

@app.get("/")
async def serve_dashboard(request: Request):
    """Serve the main dashboard UI."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/metrics")
async def metrics():
    """Basic metrics endpoint."""
    from db.models import (
        Application,
        ExtractedURL,
        Job,
        JobStatus,
        Submission,
        SubmissionStatus,
        URLStatus,
    )
    from db.session import get_session_factory

    db = get_session_factory()()
    try:
        urls_processed = db.query(ExtractedURL).count()
        urls_pending = db.query(ExtractedURL).filter(ExtractedURL.status == URLStatus.PENDING).count()
        urls_failed = db.query(ExtractedURL).filter(ExtractedURL.status == URLStatus.FAILED).count()
        urls_blocked = db.query(ExtractedURL).filter(ExtractedURL.status == URLStatus.BLOCKED).count()

        jobs_extracted = db.query(Job).count()
        jobs_draft = db.query(Job).filter(Job.status == JobStatus.DRAFT).count()
        jobs_approved = db.query(Job).filter(Job.status == JobStatus.APPROVED).count()
        jobs_submitted = db.query(Job).filter(Job.status == JobStatus.SUBMITTED).count()
        jobs_skipped = db.query(Job).filter(Job.status == JobStatus.SKIPPED).count()

        applications_total = db.query(Application).count()
        applications_draft = db.query(Application).filter(Application.status == JobStatus.DRAFT).count()
        applications_approved = db.query(Application).filter(
            Application.status == JobStatus.APPROVED
        ).count()
        applications_skipped = db.query(Application).filter(
            Application.status == JobStatus.SKIPPED
        ).count()

        submissions_total = db.query(Submission).count()
        submissions_pending = db.query(Submission).filter(Submission.status == SubmissionStatus.PENDING).count()
        submissions_success = db.query(Submission).filter(Submission.status == SubmissionStatus.SUCCESS).count()
        submissions_failed = db.query(Submission).filter(Submission.status == SubmissionStatus.FAILED).count()
        submissions_needs_human = db.query(Submission).filter(
            Submission.status == SubmissionStatus.NEEDS_HUMAN_CONFIRMATION
        ).count()
        submissions_draft_only = db.query(Submission).filter(
            Submission.status == SubmissionStatus.DRAFT_ONLY
        ).count()

        metrics = {
            "urls_processed": urls_processed,
            "urls_pending": urls_pending,
            "urls_failed": urls_failed,
            "urls_blocked": urls_blocked,
            "jobs_extracted": jobs_extracted,
            "jobs_draft": jobs_draft,
            "jobs_approved": jobs_approved,
            "jobs_submitted": jobs_submitted,
            "jobs_skipped": jobs_skipped,
            "applications_drafted": applications_total,
            "applications_draft": applications_draft,
            "applications_approved": applications_approved,
            "applications_skipped": applications_skipped,
            "submissions_total": submissions_total,
            "submissions_pending": submissions_pending,
            "submissions_success": submissions_success,
            "submissions_failed": submissions_failed,
            "submissions_needs_human_confirmation": submissions_needs_human,
            "submissions_draft_only": submissions_draft_only,
            "application_approval_rate": round(applications_approved / applications_total, 4) if applications_total else 0.0,
            "submission_success_rate": round(submissions_success / submissions_total, 4) if submissions_total else 0.0,
        }
        webhook_metrics = get_webhook_metrics_snapshot()
        for key, value in webhook_metrics.items():
            metrics[f"whatsapp_{key}"] = value

        return metrics
    finally:
        db.close()



@app.get("/api/whatsapp/metrics")
async def whatsapp_metrics():
    """Detailed WhatsApp interaction metrics, including top URL domains."""
    return get_webhook_metrics_payload()
