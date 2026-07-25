"""Authoritative, fail-closed submission verification helpers.

Legacy ``SUCCESS`` rows and operator reconciliation are intentionally not
treated as employer-verified.  A future domain migration will replace these
reason-code markers with a typed verification column; keeping the decision in
one helper prevents optimistic API and dashboard code in the meantime.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import and_, func, or_

from db.models import Submission, SubmissionStatus

EMPLOYER_VERIFIED_REASON_CODES = frozenset(
    {
        "EMPLOYER_VERIFIED",
        "ATS_CONFIRMATION_VERIFIED",
        "PROVIDER_RECEIPT_VERIFIED",
    }
)


class SubmissionLike(Protocol):
    status: SubmissionStatus
    reason_code: str | None
    submitted_at: object | None
    confirmation_id: str | None
    confirmation_url: str | None


def has_nonblank_employer_evidence(
    confirmation_id: str | None,
    confirmation_url: str | None,
) -> bool:
    """Reject null, empty, and whitespace-only evidence references."""
    return any(
        isinstance(value, str) and bool(value.strip())
        for value in (confirmation_id, confirmation_url)
    )


def is_employer_verified(attempt: SubmissionLike | None) -> bool:
    """Return true only for a post-action employer evidence record."""
    if attempt is None:
        return False
    has_evidence = has_nonblank_employer_evidence(
        attempt.confirmation_id,
        attempt.confirmation_url,
    )
    return (
        attempt.status == SubmissionStatus.SUCCESS
        and attempt.submitted_at is not None
        and attempt.reason_code in EMPLOYER_VERIFIED_REASON_CODES
        and has_evidence
    )


def latest_employer_verified_query(db):
    """Build a query containing one latest, employer-verified attempt per app."""
    latest_attempts = (
        db.query(
            Submission.application_id.label("application_id"),
            func.max(Submission.attempt_number).label("attempt_number"),
        )
        .group_by(Submission.application_id)
        .subquery()
    )
    return (
        db.query(Submission)
        .join(
            latest_attempts,
            and_(
                Submission.application_id == latest_attempts.c.application_id,
                Submission.attempt_number == latest_attempts.c.attempt_number,
            ),
        )
        .filter(
            Submission.status == SubmissionStatus.SUCCESS,
            Submission.submitted_at.isnot(None),
            Submission.reason_code.in_(EMPLOYER_VERIFIED_REASON_CODES),
            or_(
                and_(
                    Submission.confirmation_id.isnot(None),
                    func.length(func.trim(Submission.confirmation_id)) > 0,
                ),
                and_(
                    Submission.confirmation_url.isnot(None),
                    func.length(func.trim(Submission.confirmation_url)) > 0,
                ),
            ),
        )
    )


def latest_employer_verified_count(db) -> int:
    return latest_employer_verified_query(db).count()
