"""Profile + resume upload routes."""

from __future__ import annotations

from profile.builder import build_profile_from_pdf
from profile.loader import get_profile
from profile.writer import save_profile

import structlog
from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from db.session import get_db
from worker.rescore import rescore_pending_jobs

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.post("/resume")
async def upload_resume(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Accept a CV PDF, rebuild the profile, re-score queued jobs."""
    yaml_path = settings.profile_path
    pdf_path = yaml_path.parent / "resume.pdf"
    pdf_path.write_bytes(await file.read())

    profile = await build_profile_from_pdf(str(pdf_path))
    profile.resume.pdf_path = str(pdf_path)
    version = save_profile(profile, yaml_path, db=db)
    rescored = rescore_pending_jobs(db, profile)

    logger.info("resume_uploaded", version=version, rescored=rescored)
    return {
        "version": version,
        "name": profile.personal.name,
        "roles": profile.preferences.roles,
        "keywords_count": len(profile.preferences.keywords),
        "rescored": rescored,
    }


@router.get("")
async def get_profile_summary():
    p = get_profile()
    return {
        "name": p.personal.name,
        "location": p.personal.location,
        "roles": p.preferences.roles,
        "keywords": p.preferences.keywords,
        "has_resume_pdf": bool(p.resume.pdf_path),
    }
