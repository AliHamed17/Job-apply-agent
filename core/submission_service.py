"""Transactional admission service for durable final-submit commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from core.application_audit import record_application_event
from core.application_revision import preparation_is_current
from core.config import Settings, get_settings
from core.operational_metrics import record_attempt_stage, record_retry
from core.operations import readiness_report
from core.runtime_identity import build_runtime_capabilities
from core.submission_domain import FormPlanV1
from core.submit_permits import issue_final_submit_permit
from db.models import (
    Application,
    FormPlan,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionStatus,
    UserProfileVersion,
)
from ingestion.url_utils import normalize_url, url_hash
from llm.contracts import (
    FORM_RESOLUTION_PROMPT_VERSION,
    QUALIFIED_LOCAL_LLM_MODEL,
    QUALIFIED_LOCAL_LLM_PROVIDER,
    is_qualified_material_identity,
)
from llm.qualification_registry import is_qualified_local_model_identity
from submitters.platforms import AdapterDescriptor, adapter_for_url


class SubmissionAdmissionError(ValueError):
    """A bounded rejection safe to expose to an authenticated operator."""

    def __init__(self, reason_code: str, message: str | None = None):
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.message = message or reason_code.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class ClientReleaseIdentity:
    """Exact dashboard release that authorized the operator's final click."""

    build_sha: str
    ui_asset_digest: str
    source_digest: str
    protocol_version: str
    boot_id: str


@dataclass(frozen=True, slots=True)
class SubmissionCommandRequest:
    application_id: int
    client_idempotency_key: str
    application_revision: int
    form_plan_id: str
    client_release: ClientReleaseIdentity
    authority_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CreatedSubmissionCommand:
    application_id: int
    attempt_id: int
    command_id: int
    replayed: bool


DescriptorResolver = Callable[[str], AdapterDescriptor | None]
SessionChecker = Callable[[str, AdapterDescriptor, Settings], bool]


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError("duplicate JSON object key")
        decoded[key] = value
    return decoded


