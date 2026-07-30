"""Applications API routes — list, approve, reject, view drafts."""

from __future__ import annotations

import hashlib
import hmac
import inspect as python_inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from profile.cv_content_cache import (
    CVArtifactBindingError,
    get_selected_cv_artifact_by_id,
    require_current_selected_cv_artifact,
)
from profile.cv_routing import load_routing_config
from profile.versioned_snapshot import (
    ProfileSnapshotError,
    load_versioned_profile_snapshot,
)
from typing import Literal
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator
from sqlalchemy.orm import Session

from core.application_audit import record_application_event
from core.application_mutations import (
    ApplicationMutationBlockedError,
    ApplicationMutationIntent,
    LockedApplicationMutation,
    lock_application_for_mutation,
    mark_locked_application_prepared,
    transition_locked_application_to_skipped,
)
from core.application_revision import bump_application_revision, preparation_is_current
from core.application_state import (
    application_semantic_status,
    prepared_applications_query,
    reviewable_applications_query,
)
from core.automation_readiness import current_automation_readiness
from core.config import get_settings
from core.control_plane_review_permits import (
    ControlPlaneReviewGrantError,
    mint_control_plane_review_grant,
)
from core.form_plan_persistence import (
    ATTACHMENT_VERIFICATION_SOURCE,
    FormPlanPersistenceError,
    persist_inspected_form_plan,
)
from core.form_planning import (
    ANSWER_POLICY_VERSION,
    AnswerPolicyV1,
    option_set_hash,
    reusable_field_contract_fingerprint,
)
from core.operational_metrics import record_attempt_outcome, record_form_decision
from core.operations import readiness_report
from core.runtime_identity import build_runtime_capabilities, get_runtime_identity
from core.submission_domain import (
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FormPlanV1,
    ReasonCode,
)
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
    OperatorApprovedAnswer,
    Submission,
    SubmissionStatus,
)
from db.session import get_db
from llm.contracts import is_qualified_material_identity
from llm.generation import GeneratedApplication
from submitters.platforms import adapter_for_url, detect_platform

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
    created_at: str | None
    reconciled_at: str | None


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
    selected_cv_ref: str | None = None
    selected_cv_hash: str | None = None
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
    form_plan_review_ready: bool = False
    requires_versioned_form_plan: bool = False
    form_plan_adapter_name: str | None = None
    form_plan_adapter_version: str | None = None
    form_plan_selector_version: str | None = None
    form_plan_expires_at: str | None = None
    form_plan_invalidated_at: str | None = None
    form_plan_uses_local_llm: bool = False
    form_plan_llm_prompt_version: str | None = None
    form_plan_llm_model_provider: str | None = None
    form_plan_llm_model_name: str | None = None
    form_plan_llm_model_digest: str | None = None
    material_eligible: bool | None = None
    material_blockers: list[str] = Field(default_factory=list)
    material_claim_evidence: list[dict] = Field(default_factory=list)
    material_model_provider: str | None = None
    material_model_name: str | None = None
    material_model_digest: str | None = None
    material_prompt_version: str | None = None


class ApproveResponse(BaseModel):
    message: str
    application_id: int
    state: str
    status: str
    verified: bool = False
    attempt_id: int | None = None
    status_url: str | None = None


class InspectApplicationRequest(BaseModel):
    application_revision: PositiveInt


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


class ControlPlaneReviewGrantRequest(BaseModel):
    acknowledgement: Literal["ALLOW_REMOTE_SEND"]
    application_revision: PositiveInt
    form_plan_id: str = Field(min_length=36, max_length=36)


class ControlPlaneReviewGrantResponse(BaseModel):
    application_id: int
    application_ref: str
    grant_id: str
    application_revision: int
    adapter: str
    adapter_version: str
    expires_at: str
    projection_state: Literal["pending"] = "pending"


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
    selected_cv_ref: str
    selected_cv_hash: str
    attached_cv_id: str | None
    attached_cv_ref: str | None
    attached_cv_hash: str | None
    attachment_verified: bool
    attachment_verification_source: str | None
    attachment_verified_at: str | None
    profile_version: int | None
    fields: list = Field(default_factory=list)
    disclosures: list = Field(default_factory=list)
    decisions: list = Field(default_factory=list)
    blockers: list = Field(default_factory=list)
    locale: str = "en"
    answer_policy_version: str = ANSWER_POLICY_VERSION
    llm_prompt_version: str | None = None
    llm_model_provider: str | None = None
    llm_model_name: str | None = None
    llm_model_digest: str | None = None
    session_verified_at: str | None
    created_at: str
    expires_at: str
    invalidated_at: str | None
    invalidation_reason: str | None
    valid: bool


