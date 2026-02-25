"""WhatsApp Cloud API webhook — ingestion layer.

Receives forwarded messages via the official WhatsApp Business Cloud API,
validates the webhook signature, extracts URLs from all message types,
and enqueues processing.

Supported message types:
  text, image (caption), video (caption), document (caption),
  button (quick-reply), interactive (list/button reply).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from db.models import ExtractedURL, Message
from db.session import get_db
from ingestion.url_utils import (
    extract_urls,
    extract_urls_from_whatsapp_message,
    is_likely_job_url,
    is_short_url,
    normalize_url,
    url_hash,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def verify_signature(body: bytes, signature: str, app_secret: str) -> bool:
    """Verify the X-Hub-Signature-256 header from Meta."""
    if not app_secret:
        return True  # skip in dev when secret isn't set
    expected = "sha256=" + hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull individual message objects out of the Cloud API webhook payload."""
    messages: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                messages.append(msg)
    return messages


def _get_message_text(msg: dict[str, Any]) -> str:
    """Return a human-readable text representation of the message for storage."""
    msg_type = msg.get("type", "text")
    if msg_type == "text":
        return msg.get("text", {}).get("body", "")
    if msg_type in ("image", "video", "audio", "document", "sticker"):
        caption = msg.get(msg_type, {}).get("caption", "")
        return f"[{msg_type}] {caption}".strip()
    if msg_type == "button":
        btn = msg.get("button", {})
        return f"[button] {btn.get('text', '')} | {btn.get('payload', '')}".strip()
    if msg_type == "interactive":
        inter = msg.get("interactive", {})
        body_text = inter.get("body", {}).get("text", "")
        return f"[interactive] {body_text}".strip()
    return f"[{msg_type}]"


# ── Webhook verification (GET) ──────────────────────────────────────────────


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
):
    """WhatsApp webhook verification challenge (Meta requires this)."""
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        logger.info("webhook_verified")
        return int(hub_challenge) if hub_challenge else ""
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Webhook receiver (POST) ─────────────────────────────────────────────────


@router.post("/whatsapp")
async def receive_message(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_hub_signature_256: str = Header("", alias="X-Hub-Signature-256"),
):
    """Receive incoming WhatsApp messages and extract job URLs.

    Processes all message types — text, media captions, button/interactive
    replies.  Logs short URLs for deferred expansion by the Celery worker.
    """
    body = await request.body()

    if settings.whatsapp_app_secret and not verify_signature(
        body, x_hub_signature_256, settings.whatsapp_app_secret
    ):
        logger.warning("invalid_webhook_signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    raw_messages = _extract_messages(payload)

    total_urls = 0
    processed_count = 0

    for msg in raw_messages:
        msg_id = msg.get("id", "")
        sender = msg.get("from", "")
        msg_type = msg.get("type", "text")

        # Allowed-sender filter
        if settings.allowed_sender_list and sender not in settings.allowed_sender_list:
            logger.info("sender_not_allowed", sender=sender)
            continue

        # Dedup by message ID
        if db.query(Message).filter(Message.whatsapp_message_id == msg_id).first():
            logger.debug("duplicate_message", msg_id=msg_id)
            continue

        # Build text representation for storage
        text_body = _get_message_text(msg)

        # Persist message
        db_msg = Message(
            whatsapp_message_id=msg_id,
            sender_phone=sender,
            body=text_body,
        )
        db.add(db_msg)
        db.flush()

        # Extract URLs from ALL message fields (text, captions, buttons…)
        raw_urls = extract_urls_from_whatsapp_message(msg)

        for raw_url in raw_urls:
            normalized = normalize_url(raw_url)
            uhash = url_hash(normalized)

            if db.query(ExtractedURL).filter(ExtractedURL.url_hash == uhash).first():
                logger.debug("duplicate_url", url=normalized)
                continue

            job_hint = is_likely_job_url(normalized)
            short_hint = is_short_url(normalized)

            db_url = ExtractedURL(
                message_id=db_msg.id,
                original_url=raw_url,
                normalized_url=normalized,
                url_hash=uhash,
            )
            db.add(db_url)
            total_urls += 1

            logger.info(
                "url_extracted",
                url=normalized,
                msg_type=msg_type,
                is_job=job_hint,
                needs_expand=short_hint,
            )

        processed_count += 1

    db.commit()
    logger.info("webhook_processed", messages=processed_count, urls=total_urls)
    return {"status": "ok", "processed": processed_count, "urls_extracted": total_urls}


# ── Direct URL ingestion endpoint (used by dashboard / manual trigger) ──────


@router.post("/ingest-url")
async def ingest_url(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Directly ingest a URL without going through WhatsApp.

    Accepts: {"url": "https://...", "sender": "dashboard"}
    Used by the dashboard manual URL input.
    """
    data = await request.json()
    raw_url = (data.get("url") or "").strip()
    sender = data.get("sender", "manual")

    if not raw_url:
        raise HTTPException(status_code=422, detail="url is required")

    # Support pasting multiple URLs (newline-separated)
    pasted_urls = extract_urls(raw_url) if "\n" in raw_url else [raw_url]

    added: list[str] = []
    skipped: list[str] = []

    for single_url in pasted_urls:
        # Minimal synthetic message for storage
        db_msg = Message(
            whatsapp_message_id=f"manual_{url_hash(single_url)[:16]}",
            sender_phone=sender,
            body=single_url,
        )
        db.add(db_msg)
        db.flush()

        normalized = normalize_url(single_url)
        uhash = url_hash(normalized)

        if db.query(ExtractedURL).filter(ExtractedURL.url_hash == uhash).first():
            skipped.append(normalized)
            continue

        db_url = ExtractedURL(
            message_id=db_msg.id,
            original_url=single_url,
            normalized_url=normalized,
            url_hash=uhash,
        )
        db.add(db_url)
        added.append(normalized)

    db.commit()
    logger.info("manual_ingest", added=len(added), skipped=len(skipped))
    return {"status": "ok", "added": len(added), "skipped": len(skipped)}
