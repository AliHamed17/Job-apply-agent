"""Applications API routes — list, approve, reject, view drafts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from profile.cv_routing import load_routing_config
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from core.application_state import (
    application_semantic_status,
    prepared_applications_query,
    reviewable_applications_query,
)
from core.config import get_settings
from core.submission_truth import is_employer_verified
from db.models import Application, JobStatus, Submission, SubmissionStatus
from db.session import get_db
from submitters.platforms import detect_platform

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["applications"])


class SubmissionAttemptResponse(BaseModel):
    id: int
    attempt_number: int
    idempotency_key: str
    status: str
    verified: bool
    platform: str
    reason_code: str | None
    started_at: str | None
    finished_at: str | None
    submitted_at: str | None
    selected_cv_id: str | None
    profile_version: int | None
    confirmation_id: str | None
    confirmation_url: str | None
    diagnostics: dict = Field(default_factory=dict)


class ApplicationEventResponse(BaseModel):
    event_type: str
    actor: str
    details: dict = Field(default_factory=dict)
    created_at: str


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    job_title: str
    job_company: str
    job_score: float | None
    cover_letter: str
    recruiter_message: str
    qa_answers: dict
    status: str
    apply_url: str
    approved_at: str | None
    created_at: str
    submission_status: str | None = None
    submission_platform: str | None = None
    submission_confirmation_url: str | None = None
    submission_error: str | None = None
    submitted_at: str | None = None
    submission_verified: bool = False
    attempts: list[SubmissionAttemptResponse] = Field(default_factory=list)
    selected_cv_id: str | None = None
    profile_version: int | None = None
    cv_routing_confidence: float | None = None
    cv_routing_evidence: list[str] = Field(default_factory=list)
    cv_routing_fallback_reason: str | None = None
    cv_override_id: str | None = None
    approval_source: str | None = None
    platform: str = "unknown"
    portal_session_ready: bool | None = None
    events: list[ApplicationEventResponse] = Field(default_factory=list)


class ApproveResponse(BaseModel):
    message: str
    application_id: int
    state: str
    status: str
    verified: bool = False
    attempt_id: int | None = None
    status_url: str | None = None


class ReconcileRequest(BaseModel):
    outcome: str
    note: str = Field(min_length=3, max_length=500)
    source: Literal["candidate_portal", "email", "manual_check"] = "manual_check"
    reference: str | None = Field(default=None, max_length=500)


class BatchApproveRequest(BaseModel):
    application_ids: list[int] = Field(min_length=1, max_length=50)
    acknowledgement: Literal[
        "PREPARE_SELECTED_APPLICATIONS",
        "APPROVE_SELECTED_APPLICATIONS",
    ]


class BatchApproveResponse(BaseModel):
    message: str
    prepared_application_ids: list[int] = Field(default_factory=list)
    # Compatibility fields retained while older dashboard builds are retired.
    queued_application_ids: list[int] = Field(default_factory=list)
    failed_application_ids: list[int] = Field(default_factory=list)


def _json_dict(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _attempt_response(attempt) -> SubmissionAttemptResponse:
    verified = is_employer_verified(attempt)
    return SubmissionAttemptResponse(
        id=attempt.id,
        attempt_number=attempt.attempt_number,
        idempotency_key=attempt.idempotency_key,
        status=attempt.status.value,
        verified=verified,
        platform=attempt.submitter_name,
        reason_code=attempt.reason_code,
        started_at=attempt.started_at.isoformat() if attempt.started_at else None,
        finished_at=attempt.finished_at.isoformat() if attempt.finished_at else None,
        submitted_at=(
            attempt.submitted_at.isoformat() if verified and attempt.submitted_at else None
        ),
        selected_cv_id=attempt.selected_cv_id,
        profile_version=attempt.profile_version,
        confirmation_id=attempt.confirmation_id,
        confirmation_url=attempt.confirmation_url,
        diagnostics=_json_dict(attempt.diagnostic_details),
    )


def _attempt_history(app) -> list[SubmissionAttemptResponse]:
    return [_attempt_response(attempt) for attempt in app.submissions]


def _event_history(app) -> list[ApplicationEventResponse]:
    return [
        ApplicationEventResponse(
            event_type=event.event_type,
            actor=event.actor,
            details=_json_dict(event.details),
            created_at=event.created_at.isoformat() if event.created_at else "",
        )
        for event in app.events
    ]


def _portal_status(app) -> tuple[str, bool | None]:
    url = app.job.apply_url if app.job else ""
    platform = detect_platform(url)
    if platform != "workday":
        return platform, None
    from core.portal_sessions import PortalSessionError, portal_session_for_url

    try:
        settings = get_settings()
        session = portal_session_for_url(url, settings.portal_browser_profile_root)
        return platform, session.ready
    except PortalSessionError:
        return platform, False


def _validate_selected_cv(app: Application) -> None:
    if not app.selected_cv_id:
        raise HTTPException(
            status_code=409,
            detail="Preview or override CV routing before approval.",
        )
    settings = get_settings()
    try:
        config = load_routing_config(settings.cv_routing_path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="CV routing configuration is unavailable.",
        ) from exc
    selected = next((cv for cv in config.cvs if cv.id == app.selected_cv_id), None)
    root = Path(settings.cv_directory).resolve()
    candidate = (root / selected.file).resolve() if selected else None
    if not candidate or candidate.parent != root or not candidate.is_file():
        raise HTTPException(status_code=409, detail="Selected CV file is unavailable.")


def _prepare_response(application_id: int) -> ApproveResponse:
    return ApproveResponse(
        message="Application prepared for review; no submission was queued.",
        application_id=application_id,
        state="prepared",
        status="prepared",
        verified=False,
        attempt_id=None,
        status_url=None,
    )


def _record_prepared(
    db: Session,
    app: Application,
    *,
    actor: str,
    source: str,
    event_type: str = "application_prepared",
) -> None:
    """Record operator review without making the application worker-eligible."""
    app.status = JobStatus.DRAFT
    app.approved_at = datetime.now(UTC).replace(tzinfo=None)
    app.approval_source = source
    if app.job:
        app.job.status = JobStatus.DRAFT

    from core.application_audit import record_application_event

    record_application_event(
        db,
        app.id,
        event_type,
        actor=actor,
        details={
            "approval_source": source,
            "selected_cv_id": app.selected_cv_id,
            "profile_version": app.profile_version,
            "state": "prepared",
            "external_action_queued": False,
        },
    )
    db.commit()


@router.get("/applications", response_model=list[ApplicationResponse])
async def list_applications(
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """List all applications with job details."""
    if status:
        normalized_status = status.strip().lower()
        if normalized_status == JobStatus.DRAFT.value:
            query = reviewable_applications_query(db)
        elif normalized_status in {"prepared", JobStatus.APPROVED.value}:
            query = prepared_applications_query(db)
        else:
            query = db.query(Application)
            try:
                status_enum = JobStatus(normalized_status)
                query = query.filter(Application.status == status_enum)
            except ValueError:
                pass
    else:
        query = db.query(Application)

    apps = query.order_by(Application.created_at.desc()).limit(100).all()

    results = []
    for app in apps:
        job = app.job
        submission = app.submission
        platform, session_ready = _portal_status(app)
        results.append(
            ApplicationResponse(
                id=app.id,
                job_id=app.job_id,
                job_title=job.title if job else "",
                job_company=job.company if job else "",
                job_score=job.score if job else None,
                cover_letter=app.cover_letter or "",
                recruiter_message=app.recruiter_message or "",
                qa_answers=_json_dict(app.qa_answers),
                status=application_semantic_status(app),
                apply_url=job.apply_url if job else "",
                approved_at=app.approved_at.isoformat() if app.approved_at else None,
                created_at=app.created_at.isoformat() if app.created_at else "",
                submission_status=submission.status.value if submission else None,
                submission_platform=submission.submitter_name if submission else None,
                submission_confirmation_url=submission.confirmation_url if submission else None,
                submission_error=submission.error_message if submission else None,
                submitted_at=(
                    submission.submitted_at.isoformat()
                    if submission and is_employer_verified(submission)
                    else None
                ),
                submission_verified=is_employer_verified(submission),
                attempts=_attempt_history(app),
                selected_cv_id=app.selected_cv_id,
                profile_version=app.profile_version,
                cv_routing_confidence=app.cv_routing_confidence,
                cv_routing_evidence=_json_list(app.cv_routing_evidence),
                cv_routing_fallback_reason=app.cv_routing_fallback_reason,
                cv_override_id=app.cv_override_id,
                approval_source=app.approval_source,
                platform=platform,
                portal_session_ready=session_ready,
                events=_event_history(app),
            )
        )

    return results


@router.get("/applications/{app_id}", response_model=ApplicationResponse)
async def get_application(app_id: int, db: Session = Depends(get_db)):
    """Get a single application with full details."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    job = app.job
    submission = app.submission
    platform, session_ready = _portal_status(app)
    return ApplicationResponse(
        id=app.id,
        job_id=app.job_id,
        job_title=job.title if job else "",
        job_company=job.company if job else "",
        job_score=job.score if job else None,
        cover_letter=app.cover_letter or "",
        recruiter_message=app.recruiter_message or "",
        qa_answers=_json_dict(app.qa_answers),
        status=application_semantic_status(app),
        apply_url=job.apply_url if job else "",
        approved_at=app.approved_at.isoformat() if app.approved_at else None,
        created_at=app.created_at.isoformat() if app.created_at else "",
        submission_status=submission.status.value if submission else None,
        submission_platform=submission.submitter_name if submission else None,
        submission_confirmation_url=submission.confirmation_url if submission else None,
        submission_error=submission.error_message if submission else None,
        submitted_at=(
            submission.submitted_at.isoformat()
            if submission and is_employer_verified(submission)
            else None
        ),
        submission_verified=is_employer_verified(submission),
        attempts=_attempt_history(app),
        selected_cv_id=app.selected_cv_id,
        profile_version=app.profile_version,
        cv_routing_confidence=app.cv_routing_confidence,
        cv_routing_evidence=_json_list(app.cv_routing_evidence),
        cv_routing_fallback_reason=app.cv_routing_fallback_reason,
        cv_override_id=app.cv_override_id,
        approval_source=app.approval_source,
        platform=platform,
        portal_session_ready=session_ready,
        events=_event_history(app),
    )


