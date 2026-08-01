"""Database-authoritative claiming and execution of final-submit commands."""

from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from celery import shared_task

from core.application_audit import record_application_event
from core.application_revision import preparation_is_current
from core.async_lifecycle import (
    SameEventLoopLifecycle,
    cleanup_prepared_action_if_supported,
)
from core.config import Settings, get_settings
from core.metrics import GOVERNOR_DENIALS
from core.operational_metrics import (
    record_attempt_outcome,
    record_attempt_stage,
    record_governor_denial,
)
from core.runtime_identity import get_runtime_identity, runtime_source_is_current
from core.submission_domain import (
    AlreadyAppliedOutcome,
    AttemptOutcome,
    AttemptStage,
    CommitOutcome,
    ConfirmedSubmittedOutcome,
    FailedBeforeCommitOutcome,
    FormPlanV1,
    NeedsReviewOutcome,
    PreparedFinalActionV1,
    ReasonCode,
    UnknownOutcome,
    parse_commit_outcome,
    parse_preflight_outcome,
)
from core.submission_domain import (
    FinalSubmitPermit as DomainFinalSubmitPermit,
)
from core.submission_state import project_legacy_status, require_transition
from core.submit_permits import (
    PermitValidationError,
    consume_final_submit_permit,
    validate_final_submit_permit,
)
from db.models import (
    Application,
    FormPlan,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionEvidence,
)
from db.session import get_session_factory
from ingestion.url_utils import normalize_url, url_hash
from llm.execution_guard import prohibit_llm_generation
from worker.control_plane_event_outbox import (
    enqueue_control_plane_attempt_transition,
)

logger = structlog.get_logger(__name__)


class _CommitBoundaryRejectedError(RuntimeError):
    """The locked boundary safely terminalized the attempt before any action."""


def _governor_metric_reason(reason: str) -> str:
    normalized = (reason or "").lower()
    if "kill" in normalized:
        return "kill_switch"
    if "cooldown" in normalized or "challenge" in normalized:
        return "cooldown"
    if "active hours" in normalized:
        return "active_hours"
    if "daily cap" in normalized:
        return "daily_cap"
    if "gap" in normalized:
        return "minimum_gap"
    return "policy"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _runner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


def _set_stage(
    db,
    attempt: Submission,
    target: AttemptStage,
    *,
    occurred_at: datetime,
) -> None:
    current = AttemptStage(attempt.stage)
    require_transition(current, target)
    attempt.stage = target.value
    attempt.outcome = None
    attempt.status = project_legacy_status(target)
    record_attempt_stage(
        db,
        attempt,
        stage=target,
        previous_stage=current,
        occurred_at=occurred_at,
    )


def _confirmed_evidence_is_post_action(
    attempt: Submission,
    outcome: ConfirmedSubmittedOutcome,
    *,
    observed_by: datetime,
) -> bool:
    if attempt.final_action_at is None:
        return False
    evidence_at = _aware(outcome.evidence.observed_at)
    final_action_at = _aware(attempt.final_action_at)
    upper_bound = _aware(observed_by) + timedelta(seconds=5)
    return final_action_at <= evidence_at <= upper_bound


def _confirmed_evidence_is_valid(
    attempt: Submission,
    outcome: ConfirmedSubmittedOutcome,
    *,
    observed_by: datetime,
) -> bool:
    evidence = outcome.evidence
    return (
        evidence.attempt_id == attempt.id
        and evidence.form_fingerprint == attempt.form_plan_fingerprint
        and evidence.attached_cv_hash == attempt.attached_cv_hash
        and _confirmed_evidence_is_post_action(
            attempt,
            outcome,
            observed_by=observed_by,
        )
    )


def claim_submission_command(
    db,
    *,
    command_id: int | None = None,
    runner_id: str | None = None,
    now: datetime | None = None,
) -> int | None:
    """Atomically claim one pending outbox command with PostgreSQL SKIP LOCKED."""
    timestamp = now or _now()
    query = (
        db.query(SubmissionCommand.id, Submission.application_id)
        .join(Submission, Submission.id == SubmissionCommand.attempt_id)
        .filter(
            SubmissionCommand.state == "pending",
            SubmissionCommand.available_at <= timestamp,
        )
    )
    if command_id is not None:
        query = query.filter(SubmissionCommand.id == command_id)
    query = query.order_by(SubmissionCommand.available_at, SubmissionCommand.id)
    candidates = query.limit(50).all()

    command = None
    for candidate_id, application_id in candidates:
        if db.bind.dialect.name == "postgresql":
            application = (
                db.query(Application)
                .filter(Application.id == application_id)
                .with_for_update(skip_locked=True)
                .populate_existing()
                .first()
            )
            if application is None:
                continue
        command_query = db.query(SubmissionCommand).filter(
            SubmissionCommand.id == candidate_id,
            SubmissionCommand.state == "pending",
            SubmissionCommand.available_at <= timestamp,
        )
        if db.bind.dialect.name == "postgresql":
            command_query = command_query.with_for_update(skip_locked=True)
        command = command_query.populate_existing().first()
        if command is not None:
            break
    if command is None:
        db.rollback()
        return None
    attempt = command.attempt
    if attempt is None or attempt.stage != AttemptStage.QUEUED.value:
        command.state = "cancelled"
        command.last_error_code = "COMMAND_STATE_INVALID"
        command.completed_at = timestamp
        db.commit()
        return None

    expected_release = str(attempt.runner_release or "")
    worker_identity = get_runtime_identity()
    worker_release = worker_identity.release_id
    if (
        expected_release in {"", "unknown", "unavailable"}
        or worker_release in {"unknown", "unavailable"}
        or expected_release != worker_release
        or not runtime_source_is_current(worker_identity)
    ):
        _finish_attempt(
            db,
            attempt=attempt,
            command=command,
            outcome=FailedBeforeCommitOutcome(reason_code=ReasonCode.BUILD_MISMATCH),
            now=timestamp,
        )
        db.commit()
        return None

    command.state = "claimed"
    command.claimed_at = timestamp
    command.claimed_by = (runner_id or _runner_id())[:128]
    command.claim_token = secrets.token_hex(32)
    attempt.started_at = timestamp
    _set_stage(
        db,
        attempt,
        AttemptStage.INSPECTING,
        occurred_at=timestamp,
    )
    enqueue_control_plane_attempt_transition(
        db,
        attempt=attempt,
        command=command,
        occurred_at=timestamp,
    )
    db.commit()
    return command.id


