"""WhatsApp Cloud API webhook — ingestion layer.

Receives forwarded messages via the official WhatsApp Business Cloud API,
validates the webhook signature, extracts URLs, and enqueues processing.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from db.models import ExtractedURL, Message
from db.session import get_db
from ingestion.url_utils import normalize_url, url_hash

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

# ── URL extraction regex ────────────────────────────────
URL_PATTERN = re.compile(
    r"https?://[^\s<>\"')\]},;]+",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from a text string."""
    if not text:
        return []
    urls = URL_PATTERN.findall(text)
    # Strip trailing punctuation that may have been captured
    cleaned: list[str] = []
    for u in urls:
        u = u.rstrip(".,;:!?)")
        if u:
            cleaned.append(u)
    return list(dict.fromkeys(cleaned))  # dedup, preserve order


def verify_signature(body: bytes, signature: str, app_secret: str) -> bool:
    """Verify the X-Hub-Signature-256 header from Meta."""
    if not app_secret:
        return True  # skip in dev when secret isn't set
    expected = "sha256=" + hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull individual messages out of the webhook payload."""
    messages: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                messages.append(msg)
    return messages


# ── Webhook verification (GET) ──────────────────────────


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


# ── Webhook receiver (POST) ─────────────────────────────


@router.post("/whatsapp")
async def receive_message(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_hub_signature_256: str = Header("", alias="X-Hub-Signature-256"),
):
    """Receive incoming WhatsApp messages and extract URLs."""
    body = await request.body()

    # Signature verification
    if settings.whatsapp_app_secret and not verify_signature(
        body, x_hub_signature_256, settings.whatsapp_app_secret
    ):
        logger.warning("invalid_webhook_signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    raw_messages = _extract_messages(payload)
    processed_count = 0

    for msg in raw_messages:
        msg_id = msg.get("id", "")
        sender = msg.get("from", "")
        text_body = msg.get("text", {}).get("body", "")

        # Allowed-sender filter
        if settings.allowed_sender_list and sender not in settings.allowed_sender_list:
            logger.info("sender_not_allowed", sender=sender)
            continue

        # Dedup by message ID
        exists = db.query(Message).filter(Message.whatsapp_message_id == msg_id).first()
        if exists:
            logger.debug("duplicate_message", msg_id=msg_id)
            continue

        # Persist message
        db_msg = Message(
            whatsapp_message_id=msg_id,
            sender_phone=sender,
            body=text_body,
        )
        db.add(db_msg)
        db.flush()  # get db_msg.id

        # Extract and persist URLs
        urls = extract_urls(text_body)
        for raw_url in urls:
            normalized = normalize_url(raw_url)
            uhash = url_hash(normalized)

            # Dedup by URL hash
            url_exists = db.query(ExtractedURL).filter(
                ExtractedURL.url_hash == uhash
            ).first()
            if url_exists:
                logger.debug("duplicate_url", url=normalized)
                continue

            db_url = ExtractedURL(
                message_id=db_msg.id,
                original_url=raw_url,
                normalized_url=normalized,
                url_hash=uhash,
            )
            db.add(db_url)

        processed_count += 1

    db.commit()
    logger.info("webhook_processed", messages=processed_count)
    return {"status": "ok", "processed": processed_count}
