"""Dispatch notifications API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db
from notifications.dispatcher import dispatch_high_match_alert

router = APIRouter(tags=["notifications"])


class DispatchRequest(BaseModel):
    application_id: int
    event_type: str = "auto_applied"


class DispatchResponse(BaseModel):
    recipient_email: str
    recipient_phone: str
    message: str
    dispatched: bool


@router.post("/notifications/dispatch", response_model=DispatchResponse)
async def dispatch_application_notification(
    payload: DispatchRequest,
    db: Session = Depends(get_db),
):
    """Trigger email & WhatsApp notification alert for a specific application event."""
    app = db.query(Application).filter(Application.id == payload.application_id).first()
    if not app or not app.job:
        raise HTTPException(status_code=404, detail=f"Application {payload.application_id} not found")

    res = dispatch_high_match_alert(
        job_title=app.job.title or "",
        company=app.job.company or "",
        score=app.job.score or 0.0,
        event_type=payload.event_type,
    )

    return DispatchResponse(
        recipient_email=res.recipient_email,
        recipient_phone=res.recipient_phone,
        message=res.message,
        dispatched=res.dispatched,
    )