def _finish_attempt(
    db,
    *,
    attempt: Submission,
    command: SubmissionCommand,
    outcome: CommitOutcome,
    now: datetime,
) -> None:
    current = AttemptStage(attempt.stage)
    terminal = AttemptOutcome(outcome.kind)
    require_transition(current, AttemptStage.FINISHED, terminal)
    record_attempt_stage(
        db,
        attempt,
        stage=AttemptStage.FINISHED,
        previous_stage=current,
        occurred_at=now,
        transition_key=f"finished:{terminal.value}",
    )
    attempt.stage = AttemptStage.FINISHED.value
    attempt.outcome = terminal.value
    attempt.status = project_legacy_status(AttemptStage.FINISHED, terminal)
    attempt.finished_at = now
    attempt.error_message = None
    attempt.application.prepared_revision = None
    attempt.application.approved_at = None
    attempt.application.approval_source = None

    if (
        isinstance(outcome, FailedBeforeCommitOutcome)
        and outcome.reason_code
        in {
            ReasonCode.FORM_CHANGED,
            ReasonCode.SELECTOR_DRIFT,
        }
        and attempt.form_plan is not None
        and attempt.form_plan.invalidated_at is None
    ):
        attempt.form_plan.invalidated_at = now
        attempt.form_plan.invalidation_reason = outcome.reason_code.value

    if isinstance(outcome, ConfirmedSubmittedOutcome):
        evidence = outcome.evidence
        if not _confirmed_evidence_is_valid(attempt, outcome, observed_by=now):
            raise ValueError("EVIDENCE_BINDING_MISMATCH")
        evidence_row = SubmissionEvidence(
            attempt_id=attempt.id,
            evidence_type=evidence.evidence_type.value,
            evidence_digest=evidence.digest,
            employer_application_ref=evidence.employer_application_id,
            receipt_ref=evidence.api_receipt_id,
            portal_record_ref=evidence.candidate_portal_reference,
            form_fingerprint=evidence.form_fingerprint,
            cv_hash=evidence.attached_cv_hash,
            observed_at=_aware(evidence.observed_at).replace(tzinfo=None),
        )
        db.add(evidence_row)
        attempt.reason_code = "EMPLOYER_VERIFIED"
        attempt.verification_kind = evidence.evidence_type.value
        attempt.evidence_digest = evidence.digest
        attempt.confirmation_id = (
            evidence.employer_application_id
            or evidence.api_receipt_id
            or evidence.candidate_portal_reference
            or evidence.digest
        )
        attempt.submitted_at = now
        attempt.application.status = JobStatus.SUBMITTED
        if attempt.application.job:
            attempt.application.job.status = JobStatus.SUBMITTED
    elif isinstance(outcome, AlreadyAppliedOutcome):
        attempt.reason_code = outcome.reason_code.value
        attempt.submitted_at = None
        attempt.application.outcome = AttemptOutcome.ALREADY_APPLIED.value
        attempt.application.status = JobStatus.SUBMITTED
        if attempt.application.job:
            attempt.application.job.status = JobStatus.SUBMITTED
    else:
        attempt.reason_code = outcome.reason_code.value
        attempt.submitted_at = None
        if isinstance(outcome, (NeedsReviewOutcome, UnknownOutcome)):
            attempt.application.status = JobStatus.NEEDS_REVIEW
            attempt.application.needs_review_reason = outcome.reason_code.value
            if attempt.application.job:
                attempt.application.job.status = JobStatus.NEEDS_REVIEW
        elif isinstance(outcome, FailedBeforeCommitOutcome):
            attempt.application.status = JobStatus.FAILED
            attempt.application.needs_review_reason = outcome.reason_code.value
            if attempt.application.job:
                attempt.application.job.status = JobStatus.FAILED
        else:
            attempt.application.status = JobStatus.DRAFT
            if attempt.application.job:
                attempt.application.job.status = JobStatus.DRAFT

    record_attempt_outcome(
        db,
        attempt,
        occurred_at=now,
    )
    command.state = "completed"
    command.completed_at = now
    command.claimed_at = None
    command.claimed_by = None
    command.claim_token = None
    command.last_error_code = attempt.reason_code
    record_application_event(
        db,
        attempt.application_id,
        "submission_attempt_finished",
        actor="worker",
        details={
            "attempt_number": attempt.attempt_number,
            "platform": attempt.adapter_name,
            "reason_code": attempt.reason_code,
            "selected_cv_id": attempt.selected_cv_id,
            "profile_version": attempt.profile_version,
            "state": attempt.outcome,
        },
    )
    enqueue_control_plane_attempt_transition(
        db,
        attempt=attempt,
        command=command,
        occurred_at=now,
    )


