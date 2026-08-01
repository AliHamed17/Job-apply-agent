"""Fail-closed quarantine for a restored private application database."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from core.application_audit import record_application_event
from core.automation_authority_fence import lock_automation_authority_fence
from core.submission_truth import is_employer_verified
from db.models import (
    Application,
    AutomationPolicyRevisionRecord,
    AutopilotInspectionRun,
    ControlPlaneReviewGrant,
    FinalSubmitPermit,
    FormPlan,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionStatus,
)

RESTORE_REASON = "RESTORE_QUARANTINE"
PRECOMMIT_REASON = "RUNTIME_NOT_READY"
POSTCOMMIT_REASON = "STALE_INDETERMINATE"
_PRECOMMIT_STAGES = frozenset({"queued", "inspecting", "preparing", "ready"})
_POSTCOMMIT_STAGES = frozenset({"committing", "verifying"})
_ACTIVE_COMMAND_STATES = frozenset({"pending", "claimed"})


def _naive_utc(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp
    return timestamp.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class RestoreQuarantineSummary:
    """Bounded counts suitable for operator output without private content."""

    automation_policies_revoked: int = 0
    autopilot_inspections_quarantined: int = 0
    form_plans_invalidated: int = 0
    final_permits_expired: int = 0
    review_grants_revoked: int = 0
    review_grant_revocations_rearmed: int = 0
    commands_cancelled: int = 0
    precommit_attempts_cancelled: int = 0
    postcommit_attempts_marked_unknown: int = 0
    applications_moved_to_review: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class _FinishedAttemptEvidenceView:
    """Validate exact persisted evidence while ignoring only stale stage metadata."""

    stage = "finished"

    def __init__(self, attempt: Submission):
        self._attempt = attempt

    def __getattr__(self, name: str):
        return getattr(self._attempt, name)


def _has_employer_verified_evidence(attempt: Submission) -> bool:
    """Protect employer proof even if a restored legacy stage is inconsistent."""

    return is_employer_verified(attempt) or (
        attempt.outcome == "confirmed_submitted"
        and is_employer_verified(_FinishedAttemptEvidenceView(attempt))
    )


def _lock_all(db, model):
    query = db.query(model)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.all()


def _rearm_review_grant_revocation(
    grant: ControlPlaneReviewGrant,
    *,
    timestamp: datetime,
) -> bool:
    """Make an unconsumed local tombstone deliverable after a restore."""

    if grant.consumed_at is not None or grant.revocation_state == "delivered":
        return False
    desired_state = "expired" if grant.expires_at <= timestamp else "pending"
    desired_available_at = grant.revocation_available_at or timestamp
    changed = (
        grant.revocation_state != desired_state
        or grant.revocation_available_at != desired_available_at
        or grant.revocation_claimed_at is not None
        or grant.revocation_claimed_by is not None
        or grant.revocation_claim_token is not None
        or grant.revocation_sent_at is not None
    )
    if changed:
        grant.revocation_state = desired_state
        grant.revocation_available_at = desired_available_at
        grant.revocation_claimed_at = None
        grant.revocation_claimed_by = None
        grant.revocation_claim_token = None
        grant.revocation_sent_at = None
        grant.last_revocation_error_code = RESTORE_REASON
    return changed


def quarantine_restored_runtime(
    db,
    *,
    now: datetime | None = None,
) -> RestoreQuarantineSummary:
    """Invalidate all restored authority without deleting historical evidence.

    The caller owns the transaction. Repeating this function is a no-op after
    the first successful commit. Confirmed terminal attempts and their evidence
    are never rewritten. Any attempt that may have crossed the irreversible
    boundary becomes ``unknown`` and therefore cannot be retried automatically.
    """

    timestamp = _naive_utc(now)
    # Restoration changes the authority domain itself. Serialize this mutation
    # with policy activation/revocation and the final irreversible boundary.
    lock_automation_authority_fence(db)
    affected_applications: dict[int, str] = {}
    policies_revoked = 0
    inspections_quarantined = 0
    plans_invalidated = 0
    permits_expired = 0
    grants_revoked = 0
    grant_revocations_rearmed = 0
    commands_cancelled = 0
    precommit_cancelled = 0
    postcommit_unknown = 0
    attempts = _lock_all(db, Submission)
    verified_application_ids = {
        attempt.application_id for attempt in attempts if _has_employer_verified_evidence(attempt)
    }

    for policy in _lock_all(db, AutomationPolicyRevisionRecord):
        if policy.active_slot != 1 or policy.revoked_at is not None:
            continue
        policy.active_slot = None
        policy.revoked_at = timestamp
        policy.revoked_by = "restore_quarantine"
        policy.revocation_reason = RESTORE_REASON
        policies_revoked += 1

    for run in _lock_all(db, AutopilotInspectionRun):
        if run.state == "finished":
            continue
        run.state = "finished"
        run.claimed_at = run.claimed_at or timestamp
        run.lease_expires_at = None
        run.claim_token = None
        run.finished_at = timestamp
        run.reason_code = RESTORE_REASON
        inspections_quarantined += 1
        if run.application_id not in verified_application_ids:
            affected_applications.setdefault(run.application_id, PRECOMMIT_REASON)

    for plan in _lock_all(db, FormPlan):
        if plan.invalidated_at is not None:
            continue
        plan.invalidated_at = timestamp
        plan.invalidation_reason = RESTORE_REASON
        plans_invalidated += 1
        if plan.application_id not in verified_application_ids:
            affected_applications.setdefault(plan.application_id, PRECOMMIT_REASON)

    for permit in _lock_all(db, FinalSubmitPermit):
        if permit.consumed_at is None and permit.expires_at > timestamp:
            permit.expires_at = timestamp
            permits_expired += 1
            if permit.attempt.application_id not in verified_application_ids:
                affected_applications.setdefault(
                    permit.attempt.application_id,
                    PRECOMMIT_REASON,
                )

    for grant in _lock_all(db, ControlPlaneReviewGrant):
        if grant.consumed_at is not None:
            continue
        if grant.revoked_at is None:
            grant.revoked_at = timestamp
            grants_revoked += 1
            if grant.application_id not in verified_application_ids:
                affected_applications.setdefault(grant.application_id, PRECOMMIT_REASON)
        if _rearm_review_grant_revocation(grant, timestamp=timestamp):
            grant_revocations_rearmed += 1
            if grant.application_id not in verified_application_ids:
                affected_applications.setdefault(grant.application_id, PRECOMMIT_REASON)

    for command in _lock_all(db, SubmissionCommand):
        if command.state not in _ACTIVE_COMMAND_STATES:
            continue
        command.state = "cancelled"
        command.completed_at = timestamp
        command.claimed_at = None
        command.claimed_by = None
        command.claim_token = None
        command.last_error_code = RESTORE_REASON
        commands_cancelled += 1
        if command.attempt.application_id not in verified_application_ids:
            affected_applications.setdefault(
                command.attempt.application_id,
                PRECOMMIT_REASON,
            )

    for attempt in attempts:
        if _has_employer_verified_evidence(attempt):
            continue
        if attempt.stage == "finished":
            continue
        permit_consumed = (
            attempt.final_submit_permit is not None
            and attempt.final_submit_permit.consumed_at is not None
        )
        crossed_boundary = (
            attempt.stage in _POSTCOMMIT_STAGES
            or attempt.final_action_at is not None
            or permit_consumed
        )
        if crossed_boundary:
            attempt.status = SubmissionStatus.UNKNOWN
            attempt.outcome = "unknown"
            attempt.reason_code = POSTCOMMIT_REASON
            postcommit_unknown += 1
            if attempt.application_id not in verified_application_ids:
                affected_applications[attempt.application_id] = POSTCOMMIT_REASON
        else:
            if attempt.stage not in _PRECOMMIT_STAGES:
                # Unknown future stages fail toward the indeterminate outcome.
                attempt.status = SubmissionStatus.UNKNOWN
                attempt.outcome = "unknown"
                attempt.reason_code = POSTCOMMIT_REASON
                postcommit_unknown += 1
                if attempt.application_id not in verified_application_ids:
                    affected_applications[attempt.application_id] = POSTCOMMIT_REASON
            else:
                attempt.status = SubmissionStatus.FAILED
                attempt.outcome = "failed_before_commit"
                attempt.reason_code = PRECOMMIT_REASON
                precommit_cancelled += 1
                if attempt.application_id not in verified_application_ids:
                    affected_applications.setdefault(
                        attempt.application_id,
                        PRECOMMIT_REASON,
                    )
        attempt.stage = "finished"
        attempt.finished_at = attempt.finished_at or timestamp
        attempt.submitted_at = None

    applications_moved = 0
    if affected_applications:
        applications = (
            db.query(Application).filter(Application.id.in_(tuple(affected_applications))).all()
        )
        for application in applications:
            reason = affected_applications[application.id]
            changed = (
                application.status != JobStatus.NEEDS_REVIEW
                or application.needs_review_reason != reason
                or application.prepared_revision is not None
                or application.approved_at is not None
                or application.approval_source is not None
                or (
                    application.job is not None and application.job.status != JobStatus.NEEDS_REVIEW
                )
            )
            application.status = JobStatus.NEEDS_REVIEW
            application.needs_review_reason = reason
            application.prepared_revision = None
            application.approved_at = None
            application.approval_source = None
            if application.job is not None:
                application.job.status = JobStatus.NEEDS_REVIEW
            if changed:
                applications_moved += 1
                record_application_event(
                    db,
                    application.id,
                    "restore_quarantine_applied",
                    actor="system",
                    details={
                        "reason_code": reason,
                        "state": JobStatus.NEEDS_REVIEW.value,
                        "external_action_queued": False,
                    },
                )

    return RestoreQuarantineSummary(
        automation_policies_revoked=policies_revoked,
        autopilot_inspections_quarantined=inspections_quarantined,
        form_plans_invalidated=plans_invalidated,
        final_permits_expired=permits_expired,
        review_grants_revoked=grants_revoked,
        review_grant_revocations_rearmed=grant_revocations_rearmed,
        commands_cancelled=commands_cancelled,
        precommit_attempts_cancelled=precommit_cancelled,
        postcommit_attempts_marked_unknown=postcommit_unknown,
        applications_moved_to_review=applications_moved,
    )


__all__ = [
    "RESTORE_REASON",
    "RestoreQuarantineSummary",
    "quarantine_restored_runtime",
]
