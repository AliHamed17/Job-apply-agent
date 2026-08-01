"""Issue, validate, and consume one-use final-submit permits."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from db.models import FinalSubmitPermit, FormPlan, Submission


class PermitValidationError(ValueError):
    """A stable, fail-closed permit rejection."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def hash_permit_nonce(nonce: str) -> str:
    """Hash the bearer nonce so the database never contains reusable authority."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def issue_final_submit_permit(
    db,
    *,
    attempt: Submission,
    form_plan: FormPlan,
    job_url_hash: str,
    ttl_seconds: int,
    now: datetime | None = None,
    not_after: datetime | None = None,
) -> tuple[FinalSubmitPermit, str]:
    """Create one permit bound to the exact reviewed application state."""
    timestamp = _naive_utc(now or datetime.now(UTC))
    if not form_plan.attachment_verified or not form_plan.attached_cv_hash:
        raise PermitValidationError("ATTACHMENT_UNVERIFIED")
    expires_at = min(
        cast(datetime, form_plan.expires_at),
        timestamp + timedelta(seconds=max(1, ttl_seconds)),
    )
    if not_after is not None:
        expires_at = min(expires_at, _naive_utc(not_after))
    if expires_at <= timestamp:
        raise PermitValidationError("SUBMIT_PERMIT_EXPIRED")
    nonce = secrets.token_urlsafe(32)
    permit = FinalSubmitPermit(
        attempt_id=attempt.id,
        nonce_hash=hash_permit_nonce(nonce),
        job_url_hash=job_url_hash,
        application_revision=attempt.application_revision,
        adapter_name=attempt.adapter_name or attempt.submitter_name,
        adapter_version=attempt.adapter_version or "",
        selector_version=attempt.selector_version or "",
        form_plan_fingerprint=attempt.form_plan_fingerprint or "",
        cv_hash=attempt.attached_cv_hash or "",
        authority_kind=attempt.authority_kind,
        automation_policy_decision_digest=attempt.automation_policy_decision_digest,
        qualification_canary_authorization_digest=(
            attempt.qualification_canary_authorization_digest
        ),
        issued_at=timestamp,
        expires_at=expires_at,
    )
    db.add(permit)
    return permit, nonce


def validate_final_submit_permit(
    permit: FinalSubmitPermit | None,
    *,
    attempt: Submission,
    form_plan: FormPlan,
    job_url_hash: str,
    now: datetime | None = None,
) -> None:
    """Reject expiry, replay, or any binding drift before external work."""
    timestamp = now or datetime.now(UTC).replace(tzinfo=None)
    if permit is None or permit.attempt_id != attempt.id:
        raise PermitValidationError("SUBMIT_PERMIT_REQUIRED")
    if permit.consumed_at is not None:
        raise PermitValidationError("SUBMIT_PERMIT_REPLAYED")
    if permit.expires_at <= timestamp:
        raise PermitValidationError("SUBMIT_PERMIT_EXPIRED")
    bindings = (
        (permit.job_url_hash, job_url_hash, "JOB_URL_CHANGED"),
        (
            permit.application_revision,
            attempt.application_revision,
            "APPLICATION_REVISION_CHANGED",
        ),
        (
            permit.application_revision,
            form_plan.application_revision,
            "APPLICATION_REVISION_CHANGED",
        ),
        (permit.adapter_name, attempt.adapter_name, "ADAPTER_VERSION_CHANGED"),
        (permit.adapter_name, form_plan.adapter_name, "ADAPTER_VERSION_CHANGED"),
        (permit.adapter_version, attempt.adapter_version, "ADAPTER_VERSION_CHANGED"),
        (permit.adapter_version, form_plan.adapter_version, "ADAPTER_VERSION_CHANGED"),
        (permit.selector_version, attempt.selector_version, "SELECTOR_DRIFT"),
        (permit.selector_version, form_plan.selector_version, "SELECTOR_DRIFT"),
        (
            permit.form_plan_fingerprint,
            attempt.form_plan_fingerprint,
            "FORM_CHANGED",
        ),
        (permit.form_plan_fingerprint, form_plan.fingerprint, "FORM_CHANGED"),
        (permit.cv_hash, attempt.attached_cv_hash, "ATTACHMENT_CHANGED"),
        (permit.cv_hash, form_plan.attached_cv_hash, "ATTACHMENT_CHANGED"),
        (permit.authority_kind, attempt.authority_kind, "SUBMISSION_AUTHORITY_CHANGED"),
        (
            permit.automation_policy_decision_digest,
            attempt.automation_policy_decision_digest,
            "AUTOMATION_POLICY_CHANGED",
        ),
        (
            permit.qualification_canary_authorization_digest,
            attempt.qualification_canary_authorization_digest,
            "CANARY_AUTHORIZATION_CHANGED",
        ),
    )
    for expected, observed, reason_code in bindings:
        if not hmac.compare_digest(str(expected or ""), str(observed or "")):
            raise PermitValidationError(reason_code)
    if not attempt.attachment_verified or not form_plan.attachment_verified:
        raise PermitValidationError("ATTACHMENT_UNVERIFIED")


def consume_final_submit_permit(
    permit: FinalSubmitPermit,
    *,
    now: datetime | None = None,
) -> None:
    """Mark the permit spent in the transaction that enters `committing`."""
    if permit.consumed_at is not None:
        raise PermitValidationError("SUBMIT_PERMIT_REPLAYED")
    permit.consumed_at = now or datetime.now(UTC).replace(tzinfo=None)
