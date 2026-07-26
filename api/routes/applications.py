"""Applications API routes — list, approve, reject, view drafts."""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from profile.cv_routing import load_routing_config
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, PositiveInt
from sqlalchemy.orm import Session

from core.application_mutations import (
    ApplicationMutationBlockedError,
    ApplicationMutationIntent,
    LockedApplicationMutation,
    lock_application_for_mutation,
    mark_locked_application_prepared,
    transition_locked_application_to_skipped,
)
from core.application_revision import preparation_is_current
from core.application_state import (
    application_semantic_status,
    prepared_applications_query,
    reviewable_applications_query,
)
from core.config import get_settings
from core.submission_service import (
    ClientReleaseIdentity,
    SubmissionAdmissionError,
    SubmissionCommandRequest,
    create_submission_commands,
    reconstruct_persisted_form_plan,
)
from core.submission_truth import is_employer_verified
from db.models import (
    Application,
    FormPlan,
    JobStatus,
    Submission,
    SubmissionStatus,
)
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
    stage: str
    outcome: str | None
    adapter_version: str | None
    selector_version: str | None
    form_plan_id: str | None
    form_plan_fingerprint: str | None
    application_revision: int
    requested_cv_id: str | None
    requested_cv_hash: str | None
    attached_cv_id: str | None
    attached_cv_hash: str | None
    attachment_verified: bool
    final_action_at: str | None
    verification_kind: str | None
    evidence_digest: str | None
    runner_release: str | None
    reconciliation_source: str | None
    reconciliation_evidence_ref: str | None


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
    revision: int = 1
    prepared_revision: int | None = None
    form_plan_id: str | None = None
    form_plan_fingerprint: str | None = None
    form_plan_valid: bool = False


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
    reference: str | None = Field(default=None, max_length=255)


class ClientReleaseIdentityRequest(BaseModel):
    build_sha: str = Field(min_length=1, max_length=64)
    ui_asset_digest: str = Field(min_length=1, max_length=80)
    source_digest: str = Field(min_length=1, max_length=80)
    protocol_version: str = Field(min_length=1, max_length=64)
    boot_id: str = Field(min_length=1, max_length=64)


class SubmitApplicationRequest(BaseModel):
    acknowledgement: Literal["SEND_APPLICATION"]
    idempotency_key: str = Field(min_length=8, max_length=128)
    application_revision: PositiveInt
    form_plan_id: str = Field(min_length=36, max_length=36)
    client_release: ClientReleaseIdentityRequest


class SubmitAcceptedResponse(BaseModel):
    application_id: int
    attempt_id: int
    command_id: int
    state: Literal["queued"] = "queued"
    verified: Literal[False] = False
    status_url: str
    replayed: bool = False


class BatchSubmitItem(BaseModel):
    application_id: PositiveInt
    idempotency_key: str = Field(min_length=8, max_length=128)
    application_revision: PositiveInt
    form_plan_id: str = Field(min_length=36, max_length=36)


class BatchSubmitRequest(BaseModel):
    acknowledgement: Literal["SEND_SELECTED_APPLICATIONS"]
    applications: list[BatchSubmitItem] = Field(min_length=1, max_length=50)
    client_release: ClientReleaseIdentityRequest


class BatchSubmitAcceptedResponse(BaseModel):
    state: Literal["queued"] = "queued"
    verified: Literal[False] = False
    attempts: list[SubmitAcceptedResponse]


class FormPlanResponse(BaseModel):
    plan_id: str
    application_id: int
    application_revision: int
    adapter_name: str
    adapter_version: str
    selector_version: str
    fingerprint: str
    selected_cv_id: str
    selected_cv_hash: str
    attached_cv_id: str | None
    attached_cv_hash: str | None
    attachment_verified: bool
    profile_version: int | None
    fields: list = Field(default_factory=list)
    decisions: list = Field(default_factory=list)
    blockers: list = Field(default_factory=list)
    session_verified_at: str | None
    created_at: str
    expires_at: str
    invalidated_at: str | None
    invalidation_reason: str | None
    valid: bool


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
        stage=attempt.stage,
        outcome=attempt.outcome,
        adapter_version=attempt.adapter_version,
        selector_version=attempt.selector_version,
        form_plan_id=(attempt.form_plan.plan_id if attempt.form_plan else None),
        form_plan_fingerprint=attempt.form_plan_fingerprint,
        application_revision=attempt.application_revision,
        requested_cv_id=attempt.requested_cv_id,
        requested_cv_hash=attempt.requested_cv_hash,
        attached_cv_id=attempt.attached_cv_id,
        attached_cv_hash=attempt.attached_cv_hash,
        attachment_verified=attempt.attachment_verified,
        final_action_at=(attempt.final_action_at.isoformat() if attempt.final_action_at else None),
        verification_kind=attempt.verification_kind,
        evidence_digest=attempt.evidence_digest,
        runner_release=attempt.runner_release,
        reconciliation_source=attempt.reconciliation_source,
        reconciliation_evidence_ref=attempt.reconciliation_evidence_ref,
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


