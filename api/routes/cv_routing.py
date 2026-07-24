"""CV routing preview, override, and measured-quality APIs."""

from __future__ import annotations

import json
from profile.cv_routing import (
    RoutingDecision,
    RoutingJob,
    load_routing_config,
    parse_required_skills,
    route_cv,
)

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Application, UserProfileVersion
from db.session import get_db

router = APIRouter(tags=["cv-routing"])


class RoutingPreviewRequest(RoutingJob):
    application_id: int | None = None


class CVOverrideRequest(BaseModel):
    cv_id: str


class OutcomeRequest(BaseModel):
    outcome: str
    note: str | None = Field(default=None, max_length=500)


def _config():
    settings = get_settings()
    try:
        return load_routing_config(settings.cv_routing_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=409,
            detail="Personal CV routing configuration is not configured.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid CV routing configuration: {exc}"
        ) from exc


def _persist_decision(
    db: Session, application: Application, decision: RoutingDecision
) -> None:
    latest_profile = (
        db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
    )
    application.selected_cv_id = decision.selected_cv_id
    application.profile_version = latest_profile.version if latest_profile else None
    application.cv_routing_confidence = decision.confidence
    application.cv_routing_evidence = json.dumps(decision.matched_evidence)
    application.cv_routing_fallback_reason = decision.fallback_reason


@router.post("/cv-routing/preview", response_model=RoutingDecision)
async def preview_cv_routing(
    payload: RoutingPreviewRequest, db: Session = Depends(get_db)
):
    config = _config()
    job = RoutingJob.model_validate(payload.model_dump(exclude={"application_id"}))
    application = None
    if payload.application_id is not None:
        application = db.query(Application).filter(Application.id == payload.application_id).first()
        if not application:
            raise HTTPException(status_code=404, detail="Application not found")
        if application.job:
            job = RoutingJob(
                title=application.job.title or "",
                description=" ".join(
                    filter(None, [application.job.description, application.job.requirements])
                ),
                seniority=application.job.seniority or "",
                required_skills=parse_required_skills(application.job.keywords),
            )
    decision = route_cv(job, config)
    if application:
        _persist_decision(db, application, decision)
        db.commit()
    return decision


@router.post("/applications/{application_id}/cv-override")
async def override_application_cv(
    application_id: int,
    payload: CVOverrideRequest,
    db: Session = Depends(get_db),
):
    application = db.query(Application).filter(Application.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    config = _config()
    cv = next((item for item in config.cvs if item.id == payload.cv_id), None)
    if not cv:
        raise HTTPException(status_code=422, detail="Unknown CV id")
    application.cv_override_id = cv.id
    application.selected_cv_id = cv.id
    application.cv_routing_confidence = 1.0
    application.cv_routing_evidence = json.dumps(["user_override"])
    application.cv_routing_fallback_reason = None
    latest = db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
    application.profile_version = latest.version if latest else None
    db.commit()
    return {"application_id": application.id, "selected_cv_id": cv.id}


@router.post("/applications/{application_id}/outcome")
async def record_application_outcome(
    application_id: int,
    payload: OutcomeRequest,
    db: Session = Depends(get_db),
):
    allowed = {"interview", "rejected", "withdrawn", "offer", "user_correction"}
    if payload.outcome not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported outcome")
    application = db.query(Application).filter(Application.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    application.outcome = payload.outcome
    application.outcome_note = payload.note
    db.commit()
    return {"application_id": application.id, "outcome": application.outcome}