@router.post(
    "/applications/{app_id}/prepare",
    response_model=ApproveResponse,
    status_code=202,
)
@router.post(
    "/applications/{app_id}/approve",
    response_model=ApproveResponse,
    status_code=202,
    deprecated=True,
)
async def approve_application(app_id: int, db: Session = Depends(get_db)):
    """Prepare an application without queueing or performing an external action."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    if app.status in (JobStatus.SUBMITTED, JobStatus.SKIPPED):
        raise HTTPException(
            status_code=409,
            detail="A terminal application cannot be prepared.",
        )
    if app.status not in (JobStatus.DRAFT, JobStatus.APPROVED):
        raise HTTPException(
            status_code=409,
            detail="Only a reviewable draft can be prepared.",
        )
    if (
        app.status == JobStatus.DRAFT
        and app.approved_at is not None
        and app.approval_source in {"manual_prepare", "batch_prepare", "retry_prepare"}
    ):
        return _prepare_response(app.id)
    _validate_selected_cv(app)
    _record_prepared(
        db,
        app,
        actor="operator",
        source="manual_prepare",
    )

    logger.info("application_prepared_via_api", app_id=app.id)
    return _prepare_response(app.id)


@router.post(
    "/applications/batch-prepare",
    response_model=BatchApproveResponse,
    status_code=202,
)
@router.post(
    "/applications/batch-approve",
    response_model=BatchApproveResponse,
    status_code=202,
    deprecated=True,
)
async def batch_approve_applications(
    payload: BatchApproveRequest,
    db: Session = Depends(get_db),
):
    """Prepare an exact reviewed set without queueing any external action."""
    application_ids = list(dict.fromkeys(payload.application_ids))
    apps = db.query(Application).filter(Application.id.in_(application_ids)).all()
    by_id = {app.id: app for app in apps}
    missing = [application_id for application_id in application_ids if application_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail="One or more applications were not found.")

    for application_id in application_ids:
        app = by_id[application_id]
        if app.status != JobStatus.DRAFT or app.approved_at is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Application {application_id} is not a reviewable draft.",
            )
        _validate_selected_cv(app)

    now = datetime.now(UTC).replace(tzinfo=None)
    from core.application_audit import record_application_event

    for application_id in application_ids:
        app = by_id[application_id]
        app.approved_at = now
        app.approval_source = "batch_prepare"
        if app.job:
            app.job.status = JobStatus.DRAFT
        record_application_event(
            db,
            app.id,
            "application_prepared",
            actor="batch_operator",
            details={
                "approval_source": "batch_prepare",
                "selected_cv_id": app.selected_cv_id,
                "profile_version": app.profile_version,
                "state": "prepared",
                "external_action_queued": False,
            },
        )
    db.commit()

    return BatchApproveResponse(
        message=f"{len(application_ids)} application(s) prepared; nothing was queued.",
        prepared_application_ids=application_ids,
    )


@router.post("/applications/{app_id}/submit", status_code=409)
async def submit_application(app_id: int, db: Session = Depends(get_db)):
    """Fail closed until the permit-backed submission command exists."""
    if db.get(Application, app_id) is None:
        raise HTTPException(status_code=404, detail="Application not found")
    raise HTTPException(
        status_code=409,
        detail={
            "code": "SUBMIT_PERMIT_REQUIRED",
            "message": (
                "Final submission is unavailable until a verified form plan and permit exist."
            ),
        },
    )


@router.post(
    "/applications/{app_id}/retry",
    response_model=ApproveResponse,
    status_code=202,
)
async def retry_application(app_id: int, db: Session = Depends(get_db)):
    """Prepare a definitively retryable application; sending remains explicit."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    latest = app.submission
    if latest is None or latest.status not in (
        SubmissionStatus.FAILED,
        SubmissionStatus.DRAFT_ONLY,
    ):
        raise HTTPException(
            status_code=409,
            detail="Only definitively failed or draft-only attempts may be retried.",
        )
    _validate_selected_cv(app)
    _record_prepared(
        db,
        app,
        actor="operator",
        source="retry_prepare",
        event_type="submission_retry_prepared",
    )

    logger.info(
        "application_retry_prepared",
        app_id=app.id,
        previous_attempt_number=latest.attempt_number,
    )
    return _prepare_response(app.id)


