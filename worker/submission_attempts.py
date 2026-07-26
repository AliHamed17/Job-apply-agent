"""Atomic submission-attempt claims and fail-closed recovery."""

from __future__ import annotations

import json
import re
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


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:+-]{1,80}$")
_TRACE_KEYS = {
    "event",
    "selector_version",
    "step",
    "field_types",
    "resolver_sources",
    "terminal_reason",
    "timestamp",
}


def _safe_trace_event(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    event: dict = {}
    for key, value in raw.items():
        if key not in _TRACE_KEYS:
            continue
        if key == "step" and isinstance(value, int) and 0 <= value <= 100:
            event[key] = value
        elif key in {"field_types", "resolver_sources"} and isinstance(value, list):
            event[key] = [
                item for item in value[:20] if isinstance(item, str) and _SAFE_TOKEN.fullmatch(item)
            ]
        elif isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
            event[key] = value
    return event


def redacted_result_diagnostics(
    error: str | None,
    details: dict | None,
) -> str | None:
    """Serialize only structural browser trace metadata from a submitter."""
    safe: dict = {}
    details = details or {}
    selector_version = details.get("selector_version")
    terminal_reason = details.get("terminal_reason")
    step_count = details.get("step_count")
    if isinstance(selector_version, str) and _SAFE_TOKEN.fullmatch(selector_version):
        safe["selector_version"] = selector_version
    if isinstance(terminal_reason, str) and _SAFE_TOKEN.fullmatch(terminal_reason):
        safe["terminal_reason"] = terminal_reason
    if isinstance(step_count, int) and 0 <= step_count <= 100:
        safe["step_count"] = step_count
    if isinstance(details.get("events"), list):
        events = [_safe_trace_event(item) for item in details["events"][-30:]]
        safe["events"] = [event for event in events if event]
    if not safe and error:
        safe["error_type"] = classify_reason(error, "failed") or "UNCLASSIFIED"
    if not safe:
        return None
    return json.dumps(safe, separators=(",", ":"), sort_keys=True)


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
            Submission.status.in_((SubmissionStatus.PENDING, SubmissionStatus.RUNNING)),
            Submission.started_at < cutoff,
            ~Submission.command.has(),
        )
        .all()
    )
    for attempt in rows:
        attempt.status = SubmissionStatus.UNKNOWN
        attempt.stage = "finished"
        attempt.outcome = "unknown"
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
    if current and current.status in (
        SubmissionStatus.FAILED,
        SubmissionStatus.DRAFT_ONLY,
    ):
        # Compatibility for legacy callers that set only the v3 status field.
        # These are definitively pre-commit outcomes, so closing the typed
        # lifecycle is safe and releases the unfinished-attempt uniqueness gate.
        current.stage = "finished"  # type: ignore[assignment]
        legacy_outcome = (
            "draft_only"
            if current.status == SubmissionStatus.DRAFT_ONLY
            else "failed_before_commit"
        )
        current.outcome = legacy_outcome  # type: ignore[assignment]
        finished_at = current.finished_at or datetime.now(UTC).replace(tzinfo=None)
        current.finished_at = finished_at  # type: ignore[assignment]
        db.flush()

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
        stage="inspecting",
        outcome=None,
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
    attempt.status = SubmissionStatus.UNKNOWN  # type: ignore[assignment]
    attempt.stage = "finished"  # type: ignore[assignment]
    attempt.outcome = "unknown"  # type: ignore[assignment]
    attempt.reason_code = reason  # type: ignore[assignment]
    attempt.finished_at = now  # type: ignore[assignment]
    app = attempt.application
    app.status = JobStatus.NEEDS_REVIEW
    app.needs_review_reason = "Submission outcome is unknown; reconcile manually."
    if app.job:
        app.job.status = JobStatus.NEEDS_REVIEW
    db.commit()
