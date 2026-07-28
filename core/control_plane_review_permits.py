"""Local authority behind opaque, short-lived control-plane review grants.

The hosted control plane receives only the values returned by
``ReviewGrantProjection.to_wire``. Job URLs, form contents, CV identifiers and
hashes, profile data, selector versions, and local release bindings never cross
this module's public projection boundary. The canonical form-fingerprint
digest is the sole private-review digest intentionally projected.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from core.application_revision import preparation_is_current
from core.submission_service import reconstruct_persisted_form_plan
from db.models import (
    Application,
    ControlPlaneApplicationRef,
    ControlPlaneReviewGrant,
    FormPlan,
    JobStatus,
)
from ingestion.url_utils import normalize_url, url_hash

MAX_REVIEW_GRANT_TTL_SECONDS = 300
PROJECTION_CLAIM_TTL_SECONDS = 30


class ControlPlaneReviewGrantError(ValueError):
    """A stable fail-closed rejection with no private diagnostic content."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True, repr=False)
class ReviewGrantProjection:
    """The complete, intentionally redacted cloud representation of a grant."""

    remote_application_ref: str
    review_grant_ref: str
    application_revision: int
    adapter_name: str
    adapter_version: str
    selector_version: str
    form_fingerprint_digest: str
    runner_release: str
    issued_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "ReviewGrantProjection("
            f"remote_application_ref={self.remote_application_ref!r}, "
            f"review_grant_ref={self.review_grant_ref!r}, "
            f"adapter_name={self.adapter_name!r}, "
            f"adapter_version={self.adapter_version!r}, "
            f"selector_version={self.selector_version!r}, "
            f"runner_release={self.runner_release!r}, "
            f"expires_at={self.expires_at.isoformat()!r})"
        )

    def to_wire(self) -> dict[str, object]:
        """Return the only review-grant fields allowed outside the PC."""

        return {
            "application_ref": self.remote_application_ref,
            "grant_id": self.review_grant_ref,
            "application_revision": self.application_revision,
            "adapter": self.adapter_name,
            "adapter_version": self.adapter_version,
            "form_fingerprint_digest": self.form_fingerprint_digest,
            "reviewed_at": _aware(self.issued_at).isoformat(),
        }


