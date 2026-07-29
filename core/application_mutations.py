"""App-first locking and fail-closed application mutation helpers.

Every writer that can change private application content or lifecycle status
must lock and refresh the ``Application`` row before inspecting submission
state.  This prevents a stale ORM instance from resurrecting a skipped or
submitted application after another transaction has completed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.exc import DBAPIError

from core.application_audit import record_application_event
from core.application_revision import (
    bump_application_revision,
    mark_application_prepared,
    preparation_is_current,
)
from core.operational_metrics import record_attempt_outcome, record_attempt_stage
from db.models import (
    Application,
    FinalSubmitPermit,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionStatus,
)


class ApplicationMutationIntent(StrEnum):
    """The safety policy applied after the application row is locked."""

    CONTENT = "content"
    PREPARE = "prepare"
    TERMINAL = "terminal"


class ApplicationMutationBlockedError(RuntimeError):
    """A stable, privacy-safe reason that a requested mutation was refused."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(slots=True)
class LockedApplicationMutation:
    """Rows locked in app-first order for one exact mutation transaction."""

    application: Application
    job: Job | None
    latest_attempt: Submission | None
    active_command: SubmissionCommand | None
    active_permit: FinalSubmitPermit | None
    intent: ApplicationMutationIntent


_PRECOMMIT_STAGES = frozenset({"queued", "inspecting", "preparing", "ready"})
_POSTCOMMIT_STAGES = frozenset({"committing", "verifying"})
_IMMUTABLE_OUTCOMES = frozenset(
    {
        "confirmed_submitted",
        "already_applied",
        "unknown",
        "operator_confirmed",
        "legacy_unverified",
    }
)


def _lock_related_row(db, query):
    """Lock a child row without waiting behind a command worker.

    The application lock is intentionally acquired first.  A command worker
    may already hold the command row, so related locks use NOWAIT to avoid an
    app->command / command->app deadlock.  A busy lifecycle is a safe refusal,
    never a reason to continue from stale state.
    """

    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(nowait=True)
    try:
        return query.populate_existing().first()
    except DBAPIError as exc:
        db.rollback()
        raise ApplicationMutationBlockedError("SUBMISSION_LIFECYCLE_BUSY") from exc