@router.get(
    "/submission-attempts/{attempt_id}",
    response_model=SubmissionAttemptResponse,
)
async def get_submission_attempt(
    attempt_id: int,
    db: Session = Depends(get_db),
):
    """Return the durable attempt state used by truthful dashboard polling."""
    attempt = db.query(Submission).filter(Submission.id == attempt_id).first()
    if attempt is None:
        raise HTTPException(status_code=404, detail="Submission attempt not found")
    return _attempt_response(attempt)


@router.post("/applications/{app_id}/reconcile")
async def reconcile_application(
    app_id: int,
    payload: ReconcileRequest,
    db: Session = Depends(get_db),
):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app or not app.submission:
        raise HTTPException(status_code=404, detail="Application attempt not found")
    attempt = app.submission
    if attempt.status != SubmissionStatus.UNKNOWN:
        raise HTTPException(status_code=409, detail="Only unknown attempts require reconciliation")
    if payload.outcome not in ("confirmed_submitted", "confirmed_not_submitted"):
        raise HTTPException(status_code=422, detail="Unsupported reconciliation outcome")

    now = datetime.now(UTC).replace(tzinfo=None)
    attempt.reconciled_at = now
    reconciliation = {
        "source": payload.source,
        "reference": payload.reference,
        "note": payload.note,
    }
    attempt.reconciliation_note = json.dumps(
        reconciliation,
        separators=(",", ":"),
        sort_keys=True,
    )
    attempt.finished_at = now
    if payload.outcome == "confirmed_submitted":
        attempt.status = SubmissionStatus.SUCCESS
        attempt.reason_code = "OPERATOR_CONFIRMED_SUBMITTED"
        attempt.submitted_at = None
        app.status = JobStatus.SUBMITTED
        if app.job:
            app.job.status = JobStatus.SUBMITTED
    else:
        attempt.status = SubmissionStatus.FAILED
        attempt.reason_code = "RECONCILED_NOT_SUBMITTED"
        app.status = JobStatus.DRAFT
        if app.job:
            app.job.status = JobStatus.DRAFT
    from core.application_audit import record_application_event

    record_application_event(
        db,
        app.id,
        "submission_reconciled",
        actor="operator",
        details={
            "attempt_number": attempt.attempt_number,
            "reason_code": attempt.reason_code,
            "verification_kind": "operator_confirmed",
            "state": attempt.status.value,
        },
    )
    db.commit()
    return {
        "message": "Submission attempt reconciled",
        "application_id": app.id,
        "outcome": payload.outcome,
        "verified": False,
        "verification_kind": "operator_confirmed",
    }