def _naive(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp
    return timestamp.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _opaque_ref() -> str:
    return str(uuid4())


def _projection(grant: ControlPlaneReviewGrant) -> ReviewGrantProjection:
    return ReviewGrantProjection(
        remote_application_ref=grant.application_ref.remote_ref,
        review_grant_ref=grant.grant_ref,
        application_revision=grant.application_revision,
        adapter_name=grant.adapter_name,
        adapter_version=grant.adapter_version,
        selector_version=grant.selector_version,
        form_fingerprint_digest=grant.form_plan_fingerprint,
        runner_release=grant.runner_release,
        issued_at=grant.issued_at,
        expires_at=grant.expires_at,
    )


def _lock_application(db, application_id: int) -> Application | None:
    query = db.query(Application).filter(Application.id == application_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.one_or_none()


def _lock_grant(db, grant_ref: str) -> ControlPlaneReviewGrant | None:
    query = db.query(ControlPlaneReviewGrant).filter(ControlPlaneReviewGrant.grant_ref == grant_ref)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.one_or_none()


def _ensure_application_ref(
    db,
    application: Application,
) -> ControlPlaneApplicationRef:
    row = (
        db.query(ControlPlaneApplicationRef)
        .filter(ControlPlaneApplicationRef.application_id == application.id)
        .one_or_none()
    )
    if row is not None:
        return row
    row = ControlPlaneApplicationRef(
        application_id=application.id,
        remote_ref=_opaque_ref(),
    )
    db.add(row)
    db.flush()
    return row


def _current_plan(
    db,
    application: Application,
    form_plan_id: int,
    timestamp: datetime,
) -> FormPlan:
    query = db.query(FormPlan).filter(
        FormPlan.id == form_plan_id,
        FormPlan.application_id == application.id,
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    plan = query.one_or_none()
    if plan is None:
        raise ControlPlaneReviewGrantError("FORM_PLAN_NOT_FOUND")
    if plan.invalidated_at is not None:
        raise ControlPlaneReviewGrantError("FORM_CHANGED")
    try:
        domain_plan = reconstruct_persisted_form_plan(plan)
    except ValueError as exc:
        reason_code = getattr(exc, "reason_code", "FORM_PLAN_BLOCKED")
        raise ControlPlaneReviewGrantError(str(reason_code)) from exc
    if domain_plan.is_expired(_aware(timestamp)):
        raise ControlPlaneReviewGrantError("FORM_PLAN_EXPIRED")
    if not domain_plan.ready_for_permit_at(_aware(timestamp)):
        reason = "ATTACHMENT_UNVERIFIED" if not plan.attachment_verified else "FORM_PLAN_BLOCKED"
        raise ControlPlaneReviewGrantError(reason)
    return plan


def _job_hash(application: Application) -> str:
    job = application.job
    raw_url = ((job.apply_url or job.source_url) if job is not None else "") or ""
    try:
        return url_hash(normalize_url(raw_url))
    except (TypeError, ValueError) as exc:
        raise ControlPlaneReviewGrantError("JOB_URL_INVALID") from exc


def mint_control_plane_review_grant(
    db,
    *,
    application_id: int,
    form_plan_id: int,
    runner_release: str,
    ttl_seconds: int = MAX_REVIEW_GRANT_TTL_SECONDS,
    now: datetime | None = None,
) -> ReviewGrantProjection:
    """Mint private exact bindings and return their opaque cloud projection.

    The caller owns the transaction and must commit the returned grant together
    with the local operator-review action that authorized it.
    """

    timestamp = _naive(now)
    release = str(runner_release or "").strip()
    if not 1 <= ttl_seconds <= MAX_REVIEW_GRANT_TTL_SECONDS:
        raise ControlPlaneReviewGrantError("REVIEW_GRANT_TTL_INVALID")
    if not 1 <= len(release) <= 64:
        raise ControlPlaneReviewGrantError("RUNTIME_NOT_READY")

    application = _lock_application(db, application_id)
    if application is None:
        raise ControlPlaneReviewGrantError("APPLICATION_NOT_FOUND")
    if application.status != JobStatus.DRAFT or not preparation_is_current(application):
        raise ControlPlaneReviewGrantError("APPLICATION_REVIEW_REQUIRED")

    plan = _current_plan(db, application, form_plan_id, timestamp)
    if (
        plan.application_revision != application.revision
        or plan.selected_cv_id != application.selected_cv_id
        or plan.selected_cv_hash != application.selected_cv_hash
        or plan.profile_version != application.profile_version
        or not plan.attached_cv_hash
        or not plan.attachment_verified
    ):
        raise ControlPlaneReviewGrantError("FORM_CHANGED")
    if plan.attached_cv_id != plan.selected_cv_id or not hmac.compare_digest(
        plan.attached_cv_hash, plan.selected_cv_hash
    ):
        raise ControlPlaneReviewGrantError("ATTACHMENT_UNVERIFIED")

    application_ref = _ensure_application_ref(db, application)
    # A fresh local review supersedes every unconsumed projection. Expired
    # grants are revoked too so the database records the exact authority chain.
    for previous in (
        db.query(ControlPlaneReviewGrant)
        .filter(
            ControlPlaneReviewGrant.application_id == application.id,
            ControlPlaneReviewGrant.consumed_at.is_(None),
            ControlPlaneReviewGrant.revoked_at.is_(None),
        )
        .all()
    ):
        previous.revoked_at = timestamp

    expires_at = min(
        cast(datetime, plan.expires_at),
        timestamp + timedelta(seconds=ttl_seconds),
    )
    if expires_at <= timestamp:
        raise ControlPlaneReviewGrantError("FORM_PLAN_EXPIRED")
    grant = ControlPlaneReviewGrant(
        grant_ref=_opaque_ref(),
        application_id=application.id,
        application_ref_id=application_ref.id,
        form_plan_id=plan.id,
        application_revision=application.revision,
        job_url_hash=_job_hash(application),
        form_plan_fingerprint=plan.fingerprint,
        cv_hash=plan.attached_cv_hash,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        runner_release=release,
        issued_at=timestamp,
        expires_at=expires_at,
        projection_available_at=timestamp,
    )
    db.add(grant)
    db.flush()
    return _projection(grant)


def validate_control_plane_review_grant(
    db,
    *,
    review_grant_ref: str,
    remote_application_ref: str,
    runner_release: str,
    now: datetime | None = None,
) -> ControlPlaneReviewGrant:
    """Lock and revalidate every private binding before local admission."""

    timestamp = _naive(now)
    grant_identity = (
        db.query(
            ControlPlaneReviewGrant.id,
            ControlPlaneReviewGrant.application_id,
        )
        .filter(ControlPlaneReviewGrant.grant_ref == review_grant_ref)
        .one_or_none()
    )
    if grant_identity is None:
        raise ControlPlaneReviewGrantError("REVIEW_GRANT_NOT_FOUND")
    # All paths that need both rows lock Application before ReviewGrant. This
    # matches local re-authorization and prevents a PostgreSQL deadlock between
    # a new operator review and remote command admission.
    application = _lock_application(db, grant_identity.application_id)
    if application is None:
        raise ControlPlaneReviewGrantError("APPLICATION_NOT_FOUND")
    grant = _lock_grant(db, review_grant_ref)
    if grant is None or grant.application_id != application.id:
        raise ControlPlaneReviewGrantError("REVIEW_GRANT_NOT_FOUND")
    if grant.revoked_at is not None:
        raise ControlPlaneReviewGrantError("REVIEW_GRANT_REVOKED")
    if grant.consumed_at is not None:
        raise ControlPlaneReviewGrantError("REVIEW_GRANT_REPLAYED")
    if grant.expires_at <= timestamp:
        raise ControlPlaneReviewGrantError("REVIEW_GRANT_EXPIRED")
    if not hmac.compare_digest(grant.application_ref.remote_ref, remote_application_ref):
        raise ControlPlaneReviewGrantError("REMOTE_APPLICATION_CHANGED")
    if not hmac.compare_digest(grant.runner_release, str(runner_release or "")):
        raise ControlPlaneReviewGrantError("BUILD_MISMATCH")

    if application.status != JobStatus.DRAFT or not preparation_is_current(application):
        raise ControlPlaneReviewGrantError("APPLICATION_REVIEW_REQUIRED")
    plan = _current_plan(db, application, grant.form_plan_id, timestamp)
    exact_bindings = (
        (grant.application_revision, application.revision, "APPLICATION_REVISION_CHANGED"),
        (grant.application_revision, plan.application_revision, "APPLICATION_REVISION_CHANGED"),
        (grant.job_url_hash, _job_hash(application), "JOB_URL_CHANGED"),
        (grant.form_plan_fingerprint, plan.fingerprint, "FORM_CHANGED"),
        (grant.cv_hash, application.selected_cv_hash, "CV_SELECTION_CHANGED"),
        (grant.cv_hash, plan.selected_cv_hash, "CV_SELECTION_CHANGED"),
        (grant.cv_hash, plan.attached_cv_hash, "ATTACHMENT_CHANGED"),
        (grant.adapter_name, plan.adapter_name, "ADAPTER_VERSION_CHANGED"),
        (grant.adapter_version, plan.adapter_version, "ADAPTER_VERSION_CHANGED"),
        (grant.selector_version, plan.selector_version, "SELECTOR_DRIFT"),
    )
    for expected, observed, reason_code in exact_bindings:
        if not hmac.compare_digest(str(expected or ""), str(observed or "")):
            raise ControlPlaneReviewGrantError(reason_code)
    if not plan.attachment_verified or plan.attached_cv_id != plan.selected_cv_id:
        raise ControlPlaneReviewGrantError("ATTACHMENT_UNVERIFIED")
    return grant


def consume_control_plane_review_grant(
    grant: ControlPlaneReviewGrant,
    *,
    remote_command_ref: str,
    now: datetime | None = None,
) -> None:
    """Spend a validated grant inside the submission-admission transaction."""

    if grant.consumed_at is not None:
        raise ControlPlaneReviewGrantError("REVIEW_GRANT_REPLAYED")
    command_ref = str(remote_command_ref or "").strip()
    if not 16 <= len(command_ref) <= 64:
        raise ControlPlaneReviewGrantError("REMOTE_COMMAND_INVALID")
    grant.consumed_at = _naive(now)
    grant.consumed_command_ref = command_ref


def claim_review_grant_projection(
    db,
    *,
    runner_id: str,
    now: datetime | None = None,
) -> tuple[int, str] | None:
    """Claim one due redacted grant projection without holding a network lock."""

    timestamp = _naive(now)
    bounded_runner_id = str(runner_id or "").strip()
    if not 1 <= len(bounded_runner_id) <= 64:
        raise ControlPlaneReviewGrantError("RUNNER_ID_INVALID")
    stale_cutoff = timestamp - timedelta(seconds=PROJECTION_CLAIM_TTL_SECONDS)
    stale_query = db.query(ControlPlaneReviewGrant).filter(
        ControlPlaneReviewGrant.projection_state == "claimed",
        ControlPlaneReviewGrant.projection_claimed_at < stale_cutoff,
        ControlPlaneReviewGrant.projected_at.is_(None),
    )
    if db.bind.dialect.name == "postgresql":
        stale_query = stale_query.with_for_update(skip_locked=True)
    for stale in stale_query.limit(25).all():
        stale.projection_state = "pending"
        stale.projection_claimed_at = None
        stale.projection_claimed_by = None
        stale.projection_claim_token = None
        stale.projection_available_at = timestamp
        stale.last_projection_error_code = "PROJECTION_CLAIM_STALE"

    query = (
        db.query(ControlPlaneReviewGrant)
        .filter(
            ControlPlaneReviewGrant.projection_state == "pending",
            ControlPlaneReviewGrant.projection_available_at <= timestamp,
            ControlPlaneReviewGrant.expires_at > timestamp,
            ControlPlaneReviewGrant.revoked_at.is_(None),
            ControlPlaneReviewGrant.consumed_at.is_(None),
        )
        .order_by(
            ControlPlaneReviewGrant.projection_available_at,
            ControlPlaneReviewGrant.id,
        )
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    grant = query.first()
    if grant is None:
        db.commit()
        return None
    claim_token = secrets.token_hex(32)
    grant.projection_state = "claimed"
    grant.projection_claimed_at = timestamp
    grant.projection_claimed_by = bounded_runner_id
    grant.projection_claim_token = claim_token
    grant.projection_attempts += 1
    db.commit()
    return grant.id, claim_token


def load_claimed_review_grant_projection(
    db,
    *,
    grant_id: int,
    claim_token: str,
    runner_release: str,
    now: datetime | None = None,
) -> ReviewGrantProjection:
    """Revalidate a claimed grant before signing its cloud projection."""

    grant = (
        db.query(ControlPlaneReviewGrant)
        .filter(
            ControlPlaneReviewGrant.id == grant_id,
            ControlPlaneReviewGrant.projection_state == "claimed",
            ControlPlaneReviewGrant.projection_claim_token == claim_token,
        )
        .one_or_none()
    )
    if grant is None:
        raise ControlPlaneReviewGrantError("PROJECTION_CLAIM_LOST")
    validated = validate_control_plane_review_grant(
        db,
        review_grant_ref=grant.grant_ref,
        remote_application_ref=grant.application_ref.remote_ref,
        runner_release=runner_release,
        now=now,
    )
    if (
        validated.id != grant_id
        or validated.projection_state != "claimed"
        or not hmac.compare_digest(
            str(validated.projection_claim_token or ""),
            str(claim_token),
        )
    ):
        raise ControlPlaneReviewGrantError("PROJECTION_CLAIM_LOST")
    return _projection(validated)


def mark_review_grant_projected(
    db,
    *,
    grant_id: int,
    claim_token: str,
    now: datetime | None = None,
) -> None:
    query = db.query(ControlPlaneReviewGrant).filter(
        ControlPlaneReviewGrant.id == grant_id,
        ControlPlaneReviewGrant.projection_state == "claimed",
        ControlPlaneReviewGrant.projection_claim_token == claim_token,
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    grant = query.one_or_none()
    if grant is None:
        raise ControlPlaneReviewGrantError("PROJECTION_CLAIM_LOST")
    grant.projection_state = "projected"
    grant.projected_at = _naive(now)
    grant.projection_claimed_at = None
    grant.projection_claimed_by = None
    grant.projection_claim_token = None
    grant.last_projection_error_code = None
    db.commit()


def release_review_grant_projection(
    db,
    *,
    grant_id: int,
    claim_token: str,
    reason_code: str = "CONTROL_PLANE_DELIVERY_FAILED",
    now: datetime | None = None,
) -> None:
    query = db.query(ControlPlaneReviewGrant).filter(
        ControlPlaneReviewGrant.id == grant_id,
        ControlPlaneReviewGrant.projection_state == "claimed",
        ControlPlaneReviewGrant.projection_claim_token == claim_token,
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    grant = query.one_or_none()
    if grant is None:
        raise ControlPlaneReviewGrantError("PROJECTION_CLAIM_LOST")
    bounded_reason = str(reason_code or "")[:64]
    if not bounded_reason or not all(
        character == "_" or character.isupper() or character.isdigit()
        for character in bounded_reason
    ):
        bounded_reason = "CONTROL_PLANE_DELIVERY_FAILED"
    grant.projection_state = "pending"
    grant.projection_available_at = _naive(now) + timedelta(seconds=10)
    grant.projection_claimed_at = None
    grant.projection_claimed_by = None
    grant.projection_claim_token = None
    grant.last_projection_error_code = bounded_reason
    db.commit()