def _reject_non_finite_json_number(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _json_array(value: str | None) -> list[object]:
    if not isinstance(value, str):
        raise ValueError("persisted form-plan JSON must be a string")
    decoded = json.loads(
        value,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_non_finite_json_number,
    )
    if not isinstance(decoded, list):
        raise ValueError("persisted form-plan JSON must be an array")
    return decoded


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("persisted form-plan timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def reconstruct_persisted_form_plan(plan: FormPlan) -> FormPlanV1:
    """Validate every persisted field through the immutable domain contract."""

    try:
        reconstructed = FormPlanV1.model_validate(
            {
                "plan_id": plan.plan_id,
                "application_id": plan.application_id,
                "application_revision": plan.application_revision,
                "adapter_name": plan.adapter_name,
                "adapter_version": plan.adapter_version,
                "selector_version": plan.selector_version,
                "form_fingerprint": plan.fingerprint,
                "selected_cv_id": plan.selected_cv_id,
                "selected_cv_hash": plan.selected_cv_hash,
                "attached_cv_id": plan.attached_cv_id,
                "attached_cv_hash": plan.attached_cv_hash,
                "attachment_verified": plan.attachment_verified,
                "profile_version": plan.profile_version,
                "session_verified_at": _aware_utc(plan.session_verified_at),
                "created_at": _aware_utc(plan.created_at),
                "expires_at": _aware_utc(plan.expires_at),
                "fields": _json_array(plan.fields_json),
                "disclosures": _json_array(getattr(plan, "disclosures_json", "[]")),
                "decisions": _json_array(plan.decisions_json),
                "blockers": _json_array(plan.blockers_json),
                "locale": getattr(plan, "locale", "en") or "en",
                "answer_policy_version": (
                    getattr(plan, "answer_policy_version", "answer-policy-v1") or "answer-policy-v1"
                ),
                "llm_prompt_version": getattr(plan, "llm_prompt_version", None),
                "llm_model_provider": getattr(plan, "llm_model_provider", None),
                "llm_model_name": getattr(plan, "llm_model_name", None),
                "llm_model_digest": getattr(plan, "llm_model_digest", None),
            }
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise SubmissionAdmissionError("FORM_PLAN_BLOCKED") from exc
    # UUID parsing is intentionally not enough: persisted review references are
    # canonical identifiers, so alternative spellings cannot acquire authority.
    if str(reconstructed.plan_id) != plan.plan_id:
        raise SubmissionAdmissionError("FORM_PLAN_BLOCKED")
    return reconstructed


def _default_session_checker(
    url: str,
    descriptor: AdapterDescriptor,
    settings: Settings,
) -> bool:
    if descriptor.authentication_mode != "persistent_profile":
        return True
    from core.portal_sessions import PortalSessionError, portal_session_for_url

    try:
        return portal_session_for_url(
            url,
            settings.portal_browser_profile_root,
        ).ready
    except PortalSessionError:
        return False


def _runtime_capabilities(settings: Settings) -> Mapping[str, object]:
    return build_runtime_capabilities(
        settings,
        readiness_report(settings),
    )


def _runtime_release(capabilities: Mapping[str, object]) -> str:
    release = capabilities.get("release")
    if not isinstance(release, Mapping):
        return "unknown"
    values = (
        str(release.get("build_sha") or ""),
        str(release.get("source_digest") or ""),
        str(release.get("protocol_version") or ""),
    )
    digest = hashlib.sha256()
    for component in values:
        digest.update(component.encode("utf-8"))
        digest.update(b"\0")
    expected = digest.hexdigest()
    value = str(release.get("release_id") or "unknown")
    if value != expected:
        return "unknown"
    return value[:64]


def _require_runtime(capabilities: Mapping[str, object]) -> str:
    submission = capabilities.get("submission")
    if not isinstance(submission, Mapping) or submission.get("allowed") is not True:
        reasons = submission.get("reasons", []) if isinstance(submission, Mapping) else []
        reason = next(
            (str(item) for item in reasons if isinstance(item, str) and 1 <= len(item) <= 64),
            "RUNTIME_NOT_READY",
        )
        raise SubmissionAdmissionError(reason)
    release = _runtime_release(capabilities)
    if release in {"unknown", "unavailable"}:
        raise SubmissionAdmissionError("RUNTIME_NOT_READY")
    return release


def _require_matching_client_release(
    capabilities: Mapping[str, object],
    client: ClientReleaseIdentity,
) -> None:
    release = capabilities.get("release")
    if not isinstance(release, Mapping):
        raise SubmissionAdmissionError("RUNTIME_NOT_READY")
    expected = {
        "build_sha": str(release.get("build_sha") or ""),
        "ui_asset_digest": str(release.get("ui_asset_digest") or ""),
        "source_digest": str(release.get("source_digest") or ""),
        "protocol_version": str(release.get("protocol_version") or ""),
        "boot_id": str(release.get("boot_id") or ""),
    }
    supplied = {
        "build_sha": client.build_sha,
        "ui_asset_digest": client.ui_asset_digest,
        "source_digest": client.source_digest,
        "protocol_version": client.protocol_version,
        "boot_id": client.boot_id,
    }
    if (
        not supplied["protocol_version"]
        or supplied["protocol_version"] != expected["protocol_version"]
    ):
        raise SubmissionAdmissionError("PROTOCOL_MISMATCH")
    if any(
        not supplied[field] or supplied[field] != expected[field]
        for field in ("build_sha", "ui_asset_digest", "source_digest", "boot_id")
    ):
        raise SubmissionAdmissionError("BUILD_MISMATCH")


def _lock_application(db, application_id: int) -> Application | None:
    query = db.query(Application).filter(Application.id == application_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.first()


def _find_replay(
    db,
    request: SubmissionCommandRequest,
) -> CreatedSubmissionCommand | None:
    command = (
        db.query(SubmissionCommand)
        .filter(SubmissionCommand.idempotency_key == request.client_idempotency_key)
        .first()
    )
    if command is None:
        return None
    attempt = command.attempt
    if (
        attempt.application_id != request.application_id
        or attempt.application_revision != request.application_revision
        or attempt.form_plan is None
        or attempt.form_plan.plan_id != request.form_plan_id
    ):
        raise SubmissionAdmissionError("IDEMPOTENCY_KEY_CONFLICT")
    return CreatedSubmissionCommand(
        application_id=attempt.application_id,
        attempt_id=attempt.id,
        command_id=command.id,
        replayed=True,
    )


def _validate_plan(
    *,
    application: Application,
    plan: FormPlan | None,
    request: SubmissionCommandRequest,
    now: datetime,
) -> tuple[FormPlan, FormPlanV1]:
    if plan is None or plan.application_id != application.id:
        raise SubmissionAdmissionError("FORM_PLAN_NOT_FOUND")
    if plan.invalidated_at is not None:
        raise SubmissionAdmissionError("FORM_CHANGED")
    if plan.profile_version is None or plan.profile_version < 1:
        raise SubmissionAdmissionError("PROFILE_VERSION_CHANGED")
    if plan.session_verified_at is None:
        raise SubmissionAdmissionError("SESSION_EXPIRED")
    if not plan.attached_cv_id or not plan.attached_cv_hash or not plan.attachment_verified:
        raise SubmissionAdmissionError("ATTACHMENT_UNVERIFIED")
    domain_plan = reconstruct_persisted_form_plan(plan)
    from core.form_planning import ANSWER_POLICY_VERSION

    if domain_plan.answer_policy_version != ANSWER_POLICY_VERSION:
        raise SubmissionAdmissionError("ANSWER_POLICY_CHANGED")
    if application.material_eligible is not True:
        raise SubmissionAdmissionError("MATERIAL_NOT_ELIGIBLE")
    if not is_qualified_material_identity(
        provider=application.material_model_provider,
        model=application.material_model_name,
        local=True,
        digest=application.material_model_digest,
        prompt_version=application.material_prompt_version,
    ):
        raise SubmissionAdmissionError("MATERIAL_MODEL_NOT_QUALIFIED")
    if (
        not application.selected_cv_hash
        or application.selected_cv_hash != domain_plan.selected_cv_hash
    ):
        raise SubmissionAdmissionError("ATTACHMENT_CHANGED")
    admission_time = _aware_utc(now)
    if admission_time is None:
        raise SubmissionAdmissionError("FORM_PLAN_BLOCKED")
    authority_expires_at = _aware_utc(request.authority_expires_at)
    if authority_expires_at is not None and authority_expires_at <= admission_time:
        raise SubmissionAdmissionError("COMMAND_EXPIRED")
    if domain_plan.is_expired(admission_time):
        raise SubmissionAdmissionError("FORM_PLAN_EXPIRED")
    if domain_plan.created_at > admission_time:
        raise SubmissionAdmissionError("FORM_PLAN_BLOCKED")
    if domain_plan.session_verified_at > admission_time:
        raise SubmissionAdmissionError("SESSION_EXPIRED")
    if str(domain_plan.plan_id) != request.form_plan_id:
        raise SubmissionAdmissionError("FORM_PLAN_NOT_FOUND")
    if plan.application_revision != request.application_revision:
        raise SubmissionAdmissionError("APPLICATION_REVISION_CHANGED")
    if plan.application_revision != application.revision:
        raise SubmissionAdmissionError("APPLICATION_REVISION_CHANGED")
    if application.selected_cv_id != plan.selected_cv_id:
        raise SubmissionAdmissionError("CV_SELECTION_CHANGED")
    if application.profile_version != plan.profile_version:
        raise SubmissionAdmissionError("PROFILE_VERSION_CHANGED")
    if domain_plan.blockers:
        raise SubmissionAdmissionError("FORM_PLAN_BLOCKED")
    if not (domain_plan.created_at <= domain_plan.session_verified_at <= domain_plan.expires_at):
        raise SubmissionAdmissionError("SESSION_EXPIRED")
    if not domain_plan.ready_for_permit_at(admission_time):
        if not domain_plan.attachment_verified:
            raise SubmissionAdmissionError("ATTACHMENT_UNVERIFIED")
        if (
            domain_plan.attached_cv_id != domain_plan.selected_cv_id
            or domain_plan.attached_cv_hash != domain_plan.selected_cv_hash
        ):
            raise SubmissionAdmissionError("ATTACHMENT_UNVERIFIED")
        raise SubmissionAdmissionError("FORM_PLAN_BLOCKED")
    if not domain_plan.attachment_verified:
        raise SubmissionAdmissionError("ATTACHMENT_UNVERIFIED")
    if (
        not domain_plan.attached_cv_id
        or not domain_plan.attached_cv_hash
        or domain_plan.attached_cv_id != domain_plan.selected_cv_id
        or domain_plan.attached_cv_hash != domain_plan.selected_cv_hash
    ):
        raise SubmissionAdmissionError("ATTACHMENT_UNVERIFIED")
    return plan, domain_plan


def _require_model_binding(
    application: Application,
    domain_plan: FormPlanV1,
    capabilities: Mapping[str, object],
) -> None:
    """Bind every LLM-derived review artifact to the currently ready local model."""

    llm = capabilities.get("llm")
    if not isinstance(llm, Mapping):
        raise SubmissionAdmissionError("RUNTIME_NOT_READY")
    runtime_digest = str(llm.get("digest") or "")
    if llm.get("ready") is not True or not is_qualified_local_model_identity(
        provider=llm.get("provider"),
        model=llm.get("model"),
        local=llm.get("local"),
        digest=runtime_digest,
    ):
        raise SubmissionAdmissionError("RUNTIME_NOT_READY")

    material_identity = (
        application.material_model_provider,
        application.material_model_name,
        application.material_model_digest,
    )
    if material_identity != (
        QUALIFIED_LOCAL_LLM_PROVIDER,
        QUALIFIED_LOCAL_LLM_MODEL,
        runtime_digest,
    ):
        raise SubmissionAdmissionError("LLM_MODEL_CHANGED")

    uses_local_llm = any(
        decision.provenance.value == "local_llm" for decision in domain_plan.decisions
    )
    if uses_local_llm and (
        domain_plan.llm_prompt_version != FORM_RESOLUTION_PROMPT_VERSION
        or domain_plan.llm_model_provider != QUALIFIED_LOCAL_LLM_PROVIDER
        or domain_plan.llm_model_name != QUALIFIED_LOCAL_LLM_MODEL
        or domain_plan.llm_model_digest != runtime_digest
    ):
        raise SubmissionAdmissionError("LLM_MODEL_CHANGED")


def _validate_adapter(
    descriptor: AdapterDescriptor | None,
    plan: FormPlan,
) -> AdapterDescriptor:
    if descriptor is None:
        raise SubmissionAdmissionError("ADAPTER_NOT_QUALIFIED")
    if (
        descriptor.platform != plan.adapter_name
        or descriptor.adapter_version != plan.adapter_version
        or descriptor.selector_version != plan.selector_version
    ):
        raise SubmissionAdmissionError("ADAPTER_VERSION_CHANGED")
    if not descriptor.allows_final_execution:
        raise SubmissionAdmissionError("ADAPTER_NOT_QUALIFIED")
    if not descriptor.qualifies_form_fingerprint(plan.fingerprint):
        raise SubmissionAdmissionError("FORM_CHANGED")
    return descriptor


def _require_profile_snapshot(db, version: int) -> None:
    """Lock and validate the exact immutable profile identity used in review."""

    query = db.query(UserProfileVersion.id).filter(UserProfileVersion.version == version)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    if query.first() is None:
        raise SubmissionAdmissionError("PROFILE_VERSION_NOT_FOUND")

    from profile.versioned_snapshot import (
        ProfileSnapshotError,
        load_versioned_profile_snapshot,
    )

    try:
        load_versioned_profile_snapshot(db, version=version)
    except ProfileSnapshotError as exc:
        reason = (
            "PROFILE_VERSION_NOT_FOUND"
            if str(exc) == "PROFILE_VERSION_NOT_FOUND"
            else "PROFILE_SNAPSHOT_INVALID"
        )
        raise SubmissionAdmissionError(reason) from exc


def _create_one(
    db,
    *,
    request: SubmissionCommandRequest,
    settings: Settings,
    capabilities: Mapping[str, object],
    descriptor_resolver: DescriptorResolver,
    session_checker: SessionChecker,
    now: datetime,
) -> CreatedSubmissionCommand:
    _require_matching_client_release(capabilities, request.client_release)
    replay = _find_replay(db, request)
    if replay is not None:
        return replay

    application = _lock_application(db, request.application_id)
    if application is None:
        raise SubmissionAdmissionError("APPLICATION_NOT_FOUND")
    # A concurrent request with the same key may have committed while this
    # transaction waited for the application row lock.  Recheck inside the
    # serialized section so idempotent retries return the original command
    # instead of being misreported as a second active submission.
    replay = _find_replay(db, request)
    if replay is not None:
        return replay
    if request.application_revision != application.revision:
        raise SubmissionAdmissionError("APPLICATION_REVISION_CHANGED")
    if application.status != JobStatus.DRAFT:
        raise SubmissionAdmissionError("APPLICATION_NOT_ELIGIBLE")
    if not preparation_is_current(application):
        raise SubmissionAdmissionError("APPLICATION_REVIEW_REQUIRED")

    immutable_history_query = (
        db.query(Submission.id)
        .filter(
            Submission.application_id == application.id,
            Submission.outcome.in_(
                {
                    "confirmed_submitted",
                    "already_applied",
                    "unknown",
                    "operator_confirmed",
                    "legacy_unverified",
                }
            ),
        )
        .order_by(Submission.attempt_number.desc(), Submission.id.desc())
    )
    if db.bind.dialect.name == "postgresql":
        immutable_history_query = immutable_history_query.with_for_update()
    if immutable_history_query.first() is not None:
        raise SubmissionAdmissionError("APPLICATION_NOT_ELIGIBLE")

    latest_attempt = (
        db.query(Submission)
        .filter(Submission.application_id == application.id)
        .order_by(Submission.attempt_number.desc())
        .first()
    )
    if latest_attempt is not None and latest_attempt.stage == "finished":
        if latest_attempt.outcome in {"failed_before_commit", "draft_only"}:
            if application.approval_source != "retry_prepare":
                raise SubmissionAdmissionError("RETRY_PREPARATION_REQUIRED")
        else:
            raise SubmissionAdmissionError("APPLICATION_NOT_ELIGIBLE")

    active_attempt = (
        db.query(Submission.id)
        .filter(
            Submission.application_id == application.id,
            Submission.stage != "finished",
        )
        .first()
    )
    if active_attempt is not None:
        raise SubmissionAdmissionError("SUBMISSION_ALREADY_ACTIVE")

    plan_query = db.query(FormPlan).filter(FormPlan.plan_id == request.form_plan_id)
    if db.bind.dialect.name == "postgresql":
        plan_query = plan_query.with_for_update()
    plan = plan_query.first()
    plan, domain_plan = _validate_plan(
        application=application,
        plan=plan,
        request=request,
        now=now,
    )
    assert plan.profile_version is not None
    _require_profile_snapshot(db, plan.profile_version)
    job = application.job
    job_url = ((job.apply_url or job.source_url) if job else "") or ""
    descriptor = _validate_adapter(descriptor_resolver(job_url), plan)
    if not session_checker(job_url, descriptor, settings):
        raise SubmissionAdmissionError("SESSION_EXPIRED")
    runner_release = _require_runtime(capabilities)
    _require_model_binding(application, domain_plan, capabilities)

    next_attempt_number = (
        db.query(func.coalesce(func.max(Submission.attempt_number), 0))
        .filter(Submission.application_id == application.id)
        .scalar()
        + 1
    )
    attempt = Submission(
        application_id=application.id,
        attempt_number=next_attempt_number,
        idempotency_key=request.client_idempotency_key,
        submitter_name=descriptor.platform,
        status=SubmissionStatus.PENDING,
        stage="queued",
        outcome=None,
        application_revision=request.application_revision,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        form_plan_id=plan.id,
        form_plan_fingerprint=plan.fingerprint,
        selected_cv_id=plan.selected_cv_id,
        requested_cv_id=plan.selected_cv_id,
        requested_cv_hash=plan.selected_cv_hash,
        attached_cv_id=plan.attached_cv_id,
        attached_cv_hash=plan.attached_cv_hash,
        attachment_verified=plan.attachment_verified,
        profile_version=plan.profile_version,
        runner_release=runner_release,
    )
    db.add(attempt)
    db.flush()
    record_attempt_stage(
        db,
        attempt,
        stage="queued",
        occurred_at=now,
        transition_key="initial",
    )
    record_retry(db, attempt, occurred_at=now)
    try:
        normalized_url = normalize_url(job_url)
    except Exception as exc:
        raise SubmissionAdmissionError("JOB_URL_INVALID") from exc
    permit, _raw_nonce = issue_final_submit_permit(
        db,
        attempt=attempt,
        form_plan=plan,
        job_url_hash=url_hash(normalized_url),
        ttl_seconds=settings.submit_permit_ttl_seconds,
        now=now,
        not_after=request.authority_expires_at,
    )
    db.flush()
    command = SubmissionCommand(
        attempt_id=attempt.id,
        idempotency_key=request.client_idempotency_key,
        state="pending",
        available_at=now,
    )
    db.add(command)
    record_application_event(
        db,
        application.id,
        "submission_command_created",
        actor="operator",
        details={
            "attempt_number": attempt.attempt_number,
            "platform": descriptor.platform,
            "selected_cv_id": attempt.selected_cv_id,
            "profile_version": attempt.profile_version,
            "state": attempt.stage,
            "external_action_queued": True,
        },
    )
    db.flush()
    # Make the one-use relationship explicit before callers inspect the result.
    attempt.final_submit_permit = permit
    attempt.command = command
    return CreatedSubmissionCommand(
        application_id=application.id,
        attempt_id=attempt.id,
        command_id=command.id,
        replayed=False,
    )


def create_submission_commands(
    db,
    requests: Sequence[SubmissionCommandRequest],
    *,
    settings: Settings | None = None,
    capabilities: Mapping[str, object] | None = None,
    descriptor_resolver: DescriptorResolver = adapter_for_url,
    session_checker: SessionChecker = _default_session_checker,
    now: datetime | None = None,
) -> list[CreatedSubmissionCommand]:
    """Create attempts, permits, and outbox rows in one database transaction."""
    if not requests:
        raise SubmissionAdmissionError("EMPTY_SUBMISSION_BATCH")
    if len(requests) > 50:
        raise SubmissionAdmissionError("SUBMISSION_BATCH_TOO_LARGE")
    application_ids = [request.application_id for request in requests]
    if len(application_ids) != len(set(application_ids)):
        raise SubmissionAdmissionError("DUPLICATE_APPLICATION_IN_BATCH")
    idempotency_keys = [request.client_idempotency_key for request in requests]
    if len(idempotency_keys) != len(set(idempotency_keys)):
        raise SubmissionAdmissionError("DUPLICATE_IDEMPOTENCY_KEY_IN_BATCH")

    resolved_settings = settings or get_settings()
    if not resolved_settings.operator_auth_configured:
        raise SubmissionAdmissionError("OPERATOR_AUTH_REQUIRED")
    if db.bind.dialect.name != "postgresql" and resolved_settings.app_env != "test":
        raise SubmissionAdmissionError("DATABASE_SERIALIZATION_REQUIRED")
    resolved_capabilities = capabilities or _runtime_capabilities(resolved_settings)
    timestamp = now or datetime.now(UTC).replace(tzinfo=None)
    results: list[CreatedSubmissionCommand] = []
    try:
        for request in sorted(requests, key=lambda item: item.application_id):
            results.append(
                _create_one(
                    db,
                    request=request,
                    settings=resolved_settings,
                    capabilities=resolved_capabilities,
                    descriptor_resolver=descriptor_resolver,
                    session_checker=session_checker,
                    now=timestamp,
                )
            )
        wall_clock = datetime.now(UTC)
        if any(
            deadline is not None and deadline <= wall_clock
            for deadline in (_aware_utc(request.authority_expires_at) for request in requests)
        ):
            raise SubmissionAdmissionError("COMMAND_EXPIRED")
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if len(requests) == 1:
            replay = _find_replay(db, requests[0])
            if replay is not None:
                return [replay]
        raise SubmissionAdmissionError("SUBMISSION_ALREADY_ACTIVE") from exc
    except Exception:
        db.rollback()
        raise
    return results
