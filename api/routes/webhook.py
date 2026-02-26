"""WhatsApp webhook routes with interactive action handling.

Handles:
- GET  /webhook/whatsapp — Meta verification challenge
- POST /webhook/whatsapp — Incoming messages + interactive button replies
                           (approve_, skip_, edit_ actions)
"""

from __future__ import annotations

import hashlib
import hmac
import re
from functools import lru_cache
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import redis
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from db.models import Application, ExtractedURL, Job, JobStatus, Message
from db.session import get_db
from ingestion.url_utils import identify_job_platform, is_likely_job_url, normalize_url, url_hash

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


@lru_cache
def _get_redis_client():
    """Best-effort Redis client for shared webhook metrics."""
    try:
        settings = get_settings()
        client = redis.from_url(settings.redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
        client.ping()
        return client
    except Exception:
        return None


def _metrics_key(name: str) -> str:
    return f"webhook:metric:{name}"

# ── In-memory interaction metrics (process-local) ───────
_webhook_metrics: dict[str, int] = defaultdict(int)
_webhook_url_domain_counts: Counter[str] = Counter()
WEBHOOK_METRICS_HASH_KEY = "webhook:metrics"
WEBHOOK_DOMAINS_ZSET_KEY = "webhook:domains"
WEBHOOK_METRICS_TTL_SECONDS = 60 * 60 * 24 * 30
WEBHOOK_MAX_DOMAIN_CARDINALITY = 1000
WEBHOOK_HOURLY_PREFIX = "webhook:metrics:hour"
WEBHOOK_DAILY_PREFIX = "webhook:metrics:day"


def _inc_metric(name: str, amount: int = 1) -> None:
    _webhook_metrics[name] += amount

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            redis_client.hincrby(WEBHOOK_METRICS_HASH_KEY, name, amount)
            redis_client.expire(WEBHOOK_METRICS_HASH_KEY, WEBHOOK_METRICS_TTL_SECONDS)

            hour_key, day_key = _metrics_bucket_keys()
            redis_client.hincrby(hour_key, name, amount)
            redis_client.hincrby(day_key, name, amount)
            redis_client.expire(hour_key, 60 * 60 * 24 * 7)
            redis_client.expire(day_key, 60 * 60 * 24 * 90)
        except Exception:
            pass


def _track_url_domain(url: str) -> None:
    """Track normalized URL host usage for WhatsApp messages."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().strip()
    if not host:
        return

    _webhook_url_domain_counts[host] += 1

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            redis_client.zincrby(WEBHOOK_DOMAINS_ZSET_KEY, 1, host)
            redis_client.expire(WEBHOOK_DOMAINS_ZSET_KEY, WEBHOOK_METRICS_TTL_SECONDS)
            redis_client.zremrangebyrank(WEBHOOK_DOMAINS_ZSET_KEY, 0, -WEBHOOK_MAX_DOMAIN_CARDINALITY - 1)
        except Exception:
            pass


def _metrics_bucket_keys() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    hour_key = f"{WEBHOOK_HOURLY_PREFIX}:{now:%Y%m%d%H}"
    day_key = f"{WEBHOOK_DAILY_PREFIX}:{now:%Y%m%d}"
    return hour_key, day_key


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def get_webhook_metrics_snapshot() -> dict[str, int]:
    """Return a snapshot of webhook interaction counters."""
    counters = dict(_webhook_metrics)

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            raw = redis_client.hgetall(WEBHOOK_METRICS_HASH_KEY)
            counters = {
                (k.decode() if isinstance(k, bytes) else str(k)): int(v)
                for k, v in raw.items()
            }
        except Exception:
            pass

    return counters


def get_webhook_metrics_payload(top_n_domains: int = 10) -> dict[str, Any]:
    """Return detailed WhatsApp metrics payload for APIs and dashboards."""
    counters = get_webhook_metrics_snapshot()
    top_domains = [
        {"domain": domain, "count": count}
        for domain, count in _webhook_url_domain_counts.most_common(top_n_domains)
    ]

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            redis_domains = redis_client.zrevrange(WEBHOOK_DOMAINS_ZSET_KEY, 0, top_n_domains - 1, withscores=True)
            top_domains = [
                {"domain": domain.decode() if isinstance(domain, bytes) else domain, "count": int(score)}
                for domain, score in redis_domains
            ]
        except Exception:
            pass

    platform_counts = {
        key.removeprefix("platform_").removesuffix("_urls"): value
        for key, value in counters.items()
        if key.startswith("platform_") and key.endswith("_urls")
    }

    messages_received = counters.get("messages_received", 0)
    urls_extracted = counters.get("urls_extracted", 0)
    urls_enqueued = counters.get("urls_enqueued", 0)
    likely_job_urls = counters.get("likely_job_urls", 0)
    duplicate_messages = counters.get("duplicate_messages", 0)

    quality = {
        "urls_per_message": _safe_ratio(urls_extracted, messages_received),
        "likely_job_url_rate": _safe_ratio(likely_job_urls, urls_extracted),
        "url_enqueue_rate": _safe_ratio(urls_enqueued, urls_extracted),
        "message_dedup_rate": _safe_ratio(duplicate_messages, messages_received),
    }

    recent_buckets: dict[str, dict[str, int]] = {}
    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            hour_key, day_key = _metrics_bucket_keys()
            for key in (hour_key, day_key):
                raw = redis_client.hgetall(key)
                recent_buckets[key] = {
                    (k.decode() if isinstance(k, bytes) else str(k)): int(v)
                    for k, v in raw.items()
                }
        except Exception:
            pass

    return {
        "counters": counters,
        "top_url_domains": top_domains,
        "platform_breakdown": platform_counts,
        "quality": quality,
        "recent_buckets": recent_buckets,
    }


def reset_webhook_metrics() -> None:
    """Reset webhook counters (used by tests)."""
    _webhook_metrics.clear()
    _webhook_url_domain_counts.clear()

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            redis_client.delete(WEBHOOK_METRICS_HASH_KEY)
            redis_client.delete(WEBHOOK_DOMAINS_ZSET_KEY)
            hourly = list(redis_client.scan_iter(match=f"{WEBHOOK_HOURLY_PREFIX}:*"))
            daily = list(redis_client.scan_iter(match=f"{WEBHOOK_DAILY_PREFIX}:*"))
            keys = hourly + daily
            if keys:
                redis_client.delete(*keys)
        except Exception:
            pass


# ── URL extraction regex ────────────────────────────────
URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]},;]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from text."""
    if not text:
        return []
    urls = URL_PATTERN.findall(text)
    cleaned = [u.rstrip(".,;:!?)") for u in urls if u.rstrip(".,;:!?)")]
    return list(dict.fromkeys(cleaned))


def _verify_signature(body: bytes, signature: str, app_secret: str) -> bool:
    """Verify the X-Hub-Signature-256 header from Meta."""
    if not app_secret:
        return True
    expected = "sha256=" + hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull messages from webhook payload."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                messages.append(msg)
    return messages


def _parse_action_job_id(action_id: str, prefix: str) -> int | None:
    """Safely parse a job_id from an action string like `approve_123`."""
    if not action_id.startswith(prefix):
        return None

    raw = action_id.removeprefix(prefix).strip()
    if not raw.isdigit():
        return None

    return int(raw)


# ── WhatsApp API helpers ────────────────────────────────

async def _send_whatsapp_message(
    phone: str, text: str, settings: Settings
) -> None:
    """Send a text message via WhatsApp Cloud API."""
    if not settings.whatsapp_api_token or not settings.whatsapp_phone_number_id:
        logger.warning("whatsapp_api_not_configured")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://graph.facebook.com/v18.0/{settings.whatsapp_phone_number_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": text},
                },
                headers={"Authorization": f"Bearer {settings.whatsapp_api_token}"},
            )
    except Exception as exc:
        logger.error("whatsapp_send_failed", error=str(exc))


