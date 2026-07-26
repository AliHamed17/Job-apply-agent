"""Real-time application submission progress SSE stream router."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from core.submission_truth import is_employer_verified
from db.models import Application
from db.session import get_db

router = APIRouter(tags=["applications"])


async def _progress_event_generator(app_id: int, db: Session) -> AsyncGenerator[str, None]:
    """Emit one truthful durable snapshot; clients reconnect to poll progress."""
    db.expire_all()
    app = db.get(Application, app_id)
    attempt = app.submission if app is not None else None
    if attempt is None:
        item = {
            "step": "not_started",
            "message": "No submission attempt has been created.",
            "verified": False,
        }
    else:
        verified = is_employer_verified(attempt)
        item = {
            "attempt_id": attempt.id,
            "step": attempt.stage,
            "outcome": attempt.outcome,
            "reason_code": attempt.reason_code,
            "verified": verified,
            "message": (
                "Employer-verified submission confirmed."
                if verified
                else "Durable submission state updated."
            ),
        }
    yield f"data: {json.dumps(item, separators=(',', ':'))}\n\n"


@router.get("/applications/{id}/stream", deprecated=True)
async def stream_application_progress(
    id: int,
    db: Session = Depends(get_db),
):
    """Compatibility SSE snapshot backed only by the durable attempt record."""
    app = db.query(Application).filter(Application.id == id).first()
    if not app:
        raise HTTPException(status_code=404, detail=f"Application {id} not found")

    return StreamingResponse(
        _progress_event_generator(id, db),
        media_type="text/event-stream",
    )