def _form_plan_valid(plan: FormPlan | None, app: Application) -> bool:
    if plan is None:
        return False
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        domain_plan = reconstruct_persisted_form_plan(plan)
        domain_ready = domain_plan.ready_for_permit_at(now.replace(tzinfo=UTC))
    except (SubmissionAdmissionError, TypeError, ValueError):
        return False
    return (
        plan.invalidated_at is None
        and plan.expires_at > now
        and plan.application_revision == app.revision
        and app.prepared_revision == app.revision
        and plan.selected_cv_id == app.selected_cv_id
        and plan.profile_version == app.profile_version
        and domain_ready
    )


def _latest_form_plan(app: Application) -> FormPlan | None:
    return app.form_plans[-1] if app.form_plans else None


def _form_plan_response(plan: FormPlan, app: Application) -> FormPlanResponse:
    return FormPlanResponse(
        plan_id=plan.plan_id,
        application_id=plan.application_id,
        application_revision=plan.application_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        fingerprint=plan.fingerprint,
        selected_cv_id=plan.selected_cv_id,
        selected_cv_hash=plan.selected_cv_hash,
        attached_cv_id=plan.attached_cv_id,
        attached_cv_hash=plan.attached_cv_hash,
        attachment_verified=plan.attachment_verified,
        profile_version=plan.profile_version,
        fields=_json_list(plan.fields_json),
        decisions=_json_list(plan.decisions_json),
        blockers=_json_list(plan.blockers_json),
        session_verified_at=(
            plan.session_verified_at.isoformat() if plan.session_verified_at else None
        ),
        created_at=plan.created_at.isoformat(),
        expires_at=plan.expires_at.isoformat(),
        invalidated_at=(plan.invalidated_at.isoformat() if plan.invalidated_at else None),
        invalidation_reason=plan.invalidation_reason,
        valid=_form_plan_valid(plan, app),
    )


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
    locked: LockedApplicationMutation,
    *,
    actor: str,
    source: str,
    event_type: str = "application_prepared",
    allowed_statuses: frozenset[JobStatus] = frozenset({JobStatus.DRAFT, JobStatus.APPROVED}),
) -> None:
    """Record operator review without making the application worker-eligible."""
    mark_locked_application_prepared(
        db,
        locked,
        actor=actor,
        source=source,
        event_type=event_type,
        allowed_statuses=allowed_statuses,
    )
    db.commit()


_MUTATION_MESSAGES = {
    "APPLICATION_TERMINAL": "A terminal application cannot be changed.",
    "APPLICATION_NOT_REVIEWABLE": "Only a reviewable application can be prepared.",
    "APPLICATION_REVISION_CHANGED": "The application changed; review the latest version.",
    "SUBMISSION_ALREADY_ACTIVE": "An active submission attempt cannot be changed.",
    "SUBMISSION_OUTCOME_UNKNOWN": (
        "Reconcile the unknown submission outcome before changing this application."
    ),
    "SUBMISSION_OUTCOME_IMMUTABLE": "The recorded submission outcome is immutable.",
    "FINAL_ACTION_INDETERMINATE": (
        "The final action is indeterminate; wait for verification or reconcile."
    ),
    "SUBMISSION_LIFECYCLE_BUSY": "Submission state is changing; try again.",
    "SUBMISSION_STATE_INVALID": "The submission lifecycle cannot be safely changed.",
}