class ConfirmAnswerRequest(BaseModel):
    plan_id: str = Field(min_length=36, max_length=36)
    application_revision: PositiveInt
    value: str | bool | int | float | list[str]
    reusable: bool = False
    evidence_source: Literal["operator_confirmation"] = "operator_confirmation"
    evidence_reference: str = Field(
        default="operator_confirmation",
        min_length=1,
        max_length=255,
    )

    @field_validator("evidence_reference")
    @classmethod
    def validate_evidence_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("evidence_reference must not be blank")
        return normalized


_FIELD_LEVEL_ANSWER_BLOCKERS = frozenset(
    {
        ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ReasonCode.UNSUPPORTED_CONTROL,
        ReasonCode.LLM_UNAVAILABLE,
        ReasonCode.LLM_MODEL_MISSING,
        ReasonCode.LLM_TIMEOUT,
        ReasonCode.LLM_CIRCUIT_OPEN,
        ReasonCode.LLM_SCHEMA_INVALID,
        ReasonCode.UNSUPPORTED_CLAIM,
        ReasonCode.ATTACHMENT_UNVERIFIED,
    }
)
_GLOBAL_FORM_PLAN_BLOCKERS = frozenset({ReasonCode.FORM_PLAN_INCOMPLETE})


def _recompute_answer_blockers(
    plan: FormPlanV1,
    decisions_by_id: dict[str, AnswerDecisionV1],
) -> tuple[ReasonCode, ...]:
    """Replace stale field-level blockers while preserving plan-global gates."""

    blockers = [
        blocker
        for blocker in plan.blockers
        if blocker in _GLOBAL_FORM_PLAN_BLOCKERS or blocker not in _FIELD_LEVEL_ANSWER_BLOCKERS
    ]
    for field in plan.fields:
        decision = decisions_by_id.get(field.field_id)
        if not field.required or (
            decision is not None and decision.disposition == AnswerDisposition.RESOLVED
        ):
            continue
        if ReasonCode.REQUIRED_FIELD_UNKNOWN not in blockers:
            blockers.append(ReasonCode.REQUIRED_FIELD_UNKNOWN)
        if (
            decision is not None
            and decision.reason_code in _FIELD_LEVEL_ANSWER_BLOCKERS
            and decision.reason_code != ReasonCode.REQUIRED_FIELD_UNKNOWN
            and decision.reason_code not in blockers
        ):
            blockers.append(decision.reason_code)
    return tuple(blockers)


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


def _redacted_cv_ref(cv_id: str | None) -> str | None:
    """Return a stable opaque dashboard reference without exposing a CV name."""

    if not cv_id:
        return None
    digest = hashlib.sha256(cv_id.encode("utf-8")).hexdigest()
    return f"cv-ref-{digest[:12]}"


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")


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
        started_at=_utc_iso(attempt.started_at),
        finished_at=_utc_iso(attempt.finished_at),
        submitted_at=_utc_iso(attempt.submitted_at) if verified else None,
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
        final_action_at=_utc_iso(attempt.final_action_at),
        verification_kind=attempt.verification_kind,
        evidence_digest=attempt.evidence_digest,
        runner_release=attempt.runner_release,
        reconciliation_source=attempt.reconciliation_source,
        reconciliation_evidence_ref=attempt.reconciliation_evidence_ref,
        created_at=_utc_iso(attempt.created_at),
        reconciled_at=_utc_iso(attempt.reconciled_at),
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


def _form_plan_review_ready(plan: FormPlan | None, app: Application) -> bool:
    """Return whether one observed plan is safe for explicit operator review.

    Preparation is intentionally excluded from this predicate.  The operator
    must be able to review a current plan before preparation marks the exact
    application revision eligible for a later submission request.
    """
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
        and plan.selected_cv_id == app.selected_cv_id
        and app.material_eligible is True
        and plan.selected_cv_hash == app.selected_cv_hash
        and plan.attached_cv_id == plan.selected_cv_id
        and plan.attached_cv_hash == plan.selected_cv_hash
        and plan.attachment_verified is True
        and plan.attachment_verification_source == ATTACHMENT_VERIFICATION_SOURCE
        and plan.attachment_verified_at is not None
        and plan.profile_version == app.profile_version
        and domain_plan.answer_policy_version == ANSWER_POLICY_VERSION
        and domain_ready
    )


def _form_plan_valid(plan: FormPlan | None, app: Application) -> bool:
    """Return whether preparation remains bound to this exact latest plan."""

    latest = _latest_form_plan(app)
    return (
        plan is not None
        and latest is not None
        and latest.id == plan.id
        and _form_plan_review_ready(plan, app)
        and app.prepared_revision == app.revision
    )