async def _send_approval_buttons(
    phone: str, job_id: int, title: str, company: str, score: float,
    settings: Settings,
) -> None:
    """Send an interactive approval message with approve/skip/edit buttons."""
    if not settings.whatsapp_api_token:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://graph.facebook.com/v18.0/{settings.whatsapp_phone_number_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {
                            "text": (
                                f"📋 *{title}*\n"
                                f"🏢 {company}\n"
                                f"📊 Score: {score:.0f}/100\n\n"
                                f"Application draft ready. What would you like to do?"
                            )
                        },
                        "action": {
                            "buttons": [
                                {
                                    "type": "reply",
                                    "reply": {"id": f"approve_{job_id}", "title": "✅ Approve"},
                                },
                                {
                                    "type": "reply",
                                    "reply": {"id": f"skip_{job_id}", "title": "⏭️ Skip"},
                                },
                                {
                                    "type": "reply",
                                    "reply": {"id": f"edit_{job_id}", "title": "✏️ Edit"},
                                },
                            ]
                        },
                    },
                },
                headers={"Authorization": f"Bearer {settings.whatsapp_api_token}"},
            )
    except Exception as exc:
        logger.error("whatsapp_buttons_failed", error=str(exc))


# ── Interactive action handlers ─────────────────────────

async def _handle_approve(job_id: int, sender: str, db: Session, settings: Settings) -> None:
    """Handle approve_ action: mark application as approved and enqueue submission."""
    app = db.query(Application).filter(Application.job_id == job_id).first()
    if not app:
        await _send_whatsapp_message(
            sender,
            f"❌ Application for job #{job_id} not found.",
            settings,
        )
        return

    if app.status == JobStatus.APPROVED:
        await _send_whatsapp_message(sender, "ℹ️ Already approved.", settings)
        return

    app.status = JobStatus.APPROVED
    app.approved_at = datetime.utcnow()

    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.status = JobStatus.APPROVED

    db.commit()

    # Enqueue submission
    from worker.tasks import submit_application_task
    submit_application_task.delay(app.id)

    await _send_whatsapp_message(
        sender,
        (
            f"✅ Approved! Application for *{job.title if job else 'Unknown'}* "
            "has been queued for submission."
        ),
        settings,
    )
    logger.info("application_approved_via_whatsapp", job_id=job_id)


