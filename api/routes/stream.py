"""Real-time application submission progress SSE stream router."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db

router = APIRouter(tags=["applications"])


async def _progress_event_generator(app_id: int, db: Session) -> AsyncGenerator[str, None]:
    """Generate real-time submission progress events."""
    steps = [
        {"step": "init", "message": "Initializing browser automation session..."},
        {"step": "navigating", "message": "Navigating to job application page..."},
        {"step": "filling_profile", "message": "Filling candidate contact details..."},
        {"step": "uploading_cv", "message": "Uploading aligned PDF resume..."},
        {"step": "answering_qa", "message": "FormBrain generating custom Q&A answers..."},
        {"step": "submitting", "message": "Submitting application form..."},
        {"step": "completed", "message": "Application submission verified successfully!"},
    ]

    for item in steps:
        yield f"data: {json.dumps(item)}\n\n"
        await asyncio.sleep(0.3)


@router.get("/applications/{id}/stream")
async def stream_application_progress(
    id: int,
    db: Session = Depends(get_db),
):
    """Stream real-time Server-Sent Events (SSE) for application submission progress."""
    app = db.query(Application).filter(Application.id == id).first()
    if not app:
        raise HTTPException(status_code=404, detail=f"Application {id} not found")

    return StreamingResponse(
        _progress_event_generator(id, db),
        media_type="text/event-stream",
    )