def _requires_versioned_form_plan(app: Application) -> bool:
    """Gate every adapter that has entered the two-phase execution contract."""

    if app.job is None:
        return False
    descriptor = adapter_for_url(app.job.apply_url or app.job.source_url or "")
    return bool(descriptor and descriptor.execution_contract_version)


def _require_review_ready_form_plan(app: Application) -> None:
    if not _requires_versioned_form_plan(app):
        return
    if _form_plan_review_ready(_latest_form_plan(app), app):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "FORM_PLAN_REQUIRED",
            "message": (
                "Inspect and review the current application form, including "
                "the verified CV attachment, before preparation."
            ),
        },
    )


def _latest_form_plan(app: Application) -> FormPlan | None:
    if not app.form_plans:
        return None
    # Exactly one plan should remain active. Prefer it over wall-clock ordering
    # so a local clock adjustment cannot resurrect an invalidated observation.
    active = [plan for plan in app.form_plans if plan.invalidated_at is None]
    return active[-1] if active else app.form_plans[-1]


def _form_plan_uses_local_llm(plan: FormPlan | None) -> bool:
    if plan is None:
        return False
    return any(
        decision.get("provenance") == "local_llm"
        for decision in _json_list(plan.decisions_json)
        if isinstance(decision, dict)
    )


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
        selected_cv_ref=_redacted_cv_ref(plan.selected_cv_id) or "cv-ref-unavailable",
        selected_cv_hash=plan.selected_cv_hash,
        attached_cv_id=plan.attached_cv_id,
        attached_cv_ref=_redacted_cv_ref(plan.attached_cv_id),
        attached_cv_hash=plan.attached_cv_hash,
        attachment_verified=plan.attachment_verified,
        attachment_verification_source=plan.attachment_verification_source,
        attachment_verified_at=(
            plan.attachment_verified_at.isoformat() if plan.attachment_verified_at else None
        ),
        profile_version=plan.profile_version,
        fields=_json_list(plan.fields_json),
        disclosures=_json_list(getattr(plan, "disclosures_json", "[]")),
        decisions=_json_list(plan.decisions_json),
        blockers=_json_list(plan.blockers_json),
        locale=plan.locale,
        answer_policy_version=plan.answer_policy_version,
        llm_prompt_version=plan.llm_prompt_version,
        llm_model_provider=plan.llm_model_provider,
        llm_model_name=plan.llm_model_name,
        llm_model_digest=plan.llm_model_digest,
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
    artifact = (
        get_selected_cv_artifact_by_id(
            app.selected_cv_id,
            cv_routing_path=settings.cv_routing_path,
            cv_directory=settings.cv_directory,
        )
        if selected is not None
        else None
    )
    if artifact is None:
        raise HTTPException(status_code=409, detail="Selected CV file is unavailable.")
    if (
        app.material_eligible is True
        and app.selected_cv_hash is not None
        and artifact.pdf_sha256 != app.selected_cv_hash
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ATTACHMENT_CHANGED",
                "message": "The selected CV changed; regenerate and review the application.",
            },
        )