async def _handle_skip(job_id: int, sender: str, db: Session, settings: Settings) -> None:
    """Handle skip_ action: mark application as rejected."""
    app = db.query(Application).filter(Application.job_id == job_id).first()
    job = db.query(Job).filter(Job.id == job_id).first()

    if app:
        app.status = JobStatus.SKIPPED
        app.rejected_at = datetime.utcnow()
        app.rejection_reason = "Skipped by user via WhatsApp"
    if job:
        job.status = JobStatus.SKIPPED

    db.commit()

    await _send_whatsapp_message(
        sender,
        f"⏭️ Skipped *{job.title if job else 'job'}*.",
        settings,
    )
    logger.info("application_skipped_via_whatsapp", job_id=job_id)


async def _handle_edit(job_id: int, sender: str, db: Session, settings: Settings) -> None:
    """Handle edit_ action: send application details for review."""
    app = db.query(Application).filter(Application.job_id == job_id).first()
    job = db.query(Job).filter(Job.id == job_id).first()

    if not app or not job:
        await _send_whatsapp_message(sender, "❌ Application not found.", settings)
        return

    # Send cover letter preview
    preview = (
        f"✏️ *Application for {job.title} at {job.company}*\n\n"
        f"*Cover Letter:*\n{(app.cover_letter or '')[:1000]}\n\n"
        f"*Recruiter Message:*\n{(app.recruiter_message or '')[:500]}\n\n"
        f"Reply with 'approve_{job_id}' to approve or 'skip_{job_id}' to skip."
    )
    await _send_whatsapp_message(sender, preview, settings)
    logger.info("application_edit_preview_sent", job_id=job_id)


# ── Webhook Endpoints ───────────────────────────────────

