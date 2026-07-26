"""Safe compatibility endpoint for ingesting a job link from WhatsApp."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Application, ExtractedURL, Job, Message, URLStatus
from db.session import get_db
from ingestion.url_utils import normalize_url, url_hash

router = APIRouter(tags=["webhooks"])


class WhatsAppIngestRequest(BaseModel):
    sender_phone: str = ""
    message_text: str
    # Deprecated compatibility field. Ingestion never represents approval.
    auto_apply_immediately: bool = False


class WhatsAppIngestResponse(BaseModel):
    url_id: int
    job_id: int | None = None
    title: str | None = None
    company: str | None = None
    score: float | None = None
    selected_cv_id: str | None = None
    status: str
    message: str


def _response_for_url(db: Session, record: ExtractedURL) -> WhatsAppIngestResponse:
    job = db.query(Job).filter(Job.extracted_url_id == record.id).order_by(Job.id.desc()).first()
    application = (
        db.query(Application).filter(Application.job_id == job.id).first() if job else None
    )
    if job:
        return WhatsAppIngestResponse(
            url_id=record.id,
            job_id=job.id,
            title=job.title,
            company=job.company,
            score=job.score,
            selected_cv_id=application.selected_cv_id if application else None,
            status=application.status.value if application else job.status.value,
            message="Job link processed through the real extraction pipeline.",
        )

    status = record.status.value if record.status else URLStatus.PENDING.value
    message = (
        "Job link is queued for extraction."
        if record.status in {None, URLStatus.PENDING}
        else "No job was created; inspect the URL record for the extraction result."
    )
    return WhatsAppIngestResponse(
        url_id=record.id,
        status=status,
        message=message,
    )


@router.post("/webhook/whatsapp-link", response_model=WhatsAppIngestResponse)
async def ingest_whatsapp_job_link(
    payload: WhatsAppIngestRequest,
    db: Session = Depends(get_db),
):
    """Queue the first supplied URL; never fabricate or approve an application."""
    urls = re.findall(r"https?://[^\s<>\"')\]},;]+", payload.message_text)
    if not urls:
        raise HTTPException(
            status_code=400,
            detail="No valid HTTP/HTTPS URL found in WhatsApp message",
        )

    normalized = normalize_url(urls[0])
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="The job URL is invalid.")
    digest = url_hash(normalized)

    existing = db.query(ExtractedURL).filter(ExtractedURL.url_hash == digest).first()
    if existing:
        return _response_for_url(db, existing)

    message = Message(
        whatsapp_message_id=f"whatsapp-link-{digest[:16]}",
        sender_phone=payload.sender_phone or "whatsapp-link",
        body=urls[0],
    )
    db.add(message)
    db.flush()
    record = ExtractedURL(
        message_id=message.id,
        original_url=urls[0],
        normalized_url=normalized,
        url_hash=digest,
        status=URLStatus.PENDING,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    from worker.tasks import process_url_task

    settings = get_settings()
    if settings.tasks_always_eager:
        process_url_task.apply(args=[record.id])
    else:
        process_url_task.delay(record.id)

    db.expire_all()
    refreshed = db.get(ExtractedURL, record.id)
    return _response_for_url(db, refreshed or record)
