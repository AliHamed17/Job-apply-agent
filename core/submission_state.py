"""Fail-closed attempt-stage transitions and legacy status projection."""

from __future__ import annotations

from collections.abc import Mapping

from core.submission_domain import AttemptOutcome, AttemptStage, CommitOutcome
from db.models import SubmissionStatus


class InvalidSubmissionStateError(ValueError):
    """Raised when a caller requests a transition not allowed by the domain."""


_ALLOWED_NEXT_STAGES: Mapping[AttemptStage, frozenset[AttemptStage]] = {
    AttemptStage.QUEUED: frozenset({AttemptStage.INSPECTING, AttemptStage.FINISHED}),
    AttemptStage.INSPECTING: frozenset(
        {AttemptStage.PREPARING, AttemptStage.READY, AttemptStage.FINISHED}
    ),
    AttemptStage.PREPARING: frozenset({AttemptStage.READY, AttemptStage.FINISHED}),
    AttemptStage.READY: frozenset({AttemptStage.COMMITTING, AttemptStage.FINISHED}),
    AttemptStage.COMMITTING: frozenset({AttemptStage.VERIFYING, AttemptStage.FINISHED}),
    AttemptStage.VERIFYING: frozenset({AttemptStage.FINISHED}),
    AttemptStage.FINISHED: frozenset(),
}

_ALLOWED_TERMINAL_OUTCOMES: Mapping[AttemptStage, frozenset[AttemptOutcome]] = {
    AttemptStage.QUEUED: frozenset(
        {
            AttemptOutcome.NEEDS_REVIEW,
            AttemptOutcome.FAILED_BEFORE_COMMIT,
            AttemptOutcome.DRAFT_ONLY,
        }
    ),
    AttemptStage.INSPECTING: frozenset(
        {
            AttemptOutcome.ALREADY_APPLIED,
            AttemptOutcome.NEEDS_REVIEW,
            AttemptOutcome.FAILED_BEFORE_COMMIT,
            AttemptOutcome.DRAFT_ONLY,
        }
    ),
    AttemptStage.PREPARING: frozenset(
        {
            AttemptOutcome.ALREADY_APPLIED,
            AttemptOutcome.NEEDS_REVIEW,
            AttemptOutcome.FAILED_BEFORE_COMMIT,
            AttemptOutcome.DRAFT_ONLY,
        }
    ),
    AttemptStage.READY: frozenset(
        {
            AttemptOutcome.ALREADY_APPLIED,
            AttemptOutcome.NEEDS_REVIEW,
            AttemptOutcome.FAILED_BEFORE_COMMIT,
            AttemptOutcome.DRAFT_ONLY,
        }
    ),
    # COMMITTING means the ambiguity boundary has been crossed.  Even a
    # browser exception is indeterminate until employer evidence is reconciled.
    AttemptStage.COMMITTING: frozenset({AttemptOutcome.UNKNOWN}),
    AttemptStage.VERIFYING: frozenset({AttemptOutcome.CONFIRMED_SUBMITTED, AttemptOutcome.UNKNOWN}),
    AttemptStage.FINISHED: frozenset(),
}

_LEGACY_TERMINAL_PROJECTION: Mapping[AttemptOutcome, SubmissionStatus] = {
    AttemptOutcome.CONFIRMED_SUBMITTED: SubmissionStatus.SUCCESS,
    AttemptOutcome.ALREADY_APPLIED: SubmissionStatus.FAILED,
    AttemptOutcome.NEEDS_REVIEW: SubmissionStatus.FAILED,
    AttemptOutcome.UNKNOWN: SubmissionStatus.UNKNOWN,
    AttemptOutcome.FAILED_BEFORE_COMMIT: SubmissionStatus.FAILED,
    AttemptOutcome.DRAFT_ONLY: SubmissionStatus.DRAFT_ONLY,
    # Reconciliation and historical imports are intentionally never projected
    # to legacy SUCCESS, which protects all existing green/counting paths.
    AttemptOutcome.OPERATOR_CONFIRMED: SubmissionStatus.UNKNOWN,
    AttemptOutcome.LEGACY_UNVERIFIED: SubmissionStatus.UNKNOWN,
}


def _normalize_outcome(outcome: AttemptOutcome | CommitOutcome) -> AttemptOutcome:
    if isinstance(outcome, AttemptOutcome):
        return outcome
    return AttemptOutcome(outcome.kind)


def allowed_next_stages(stage: AttemptStage) -> frozenset[AttemptStage]:
    """Return an immutable copy of the explicit transition targets."""

    return _ALLOWED_NEXT_STAGES.get(stage, frozenset())


def allowed_terminal_outcomes(stage: AttemptStage) -> frozenset[AttemptOutcome]:
    """Return the outcomes accepted when finishing from ``stage``."""

    return _ALLOWED_TERMINAL_OUTCOMES.get(stage, frozenset())


def can_transition(
    current: AttemptStage,
    target: AttemptStage,
    outcome: AttemptOutcome | CommitOutcome | None = None,
) -> bool:
    """Return false for any unlisted, regressive, or contradictory transition."""

    if target not in allowed_next_stages(current):
        return False
    if target == AttemptStage.FINISHED:
        return outcome is not None and _normalize_outcome(outcome) in allowed_terminal_outcomes(
            current
        )
    return outcome is None


def require_transition(
    current: AttemptStage,
    target: AttemptStage,
    outcome: AttemptOutcome | CommitOutcome | None = None,
) -> None:
    """Raise instead of silently accepting an invalid attempt transition."""

    if not can_transition(current, target, outcome):
        outcome_value = _normalize_outcome(outcome).value if outcome is not None else None
        raise InvalidSubmissionStateError(
            f"invalid submission transition: {current.value} -> {target.value}"
            f" (outcome={outcome_value})"
        )


def project_legacy_status(
    stage: AttemptStage,
    outcome: AttemptOutcome | CommitOutcome | None = None,
) -> SubmissionStatus:
    """Project v4 state without allowing unverified outcomes to become green."""

    if stage == AttemptStage.FINISHED:
        if outcome is None:
            raise InvalidSubmissionStateError("finished attempts require a terminal outcome")
        normalized = _normalize_outcome(outcome)
        try:
            return _LEGACY_TERMINAL_PROJECTION[normalized]
        except KeyError as exc:  # pragma: no cover - defensive for future enum additions
            raise InvalidSubmissionStateError(
                "terminal outcome has no safe legacy projection"
            ) from exc

    if outcome is not None:
        raise InvalidSubmissionStateError("non-finished attempts cannot have a terminal outcome")
    if stage in {
        AttemptStage.QUEUED,
        AttemptStage.INSPECTING,
        AttemptStage.PREPARING,
        AttemptStage.READY,
    }:
        return SubmissionStatus.PENDING
    if stage in {AttemptStage.COMMITTING, AttemptStage.VERIFYING}:
        return SubmissionStatus.RUNNING
    raise InvalidSubmissionStateError("attempt stage has no safe legacy projection")