def _validate_material_quality(app: Application) -> None:
    """Block preparation when a v4 material audit is explicitly ineligible.

    Historical rows have no material audit and remain viewable/preparable for
    migration compatibility, but cannot obtain a final-submit permit without a
    current qualified form plan. Newly generated rows always persist True or
    False and therefore fail closed at this boundary.
    """

    if app.material_eligible is None:
        return
    blockers = _json_list(app.material_blockers_json)
    if not app.material_eligible:
        reason_code = str(blockers[0]) if blockers else "MATERIAL_NOT_ELIGIBLE"
        raise HTTPException(
            status_code=409,
            detail={
                "code": reason_code,
                "message": "Application materials require review before preparation.",
            },
        )
    if not app.selected_cv_hash or len(app.selected_cv_hash) != 64:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MATERIAL_CV_ARTIFACT_REQUIRED",
                "message": "The selected CV attachment is not cryptographically bound.",
            },
        )
    if not is_qualified_material_identity(
        provider=app.material_model_provider,
        model=app.material_model_name,
        local=True,
        digest=app.material_model_digest,
        prompt_version=app.material_prompt_version,
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MATERIAL_MODEL_NOT_QUALIFIED",
                "message": "Application materials were not produced by the qualified local model.",
            },
        )
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "code": str(blockers[0]),
                "message": "Application materials contain an unresolved blocker.",
            },
        )


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
                selected_cv_ref=_redacted_cv_ref(app.selected_cv_id),
                selected_cv_hash=app.selected_cv_hash,
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
                form_plan_review_ready=_form_plan_review_ready(form_plan, app),
                requires_versioned_form_plan=_requires_versioned_form_plan(app),
                form_plan_adapter_name=form_plan.adapter_name if form_plan else None,
                form_plan_adapter_version=form_plan.adapter_version if form_plan else None,
                form_plan_selector_version=form_plan.selector_version if form_plan else None,
                form_plan_expires_at=(form_plan.expires_at.isoformat() if form_plan else None),
                form_plan_invalidated_at=(
                    form_plan.invalidated_at.isoformat()
                    if form_plan and form_plan.invalidated_at
                    else None
                ),
                form_plan_uses_local_llm=_form_plan_uses_local_llm(form_plan),
                form_plan_llm_prompt_version=(form_plan.llm_prompt_version if form_plan else None),
                form_plan_llm_model_provider=(form_plan.llm_model_provider if form_plan else None),
                form_plan_llm_model_name=(form_plan.llm_model_name if form_plan else None),
                form_plan_llm_model_digest=(form_plan.llm_model_digest if form_plan else None),
                material_eligible=app.material_eligible,
                material_blockers=_json_list(app.material_blockers_json),
                material_claim_evidence=_json_list(app.material_claims_json),
                material_model_provider=app.material_model_provider,
                material_model_name=app.material_model_name,
                material_model_digest=app.material_model_digest,
                material_prompt_version=app.material_prompt_version,
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
        selected_cv_ref=_redacted_cv_ref(app.selected_cv_id),
        selected_cv_hash=app.selected_cv_hash,
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
        form_plan_review_ready=_form_plan_review_ready(form_plan, app),
        requires_versioned_form_plan=_requires_versioned_form_plan(app),
        form_plan_adapter_name=form_plan.adapter_name if form_plan else None,
        form_plan_adapter_version=form_plan.adapter_version if form_plan else None,
        form_plan_selector_version=form_plan.selector_version if form_plan else None,
        form_plan_expires_at=(form_plan.expires_at.isoformat() if form_plan else None),
        form_plan_invalidated_at=(
            form_plan.invalidated_at.isoformat() if form_plan and form_plan.invalidated_at else None
        ),
        form_plan_uses_local_llm=_form_plan_uses_local_llm(form_plan),
        form_plan_llm_prompt_version=(form_plan.llm_prompt_version if form_plan else None),
        form_plan_llm_model_provider=(form_plan.llm_model_provider if form_plan else None),
        form_plan_llm_model_name=(form_plan.llm_model_name if form_plan else None),
        form_plan_llm_model_digest=(form_plan.llm_model_digest if form_plan else None),
        material_eligible=app.material_eligible,
        material_blockers=_json_list(app.material_blockers_json),
        material_claim_evidence=_json_list(app.material_claims_json),
        material_model_provider=app.material_model_provider,
        material_model_name=app.material_model_name,
        material_model_digest=app.material_model_digest,
        material_prompt_version=app.material_prompt_version,
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
        and (
            not _requires_versioned_form_plan(app) or _form_plan_valid(_latest_form_plan(app), app)
        )
    ):
        db.rollback()
        return _prepare_response(app.id)
    try:
        _validate_selected_cv(app)
        _validate_material_quality(app)
        _require_review_ready_form_plan(app)
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
            _validate_material_quality(app)
            _require_review_ready_form_plan(app)
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


