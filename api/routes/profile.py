"""Profile + resume upload routes."""

from __future__ import annotations

from profile.cv_intake import CVIngestError, ingest_cv_from_temp, stream_to_temp
from profile.loader import get_profile

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from db.session import get_db

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.post("/resume")
async def upload_resume(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Accept a CV PDF, rebuild the profile, re-score queued jobs."""
    try:
        tmp = await stream_to_temp(
            lambda: file.read(64 * 1024),
            settings.profile_path.parent,
            settings.max_resume_bytes,
        )
        result = await ingest_cv_from_temp(
            tmp, settings=settings, db=db, max_bytes=settings.max_resume_bytes
        )
    except CVIngestError as exc:
        raise HTTPException(
            status_code=413 if exc.code == "TOO_LARGE" else 422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    logger.info(
        "resume_uploaded", version=result["version"], rescored=result["rescored"]
    )
    return result


@router.get("")
async def get_profile_summary():
    """Full profile summary — used by the dashboard and application form-filling."""
    p = get_profile()
    return {
        "name": p.personal.name,
        "email": p.personal.email,
        "phone": p.personal.phone,
        "location": p.personal.location,
        "linkedin": p.links.linkedin,
        "github": p.links.github,
        "portfolio": p.links.portfolio,
        "resume_pdf": p.resume.pdf_path,
        "roles": p.preferences.roles,
        "keywords": p.preferences.keywords,
        "skills": p.preferences.keywords[:20],
        "has_resume_pdf": bool(p.resume.pdf_path),
    }
