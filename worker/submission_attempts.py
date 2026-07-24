"""Atomic submission-attempt claims and fail-closed recovery."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from db.models import Application, JobStatus, Submission, SubmissionStatus

STALE_ATTEMPT_MINUTES = 15

REASON_CODES = {
    "CAPTCHA": "CHALLENGE_DETECTED",
    "SESSION_EXPIRED": "SESSION_EXPIRED",
    "Easy Apply button not found": "EASY_APPLY_UNAVAILABLE",
    "Submit clicked but no success dialog appeared": "SUBMIT_UNCONFIRMED",
    "DRY_RUN": "DRY_RUN_DISCARDED",
}


def classify_reason(error: str | None, status: str) -> str | None:
    if error and error.startswith("NEEDS_REVIEW:"):
        return "REQUIRED_FIELD_UNKNOWN"
    for marker, code in REASON_CODES.items():
        if error and marker.lower() in error.lower():
            return code
    if status == "failed":
        return "SUBMITTER_FAILED"
    if status == "draft_only":
        return "DRAFT_ONLY"
    return None


def redacted_diagnostics(error: str | None) -> str | None:
    """Store only bounded structural metadata, never the raw external error."""
    if not error:
        return None
    return json.dumps(
        {"error_type": classify_reason(error, "failed") or "UNCLASSIFIED"},
        separators=(",", ":"),
    )


def latest_attempt(db, application_id: int) -> Submission | None:
    return (
        db.query(Submission)
        .filter(Submission.application_id == application_id)
        .order_by(Submission.attempt_number.desc())
        .first()
    )


def mark_stale_attempts_unknown(
    db, now: datetime | None = None, stale_minutes: int = STALE_ATTEMPT_MINUTES
) -> int:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(minutes=stale_minutes)
    rows = (
        db.query(Submission)
        .filter(
            Submission.status.in_(
                (SubmissionStatus.PENDING, SubmissionStatus.RUNNING)
            ),
            Submission.started_at < cutoff,
        )
        .all()
    )
    for attempt in rows:
        attempt.status = SubmissionStatus.UNKNOWN
        attempt.reason_code = "STALE_INDETERMINATE"
        attempt.finished_at = now
        app = attempt.application
        app.status = JobStatus.NEEDS_REVIEW
        app.needs_review_reason = "Submission outcome is unknown; reconcile manually."
        if app.job:
            app.job.status = JobStatus.NEEDS_REVIEW
    db.commit()
    return len(rows)


def claim_attempt(db, application_id: int) -> Submission | None:
    """Claim exactly one attempt; PostgreSQL serializes with SKIP LOCKED."""
    query = db.query(Application).filter(Application.id == application_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    app = query.first()
    if app is None or app.status != JobStatus.APPROVED:
        db.rollback()
        return None

    current = latest_attempt(db, application_id)
    if current and current.status in (
        SubmissionStatus.PENDING,
        SubmissionStatus.RUNNING,
        SubmissionStatus.SUCCESS,
        SubmissionStatus.UNKNOWN,
    ):
        db.rollback()
        return None

    next_number = (
        db.query(func.coalesce(func.max(Submission.attempt_number), 0))
        .filter(Submission.application_id == application_id)
        .scalar()
        + 1
    )
    attempt = Submission(
        application_id=application_id,
        attempt_number=next_number,
        idempotency_key=str(uuid.uuid4()),
        submitter_name="unresolved",
        status=SubmissionStatus.RUNNING,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(attempt)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(attempt)
    return attempt


def mark_attempt_unknown(db, attempt: Submission, reason: str) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    attempt.status = SubmissionStatus.UNKNOWN
    attempt.reason_code = reason
    attempt.finished_at = now
    app = attempt.application
    app.status = JobStatus.NEEDS_REVIEW
    app.needs_review_reason = "Submission outcome is unknown; reconcile manually."
    if app.job:
        app.job.status = JobStatus.NEEDS_REVIEW
    db.commit()