def _lock_mutation_or_http(
    db: Session,
    *,
    application_id: int,
    intent: ApplicationMutationIntent,
    expected_revision: int | None = None,
) -> LockedApplicationMutation:
    try:
        locked = lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=intent,
            expected_revision=expected_revision,
        )
    except ApplicationMutationBlockedError as exc:
        db.rollback()
        raise HTTPException(
            status_code=404 if exc.reason_code == "APPLICATION_NOT_FOUND" else 409,
            detail=_MUTATION_MESSAGES.get(exc.reason_code, exc.reason_code),
        ) from exc
    assert locked is not None
    return locked


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
        form_plan = _latest_form_plan(app)
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
                revision=app.revision,
                prepared_revision=app.prepared_revision,
                form_plan_id=form_plan.plan_id if form_plan else None,
                form_plan_fingerprint=form_plan.fingerprint if form_plan else None,
                form_plan_valid=_form_plan_valid(form_plan, app),
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
    form_plan = _latest_form_plan(app)
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
        revision=app.revision,
        prepared_revision=app.prepared_revision,
        form_plan_id=form_plan.plan_id if form_plan else None,
        form_plan_fingerprint=form_plan.fingerprint if form_plan else None,
        form_plan_valid=_form_plan_valid(form_plan, app),
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
    locked = _lock_mutation_or_http(
        db,
        application_id=app_id,
        intent=ApplicationMutationIntent.PREPARE,
    )
    app = locked.application
    if app.status not in (JobStatus.DRAFT, JobStatus.APPROVED):
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Only a reviewable draft can be prepared.",
        )
    if (
        app.status == JobStatus.DRAFT
        and app.approved_at is not None
        and preparation_is_current(app)
        and app.approval_source in {"manual_prepare", "batch_prepare", "retry_prepare"}
    ):
        db.rollback()
        return _prepare_response(app.id)
    try:
        _validate_selected_cv(app)
    except HTTPException:
        db.rollback()
        raise
    _record_prepared(
        db,
        locked,
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
    locked_by_id: dict[int, LockedApplicationMutation] = {}
    try:
        # Stable lock order prevents two overlapping reviewed batches from
        # deadlocking while preserving the exact caller-selected response.
        for application_id in sorted(application_ids):
            locked_by_id[application_id] = _lock_mutation_or_http(
                db,
                application_id=application_id,
                intent=ApplicationMutationIntent.PREPARE,
            )

        for application_id in application_ids:
            app = locked_by_id[application_id].application
            if app.status != JobStatus.DRAFT or app.approved_at is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Application {application_id} is not a reviewable draft.",
                )
            _validate_selected_cv(app)
    except HTTPException:
        db.rollback()
        raise

    now = datetime.now(UTC).replace(tzinfo=None)
    for application_id in application_ids:
        mark_locked_application_prepared(
            db,
            locked_by_id[application_id],
            actor="batch_operator",
            source="batch_prepare",
            now=now,
        )
    db.commit()

    return BatchApproveResponse(
        message=f"{len(application_ids)} application(s) prepared; nothing was queued.",
        prepared_application_ids=application_ids,
    )


@router.get(
    "/applications/{app_id}/form-plan",
    response_model=FormPlanResponse,
)
async def get_application_form_plan(
    app_id: int,
    db: Session = Depends(get_db),
):
    """Return the latest local, private form plan and its current validity."""
    app = db.get(Application, app_id)
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")
    plan = _latest_form_plan(app)
    if plan is None:
        raise HTTPException(status_code=404, detail="Form plan not found")
    return _form_plan_response(plan, app)


def _admission_http_error(exc: SubmissionAdmissionError) -> HTTPException:
    status_code = (
        404 if exc.reason_code in {"APPLICATION_NOT_FOUND", "FORM_PLAN_NOT_FOUND"} else 409
    )
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.reason_code, "message": exc.message},
    )


def _wake_submission_command(command_id: int) -> None:
    """Best-effort broker wake; the committed database command is authoritative."""
    from worker.submission_commands import execute_submission_command_task

    try:
        execute_submission_command_task.delay(command_id)
    except Exception:
        logger.exception(
            "submission_command_wake_failed",
            command_id=command_id,
        )


def _require_live_operator_auth(request: Request) -> None:
    """Independently authenticate every irreversible operator command."""

    settings = get_settings()
    if not settings.operator_auth_configured:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "OPERATOR_AUTH_REQUIRED",
                "message": "Configure a strong operator API secret before live sending.",
            },
        )
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "OPERATOR_AUTH_REQUIRED",
                "message": "Operator authentication is required.",
            },
        )
    token = auth_header.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, settings.secret_key):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "OPERATOR_AUTH_REQUIRED",
                "message": "Operator authentication is invalid.",
            },
        )