@router.post(
    "/applications/{app_id}/inspect",
    response_model=FormPlanResponse,
)
async def inspect_application_form(
    app_id: int,
    body: InspectApplicationRequest,
    db: Session = Depends(get_db),
):
    """Observe and plan one candidate form without creating a submission attempt."""

    locked = _lock_mutation_or_http(
        db,
        application_id=app_id,
        intent=ApplicationMutationIntent.CONTENT,
        expected_revision=body.application_revision,
    )
    app = locked.application
    if app.status != JobStatus.DRAFT or locked.job is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="Only a reviewable draft can be inspected.")
    try:
        _validate_selected_cv(app)
        _validate_material_quality(app)
        if (
            app.material_eligible is not True
            or not isinstance(app.profile_version, int)
            or app.profile_version < 1
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MATERIAL_AUDIT_REQUIRED",
                    "message": "Regenerate and review application materials before inspection.",
                },
            )
        selected = get_selected_cv_artifact_by_id(app.selected_cv_id or "")
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "SELECTED_CV_UNAVAILABLE", "message": "Selected CV unavailable."},
            )
        selected = require_current_selected_cv_artifact(
            selected,
            expected_sha256=app.selected_cv_hash,
        )
        profile_snapshot = load_versioned_profile_snapshot(
            db,
            version=app.profile_version,
        )
    except (CVArtifactBindingError, ProfileSnapshotError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": str(exc),
                "message": "The private CV or profile revision changed; regenerate first.",
            },
        ) from exc
    except HTTPException:
        db.rollback()
        raise

    from jobs.models import JobData
    from submitters.registry import get_two_phase_registry

    job = locked.job
    job_data = JobData(
        title=job.title or "",
        company=job.company or "",
        location=job.location or "",
        employment_type=job.employment_type or "",
        seniority=job.seniority or "",
        description=job.description or "",
        requirements=job.requirements or "",
        apply_url=job.apply_url or "",
        source_url=job.source_url or "",
    )
    generated = GeneratedApplication(
        cover_letter=app.cover_letter or "",
        recruiter_message=app.recruiter_message or "",
        qa_answers=_json_dict(app.qa_answers),
        cv_sha256=app.selected_cv_hash,
        profile_version=app.profile_version,
    )
    application_revision = int(app.revision or 1)
    selected_cv_id = str(app.selected_cv_id)
    selected_cv_hash = str(app.selected_cv_hash)
    profile_version = app.profile_version
    profile_payload = profile_snapshot.profile.model_dump(mode="python")
    resume_path = selected.resolved_path
    inspector = get_two_phase_registry().get_inspector(job_data)
    db.rollback()

    if inspector is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ADAPTER_NOT_QUALIFIED",
                "message": "No version-qualified browser inspector is available for this form.",
            },
        )
    inspect_kwargs = {
        "application_id": app_id,
        "application_revision": application_revision,
        "job": job_data,
        "application": generated,
        "user_profile": profile_payload,
        "resume_path": resume_path,
        "selected_cv_id": selected_cv_id,
    }
    inspector_parameters: Mapping[str, python_inspect.Parameter]
    try:
        inspector_parameters = python_inspect.signature(inspector.inspect).parameters
    except (TypeError, ValueError):
        inspector_parameters = {}
    if "answer_policy" in inspector_parameters:
        # This policy is request-scoped and database-backed.  It deliberately
        # has no LLM client, so inspection cannot mutate a singleton or fall
        # back to any cloud provider.  The application lock was released by
        # the rollback above before this policy can query reusable answers.
        inspect_kwargs["answer_policy"] = AnswerPolicyV1(db=db)
    try:
        domain_plan = await inspector.inspect(**inspect_kwargs)
    except Exception as exc:
        reason = getattr(exc, "reason_code", None)
        reason_code = reason.value if isinstance(reason, ReasonCode) else "FORM_INSPECTION_FAILED"
        reason_messages = {
            ReasonCode.SESSION_EXPIRED.value: (
                "The dedicated portal session needs an operator sign-in."
            ),
            ReasonCode.MFA_REQUIRED.value: "Complete MFA manually, then inspect again.",
            ReasonCode.CHALLENGE_DETECTED.value: (
                "A browser challenge requires manual handling; no bypass was attempted."
            ),
            ReasonCode.JOB_CLOSED.value: "The employer reports that this job is closed.",
            ReasonCode.ALREADY_APPLIED.value: (
                "The employer reports an existing application; no new submission was created."
            ),
            ReasonCode.ATTACHMENT_UNVERIFIED.value: (
                "The selected CV attachment could not be verified."
            ),
            ReasonCode.SELECTOR_DRIFT.value: (
                "The employer form differs from the qualified selector version."
            ),
        }
        logger.warning(
            "application_form_inspection_failed",
            application_id=app_id,
            reason_code=reason_code,
            error_type=type(exc).__name__[:80],
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": reason_code,
                "message": reason_messages.get(
                    reason_code,
                    "Inspection stopped safely before any final action.",
                ),
            },
        ) from exc

    # Re-lock after browser navigation and prove that no review input changed.
    locked = _lock_mutation_or_http(
        db,
        application_id=app_id,
        intent=ApplicationMutationIntent.CONTENT,
        expected_revision=application_revision,
    )
    app = locked.application
    if (
        app.selected_cv_id != selected_cv_id
        or app.selected_cv_hash != selected_cv_hash
        or app.profile_version != profile_version
    ):
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "FORM_CHANGED", "message": "Review inputs changed during inspection."},
        )
    try:
        row = persist_inspected_form_plan(
            db,
            application=app,
            plan=domain_plan,
        )
    except FormPlanPersistenceError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.reason_code,
                "message": "The observed form no longer matches this application revision.",
            },
        ) from exc
    db.commit()
    db.refresh(row)
    return _form_plan_response(row, app)


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