@router.post("/applications/{app_id}/reject")
async def reject_application(
    app_id: int,
    reason: str = "Rejected by user",
    db: Session = Depends(get_db),
):
    """Reject / skip an application."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    app.status = JobStatus.SKIPPED
    app.rejected_at = datetime.utcnow()
    app.rejection_reason = reason

    job = app.job
    if job:
        job.status = JobStatus.SKIPPED

    from core.application_audit import record_application_event

    record_application_event(
        db,
        app.id,
        "application_rejected",
        actor="operator",
        details={"state": "skipped"},
    )
    db.commit()
    logger.info("application_rejected_via_api", app_id=app.id, reason=reason)
    return {"message": "Application rejected", "application_id": app.id}


class OutcomeRequest(BaseModel):
    outcome: str
    note: str | None = None


@router.post("/applications/{app_id}/outcome")
async def record_application_outcome(
    app_id: int,
    payload: OutcomeRequest,
    db: Session = Depends(get_db),
):
    """Record candidate response outcome (e.g. interview invitation) for self-tuning metrics."""
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    app.outcome = payload.outcome
    app.outcome_note = payload.note
    from core.application_audit import record_application_event

    record_application_event(
        db,
        app.id,
        "application_outcome_recorded",
        actor="operator",
        details={"state": payload.outcome},
    )
    db.commit()

    logger.info("application_outcome_recorded", app_id=app.id, outcome=payload.outcome)
    return {"message": "Outcome recorded", "application_id": app.id, "outcome": payload.outcome}