@router.post(
    "/applications/{app_id}/submit",
    response_model=SubmitAcceptedResponse,
    status_code=202,
)
async def submit_application(
    app_id: int,
    payload: SubmitApplicationRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create one exact attempt, one-use permit, and durable outbox command."""
    _require_live_operator_auth(request)
    try:
        result = create_submission_commands(
            db,
            [
                SubmissionCommandRequest(
                    application_id=app_id,
                    client_idempotency_key=payload.idempotency_key,
                    application_revision=payload.application_revision,
                    form_plan_id=payload.form_plan_id,
                    client_release=ClientReleaseIdentity(
                        **payload.client_release.model_dump(),
                    ),
                )
            ],
        )[0]
    except SubmissionAdmissionError as exc:
        raise _admission_http_error(exc) from exc
    if not result.replayed:
        _wake_submission_command(result.command_id)
    return SubmitAcceptedResponse(
        application_id=result.application_id,
        attempt_id=result.attempt_id,
        command_id=result.command_id,
        status_url=f"/api/submission-attempts/{result.attempt_id}",
        replayed=result.replayed,
    )


@router.post(
    "/applications/batch-submit",
    response_model=BatchSubmitAcceptedResponse,
    status_code=202,
)
async def batch_submit_applications(
    payload: BatchSubmitRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Atomically authorize an exact reviewed batch before waking workers."""
    _require_live_operator_auth(request)
    try:
        results = create_submission_commands(
            db,
            [
                SubmissionCommandRequest(
                    application_id=item.application_id,
                    client_idempotency_key=item.idempotency_key,
                    application_revision=item.application_revision,
                    form_plan_id=item.form_plan_id,
                    client_release=ClientReleaseIdentity(
                        **payload.client_release.model_dump(),
                    ),
                )
                for item in payload.applications
            ],
        )
    except SubmissionAdmissionError as exc:
        raise _admission_http_error(exc) from exc
    for result in results:
        if not result.replayed:
            _wake_submission_command(result.command_id)
    return BatchSubmitAcceptedResponse(
        attempts=[
            SubmitAcceptedResponse(
                application_id=result.application_id,
                attempt_id=result.attempt_id,
                command_id=result.command_id,
                status_url=f"/api/submission-attempts/{result.attempt_id}",
                replayed=result.replayed,
            )
            for result in results
        ]
    )


@router.post(
    "/applications/{app_id}/retry",
    response_model=ApproveResponse,
    status_code=202,
)
async def retry_application(app_id: int, db: Session = Depends(get_db)):
    """Prepare a definitively retryable application; sending remains explicit."""
    locked = _lock_mutation_or_http(
        db,
        application_id=app_id,
        intent=ApplicationMutationIntent.PREPARE,
    )
    app = locked.application
    latest = locked.latest_attempt
    if latest is None or latest.outcome not in {
        "failed_before_commit",
        "draft_only",
    }:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Only definitively failed or draft-only attempts may be retried.",
        )
    try:
        _validate_selected_cv(app)
    except HTTPException:
        db.rollback()
        raise
    _record_prepared(
        db,
        locked,
        actor="operator",
        source="retry_prepare",
        event_type="submission_retry_prepared",
        allowed_statuses=frozenset(
            {
                JobStatus.DRAFT,
                JobStatus.FAILED,
                JobStatus.NEEDS_REVIEW,
            }
        ),
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


def _reconcile_attempt(
    attempt: Submission,
    payload: ReconcileRequest,
    db: Session,
) -> dict:
    app = attempt.application
    reconciliable = attempt.outcome in {"unknown", "legacy_unverified"} or (
        attempt.outcome is None and attempt.status == SubmissionStatus.UNKNOWN
    )
    if not reconciliable:
        raise HTTPException(
            status_code=409,
            detail="Only unknown or legacy-unverified attempts require reconciliation",
        )
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
    attempt.reconciliation_source = payload.source
    attempt.reconciliation_evidence_ref = payload.reference
    attempt.finished_at = now
    attempt.stage = "finished"
    attempt.submitted_at = None
    attempt.verification_kind = "operator_confirmed"
    if payload.outcome == "confirmed_submitted":
        attempt.status = SubmissionStatus.UNKNOWN
        attempt.outcome = "operator_confirmed"
        attempt.reason_code = "OPERATOR_CONFIRMED_SUBMITTED"
    else:
        attempt.status = SubmissionStatus.FAILED
        attempt.outcome = "failed_before_commit"
        attempt.reason_code = "RECONCILED_NOT_SUBMITTED"
    db.flush()
    unresolved = (
        db.query(Submission.id)
        .filter(
            Submission.application_id == app.id,
            Submission.outcome.in_({"unknown", "legacy_unverified"}),
        )
        .first()
        is not None
    )
    terminal_history = (
        db.query(Submission.id)
        .filter(
            Submission.application_id == app.id,
            Submission.outcome.in_(
                {
                    "confirmed_submitted",
                    "already_applied",
                    "operator_confirmed",
                }
            ),
        )
        .first()
        is not None
    )
    app.prepared_revision = None
    app.approved_at = None
    app.approval_source = None
    if unresolved:
        app.status = JobStatus.NEEDS_REVIEW
        app.needs_review_reason = "STALE_INDETERMINATE"
    elif terminal_history:
        app.status = JobStatus.SUBMITTED
        app.needs_review_reason = None
    else:
        app.status = JobStatus.DRAFT
        app.needs_review_reason = None
    if app.job:
        app.job.status = app.status
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
        "attempt_id": attempt.id,
        "outcome": attempt.outcome,
        "reconciliation_result": payload.outcome,
        "verified": False,
        "verification_kind": "operator_confirmed",
    }