_PERMIT_REASON_MAP = {
    "SUBMIT_PERMIT_REQUIRED": ReasonCode.PERMIT_MISSING,
    "SUBMIT_PERMIT_EXPIRED": ReasonCode.PERMIT_EXPIRED,
    "SUBMIT_PERMIT_REPLAYED": ReasonCode.PERMIT_REPLAYED,
    "JOB_URL_CHANGED": ReasonCode.PERMIT_BINDING_MISMATCH,
    "APPLICATION_REVISION_CHANGED": ReasonCode.PERMIT_BINDING_MISMATCH,
    "ADAPTER_VERSION_CHANGED": ReasonCode.PERMIT_BINDING_MISMATCH,
    "SELECTOR_DRIFT": ReasonCode.SELECTOR_DRIFT,
    "FORM_CHANGED": ReasonCode.FORM_CHANGED,
    "ATTACHMENT_CHANGED": ReasonCode.ATTACHMENT_UNVERIFIED,
    "ATTACHMENT_UNVERIFIED": ReasonCode.ATTACHMENT_UNVERIFIED,
    "SUBMISSION_AUTHORITY_CHANGED": ReasonCode.PERMIT_BINDING_MISMATCH,
    "AUTOMATION_POLICY_CHANGED": ReasonCode.PERMIT_BINDING_MISMATCH,
    "PERMIT_BINDING_MISMATCH": ReasonCode.PERMIT_BINDING_MISMATCH,
    "GOVERNOR_DENIED": ReasonCode.GOVERNOR_DENIED,
}


def _validate_attempt_automation_authority(
    db,
    attempt: Submission,
    *,
    now: datetime,
) -> tuple[ReasonCode | None, str | None]:
    if attempt.authority_kind != "qualified_autopilot":
        return None, None
    decision = attempt.automation_policy_decision
    if (
        decision is None
        or attempt.automation_policy_decision_digest is None
        or decision.decision_digest != attempt.automation_policy_decision_digest
    ):
        return ReasonCode.PERMIT_BINDING_MISMATCH, "AUTOMATION_DECISION_BINDING_MISMATCH"
    try:
        from core.automation_policy_service import (
            AutomationPolicyError,
            validate_current_automation_decision,
        )

        validate_current_automation_decision(
            db,
            decision_record=decision,
            now=_aware(now),
            lock=False,
        )
    except AutomationPolicyError as exc:
        if exc.reason_code in {"KILL_SWITCH_ACTIVE", "OUTSIDE_ACTIVE_HOURS"}:
            return ReasonCode.GOVERNOR_DENIED, exc.reason_code
        return ReasonCode.PERMIT_BINDING_MISMATCH, exc.reason_code
    return None, None


def _load_domain_plan(plan: FormPlan) -> FormPlanV1:
    return FormPlanV1(
        plan_id=UUID(plan.plan_id),
        application_id=plan.application_id,
        application_revision=plan.application_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.fingerprint,
        selected_cv_id=plan.selected_cv_id,
        selected_cv_hash=plan.selected_cv_hash,
        attached_cv_id=plan.attached_cv_id,
        attached_cv_hash=plan.attached_cv_hash,
        attachment_verified=plan.attachment_verified,
        profile_version=plan.profile_version,
        session_verified_at=_aware(plan.session_verified_at),
        created_at=_aware(plan.created_at),
        expires_at=_aware(plan.expires_at),
        fields=json.loads(plan.fields_json),
        disclosures=json.loads(getattr(plan, "disclosures_json", "[]")),
        decisions=json.loads(plan.decisions_json),
        blockers=json.loads(plan.blockers_json),
        locale=plan.locale,
        answer_policy_version=plan.answer_policy_version,
        llm_prompt_version=plan.llm_prompt_version,
        llm_model_provider=plan.llm_model_provider,
        llm_model_name=plan.llm_model_name,
        llm_model_digest=plan.llm_model_digest,
    )


def _load_domain_permit(attempt: Submission) -> DomainFinalSubmitPermit:
    permit = attempt.final_submit_permit
    return DomainFinalSubmitPermit(
        attempt_id=attempt.id,
        job_url_hash=permit.job_url_hash,
        application_revision=permit.application_revision,
        adapter_name=permit.adapter_name,
        adapter_version=permit.adapter_version,
        selector_version=permit.selector_version,
        form_fingerprint=permit.form_plan_fingerprint,
        cv_hash=permit.cv_hash,
        expires_at=_aware(permit.expires_at),
        nonce=permit.nonce_hash,
    )