def lock_application_for_mutation(
    db,
    *,
    application_id: int | None = None,
    job_id: int | None = None,
    intent: ApplicationMutationIntent = ApplicationMutationIntent.CONTENT,
    expected_revision: int | None = None,
    allow_missing: bool = False,
) -> LockedApplicationMutation | None:
    """Lock and refresh one application before any content/status mutation.

    Exactly one of ``application_id`` and ``job_id`` must be supplied.
    Content and preparation mutations reject every unfinished attempt.
    Terminal mutations may later cancel a safely pre-commit attempt through
    :func:`transition_locked_application_to_skipped`, but this function never
    mutates lifecycle rows by itself.
    """

    if (application_id is None) == (job_id is None):
        raise ValueError("exactly one application identifier is required")

    query = db.query(Application)
    if application_id is not None:
        query = query.filter(Application.id == application_id)
    else:
        query = query.filter(Application.job_id == job_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    application = query.populate_existing().first()
    if application is None:
        if allow_missing:
            return None
        raise ApplicationMutationBlockedError("APPLICATION_NOT_FOUND")

    if expected_revision is not None and int(application.revision or 1) != int(expected_revision):
        raise ApplicationMutationBlockedError("APPLICATION_REVISION_CHANGED")

    # Preserve the app-first lock order, then refresh the related job before a
    # caller can overwrite its terminal status through a stale relationship.
    job = None
    if application.job_id is not None:
        job_query = db.query(Job).filter(Job.id == application.job_id)
        if db.bind.dialect.name == "postgresql":
            job_query = job_query.with_for_update()
        job = job_query.populate_existing().first()

    immutable_query = (
        db.query(Submission)
        .filter(
            Submission.application_id == application.id,
            (
                Submission.outcome.in_(_IMMUTABLE_OUTCOMES)
                | Submission.status.in_(
                    {
                        SubmissionStatus.SUCCESS,
                        SubmissionStatus.UNKNOWN,
                    }
                )
            ),
        )
        .order_by(Submission.attempt_number.desc(), Submission.id.desc())
    )
    immutable_attempt = _lock_related_row(db, immutable_query)
    if immutable_attempt is not None:
        reason = (
            "SUBMISSION_OUTCOME_UNKNOWN"
            if immutable_attempt.outcome == "unknown"
            or immutable_attempt.status == SubmissionStatus.UNKNOWN
            else "SUBMISSION_OUTCOME_IMMUTABLE"
        )
        raise ApplicationMutationBlockedError(reason)

    attempt_query = (
        db.query(Submission)
        .filter(Submission.application_id == application.id)
        .order_by(Submission.attempt_number.desc(), Submission.id.desc())
    )
    latest_attempt = _lock_related_row(db, attempt_query)

    active_command = None
    active_permit = None
    if latest_attempt is not None and latest_attempt.stage != "finished":
        command_query = db.query(SubmissionCommand).filter(
            SubmissionCommand.attempt_id == latest_attempt.id
        )
        active_command = _lock_related_row(db, command_query)
        permit_query = db.query(FinalSubmitPermit).filter(
            FinalSubmitPermit.attempt_id == latest_attempt.id
        )
        active_permit = _lock_related_row(db, permit_query)

    if application.status == JobStatus.SUBMITTED or (
        job is not None and job.status == JobStatus.SUBMITTED
    ):
        raise ApplicationMutationBlockedError("APPLICATION_TERMINAL")
    if intent is not ApplicationMutationIntent.TERMINAL and (
        application.status == JobStatus.SKIPPED
        or (job is not None and job.status == JobStatus.SKIPPED)
    ):
        raise ApplicationMutationBlockedError("APPLICATION_TERMINAL")

    if latest_attempt is not None:
        if latest_attempt.stage != "finished":
            crossed_boundary = (
                latest_attempt.stage in _POSTCOMMIT_STAGES
                or latest_attempt.final_action_at is not None
                or (active_permit is not None and active_permit.consumed_at is not None)
            )
            if crossed_boundary:
                raise ApplicationMutationBlockedError("FINAL_ACTION_INDETERMINATE")
            if latest_attempt.stage not in _PRECOMMIT_STAGES:
                raise ApplicationMutationBlockedError("SUBMISSION_STATE_INVALID")
            if active_command is not None and active_command.state in {
                "completed",
                "cancelled",
            }:
                raise ApplicationMutationBlockedError("SUBMISSION_STATE_INVALID")
            if intent is not ApplicationMutationIntent.TERMINAL:
                raise ApplicationMutationBlockedError("SUBMISSION_ALREADY_ACTIVE")

    return LockedApplicationMutation(
        application=application,
        job=job,
        latest_attempt=latest_attempt,
        active_command=active_command,
        active_permit=active_permit,
        intent=intent,
    )


def lock_job_without_application_for_mutation(
    db,
    *,
    job_id: int,
    intent: ApplicationMutationIntent = ApplicationMutationIntent.CONTENT,
) -> Job:
    """Lock a job only after proving that it still has no application.

    Pipeline tasks such as scoring legitimately run before an ``Application``
    exists.  They must still perform an application-first absence check before
    taking the job lock, then repeat that check without waiting.  The second
    check closes the race where application creation commits while the task is
    waiting for the job row; ``NOWAIT`` prevents a Job->Application deadlock
    with an app-first writer.
    """

    appeared = lock_application_for_mutation(
        db,
        job_id=job_id,
        intent=ApplicationMutationIntent.CONTENT,
        allow_missing=True,
    )
    if appeared is not None:
        raise ApplicationMutationBlockedError("APPLICATION_CREATED_DURING_MUTATION")

    job_query = db.query(Job).filter(Job.id == job_id)
    if db.bind.dialect.name == "postgresql":
        job_query = job_query.with_for_update()
    job = job_query.populate_existing().first()
    if job is None:
        raise ApplicationMutationBlockedError("JOB_NOT_FOUND")

    application_query = db.query(Application).filter(Application.job_id == job_id)
    if db.bind.dialect.name == "postgresql":
        application_query = application_query.with_for_update(nowait=True)
    try:
        appeared_after_job_lock = application_query.populate_existing().first()
    except DBAPIError as exc:
        db.rollback()
        raise ApplicationMutationBlockedError("SUBMISSION_LIFECYCLE_BUSY") from exc
    if appeared_after_job_lock is not None:
        raise ApplicationMutationBlockedError("APPLICATION_CREATED_DURING_MUTATION")

    if job.status == JobStatus.SUBMITTED or (
        intent is not ApplicationMutationIntent.TERMINAL and job.status == JobStatus.SKIPPED
    ):
        raise ApplicationMutationBlockedError("JOB_TERMINAL")
    return job


def mark_locked_application_prepared(
    db,
    locked: LockedApplicationMutation,
    *,
    actor: str,
    source: str,
    now: datetime | None = None,
    event_type: str = "application_prepared",
    allowed_statuses: frozenset[JobStatus] = frozenset({JobStatus.DRAFT, JobStatus.APPROVED}),
) -> bool:
    """Prepare the exact locked revision without reviving terminal state."""

    if locked.intent is not ApplicationMutationIntent.PREPARE:
        raise ValueError("preparation requires a prepare mutation lock")
    application = locked.application
    if application.status not in allowed_statuses:
        raise ApplicationMutationBlockedError("APPLICATION_NOT_REVIEWABLE")
    if preparation_is_current(application):
        return False

    application.status = JobStatus.DRAFT
    application.approved_at = now or datetime.now(UTC).replace(tzinfo=None)
    application.approval_source = source
    mark_application_prepared(application)
    if locked.job is not None:
        locked.job.status = JobStatus.DRAFT
    record_application_event(
        db,
        application.id,
        event_type,
        actor=actor,
        details={
            "approval_source": source,
            "selected_cv_id": application.selected_cv_id,
            "profile_version": application.profile_version,
            "state": "prepared",
            "external_action_queued": False,
        },
    )
    return True


def transition_locked_application_to_skipped(
    db,
    locked: LockedApplicationMutation,
    *,
    actor: str,
    reason_code: str,
    rejection_reason: str,
    event_type: str = "application_rejected",
    now: datetime | None = None,
) -> bool:
    """Skip an application and atomically cancel only safe pre-commit work."""

    if locked.intent is not ApplicationMutationIntent.TERMINAL:
        raise ValueError("terminal transition requires a terminal mutation lock")
    application = locked.application
    timestamp = now or datetime.now(UTC).replace(tzinfo=None)
    attempt = locked.latest_attempt
    cancelled_attempt = False

    if attempt is not None and attempt.stage != "finished":
        if (
            attempt.stage not in _PRECOMMIT_STAGES
            or attempt.final_action_at is not None
            or (locked.active_permit is not None and locked.active_permit.consumed_at is not None)
        ):
            raise ApplicationMutationBlockedError("FINAL_ACTION_INDETERMINATE")
        previous_stage = attempt.stage
        record_attempt_stage(
            db,
            attempt,
            stage="finished",
            previous_stage=previous_stage,
            occurred_at=timestamp,
            transition_key="operator-cancellation",
        )
        attempt.stage = "finished"
        attempt.outcome = "failed_before_commit"
        attempt.status = SubmissionStatus.FAILED
        attempt.reason_code = reason_code[:64]
        attempt.finished_at = timestamp
        attempt.submitted_at = None
        record_attempt_outcome(
            db,
            attempt,
            occurred_at=timestamp,
            event_kind="operator_cancellation",
        )
        cancelled_attempt = True
        if locked.active_command is not None:
            locked.active_command.state = "cancelled"
            locked.active_command.completed_at = timestamp
            locked.active_command.claimed_at = None
            locked.active_command.claimed_by = None
            locked.active_command.claim_token = None
            locked.active_command.last_error_code = reason_code[:64]
        record_application_event(
            db,
            application.id,
            "submission_attempt_cancelled",
            actor=actor,
            details={
                "attempt_number": attempt.attempt_number,
                "reason_code": reason_code,
                "state": "failed_before_commit",
            },
        )

    already_skipped = application.status == JobStatus.SKIPPED and (
        locked.job is None or locked.job.status == JobStatus.SKIPPED
    )
    if already_skipped and not cancelled_attempt:
        return False

    bump_application_revision(
        db,
        application,
        reason_code=reason_code,
        now=timestamp,
    )
    application.status = JobStatus.SKIPPED
    application.rejected_at = timestamp
    application.rejection_reason = rejection_reason
    if locked.job is not None:
        locked.job.status = JobStatus.SKIPPED
    record_application_event(
        db,
        application.id,
        event_type,
        actor=actor,
        details={"reason_code": reason_code, "state": "skipped"},
    )
    return True