def _lock_reconciliation_attempt(
    db: Session,
    *,
    attempt_id: int | None = None,
    application_id: int | None = None,
) -> Submission | None:
    """Lock application then attempt so reconciliation has one canonical winner."""
    if attempt_id is not None:
        application_id = (
            db.query(Submission.application_id).filter(Submission.id == attempt_id).scalar()
        )
    if application_id is None:
        return None

    application_query = db.query(Application).filter(Application.id == application_id)
    if db.bind.dialect.name == "postgresql":
        application_query = application_query.with_for_update()
    application = application_query.populate_existing().first()
    if application is None:
        return None

    attempt_query = db.query(Submission).filter(
        Submission.application_id == application.id,
    )
    if attempt_id is not None:
        attempt_query = attempt_query.filter(Submission.id == attempt_id)
    else:
        attempt_query = attempt_query.order_by(
            Submission.attempt_number.desc(),
            Submission.id.desc(),
        )
    if db.bind.dialect.name == "postgresql":
        attempt_query = attempt_query.with_for_update()
    return attempt_query.populate_existing().first()


@router.post("/submission-attempts/{attempt_id}/reconcile")
async def reconcile_submission_attempt(
    attempt_id: int,
    payload: ReconcileRequest,
    db: Session = Depends(get_db),
):
    """Canonical reconciliation endpoint for one exact indeterminate attempt."""
    attempt = _lock_reconciliation_attempt(db, attempt_id=attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="Submission attempt not found")
    return _reconcile_attempt(attempt, payload, db)


@router.post("/applications/{app_id}/reconcile", deprecated=True)
async def reconcile_application(
    app_id: int,
    payload: ReconcileRequest,
    db: Session = Depends(get_db),
):
    """Compatibility alias that reconciles the latest attempt only."""
    attempt = _lock_reconciliation_attempt(db, application_id=app_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="Application attempt not found")
    return _reconcile_attempt(attempt, payload, db)


@router.post("/applications/{app_id}/reject")
async def reject_application(
    app_id: int,
    reason: str = "Rejected by user",
    db: Session = Depends(get_db),
):
    """Reject a draft and atomically revoke any safe pre-commit command."""
    locked = _lock_mutation_or_http(
        db,
        application_id=app_id,
        intent=ApplicationMutationIntent.TERMINAL,
    )
    transition_locked_application_to_skipped(
        db,
        locked,
        actor="operator",
        reason_code="OPERATOR_CANCELLED",
        rejection_reason=reason,
    )
    db.commit()
    app = locked.application
    logger.info("application_rejected_via_api", app_id=app.id, reason=reason)
    return {"message": "Application rejected", "application_id": app.id}