def _lock_claimed_context(
    db,
    *,
    command_id: int,
    expected_claim_token: str,
) -> tuple[Application, Submission, SubmissionCommand] | None:
    """Lock application then command and prove this claim still owns the work."""
    owner = (
        db.query(Submission.application_id)
        .join(SubmissionCommand, SubmissionCommand.attempt_id == Submission.id)
        .filter(SubmissionCommand.id == command_id)
        .first()
    )
    if owner is None:
        db.rollback()
        return None
    application_query = db.query(Application).filter(Application.id == owner[0])
    if db.bind.dialect.name == "postgresql":
        application_query = application_query.with_for_update()
    application = application_query.populate_existing().first()
    if application is None:
        db.rollback()
        return None

    query = db.query(SubmissionCommand).filter(
        SubmissionCommand.id == command_id,
        SubmissionCommand.state == "claimed",
        SubmissionCommand.claim_token == expected_claim_token,
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    command = query.populate_existing().first()
    if command is None:
        db.rollback()
        return None
    db.refresh(command.attempt)
    db.expire(
        command.attempt,
        [
            "application",
            "form_plan",
            "final_submit_permit",
            "automation_policy_decision",
        ],
    )
    db.expire(application, ["job"])
    return application, command.attempt, command


def _finish_claimed_before_commit(
    db,
    *,
    command_id: int,
    expected_claim_token: str,
    reason: ReasonCode,
    now: datetime,
) -> str:
    """Terminalize only while this fenced claim still owns pre-commit work."""
    context = _lock_claimed_context(
        db,
        command_id=command_id,
        expected_claim_token=expected_claim_token,
    )
    if context is None:
        return "superseded"
    _application, attempt, command = context
    if attempt.stage not in {
        AttemptStage.INSPECTING.value,
        AttemptStage.PREPARING.value,
        AttemptStage.READY.value,
    }:
        db.rollback()
        return "superseded"
    _finish_attempt(
        db,
        attempt=attempt,
        command=command,
        outcome=FailedBeforeCommitOutcome(reason_code=reason),
        now=now,
    )
    db.commit()
    return AttemptOutcome.FAILED_BEFORE_COMMIT.value


def _enter_claimed_preflight(
    db,
    *,
    command_id: int,
    expected_claim_token: str,
) -> bool:
    """Enter reversible preflight only while the fenced claim is current."""
    context = _lock_claimed_context(
        db,
        command_id=command_id,
        expected_claim_token=expected_claim_token,
    )
    if context is None:
        return False
    application, attempt, _command = context
    if (
        application.status != JobStatus.DRAFT
        or application.job is None
        or application.job.status != JobStatus.DRAFT
        or not preparation_is_current(application)
        or application.revision != attempt.application_revision
        or attempt.application_id != application.id
        or attempt.stage != AttemptStage.INSPECTING.value
    ):
        db.rollback()
        return False
    stage_at = _now()
    _set_stage(
        db,
        attempt,
        AttemptStage.PREPARING,
        occurred_at=stage_at,
    )
    enqueue_control_plane_attempt_transition(
        db,
        attempt=attempt,
        command=_command,
        occurred_at=stage_at,
    )
    db.commit()
    return True


def _mark_claimed_attempt_ready(
    db,
    *,
    command_id: int,
    expected_claim_token: str,
    action: PreparedFinalActionV1,
    now: datetime,
) -> str:
    """Bind a reversible preflight handle before exposing READY."""
    context = _lock_claimed_context(
        db,
        command_id=command_id,
        expected_claim_token=expected_claim_token,
    )
    if context is None:
        return "superseded"
    application, attempt, command = context
    plan = attempt.form_plan
    permit = attempt.final_submit_permit
    if attempt.stage != AttemptStage.PREPARING.value:
        db.rollback()
        return "superseded"
    if (
        application.status != JobStatus.DRAFT
        or application.job is None
        or application.job.status != JobStatus.DRAFT
        or not preparation_is_current(application)
        or application.revision != attempt.application_revision
        or plan is None
        or permit is None
    ):
        db.rollback()
        return "invalid"
    try:
        if not action.binds(
            _load_domain_plan(plan),
            _load_domain_permit(attempt),
            at=_aware(now),
        ):
            db.rollback()
            return "invalid"
    except (TypeError, ValueError, json.JSONDecodeError):
        db.rollback()
        return "invalid"
    _set_stage(
        db,
        attempt,
        AttemptStage.READY,
        occurred_at=now,
    )
    enqueue_control_plane_attempt_transition(
        db,
        attempt=attempt,
        command=command,
        occurred_at=now,
    )
    command.claimed_at = now
    db.commit()
    return "ready"


def _enter_commit_boundary(
    db,
    *,
    command_id: int,
    expected_claim_token: str,
    job_url_hash: str,
    action: PreparedFinalActionV1,
    now: datetime | None = None,
    governor_gate=None,
) -> tuple[Submission, SubmissionCommand] | None:
    """Fence stale workers and atomically consume authority before one action."""
    context = _lock_claimed_context(
        db,
        command_id=command_id,
        expected_claim_token=expected_claim_token,
    )
    if context is None:
        return None
    application, attempt, command = context

    plan = attempt.form_plan
    permit = attempt.final_submit_permit
    validation_at = now or _now()
    if not runtime_source_is_current():
        _finish_attempt(
            db,
            attempt=attempt,
            command=command,
            outcome=FailedBeforeCommitOutcome(reason_code=ReasonCode.BUILD_MISMATCH),
            now=validation_at,
        )
        db.commit()
        raise _CommitBoundaryRejectedError(ReasonCode.BUILD_MISMATCH.value)
    if (
        application.status != JobStatus.DRAFT
        or application.job is None
        or application.job.status != JobStatus.DRAFT
        or not preparation_is_current(application)
        or application.revision != attempt.application_revision
        or attempt.application_id != application.id
        or attempt.stage != AttemptStage.READY.value
        or plan is None
        or permit is None
        or plan.invalidated_at is not None
    ):
        db.rollback()
        return None

    try:
        domain_plan = _load_domain_plan(plan)
        domain_permit = _load_domain_permit(attempt)
        automation_reason, automation_detail = _validate_attempt_automation_authority(
            db,
            attempt,
            now=validation_at,
        )
        if automation_reason is not None:
            if automation_reason is ReasonCode.GOVERNOR_DENIED:
                record_governor_denial(
                    db,
                    attempt,
                    occurred_at=validation_at,
                    reason_code=automation_detail or "AUTOMATION_POLICY_DENIED",
                )
            raise PermitValidationError(automation_reason.value)
        validate_final_submit_permit(
            permit,
            attempt=attempt,
            form_plan=plan,
            job_url_hash=job_url_hash,
            now=validation_at,
        )
        if not action.binds(domain_plan, domain_permit, at=_aware(validation_at)):
            raise PermitValidationError("FORM_CHANGED")
    except (PermitValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        reason_code = getattr(exc, "reason_code", "FORM_CHANGED")
        reason = _PERMIT_REASON_MAP.get(reason_code, ReasonCode.FORM_CHANGED)
        _finish_attempt(
            db,
            attempt=attempt,
            command=command,
            outcome=FailedBeforeCommitOutcome(reason_code=reason),
            now=validation_at,
        )
        db.commit()
        raise _CommitBoundaryRejectedError(reason.value) from exc

    if governor_gate is not None:
        try:
            allowed, raw_reason = governor_gate()
        except Exception:
            allowed, raw_reason = False, "governor backend unavailable"
        if not allowed:
            GOVERNOR_DENIALS.labels(reason=_governor_metric_reason(raw_reason)).inc()
            record_governor_denial(
                db,
                attempt,
                occurred_at=validation_at,
                reason_code=_governor_metric_reason(raw_reason).upper(),
            )
            _finish_attempt(
                db,
                attempt=attempt,
                command=command,
                outcome=FailedBeforeCommitOutcome(reason_code=ReasonCode.GOVERNOR_DENIED),
                now=validation_at,
            )
            db.commit()
            raise _CommitBoundaryRejectedError(ReasonCode.GOVERNOR_DENIED.value)
    # Redis or another locked claimant may have delayed this transaction.
    # Re-read the clock and both expiry-bound contracts immediately before
    # persisting COMMITTING; the caller omits ``now`` in production, while
    # focused tests may inject a deterministic boundary timestamp.
    commit_at = now or _now()
    if not runtime_source_is_current():
        _finish_attempt(
            db,
            attempt=attempt,
            command=command,
            outcome=FailedBeforeCommitOutcome(reason_code=ReasonCode.BUILD_MISMATCH),
            now=commit_at,
        )
        db.commit()
        raise _CommitBoundaryRejectedError(ReasonCode.BUILD_MISMATCH.value)
    try:
        automation_reason, automation_detail = _validate_attempt_automation_authority(
            db,
            attempt,
            now=commit_at,
        )
        if automation_reason is not None:
            if automation_reason is ReasonCode.GOVERNOR_DENIED:
                record_governor_denial(
                    db,
                    attempt,
                    occurred_at=commit_at,
                    reason_code=automation_detail or "AUTOMATION_POLICY_DENIED",
                )
            raise PermitValidationError(automation_reason.value)
        validate_final_submit_permit(
            permit,
            attempt=attempt,
            form_plan=plan,
            job_url_hash=job_url_hash,
            now=commit_at,
        )
        if not action.binds(domain_plan, domain_permit, at=_aware(commit_at)):
            raise PermitValidationError("FORM_CHANGED")
    except (PermitValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        reason_code = getattr(exc, "reason_code", "FORM_CHANGED")
        reason = _PERMIT_REASON_MAP.get(reason_code, ReasonCode.FORM_CHANGED)
        _finish_attempt(
            db,
            attempt=attempt,
            command=command,
            outcome=FailedBeforeCommitOutcome(reason_code=reason),
            now=commit_at,
        )
        db.commit()
        raise _CommitBoundaryRejectedError(reason.value) from exc
    _set_stage(
        db,
        attempt,
        AttemptStage.COMMITTING,
        occurred_at=commit_at,
    )
    enqueue_control_plane_attempt_transition(
        db,
        attempt=attempt,
        command=command,
        occurred_at=commit_at,
    )
    attempt.final_action_at = commit_at
    consume_final_submit_permit(permit, now=commit_at)
    # Renew the lease in the same transaction as the ambiguity boundary. A
    # stale candidate selected from an older timestamp must fail its locked
    # recheck before it can quarantine this actively executing command.
    command.claimed_at = commit_at
    db.commit()
    return attempt, command


def execute_claimed_submission_command(
    db,
    command_id: int,
    *,
    registry=None,
    settings: Settings | None = None,
    governor=None,
    now: datetime | None = None,
) -> str:
    """Execute one claimed command; no legacy one-step adapter is reachable."""
    timestamp = now or _now()
    command = db.get(SubmissionCommand, command_id)
    if command is None or command.state != "claimed":
        return "skipped"
    claim_token = str(command.claim_token or "")
    if not claim_token:
        return "skipped"
    attempt = command.attempt
    plan = attempt.form_plan
    permit = attempt.final_submit_permit
    application = attempt.application
    job = application.job
    runtime_settings = settings or get_settings()
    if (
        runtime_settings.dry_run
        or runtime_settings.draft_only
        or not runtime_settings.portal_final_submit_enabled
        or not runtime_settings.live_automation_acknowledged
    ):
        return _finish_claimed_before_commit(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
            reason=ReasonCode.RUNTIME_NOT_READY,
            now=timestamp,
        )
    if plan is None or permit is None or job is None:
        return _finish_claimed_before_commit(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
            reason=ReasonCode.PERMIT_MISSING,
            now=timestamp,
        )
    if (
        application.status != JobStatus.DRAFT
        or job.status != JobStatus.DRAFT
        or application.revision != attempt.application_revision
        or not preparation_is_current(application)
    ):
        return _finish_claimed_before_commit(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
            reason=ReasonCode.FORM_CHANGED,
            now=timestamp,
        )

    try:
        normalized_url = normalize_url((job.apply_url or job.source_url) or "")
        validate_final_submit_permit(
            permit,
            attempt=attempt,
            form_plan=plan,
            job_url_hash=url_hash(normalized_url),
            now=timestamp,
        )
        domain_plan = _load_domain_plan(plan)
        domain_permit = _load_domain_permit(attempt)
        if not domain_permit.binds(domain_plan):
            raise PermitValidationError("FORM_CHANGED")
    except (PermitValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        reason_code = getattr(exc, "reason_code", "FORM_CHANGED")
        return _finish_claimed_before_commit(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
            reason=_PERMIT_REASON_MAP.get(reason_code, ReasonCode.FORM_CHANGED),
            now=timestamp,
        )

    from jobs.models import JobData
    from submitters.base import (  # noqa: PLC0415
        AdapterPreflightContext,
        supports_preflight_context,
    )
    from submitters.platforms import adapter_for_url  # noqa: PLC0415

    descriptor = adapter_for_url(job.apply_url or job.source_url or "")
    if registry is None:
        from submitters.registry import get_two_phase_registry  # noqa: PLC0415

        resolved_registry = get_two_phase_registry()
    else:
        resolved_registry = registry
    job_data = JobData(
        title=job.title or "",
        company=job.company or "",
        location=job.location or "",
        apply_url=job.apply_url or "",
        source_url=job.source_url or "",
    )
    executor = (
        resolved_registry.resolve_final_executor(
            job_data,
            domain_plan,
            domain_permit,
            (descriptor.execution_contract_version or ""),
            _aware(timestamp),
        )
        if descriptor is not None
        else None
    )
    if executor is None:
        return _finish_claimed_before_commit(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
            reason=ReasonCode.ADAPTER_NOT_QUALIFIED,
            now=timestamp,
        )

    preflight_context: AdapterPreflightContext | None = None
    if supports_preflight_context(executor):
        try:
            from profile.cv_content_cache import (  # noqa: PLC0415
                get_selected_cv_artifact_by_id,
                require_current_selected_cv_artifact,
            )

            selected_cv_id = str(application.selected_cv_id or "").strip()
            if not selected_cv_id or selected_cv_id != domain_plan.selected_cv_id:
                raise ValueError("selected CV identity changed")
            selected_cv = get_selected_cv_artifact_by_id(
                selected_cv_id,
                cv_routing_path=runtime_settings.cv_routing_path,
                cv_directory=runtime_settings.cv_directory,
            )
            if selected_cv is None:
                raise ValueError("selected CV is unavailable")
            selected_cv = require_current_selected_cv_artifact(
                selected_cv,
                expected_sha256=domain_plan.selected_cv_hash,
            )
            if selected_cv.cv_id != selected_cv_id:
                raise ValueError("selected CV resolver returned another identity")
            preflight_context = AdapterPreflightContext(
                normalized_job_url=normalized_url,
                selected_cv_id=selected_cv.cv_id,
                selected_cv_hash=selected_cv.pdf_sha256,
                resume_path=selected_cv.resolved_path,
            )
        except Exception as exc:
            logger.warning(
                "submission_preflight_cv_unavailable",
                command_id=command_id,
                attempt_id=attempt.id,
                error_type=type(exc).__name__[:80],
            )
            return _finish_claimed_before_commit(
                db,
                command_id=command_id,
                expected_claim_token=claim_token,
                reason=ReasonCode.ATTACHMENT_UNVERIFIED,
                now=_now(),
            )

    if not _enter_claimed_preflight(
        db,
        command_id=command_id,
        expected_claim_token=claim_token,
    ):
        return "superseded"

    lifecycle = SameEventLoopLifecycle()
    try:
        lifecycle.open()
    except Exception as exc:
        logger.warning(
            "submission_async_lifecycle_unavailable",
            command_id=command_id,
            attempt_id=attempt.id,
            error_type=type(exc).__name__[:80],
        )
        return _finish_claimed_before_commit(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
            reason=ReasonCode.INTERNAL_ERROR,
            now=_now(),
        )
    action: PreparedFinalActionV1 | None = None
    try:
        try:
            with prohibit_llm_generation():
                preflight_call = (
                    executor.preflight(
                        plan=domain_plan,
                        permit=domain_permit,
                        context=preflight_context,
                    )
                    if preflight_context is not None
                    else executor.preflight(
                        plan=domain_plan,
                        permit=domain_permit,
                    )
                )
                raw_preflight = lifecycle.run(preflight_call)
            preflight = parse_preflight_outcome(raw_preflight)
        except Exception as exc:
            logger.warning(
                "submission_preflight_failed",
                command_id=command_id,
                attempt_id=attempt.id,
                error_type=type(exc).__name__[:80],
            )
            preflight = FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)

        if not isinstance(preflight, PreparedFinalActionV1):
            finishing_context = _lock_claimed_context(
                db,
                command_id=command_id,
                expected_claim_token=claim_token,
            )
            if finishing_context is None:
                return "superseded"
            _application, attempt, command = finishing_context
            _finish_attempt(
                db,
                attempt=attempt,
                command=command,
                outcome=preflight,
                now=_now(),
            )
            db.commit()
            return AttemptOutcome(preflight.kind).value

        action = preflight
        ready_result = _mark_claimed_attempt_ready(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
            action=action,
            now=_now(),
        )
        if ready_result == "superseded":
            return "superseded"
        if ready_result == "invalid":
            return _finish_claimed_before_commit(
                db,
                command_id=command_id,
                expected_claim_token=claim_token,
                reason=ReasonCode.FORM_CHANGED,
                now=_now(),
            )

        # This transaction is the ambiguity boundary. A crash after it commits
        # can never be treated as a safe retry.
        if governor is None:
            from core.governor import GovernorUnavailableError, get_governor

            try:
                governor = get_governor(require_shared=True)
            except GovernorUnavailableError:
                governor = None

        def governor_gate():
            if governor is None:
                return False, "governor backend unavailable"
            return governor.reserve_final_action(
                reservation_id=f"attempt-{attempt.id}",
                platform=str(attempt.adapter_name or ""),
            )

        try:
            boundary = _enter_commit_boundary(
                db,
                command_id=command_id,
                expected_claim_token=claim_token,
                job_url_hash=url_hash(normalized_url),
                action=action,
                governor_gate=governor_gate,
            )
        except _CommitBoundaryRejectedError:
            return AttemptOutcome.FAILED_BEFORE_COMMIT.value
        if boundary is None:
            return "superseded"
        attempt, _command = boundary

        # Hold the application/command row locks across the one irreversible
        # adapter call. This is deliberately a short critical section: if the
        # worker is alive but slow, stale reconciliation cannot publish UNKNOWN
        # and authorize operator retry while the original click can still occur.
        # If the worker process dies, its connection releases these locks while
        # the already-committed COMMITTING stage remains available for quarantine.
        commit_context = _lock_claimed_context(
            db,
            command_id=command_id,
            expected_claim_token=claim_token,
        )
        if commit_context is None:
            return "superseded"
        _application, attempt, command = commit_context
        if (
            attempt.stage != AttemptStage.COMMITTING.value
            or attempt.final_action_at is None
            or attempt.final_submit_permit is None
            or attempt.final_submit_permit.consumed_at is None
        ):
            db.rollback()
            return "superseded"

        commit_started_at = _now()
        if not action.binds(domain_plan, domain_permit, at=_aware(commit_started_at)):
            _finish_attempt(
                db,
                attempt=attempt,
                command=command,
                outcome=UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED),
                now=commit_started_at,
            )
            db.commit()
            return AttemptOutcome.UNKNOWN.value

        try:
            with prohibit_llm_generation():
                raw_outcome = lifecycle.run(
                    executor.commit(
                        action=action,
                        permit=domain_permit,
                    )
                )
            outcome = parse_commit_outcome(raw_outcome)
        except Exception as exc:
            logger.warning(
                "submission_commit_indeterminate",
                command_id=command_id,
                attempt_id=attempt.id,
                error_type=type(exc).__name__[:80],
            )
            outcome = UnknownOutcome(reason_code=ReasonCode.INTERNAL_ERROR)

        finished_at = _now()
        if isinstance(outcome, ConfirmedSubmittedOutcome) and not _confirmed_evidence_is_valid(
            attempt,
            outcome,
            observed_by=finished_at,
        ):
            outcome = UnknownOutcome(reason_code=ReasonCode.EVIDENCE_INVALID)
        if isinstance(outcome, ConfirmedSubmittedOutcome):
            _set_stage(
                db,
                attempt,
                AttemptStage.VERIFYING,
                occurred_at=finished_at,
            )
            enqueue_control_plane_attempt_transition(
                db,
                attempt=attempt,
                command=command,
                occurred_at=finished_at,
            )
        elif not isinstance(outcome, UnknownOutcome):
            outcome = UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        _finish_attempt(
            db,
            attempt=attempt,
            command=command,
            outcome=outcome,
            now=finished_at,
        )
        db.commit()
        return AttemptOutcome(outcome.kind).value
    finally:
        try:
            with prohibit_llm_generation():
                lifecycle.run(
                    cleanup_prepared_action_if_supported(
                        executor,
                        action=action,
                    )
                )
        except Exception as exc:
            logger.warning(
                "submission_async_lifecycle_cleanup_failed",
                command_id=command_id,
                attempt_id=attempt.id,
                error_type=type(exc).__name__[:80],
            )
        try:
            lifecycle.close()
        except Exception as exc:
            logger.warning(
                "submission_async_lifecycle_close_failed",
                command_id=command_id,
                attempt_id=attempt.id,
                error_type=type(exc).__name__[:80],
            )


def reconcile_stale_submission_commands(
    db,
    *,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> int:
    """Requeue safe pre-commit work; quarantine every indeterminate action."""
    timestamp = now or _now()
    ttl = stale_seconds or get_settings().submission_command_claim_ttl_seconds
    cutoff = timestamp - timedelta(seconds=max(1, ttl))
    candidate_rows = (
        db.query(SubmissionCommand.id, Submission.application_id)
        .join(Submission, Submission.id == SubmissionCommand.attempt_id)
        .filter(
            SubmissionCommand.state == "claimed",
            SubmissionCommand.claimed_at < cutoff,
        )
        .order_by(SubmissionCommand.claimed_at, SubmissionCommand.id)
        .all()
    )
    reconciled = 0
    for command_id, application_id in candidate_rows:
        if db.bind.dialect.name == "postgresql":
            application = (
                db.query(Application)
                .filter(Application.id == application_id)
                .with_for_update(skip_locked=True)
                .populate_existing()
                .first()
            )
            if application is None:
                continue
        command_query = db.query(SubmissionCommand).filter(
            SubmissionCommand.id == command_id,
            SubmissionCommand.state == "claimed",
            SubmissionCommand.claimed_at < cutoff,
        )
        if db.bind.dialect.name == "postgresql":
            command_query = command_query.with_for_update(skip_locked=True)
        command = command_query.populate_existing().first()
        if command is None:
            continue
        attempt = command.attempt
        crossed_boundary = attempt.stage in {
            AttemptStage.COMMITTING.value,
            AttemptStage.VERIFYING.value,
        } or (
            attempt.final_submit_permit is not None
            and attempt.final_submit_permit.consumed_at is not None
        )
        if crossed_boundary:
            _finish_attempt(
                db,
                attempt=attempt,
                command=command,
                outcome=UnknownOutcome(reason_code=ReasonCode.STALE_INDETERMINATE),
                now=timestamp,
            )
        else:
            previous_stage = AttemptStage(attempt.stage)
            command.state = "pending"
            command.claimed_at = None
            command.claimed_by = None
            command.claim_token = None
            command.last_error_code = "SAFE_PRECOMMIT_REDELIVERY"
            attempt.stage = AttemptStage.QUEUED.value
            attempt.outcome = None
            attempt.status = project_legacy_status(AttemptStage.QUEUED)
            attempt.started_at = None
            record_attempt_stage(
                db,
                attempt,
                stage=AttemptStage.QUEUED,
                previous_stage=previous_stage,
                occurred_at=timestamp,
                transition_key=f"safe-redelivery:{command.id}:{timestamp.isoformat()}",
            )
            enqueue_control_plane_attempt_transition(
                db,
                attempt=attempt,
                command=command,
                occurred_at=timestamp,
            )
        reconciled += 1
    db.commit()
    return reconciled


@shared_task(
    name="worker.submission_commands.execute_submission_command_task",
    bind=True,
    max_retries=0,
)
def execute_submission_command_task(self, command_id: int | None = None):
    """Claim and execute one durable command; broker redelivery is harmless."""
    del self
    db = get_session_factory()()
    try:
        claimed_id = claim_submission_command(db, command_id=command_id)
        if claimed_id is None:
            return "skipped"
        return execute_claimed_submission_command(db, claimed_id)
    finally:
        db.close()


@shared_task(name="worker.submission_commands.drain_submission_commands_task")
def drain_submission_commands_task() -> int:
    """Drain a bounded DB batch promptly when best-effort broker wakes are lost."""
    db = get_session_factory()()
    try:
        limit = max(1, min(100, get_settings().submission_command_drain_batch_size))
        processed = 0
        for _ in range(limit):
            claimed_id = claim_submission_command(db)
            if claimed_id is None:
                break
            execute_claimed_submission_command(db, claimed_id)
            processed += 1
        return processed
    finally:
        db.close()


@shared_task(name="worker.submission_commands.reconcile_stale_commands_task")
def reconcile_stale_commands_task() -> int:
    db = get_session_factory()()
    try:
        return reconcile_stale_submission_commands(db)
    finally:
        db.close()
