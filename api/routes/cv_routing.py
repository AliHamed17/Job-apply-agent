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

from core.application_audit import record_application_event
from core.application_mutations import (
    ApplicationMutationBlockedError,
    ApplicationMutationIntent,
    lock_application_for_mutation,
)
from core.config import get_settings
from db.models import Application, JobStatus, UserProfileVersion
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


def _persist_decision(db: Session, application: Application, decision: RoutingDecision) -> None:
    from core.application_revision import bump_application_revision

    bump_application_revision(
        db,
        application,
        reason_code="CV_ROUTING_CHANGED",
    )
    latest_profile = (
        db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
    )
    application.selected_cv_id = decision.selected_cv_id
    application.profile_version = latest_profile.version if latest_profile else None
    application.cv_routing_confidence = decision.confidence
    application.cv_routing_evidence = json.dumps(decision.matched_evidence)
    application.cv_routing_fallback_reason = decision.fallback_reason


def _lock_content_mutation(db: Session, application_id: int):
    try:
        locked = lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=ApplicationMutationIntent.CONTENT,
        )
    except ApplicationMutationBlockedError as exc:
        db.rollback()
        raise HTTPException(
            status_code=404 if exc.reason_code == "APPLICATION_NOT_FOUND" else 409,
            detail=exc.reason_code,
        ) from exc
    assert locked is not None
    return locked


@router.post("/cv-routing/preview", response_model=RoutingDecision)
async def preview_cv_routing(payload: RoutingPreviewRequest, db: Session = Depends(get_db)):
    config = _config()
    job = RoutingJob.model_validate(payload.model_dump(exclude={"application_id"}))
    locked = None
    if payload.application_id is not None:
        locked = _lock_content_mutation(db, payload.application_id)
        if locked.job:
            job = RoutingJob(
                title=locked.job.title or "",
                description=" ".join(
                    filter(None, [locked.job.description, locked.job.requirements])
                ),
                seniority=locked.job.seniority or "",
                required_skills=parse_required_skills(locked.job.keywords),
            )
    decision = route_cv(job, config)
    if locked is not None:
        application = locked.application
        _persist_decision(db, application, decision)
        application.status = JobStatus.DRAFT
        if locked.job:
            locked.job.status = JobStatus.DRAFT
        record_application_event(
            db,
            application.id,
            "cv_routing_changed",
            actor="operator",
            details={
                "selected_cv_id": application.selected_cv_id,
                "profile_version": application.profile_version,
                "state": "draft",
            },
        )
        db.commit()
    return decision


@router.post("/applications/{application_id}/cv-override")
async def override_application_cv(
    application_id: int,
    payload: CVOverrideRequest,
    db: Session = Depends(get_db),
):
    config = _config()
    cv = next((item for item in config.cvs if item.id == payload.cv_id), None)
    if not cv:
        raise HTTPException(status_code=422, detail="Unknown CV id")
    locked = _lock_content_mutation(db, application_id)
    application = locked.application
    from core.application_revision import bump_application_revision

    bump_application_revision(
        db,
        application,
        reason_code="CV_OVERRIDE_CHANGED",
    )
    application.cv_override_id = cv.id
    application.selected_cv_id = cv.id
    application.cv_routing_confidence = 1.0
    application.cv_routing_evidence = json.dumps(["user_override"])
    application.cv_routing_fallback_reason = None
    latest = db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
    application.profile_version = latest.version if latest else None
    application.status = JobStatus.DRAFT
    if locked.job:
        locked.job.status = JobStatus.DRAFT
    record_application_event(
        db,
        application.id,
        "cv_override_changed",
        actor="operator",
        details={
            "selected_cv_id": application.selected_cv_id,
            "profile_version": application.profile_version,
            "state": "draft",
        },
    )
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
    from core.application_audit import record_application_event

    record_application_event(
        db,
        application.id,
        "application_outcome_recorded",
        actor="operator",
        details={"state": payload.outcome},
    )
    db.commit()
    return {
        "message": "Outcome recorded",
        "application_id": application.id,
        "outcome": application.outcome,
    }
