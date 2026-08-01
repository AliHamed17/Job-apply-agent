"""Application re-alignment API route — re-runs CV selection and LLM generation."""

from __future__ import annotations

import json
from pathlib import Path
from profile.cv_content_cache import (
    CVArtifactBindingError,
    load_configured_cv_artifacts,
    require_current_selected_cv_artifact,
)
from profile.cv_routing import (
    RoutingJob,
    load_routing_config,
    parse_required_skills,
    route_cv,
)
from profile.cv_routing_llm import routing_evidence_from_artifacts, select_cv_via_llm
from profile.versioned_snapshot import load_versioned_profile_snapshot

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.application_audit import record_application_event
from core.application_mutations import (
    ApplicationMutationBlockedError,
    ApplicationMutationIntent,
    lock_application_for_mutation,
)
from core.application_revision import application_revision
from core.config import get_settings
from db.models import Application, JobStatus, UserProfileVersion
from db.session import get_db
from jobs.models import JobData as JobDataModel
from llm.generation import generate_full_application

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["applications"])


class RealignRequest(BaseModel):
    forced_cv_id: str | None = Field(default=None, description="Optional CV ID to force align")
    user_guidance: str | None = Field(
        default=None, description="Optional steering instructions for LLM generation"
    )


class RealignResponse(BaseModel):
    id: int
    job_id: int
    job_title: str
    selected_cv_id: str | None
    selected_cv_hash: str | None
    cover_letter: str
    recruiter_message: str
    qa_answers: dict
    cv_routing_confidence: float | None
    cv_routing_evidence: list[str]
    material_eligible: bool
    material_blockers: list[str]
    status: str


