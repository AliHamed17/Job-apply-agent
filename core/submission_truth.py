"""Authoritative employer-evidence verification helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, func, text

from core.submission_domain import EvidenceType
from db.models import Submission, SubmissionEvidence, SubmissionStatus

EMPLOYER_VERIFIED_REASON_CODES = frozenset(
    {
        "EMPLOYER_VERIFIED",
        "ATS_CONFIRMATION_VERIFIED",
        "PROVIDER_RECEIPT_VERIFIED",
    }
)
EMPLOYER_EVIDENCE_TYPES = frozenset(item.value for item in EvidenceType)


class SubmissionLike(Protocol):
    id: int
    status: SubmissionStatus
    stage: str
    outcome: str | None
    reason_code: str | None
    submitted_at: object | None
    final_action_at: object | None
    attachment_verified: bool
    form_plan_id: int | None
    adapter_name: str | None
    adapter_version: str | None
    selector_version: str | None
    profile_version: int | None
    runner_release: str | None
    form_plan_fingerprint: str | None
    requested_cv_id: str | None
    requested_cv_hash: str | None
    attached_cv_id: str | None
    attached_cv_hash: str | None
    verification_kind: str | None
    evidence_digest: str | None
    evidence: object


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def has_nonblank_employer_evidence(
    confirmation_id: str | None,
    confirmation_url: str | None,
) -> bool:
    """Compatibility helper; typed truth no longer trusts these fields alone."""
    return any(_nonblank(value) for value in (confirmation_id, confirmation_url))


def is_employer_verified(attempt: SubmissionLike | None) -> bool:
    """Return true only for a typed evidence row bound to this exact attempt/CV."""
    if attempt is None:
        return False
    submitted_at = _utc_datetime(getattr(attempt, "submitted_at", None))
    final_action_at = _utc_datetime(getattr(attempt, "final_action_at", None))
    if (
        getattr(attempt, "stage", None) != "finished"
        or getattr(attempt, "outcome", None) != "confirmed_submitted"
        or getattr(attempt, "status", None) != SubmissionStatus.SUCCESS
        or submitted_at is None
        or final_action_at is None
        or submitted_at < final_action_at
        or getattr(attempt, "attachment_verified", False) is not True
        or getattr(attempt, "form_plan_id", None) is None
        or not _nonblank(getattr(attempt, "adapter_name", None))
        or not _nonblank(getattr(attempt, "adapter_version", None))
        or not _nonblank(getattr(attempt, "selector_version", None))
        or not isinstance(getattr(attempt, "profile_version", None), int)
        or getattr(attempt, "profile_version", 0) < 1
        or not _nonblank(getattr(attempt, "runner_release", None))
        or len(getattr(attempt, "runner_release", "")) > 64
        or getattr(attempt, "reason_code", None) not in EMPLOYER_VERIFIED_REASON_CODES
        or getattr(attempt, "verification_kind", None) not in EMPLOYER_EVIDENCE_TYPES
        or not _sha256(getattr(attempt, "evidence_digest", None))
        or not _sha256(getattr(attempt, "form_plan_fingerprint", None))
        or not _nonblank(getattr(attempt, "requested_cv_id", None))
        or getattr(attempt, "requested_cv_id", None) != getattr(attempt, "attached_cv_id", None)
        or not _sha256(getattr(attempt, "requested_cv_hash", None))
        or getattr(attempt, "requested_cv_hash", None) != getattr(attempt, "attached_cv_hash", None)
    ):
        return False

    evidence_rows = getattr(attempt, "evidence", None)
    if not isinstance(evidence_rows, (list, tuple)):
        return False
    latest_allowed_observation = submitted_at + timedelta(seconds=5)
    return any(
        getattr(row, "attempt_id", None) == attempt.id
        and getattr(row, "evidence_type", None) == attempt.verification_kind
        and getattr(row, "evidence_digest", None) == attempt.evidence_digest
        and getattr(row, "form_fingerprint", None) == attempt.form_plan_fingerprint
        and getattr(row, "cv_hash", None) == attempt.attached_cv_hash
        and ((observed_at := _utc_datetime(getattr(row, "observed_at", None))) is not None)
        and final_action_at <= observed_at <= latest_allowed_observation
        for row in evidence_rows
    )


def latest_employer_verified_query(db):
    """Build one latest, typed employer-verified attempt per application."""
    dialect_name = db.bind.dialect.name
    if dialect_name == "postgresql":
        evidence_not_future = SubmissionEvidence.observed_at <= (
            Submission.submitted_at + text("INTERVAL '5 seconds'")
        )
    elif dialect_name == "sqlite":
        evidence_not_future = func.datetime(SubmissionEvidence.observed_at) <= func.datetime(
            Submission.submitted_at, "+5 seconds"
        )
    else:
        evidence_not_future = SubmissionEvidence.observed_at <= Submission.submitted_at

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
        .join(
            SubmissionEvidence,
            and_(
                SubmissionEvidence.attempt_id == Submission.id,
                SubmissionEvidence.evidence_type == Submission.verification_kind,
                SubmissionEvidence.evidence_digest == Submission.evidence_digest,
                SubmissionEvidence.form_fingerprint == Submission.form_plan_fingerprint,
                SubmissionEvidence.cv_hash == Submission.attached_cv_hash,
            ),
        )
        .filter(
            Submission.stage == "finished",
            Submission.outcome == "confirmed_submitted",
            Submission.status == SubmissionStatus.SUCCESS,
            Submission.submitted_at.isnot(None),
            Submission.final_action_at.isnot(None),
            Submission.submitted_at >= Submission.final_action_at,
            SubmissionEvidence.observed_at >= Submission.final_action_at,
            evidence_not_future,
            Submission.attachment_verified.is_(True),
            Submission.form_plan_id.isnot(None),
            Submission.adapter_name.isnot(None),
            func.length(func.trim(Submission.adapter_name)) > 0,
            Submission.adapter_version.isnot(None),
            func.length(func.trim(Submission.adapter_version)) > 0,
            Submission.selector_version.isnot(None),
            func.length(func.trim(Submission.selector_version)) > 0,
            Submission.profile_version.isnot(None),
            Submission.profile_version > 0,
            Submission.runner_release.isnot(None),
            func.length(func.trim(Submission.runner_release)) > 0,
            func.length(Submission.runner_release) <= 64,
            Submission.reason_code.in_(EMPLOYER_VERIFIED_REASON_CODES),
            Submission.verification_kind.in_(EMPLOYER_EVIDENCE_TYPES),
            Submission.evidence_digest.isnot(None),
            func.length(Submission.evidence_digest) == 64,
            Submission.form_plan_fingerprint.isnot(None),
            func.length(Submission.form_plan_fingerprint) == 64,
            Submission.requested_cv_id.isnot(None),
            func.length(func.trim(Submission.requested_cv_id)) > 0,
            Submission.requested_cv_id == Submission.attached_cv_id,
            Submission.requested_cv_hash.isnot(None),
            func.length(Submission.requested_cv_hash) == 64,
            Submission.attached_cv_hash.isnot(None),
            func.length(Submission.attached_cv_hash) == 64,
            Submission.requested_cv_hash == Submission.attached_cv_hash,
        )
        .distinct()
    )


def latest_employer_verified_count(db) -> int:
    return latest_employer_verified_query(db).count()