@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
):
    """WhatsApp webhook verification challenge."""
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        logger.info("webhook_verified")
        return int(hub_challenge) if hub_challenge else ""
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
async def receive_message(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_hub_signature_256: str = Header("", alias="X-Hub-Signature-256"),
):
    """Receive WhatsApp messages and handle both new URLs and interactive actions."""
    body = await request.body()

    # Signature verification
    if settings.whatsapp_app_secret:
        if not _verify_signature(body, x_hub_signature_256, settings.whatsapp_app_secret):
            _inc_metric("signature_invalid")
            logger.warning("invalid_webhook_signature")
            raise HTTPException(status_code=403, detail="Invalid signature")
        _inc_metric("signature_valid")

    payload = await request.json()
    _inc_metric("webhook_requests")
    raw_messages = _extract_messages(payload)
    _inc_metric("messages_received", len(raw_messages))
    processed = 0

    for msg in raw_messages:
        msg_id = msg.get("id", "")
        sender = msg.get("from", "")

        # Allowed-sender filter
        if settings.allowed_sender_list and sender not in settings.allowed_sender_list:
            _inc_metric("blocked_sender_messages")
            logger.info("sender_not_allowed", sender=sender)
            continue

        # ── Handle interactive button replies ────────
        if msg.get("type") == "interactive":
            interactive = msg.get("interactive", {})
            button_reply = interactive.get("button_reply", {})
            action_id = button_reply.get("id", "")

            approve_job_id = _parse_action_job_id(action_id, "approve_")
            skip_job_id = _parse_action_job_id(action_id, "skip_")
            edit_job_id = _parse_action_job_id(action_id, "edit_")

            if approve_job_id is not None:
                _inc_metric("interactive_approve_actions")
                await _handle_approve(approve_job_id, sender, db, settings)
            elif skip_job_id is not None:
                _inc_metric("interactive_skip_actions")
                await _handle_skip(skip_job_id, sender, db, settings)
            elif edit_job_id is not None:
                _inc_metric("interactive_edit_actions")
                await _handle_edit(edit_job_id, sender, db, settings)
            else:
                _inc_metric("interactive_invalid_actions")
                logger.warning("unknown_or_invalid_interactive_action", action=action_id)

            _inc_metric("interactive_messages")
            processed += 1
            continue

        # ── Handle text messages (also check for text-based actions) ──
        text_body = msg.get("text", {}).get("body", "")

        _inc_metric("text_messages")

        # Check for text-based approve/skip commands
        text_lower = text_body.strip().lower()
        approve_job_id = _parse_action_job_id(text_lower, "approve_")
        skip_job_id = _parse_action_job_id(text_lower, "skip_")

        if approve_job_id is not None:
            _inc_metric("text_approve_actions")
            await _handle_approve(approve_job_id, sender, db, settings)
            processed += 1
            continue

        if skip_job_id is not None:
            _inc_metric("text_skip_actions")
            await _handle_skip(skip_job_id, sender, db, settings)
            processed += 1
            continue

        # Dedup by message ID
        exists = db.query(Message).filter(Message.whatsapp_message_id == msg_id).first()
        if exists:
            _inc_metric("duplicate_messages")
            continue

        # Persist message
        db_msg = Message(
            whatsapp_message_id=msg_id,
            sender_phone=sender,
            body=text_body,
        )
        db.add(db_msg)
        db.flush()

        # Extract URLs and enqueue processing
        urls = extract_urls(text_body)
        _inc_metric("urls_extracted", len(urls))
        if not urls:
            _inc_metric("messages_without_urls")
        for raw_url in urls:
            normalized = normalize_url(raw_url)
            platform = identify_job_platform(normalized)
            if platform != "unknown":
                _inc_metric(f"platform_{platform}_urls")

            if is_likely_job_url(normalized):
                _inc_metric("likely_job_urls")
            else:
                _inc_metric("non_job_urls")

            uhash = url_hash(normalized)

            if db.query(ExtractedURL).filter(ExtractedURL.url_hash == uhash).first():
                continue

            db_url = ExtractedURL(
                message_id=db_msg.id,
                original_url=raw_url,
                normalized_url=normalized,
                url_hash=uhash,
            )
            db.add(db_url)
            db.flush()
            _inc_metric("urls_enqueued")
            _track_url_domain(normalized)

            # Enqueue URL processing
            from worker.tasks import process_url_task
            process_url_task.delay(db_url.id)

        if urls:
            await _send_whatsapp_message(
                sender,
                f"📬 Received {len(urls)} job link(s). Processing...",
                settings,
            )

        processed += 1

    db.commit()
    logger.info("webhook_processed", messages=processed)
    return {"status": "ok", "processed": processed}