@router.post("/applications/{id}/realign", response_model=RealignResponse)
async def realign_application(
    id: int,
    payload: RealignRequest = RealignRequest(),
    db: Session = Depends(get_db),
):
    """Re-run CV routing alignment and regenerate tailored application materials."""
    app = db.query(Application).filter(Application.id == id).first()
    if not app or not app.job:
        raise HTTPException(status_code=404, detail=f"Application {id} not found")
    if app.status in {JobStatus.SUBMITTED, JobStatus.SKIPPED}:
        raise HTTPException(
            status_code=409,
            detail="Terminal applications cannot be realigned.",
        )

    expected_revision = application_revision(app)
    settings = get_settings()
    profile_snapshot = load_versioned_profile_snapshot(db)
    expected_profile_version = profile_snapshot.version
    profile = profile_snapshot.profile
    db_job = app.job
    routing_job = RoutingJob(
        title=db_job.title or "",
        description=" ".join(filter(None, [db_job.description, db_job.requirements])),
        seniority=db_job.seniority or "",
        required_skills=parse_required_skills(db_job.keywords),
    )
    job_ref = JobDataModel(
        title=db_job.title or "",
        company=db_job.company or "",
        location=db_job.location or "",
        employment_type=db_job.employment_type or "",
        seniority=db_job.seniority or "",
        description=db_job.description or "",
        requirements=db_job.requirements or "",
        apply_url=db_job.apply_url or "",
        source_url=db_job.source_url or "",
    )

    # Release the read transaction before local routing or LLM inference.
    # The exact revision is locked and rechecked immediately before writing.
    db.rollback()

    routing_path = Path(settings.cv_routing_path)
    selected_cv_id = payload.forced_cv_id
    confidence = 1.0 if payload.forced_cv_id else 0.0
    routing_margin = 1.0 if payload.forced_cv_id else 0.0
    evidence = ["user_forced_alignment"] if payload.forced_cv_id else []
    fallback_reason = None
    configured_cv_artifacts = {}
    routing_config = None

    if routing_path.exists():
        routing_config = load_routing_config(routing_path)
        configured_cv_artifacts = dict(
            load_configured_cv_artifacts(routing_config, settings.cv_directory)
        )

    if not selected_cv_id and routing_config is not None:
        decision = route_cv(routing_job, routing_config)
        deterministic_cv = (
            configured_cv_artifacts.get(decision.selected_cv_id)
            if decision.selected_cv_id
            else None
        )
        if deterministic_cv is not None:
            decision.selected_cv_hash = deterministic_cv.pdf_sha256
        if settings.llm_cv_alignment and decision.fallback_reason:
            llm_decision = await select_cv_via_llm(
                routing_job,
                routing_config,
                routing_evidence_from_artifacts(configured_cv_artifacts),
            )
            if llm_decision.selected_cv_id is not None:
                if llm_decision.selected_cv_hash is None and llm_decision.fallback_reason is None:
                    llm_decision.fallback_reason = "llm_artifact_unbound"
                decision = llm_decision

        selected_cv_id = decision.selected_cv_id
        confidence = decision.confidence
        routing_margin = 0.0
        evidence = decision.matched_evidence
        fallback_reason = decision.fallback_reason

    selected_cv = configured_cv_artifacts.get(selected_cv_id) if selected_cv_id else None
    if selected_cv_id and selected_cv is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "CV_ARTIFACT_UNAVAILABLE"},
        )
    expected_cv_hash = (
        decision.selected_cv_hash
        if not payload.forced_cv_id and selected_cv_id and routing_config is not None
        else (selected_cv.pdf_sha256 if selected_cv is not None else None)
    )
    if selected_cv is not None:
        try:
            require_current_selected_cv_artifact(
                selected_cv,
                expected_sha256=expected_cv_hash,
            )
        except CVArtifactBindingError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": str(exc)},
            ) from exc
    generated = await generate_full_application(
        job_ref,
        profile,
        cv_artifact=selected_cv.artifact if selected_cv is not None else None,
        profile_version=expected_profile_version,
    )
    if selected_cv is not None:
        try:
            require_current_selected_cv_artifact(
                selected_cv,
                expected_sha256=expected_cv_hash,
            )
        except CVArtifactBindingError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": str(exc)},
            ) from exc

    from core.application_revision import bump_application_revision

    try:
        locked = lock_application_for_mutation(
            db,
            application_id=id,
            intent=ApplicationMutationIntent.CONTENT,
            expected_revision=expected_revision,
        )
    except ApplicationMutationBlockedError as exc:
        db.rollback()
        raise HTTPException(
            status_code=404 if exc.reason_code == "APPLICATION_NOT_FOUND" else 409,
            detail=exc.reason_code,
        ) from exc
    assert locked is not None and locked.job is not None
    app = locked.application
    db_job = locked.job
    latest_profile = (
        db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
    )
    current_profile_version = latest_profile.version if latest_profile else None
    if current_profile_version != expected_profile_version:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="PROFILE_VERSION_CHANGED",
        )
    bump_application_revision(
        db,
        app,
        reason_code="APPLICATION_REALIGNED",
    )
    app.status = JobStatus.DRAFT
    db_job.status = JobStatus.DRAFT
    app.selected_cv_id = selected_cv_id
    app.profile_version = expected_profile_version
    app.cover_letter = generated.cover_letter
    app.recruiter_message = generated.recruiter_message
    app.qa_answers = json.dumps(generated.qa_answers)
    app.cv_routing_confidence = confidence
    app.cv_routing_margin = routing_margin
    app.cv_routing_evidence = json.dumps(evidence)
    app.cv_routing_fallback_reason = fallback_reason
    app.job_fit_decision_id = None
    from core.material_audit import material_review_reason, persist_material_audit

    material_blockers = persist_material_audit(app, generated, selected_cv)
    app.needs_review_reason = material_review_reason(
        selected_cv_id=selected_cv_id,
        routing_fallback_reason=fallback_reason,
        blockers=material_blockers,
        placeholder_fields=generated.placeholder_fields,
    )
    record_application_event(
        db,
        app.id,
        "application_realigned",
        actor="operator",
        details={
            "selected_cv_id": selected_cv_id,
            "selected_cv_hash": app.selected_cv_hash,
            "profile_version": app.profile_version,
            "material_eligible": app.material_eligible,
            "material_blockers": material_blockers,
            "state": "draft",
        },
    )
    db.commit()
    db.refresh(app)

    logger.info(
        "application_realigned",
        application_id=app.id,
        selected_cv_id=selected_cv_id,
        forced=bool(payload.forced_cv_id),
    )

    return RealignResponse(
        id=app.id,
        job_id=app.job_id,
        job_title=db_job.title or "",
        selected_cv_id=app.selected_cv_id,
        selected_cv_hash=app.selected_cv_hash,
        cover_letter=app.cover_letter or "",
        recruiter_message=app.recruiter_message or "",
        qa_answers=json.loads(app.qa_answers) if app.qa_answers else {},
        cv_routing_confidence=app.cv_routing_confidence,
        cv_routing_evidence=json.loads(app.cv_routing_evidence or "[]"),
        material_eligible=bool(app.material_eligible),
        material_blockers=json.loads(app.material_blockers_json or "[]"),
        status=app.status.value if hasattr(app.status, "value") else str(app.status),
    )