@router.post(
    "/applications/{app_id}/answers/{field_id}/confirm",
    response_model=FormPlanResponse,
)
async def confirm_application_answer(
    app_id: int,
    field_id: str,
    body: ConfirmAnswerRequest,
    db: Session = Depends(get_db),
):
    """Confirm one exact observed answer and clone the immutable review plan."""
    locked = _lock_mutation_or_http(
        db,
        application_id=app_id,
        intent=ApplicationMutationIntent.CONTENT,
        expected_revision=body.application_revision,
    )
    app = locked.application
    plan = (
        db.query(FormPlan)
        .filter(
            FormPlan.application_id == app.id,
            FormPlan.plan_id == body.plan_id,
        )
        .first()
    )
    latest_plan = _latest_form_plan(app)
    now = datetime.now(UTC).replace(tzinfo=None)
    if plan is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Form plan not found")
    if (
        latest_plan is None
        or latest_plan.id != plan.id
        or plan.invalidated_at is not None
        or plan.application_revision != app.revision
        or plan.expires_at <= now
    ):
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "FORM_CHANGED", "message": "The reviewed form plan is stale."},
        )
    try:
        domain_plan = reconstruct_persisted_form_plan(plan)
    except SubmissionAdmissionError as exc:
        db.rollback()
        raise _admission_http_error(exc) from exc
    if domain_plan.answer_policy_version != ANSWER_POLICY_VERSION:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": ReasonCode.ANSWER_POLICY_CHANGED.value,
                "message": "The answer policy changed; inspect the form again.",
            },
        )
    field = next((item for item in domain_plan.fields if item.field_id == field_id), None)
    if field is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Form field not found")
    if ReasonCode.FORM_PLAN_INCOMPLETE in domain_plan.blockers and not body.reusable:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": ReasonCode.FORM_PLAN_INCOMPLETE.value,
                "message": (
                    "This is a partial form observation. Confirm the answer as "
                    "reusable, then inspect the employer form again; the partial "
                    "plan itself cannot become preparation-ready."
                ),
            },
        )
    if body.reusable and not field.canonical_name:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Reusable answers require a canonical field identity.",
        )

    # Validate the value against the exact observed control before writing a
    # reusable row or mutating any review state.
    preliminary_provenance = (
        AnswerProvenance.OPERATOR_APPROVED_REUSABLE
        if body.reusable
        else AnswerProvenance.USER_CONFIRMED
    )
    try:
        preliminary = AnswerDecisionV1(
            field_id=field.field_id,
            disposition=AnswerDisposition.RESOLVED,
            provenance=preliminary_provenance,
            value=body.value,
            confidence=1.0,
            evidence_refs=(f"{body.evidence_source}:{body.evidence_reference}",),
        )
        preliminary_decisions = {decision.field_id: decision for decision in domain_plan.decisions}
        preliminary_decisions[field.field_id] = preliminary
        preliminary_blockers = _recompute_answer_blockers(
            domain_plan,
            preliminary_decisions,
        )
        FormPlanV1.model_validate(
            {
                **domain_plan.model_dump(mode="json"),
                "decisions": [
                    preliminary_decisions[item.field_id].model_dump(mode="json")
                    for item in domain_plan.fields
                    if item.field_id in preliminary_decisions
                ],
                "blockers": [item.value for item in preliminary_blockers],
            }
        )
    except (TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANSWER_INVALID",
                "message": "The answer does not satisfy the observed field contract.",
            },
        ) from exc

    reusable_row = None
    if body.reusable:
        from profile.models import canonical_fact_key

        context_filters = (
            OperatorApprovedAnswer.canonical_field
            == canonical_fact_key(field.canonical_name or ""),
            OperatorApprovedAnswer.field_type == field.field_type.value,
            OperatorApprovedAnswer.option_set_hash == option_set_hash(field),
            OperatorApprovedAnswer.locale == domain_plan.locale,
            OperatorApprovedAnswer.profile_version == domain_plan.profile_version,
            OperatorApprovedAnswer.selected_cv_id == domain_plan.selected_cv_id,
            OperatorApprovedAnswer.selected_cv_hash == domain_plan.selected_cv_hash,
            OperatorApprovedAnswer.adapter_name == domain_plan.adapter_name,
            OperatorApprovedAnswer.adapter_version == domain_plan.adapter_version,
            OperatorApprovedAnswer.selector_version == domain_plan.selector_version,
            OperatorApprovedAnswer.form_fingerprint == domain_plan.form_fingerprint,
            OperatorApprovedAnswer.policy_version == domain_plan.answer_policy_version,
            OperatorApprovedAnswer.revoked_at.is_(None),
        )
        for previous in db.query(OperatorApprovedAnswer).filter(*context_filters).all():
            previous.revoked_at = now
            previous.revoked_by = "operator_api"
            previous.revocation_reason = "superseded_by_explicit_confirmation"
        reusable_row = OperatorApprovedAnswer(
            canonical_field=canonical_fact_key(field.canonical_name or ""),
            field_type=field.field_type.value,
            option_set_hash=option_set_hash(field),
            locale=domain_plan.locale,
            profile_version=domain_plan.profile_version,
            selected_cv_id=domain_plan.selected_cv_id,
            selected_cv_hash=domain_plan.selected_cv_hash,
            adapter_name=domain_plan.adapter_name,
            adapter_version=domain_plan.adapter_version,
            selector_version=domain_plan.selector_version,
            form_fingerprint=domain_plan.form_fingerprint,
            field_contract_fingerprint=reusable_field_contract_fingerprint(
                field,
                adapter_name=domain_plan.adapter_name,
                adapter_version=domain_plan.adapter_version,
                selector_version=domain_plan.selector_version,
            ),
            policy_version=domain_plan.answer_policy_version,
            answer_json=json.dumps(body.value, separators=(",", ":"), ensure_ascii=True),
            evidence_source=body.evidence_source,
            evidence_reference=body.evidence_reference,
            approved_by="operator_api",
            approved_at=now,
            created_at=now,
        )
        db.add(reusable_row)
        db.flush()

    new_plan_id = uuid4()
    provenance = (
        AnswerProvenance.OPERATOR_APPROVED_REUSABLE
        if reusable_row is not None
        else AnswerProvenance.USER_CONFIRMED
    )
    evidence_ref = (
        f"operator-approved-answer:{reusable_row.id}"
        if reusable_row is not None
        else f"{body.evidence_source}:{body.evidence_reference}"
    )
    try:
        replacement = AnswerDecisionV1(
            field_id=field.field_id,
            disposition=AnswerDisposition.RESOLVED,
            provenance=provenance,
            value=body.value,
            confidence=1.0,
            evidence_refs=(evidence_ref,),
        )
    except (TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANSWER_INVALID",
                "message": "The answer does not satisfy the observed field contract.",
            },
        ) from exc
    decisions_by_id = {decision.field_id: decision for decision in domain_plan.decisions}
    decisions_by_id[field.field_id] = replacement
    decisions = tuple(
        decisions_by_id[item.field_id]
        for item in domain_plan.fields
        if item.field_id in decisions_by_id
    )
    blockers = _recompute_answer_blockers(domain_plan, decisions_by_id)

    new_revision = bump_application_revision(
        db,
        app,
        reason_code="ANSWER_CONFIRMED",
        now=now,
    )
    try:
        cloned_domain = FormPlanV1.model_validate(
            {
                **domain_plan.model_dump(mode="json"),
                "plan_id": str(new_plan_id),
                "application_revision": new_revision,
                "decisions": [decision.model_dump(mode="json") for decision in decisions],
                "blockers": [blocker.value for blocker in blockers],
            }
        )
    except (TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANSWER_INVALID",
                "message": "The answer does not satisfy the observed field contract.",
            },
        ) from exc

    cloned = FormPlan(
        plan_id=str(cloned_domain.plan_id),
        application_id=app.id,
        application_revision=new_revision,
        adapter_name=cloned_domain.adapter_name,
        adapter_version=cloned_domain.adapter_version,
        selector_version=cloned_domain.selector_version,
        fingerprint=cloned_domain.form_fingerprint,
        selected_cv_id=cloned_domain.selected_cv_id,
        selected_cv_hash=cloned_domain.selected_cv_hash,
        attached_cv_id=cloned_domain.attached_cv_id,
        attached_cv_hash=cloned_domain.attached_cv_hash,
        attachment_verified=cloned_domain.attachment_verified,
        attachment_verification_source=plan.attachment_verification_source,
        attachment_verified_at=plan.attachment_verified_at,
        profile_version=cloned_domain.profile_version,
        fields_json=json.dumps(
            [item.model_dump(mode="json") for item in cloned_domain.fields],
            separators=(",", ":"),
            ensure_ascii=True,
        ),
        disclosures_json=json.dumps(
            [item.model_dump(mode="json") for item in cloned_domain.disclosures],
            separators=(",", ":"),
            ensure_ascii=True,
        ),
        decisions_json=json.dumps(
            [item.model_dump(mode="json") for item in cloned_domain.decisions],
            separators=(",", ":"),
            ensure_ascii=True,
        ),
        blockers_json=json.dumps(
            [item.value for item in cloned_domain.blockers],
            separators=(",", ":"),
        ),
        locale=cloned_domain.locale,
        answer_policy_version=cloned_domain.answer_policy_version,
        llm_prompt_version=cloned_domain.llm_prompt_version,
        llm_model_provider=cloned_domain.llm_model_provider,
        llm_model_name=cloned_domain.llm_model_name,
        llm_model_digest=cloned_domain.llm_model_digest,
        session_verified_at=plan.session_verified_at,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
    )
    db.add(cloned)
    db.flush()
    record_form_decision(
        db,
        plan=cloned_domain,
        field=field,
        decision=replacement,
        occurred_at=now,
        event_kind="operator_confirmation",
    )
    app.status = JobStatus.DRAFT
    if locked.job is not None:
        locked.job.status = JobStatus.DRAFT
    record_application_event(
        db,
        app.id,
        "form_answer_confirmed",
        actor="operator",
        details={
            "field_id_hash": hashlib.sha256(field.field_id.encode("utf-8")).hexdigest(),
            "reusable": body.reusable,
            "application_revision": new_revision,
            "form_plan_id": str(new_plan_id),
        },
    )
    db.commit()
    db.refresh(cloned)
    return _form_plan_response(cloned, app)


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
    "/applications/{app_id}/control-plane-review-grant",
    response_model=ControlPlaneReviewGrantResponse,
    status_code=202,
)
async def allow_remote_send(
    app_id: int,
    payload: ControlPlaneReviewGrantRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Mint durable local authority for one explicit remote Send action."""

    _require_live_operator_auth(request)
    settings = get_settings()
    report = readiness_report(settings)
    capabilities = build_runtime_capabilities(
        settings,
        report,
        automation_readiness=current_automation_readiness(
            settings=settings,
            dependency_report=report,
            db=db,
        ),
    )
    submission = capabilities.get("submission")
    if not isinstance(submission, Mapping) or submission.get("allowed") is not True:
        raise HTTPException(
            status_code=409,
            detail={"code": "RUNTIME_NOT_READY", "message": "Local runner is not ready."},
        )
    application_query = db.query(Application).filter(Application.id == app_id)
    if db.bind.dialect.name == "postgresql":
        application_query = application_query.with_for_update()
    application = application_query.populate_existing().one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if application.revision != payload.application_revision:
        raise HTTPException(
            status_code=409,
            detail={"code": "APPLICATION_REVISION_CHANGED"},
        )
    plan_query = db.query(FormPlan).filter(
        FormPlan.application_id == application.id,
        FormPlan.plan_id == payload.form_plan_id,
    )
    if db.bind.dialect.name == "postgresql":
        plan_query = plan_query.with_for_update()
    plan = plan_query.one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Form plan not found")
    job_url = (
        (application.job.apply_url or application.job.source_url)
        if application.job is not None
        else ""
    ) or ""
    descriptor = adapter_for_url(job_url)
    if descriptor is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "ADAPTER_NOT_QUALIFIED"},
        )
    if (
        descriptor.platform != plan.adapter_name
        or descriptor.adapter_version != plan.adapter_version
        or descriptor.selector_version != plan.selector_version
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "ADAPTER_VERSION_CHANGED"},
        )
    if not descriptor.allows_final_execution or not descriptor.qualifies_form_fingerprint(
        plan.fingerprint
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "ADAPTER_NOT_QUALIFIED"},
        )
    try:
        projection = mint_control_plane_review_grant(
            db,
            application_id=application.id,
            form_plan_id=plan.id,
            runner_release=get_runtime_identity().release_id,
        )
    except ControlPlaneReviewGrantError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": exc.reason_code},
        ) from exc
    record_application_event(
        db,
        application.id,
        "control_plane_review_grant_minted",
        actor="operator",
        details={
            "application_revision": projection.application_revision,
            "platform": projection.adapter_name,
            "adapter_version": projection.adapter_version,
            "external_action_queued": False,
        },
    )
    db.commit()
    return ControlPlaneReviewGrantResponse(
        application_id=application.id,
        application_ref=projection.remote_application_ref,
        grant_id=projection.review_grant_ref,
        application_revision=projection.application_revision,
        adapter=projection.adapter_name,
        adapter_version=projection.adapter_version,
        expires_at=projection.expires_at.replace(tzinfo=UTC).isoformat(),
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
        _validate_material_quality(app)
        _require_review_ready_form_plan(app)
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
    record_attempt_outcome(
        db,
        attempt,
        occurred_at=now,
        event_kind="operator_reconciliation",
    )
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
    if attempt.command is not None:
        from worker.control_plane_event_outbox import (
            enqueue_control_plane_attempt_transition,
        )

        enqueue_control_plane_attempt_transition(
            db,
            attempt=attempt,
            command=attempt.command,
            occurred_at=now,
            use_attempt_reason=False,
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
