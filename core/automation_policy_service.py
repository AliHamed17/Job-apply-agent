"""Persistence, evaluation, and revocation for signed qualified autopilot."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from profile.cv_content_cache import load_configured_cv_artifacts
from profile.cv_routing import load_routing_config, parse_required_skills
from profile.versioned_snapshot import latest_profile_version
from typing import Any, TypedDict
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.application_revision import mark_application_prepared
from core.automation_artifact_snapshot import (
    AutomationArtifactSnapshotError,
    materialize_policy_artifact_snapshot,
    require_policy_artifact_snapshot,
)
from core.automation_authority_fence import (
    AUTOMATION_AUTHORITY_FENCE_ID,
    lock_automation_authority_fence,
)
from core.automation_policy import (
    AutomationGeography,
    AutoSubmitDecisionV1,
    AutoSubmitPolicyV1,
    PolicyAuthoritySource,
    QualifiedFormContractV1,
    SignedAutoSubmitPolicyV1,
    canonical_model_bytes,
    sign_auto_submit_policy,
    verify_auto_submit_policy,
)
from core.automation_policy_keys import (
    AutomationPolicyKeyError,
    load_automation_policy_signing_identity,
)
from core.config import get_settings
from db.models import (
    Application,
    ApplicationPolicyDecision,
    AutomationKillSwitchEvent,
    AutomationPolicyRevisionRecord,
    BrowserQualificationRun,
    FormPlan,
    JobFitDecisionRecord,
    JobStatus,
    OperatorApprovedAnswer,
)
from jobs.models import JobData
from match.job_fit import (
    cv_manifest_digest,
    job_content_digest,
    load_fit_qualification,
    qualification_matches,
    routing_config_digest,
)
from match.job_fit_runtime import configured_fit_qualification_path
from match.job_fit_store import decision_from_record
from submitters.platforms import adapter_for_platform, adapter_for_url

_ZERO_DIGEST = "0" * 64
_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_JERUSALEM = ZoneInfo("Asia/Jerusalem")
_AUTOMATION_AUTHORITY_FENCE_ID = AUTOMATION_AUTHORITY_FENCE_ID
RETRYABLE_AUTOMATION_DENIALS = frozenset(
    {
        "KILL_SWITCH_ACTIVE",
        "OUTSIDE_ACTIVE_HOURS",
        "AUTOMATION_DAILY_LIMIT_REACHED",
        "AUTOMATION_HOURLY_LIMIT_REACHED",
        "AUTOMATION_COMPANY_LIMIT_REACHED",
    }
)


class AutomationPolicyError(ValueError):
    def __init__(self, reason_code: str):
        bounded = (
            reason_code
            if _REASON_RE.fullmatch(reason_code or "") is not None
            else "AUTOMATION_POLICY_INVALID"
        )
        super().__init__(bounded)
        self.reason_code = bounded


class PrivatePolicyBindings(TypedDict):
    profile_version: int
    role_families: tuple[str, ...]
    routing_config_digest: str
    cv_manifest_digest: str
    fit_qualification_digest: str
    confirmed_answer_revision: str


class ArtifactPolicyBindings(TypedDict):
    role_families: tuple[str, ...]
    routing_config_digest: str
    cv_manifest_digest: str
    fit_qualification_digest: str


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _aware(value).replace(tzinfo=None)


def _compact(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_compact(value).encode("utf-8")).hexdigest()


def company_identity_digest(company: str | None) -> str:
    normalized = " ".join((company or "").casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def confirmed_answer_revision(db: Session, *, profile_version: int) -> str:
    """Bind policy authority to active reusable answers without exposing values."""

    rows = (
        db.query(OperatorApprovedAnswer)
        .filter(
            OperatorApprovedAnswer.profile_version == profile_version,
            OperatorApprovedAnswer.revoked_at.is_(None),
        )
        .order_by(OperatorApprovedAnswer.id.asc())
        .all()
    )
    manifest = [
        {
            "id": row.id,
            "canonical_field": row.canonical_field,
            "field_type": row.field_type,
            "option_set_hash": row.option_set_hash,
            "selected_cv_hash": row.selected_cv_hash,
            "adapter": row.adapter_name,
            "adapter_version": row.adapter_version,
            "selector_version": row.selector_version,
            "field_contract_fingerprint": row.field_contract_fingerprint,
            "policy_version": row.policy_version,
            "answer_digest": hashlib.sha256(row.answer_json.encode("utf-8")).hexdigest(),
            "approved_at": _aware(row.approved_at).isoformat(),
        }
        for row in rows
    ]
    return _digest({"profile_version": profile_version, "answers": manifest})


def form_contract_digest(plan: FormPlan) -> str:
    """Hash only form shape; omit labels, answers, options, URLs, and CV data."""

    try:
        fields = json.loads(plan.fields_json)
        disclosures = json.loads(plan.disclosures_json)
    except (TypeError, ValueError) as exc:
        raise AutomationPolicyError("FORM_PLAN_BLOCKED") from exc
    if not isinstance(fields, list) or not isinstance(disclosures, list):
        raise AutomationPolicyError("FORM_PLAN_BLOCKED")
    safe_fields: list[dict[str, object]] = []
    for position, item in enumerate(fields):
        if not isinstance(item, dict):
            raise AutomationPolicyError("FORM_PLAN_BLOCKED")
        options = item.get("options", [])
        constraints = item.get("constraints", {})
        if not isinstance(options, list) or not isinstance(constraints, dict):
            raise AutomationPolicyError("FORM_PLAN_BLOCKED")
        safe_fields.append(
            {
                "position": position,
                "field_type": str(item.get("field_type") or "unknown")[:32],
                "required": item.get("required") is True,
                "option_count": len(options),
                "sensitive_category": str(item.get("sensitive_category") or "none")[:32],
                "constraints": {
                    key: constraints.get(key)
                    for key in (
                        "min_length",
                        "max_length",
                        "min_value",
                        "max_value",
                        "multiple",
                        "max_file_bytes",
                    )
                    if key in constraints
                },
            }
        )
    safe_disclosures: list[dict[str, object]] = []
    for position, item in enumerate(disclosures):
        if not isinstance(item, dict):
            raise AutomationPolicyError("FORM_PLAN_BLOCKED")
        safe_disclosures.append(
            {
                "position": position,
                "kind": str(item.get("kind") or "unknown")[:32],
                "source": str(item.get("source") or "unknown")[:32],
                "interactive": bool(item.get("acknowledgement_field_id")),
            }
        )
    return _digest(
        {
            "schema": "semantic-form-contract.v1",
            "fields": safe_fields,
            "disclosures": safe_disclosures,
        }
    )


def _policy_from_record(record: AutomationPolicyRevisionRecord) -> SignedAutoSubmitPolicyV1:
    try:
        policy_payload = json.loads(record.payload_json)
        signed = SignedAutoSubmitPolicyV1.model_validate(
            {
                "key_id": record.signing_key_id,
                "policy": policy_payload,
                "signature": record.signature,
            }
        )
    except (TypeError, ValueError) as exc:
        raise AutomationPolicyError("AUTOMATION_POLICY_INVALID") from exc
    if (
        signed.policy.payload_digest != record.payload_digest
        or str(signed.policy.policy_id) != record.policy_id
        or signed.policy.revision != record.revision
        or _naive(signed.policy.activated_at) != record.activated_at
        or _naive(signed.policy.expires_at) != record.expires_at
    ):
        raise AutomationPolicyError("AUTOMATION_POLICY_BINDING_MISMATCH")
    return signed


def _verified_policy(
    record: AutomationPolicyRevisionRecord,
    *,
    signing_key_path: str | Path | None = None,
) -> SignedAutoSubmitPolicyV1:
    try:
        identity = load_automation_policy_signing_identity(signing_key_path)
    except AutomationPolicyKeyError as exc:
        raise AutomationPolicyError(exc.reason_code) from exc
    signed = _policy_from_record(record)
    if str(identity.key_id) != str(signed.key_id):
        raise AutomationPolicyError("AUTOMATION_POLICY_SIGNING_KEY_CHANGED")
    try:
        verify_auto_submit_policy(signed, public_key=identity.public_key)
    except ValueError as exc:
        raise AutomationPolicyError("AUTOMATION_POLICY_SIGNATURE_INVALID") from exc
    return signed


def _active_policy_record(
    db: Session,
    *,
    lock: bool = False,
) -> AutomationPolicyRevisionRecord | None:
    query = db.query(AutomationPolicyRevisionRecord).filter(
        AutomationPolicyRevisionRecord.active_slot == 1
    )
    if lock and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.one_or_none()


def current_signed_policy(
    db: Session,
    *,
    now: datetime | None = None,
    signing_key_path: str | Path | None = None,
    lock: bool = False,
) -> tuple[AutomationPolicyRevisionRecord, SignedAutoSubmitPolicyV1] | None:
    timestamp = _aware(now or datetime.now(UTC))
    record = _active_policy_record(db, lock=lock)
    if record is None or record.revoked_at is not None:
        return None
    signed = _verified_policy(record, signing_key_path=signing_key_path)
    if signed.policy.expires_at <= timestamp:
        return None
    return record, signed


def _current_artifact_bindings(settings=None) -> ArtifactPolicyBindings:
    resolved_settings = settings or get_settings()
    routing_path = Path(resolved_settings.cv_routing_path)
    if not routing_path.is_file():
        raise AutomationPolicyError("CV_ROUTING_CONFIG_MISSING")
    try:
        config = load_routing_config(routing_path)
        artifacts = load_configured_cv_artifacts(config, resolved_settings.cv_directory)
        config_digest = routing_config_digest(config)
        manifest_digest = cv_manifest_digest(artifacts)
    except Exception as exc:
        raise AutomationPolicyError("CV_ROUTING_CONFIG_INVALID") from exc
    qualification_path = Path(configured_fit_qualification_path())
    try:
        qualification = load_fit_qualification(qualification_path)
    except Exception as exc:
        raise AutomationPolicyError("FIT_QUALIFICATION_MISSING_OR_MISMATCHED") from exc
    if not qualification_matches(
        qualification,
        config_digest=config_digest,
        manifest_digest=manifest_digest,
    ):
        raise AutomationPolicyError("FIT_QUALIFICATION_MISSING_OR_MISMATCHED")
    if not qualification.qualified or qualification.holdout_precision < 0.95:
        raise AutomationPolicyError("FIT_QUALIFICATION_NOT_PASSED")
    return {
        "role_families": tuple(item.id for item in config.cvs),
        "routing_config_digest": config_digest,
        "cv_manifest_digest": manifest_digest,
        "fit_qualification_digest": qualification.qualification_digest,
    }


def _private_bindings(db: Session, settings) -> PrivatePolicyBindings:
    profile_version = latest_profile_version(db)
    if profile_version is None:
        raise AutomationPolicyError("PROFILE_VERSION_MISSING")
    artifact_bindings = _current_artifact_bindings(settings)
    return {
        "profile_version": profile_version,
        **artifact_bindings,
        "confirmed_answer_revision": confirmed_answer_revision(
            db,
            profile_version=profile_version,
        ),
    }


def _require_current_artifact_bindings(policy: AutoSubmitPolicyV1) -> None:
    try:
        current = _current_artifact_bindings()
    except Exception as exc:
        raise AutomationPolicyError("FIT_QUALIFICATION_CHANGED") from exc
    bindings = (
        (current["routing_config_digest"], policy.routing_config_digest),
        (current["cv_manifest_digest"], policy.cv_manifest_digest),
        (current["fit_qualification_digest"], policy.fit_qualification_digest),
    )
    if any(not hmac.compare_digest(observed, expected) for observed, expected in bindings):
        raise AutomationPolicyError("FIT_QUALIFICATION_CHANGED")
    try:
        require_policy_artifact_snapshot(policy)
    except AutomationArtifactSnapshotError as exc:
        raise AutomationPolicyError("FIT_QUALIFICATION_CHANGED") from exc


def verified_policy_for_decision(
    decision_record: ApplicationPolicyDecision,
    *,
    signing_key_path: str | Path | None = None,
) -> AutoSubmitPolicyV1:
    """Verify and return the exact signed policy bound to one decision."""

    record = decision_record.policy_revision
    if record is None:
        raise AutomationPolicyError("AUTOMATION_DECISION_BINDING_MISMATCH")
    signed = _verified_policy(record, signing_key_path=signing_key_path)
    if not hmac.compare_digest(
        signed.policy.payload_digest,
        str(decision_record.policy_digest),
    ):
        raise AutomationPolicyError("AUTOMATION_DECISION_BINDING_MISMATCH")
    return signed.policy


def _scope_has_live_canary(db: Session, scope: QualifiedFormContractV1) -> bool:
    return (
        db.query(BrowserQualificationRun.id)
        .filter(
            BrowserQualificationRun.adapter_name == scope.adapter_name,
            BrowserQualificationRun.adapter_version == scope.adapter_version,
            BrowserQualificationRun.selector_version == scope.selector_version,
            BrowserQualificationRun.qualification_tier == "live_canary_qualified",
            BrowserQualificationRun.qualified.is_(True),
            BrowserQualificationRun.form_contract_digest == scope.form_contract_digest,
        )
        .first()
        is not None
    )


def activate_auto_submit_policy(
    db: Session,
    *,
    settings,
    role_families: Sequence[str],
    geographies: Sequence[AutomationGeography],
    permitted_adapters: Sequence[str],
    qualified_form_contracts: Sequence[QualifiedFormContractV1] = (),
    minimum_fit_score: float = 85.0,
    daily_limit: int = 25,
    hourly_limit: int = 5,
    company_limit: int = 2,
    expires_in_days: int = 30,
    now: datetime | None = None,
    signing_key_path: str | Path | None = None,
) -> AutomationPolicyRevisionRecord:
    """Authenticate elsewhere, then mint and persist one signed local revision."""

    timestamp = _aware(now or datetime.now(UTC))
    if not 1 <= expires_in_days <= 30:
        raise AutomationPolicyError("AUTOMATION_POLICY_EXPIRY_INVALID")
    try:
        identity = load_automation_policy_signing_identity(signing_key_path)
    except AutomationPolicyKeyError as exc:
        raise AutomationPolicyError(exc.reason_code) from exc
    lock_automation_authority_fence(db)
    bindings = _private_bindings(db, settings)
    configured_roles = set(bindings["role_families"])
    requested_roles = tuple(dict.fromkeys(role_families))
    if not requested_roles or not set(requested_roles).issubset(configured_roles):
        raise AutomationPolicyError("AUTOMATION_POLICY_ROLE_SCOPE_INVALID")
    requested_adapters = tuple(dict.fromkeys(permitted_adapters))
    descriptors = tuple(adapter_for_platform(item) for item in requested_adapters)
    if not requested_adapters or any(descriptor is None for descriptor in descriptors):
        raise AutomationPolicyError("AUTOMATION_POLICY_ADAPTER_SCOPE_INVALID")
    if any(not descriptor.allows_final_execution for descriptor in descriptors if descriptor):
        raise AutomationPolicyError("AUTOMATION_POLICY_ADAPTER_NOT_QUALIFIED")
    scopes = tuple(qualified_form_contracts)
    if not scopes:
        raise AutomationPolicyError("AUTOMATION_POLICY_FORM_SCOPE_REQUIRED")
    if any(scope.adapter_name not in requested_adapters for scope in scopes):
        raise AutomationPolicyError("AUTOMATION_POLICY_FORM_SCOPE_INVALID")
    if any(not _scope_has_live_canary(db, scope) for scope in scopes):
        raise AutomationPolicyError("AUTOMATION_POLICY_FORM_SCOPE_NOT_QUALIFIED")

    current = _active_policy_record(db, lock=True)
    next_revision = (
        int(
            db.query(func.coalesce(func.max(AutomationPolicyRevisionRecord.revision), 0)).scalar()
            or 0
        )
        + 1
    )
    if current is not None:
        current.active_slot = None
        current.revoked_at = _naive(timestamp)
        current.revoked_by = "local_operator"
        current.revocation_reason = "AUTOMATION_POLICY_SUPERSEDED"

    try:
        policy = AutoSubmitPolicyV1(
            policy_id=uuid4(),
            revision=next_revision,
            role_families=requested_roles,
            geographies=tuple(dict.fromkeys(geographies)),
            minimum_fit_score=minimum_fit_score,
            daily_limit=daily_limit,
            hourly_limit=hourly_limit,
            company_limit=company_limit,
            permitted_adapters=requested_adapters,
            qualified_form_contracts=scopes,
            profile_version=int(bindings["profile_version"]),
            routing_config_digest=str(bindings["routing_config_digest"]),
            cv_manifest_digest=str(bindings["cv_manifest_digest"]),
            fit_qualification_digest=str(bindings["fit_qualification_digest"]),
            confirmed_answer_revision=str(bindings["confirmed_answer_revision"]),
            activated_at=timestamp,
            expires_at=timestamp + timedelta(days=expires_in_days),
        )
    except ValueError as exc:
        raise AutomationPolicyError("AUTOMATION_POLICY_INVALID") from exc
    try:
        materialize_policy_artifact_snapshot(policy, settings=settings)
    except AutomationArtifactSnapshotError as exc:
        raise AutomationPolicyError("FIT_QUALIFICATION_CHANGED") from exc
    signed = sign_auto_submit_policy(
        policy,
        key_id=identity.key_id,
        private_key=identity.private_key,
    )
    record = AutomationPolicyRevisionRecord(
        policy_id=str(policy.policy_id),
        revision=policy.revision,
        schema_version=policy.schema_version,
        payload_json=canonical_model_bytes(policy).decode("utf-8"),
        payload_digest=policy.payload_digest,
        signing_key_id=str(identity.key_id),
        signature=signed.signature,
        active_slot=1,
        activated_at=_naive(policy.activated_at),
        expires_at=_naive(policy.expires_at),
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AutomationPolicyError("AUTOMATION_POLICY_CONFLICT") from exc
    return record


def revoke_auto_submit_policy(
    db: Session,
    *,
    reason_code: str = "AUTOMATION_POLICY_REVOKED",
    now: datetime | None = None,
) -> AutomationPolicyRevisionRecord:
    if _REASON_RE.fullmatch(reason_code) is None:
        raise AutomationPolicyError("AUTOMATION_POLICY_REVOCATION_INVALID")
    lock_automation_authority_fence(db)
    record = _active_policy_record(db, lock=True)
    if record is None:
        raise AutomationPolicyError("AUTOMATION_POLICY_NOT_ACTIVE")
    timestamp = _naive(now or datetime.now(UTC))
    record.active_slot = None
    record.revoked_at = timestamp
    record.revoked_by = "local_operator"
    record.revocation_reason = reason_code
    db.flush()
    return record


def latest_kill_switch_event(
    db: Session,
    *,
    lock: bool = False,
) -> AutomationKillSwitchEvent | None:
    query = db.query(AutomationKillSwitchEvent).order_by(AutomationKillSwitchEvent.revision.desc())
    if lock and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.first()


def set_automation_kill_switch(
    db: Session,
    *,
    active: bool,
    source: PolicyAuthoritySource,
    reason_code: str,
    command_digest: str | None = None,
    now: datetime | None = None,
) -> tuple[AutomationKillSwitchEvent, bool]:
    if _REASON_RE.fullmatch(reason_code) is None:
        raise AutomationPolicyError("KILL_SWITCH_REASON_INVALID")
    if source is PolicyAuthoritySource.VERCEL_SIGNED_KILL and (
        not active or command_digest is None
    ):
        raise AutomationPolicyError("REMOTE_KILL_CAN_ONLY_ACTIVATE")
    if command_digest is not None and re.fullmatch(r"^[0-9a-f]{64}$", command_digest) is None:
        raise AutomationPolicyError("KILL_SWITCH_COMMAND_INVALID")
    lock_automation_authority_fence(db)
    if command_digest is not None:
        replay = (
            db.query(AutomationKillSwitchEvent)
            .filter(AutomationKillSwitchEvent.command_digest == command_digest)
            .one_or_none()
        )
        if replay is not None:
            if replay.active is not active or replay.source != source.value:
                raise AutomationPolicyError("KILL_SWITCH_COMMAND_CONFLICT")
            return replay, True
    latest = latest_kill_switch_event(db, lock=True)
    if latest is not None and latest.active is active and command_digest is None:
        return latest, True
    row = AutomationKillSwitchEvent(
        revision=(latest.revision + 1 if latest is not None else 1),
        active=active,
        source=source.value,
        reason_code=reason_code,
        command_digest=command_digest,
        created_at=_naive(now or datetime.now(UTC)),
    )
    db.add(row)
    db.flush()
    return row, False


def kill_switch_active(db: Session, *, lock: bool = False) -> bool:
    latest = latest_kill_switch_event(db, lock=lock)
    return bool(latest and latest.active)


def _active_hours_deadline(now: datetime) -> tuple[bool, datetime]:
    local = _aware(now).astimezone(_JERUSALEM)
    start = datetime.combine(local.date(), time(8, 0), tzinfo=_JERUSALEM)
    end = datetime.combine(local.date(), time(21, 0), tzinfo=_JERUSALEM)
    return start <= local < end, end.astimezone(UTC)


def _job_data(application: Application) -> JobData:
    job = application.job
    if job is None:
        raise AutomationPolicyError("JOB_NOT_FOUND")
    return JobData(
        title=job.title or "",
        company=job.company or "",
        location=job.location or "",
        employment_type=job.employment_type or "",
        seniority=job.seniority or "",
        description=job.description or "",
        requirements=job.requirements or "",
        apply_url=job.apply_url or "",
        source_url=job.source_url or "",
        keywords=parse_required_skills(job.keywords),
    )


def _geography_allowed(policy: AutoSubmitPolicyV1, job: JobData) -> bool:
    location = " ".join(job.location.casefold().split())
    if any(token in location for token in ("israel", "ישראל", "tel aviv", "jerusalem")):
        return AutomationGeography.ISRAEL in policy.geographies
    if any(token in location for token in ("worldwide", "global", "anywhere")):
        return AutomationGeography.WORLDWIDE_REMOTE in policy.geographies
    if any(token in location for token in ("emea", "middle east")):
        return AutomationGeography.EMEA_REMOTE in policy.geographies
    return False


def _scope_matches(
    scope: QualifiedFormContractV1,
    *,
    plan: FormPlan,
    contract_digest: str,
) -> bool:
    return bool(
        scope.adapter_name == plan.adapter_name
        and scope.adapter_version == plan.adapter_version
        and scope.selector_version == plan.selector_version
        and hmac.compare_digest(scope.form_contract_digest, contract_digest)
    )


def validate_automation_inspection_candidate(
    db: Session,
    *,
    application_id: int,
    now: datetime | None = None,
    signing_key_path: str | Path | None = None,
) -> None:
    """Refuse browser inspection until signed authority and coarse scope match.

    This gate intentionally runs before opening an employer page.  It does not
    issue submission authority: the exact observed form, attachment, limits,
    and every mutable stop are evaluated again after inspection and again at
    the irreversible commit boundary.
    """

    timestamp = _aware(now or datetime.now(UTC))
    active = current_signed_policy(
        db,
        now=timestamp,
        signing_key_path=signing_key_path,
    )
    if active is None:
        raise AutomationPolicyError("AUTOMATION_POLICY_NOT_ACTIVE")
    _record, signed = active
    policy = signed.policy
    if latest_profile_version(db) != policy.profile_version:
        raise AutomationPolicyError("PROFILE_VERSION_CHANGED")
    _require_current_artifact_bindings(policy)
    if kill_switch_active(db):
        raise AutomationPolicyError("KILL_SWITCH_ACTIVE")
    active_hours, _deadline = _active_hours_deadline(timestamp)
    if not active_hours:
        raise AutomationPolicyError("OUTSIDE_ACTIVE_HOURS")

    application = db.get(Application, application_id)
    if application is None:
        raise AutomationPolicyError("APPLICATION_NOT_FOUND")
    if application.status != JobStatus.DRAFT:
        raise AutomationPolicyError("APPLICATION_NOT_ELIGIBLE")
    if application.material_eligible is not True or application.needs_review_reason:
        raise AutomationPolicyError("MATERIAL_NOT_ELIGIBLE")
    if application.job_fit_decision_id is None:
        raise AutomationPolicyError("FIT_DECISION_REQUIRED")
    fit_record = db.get(JobFitDecisionRecord, application.job_fit_decision_id)
    if fit_record is None or fit_record.job_id != application.job_id:
        raise AutomationPolicyError("FIT_DECISION_REQUIRED")
    fit = decision_from_record(fit_record)
    job = _job_data(application)
    if not fit.quality_eligible or fit.fit_score < policy.minimum_fit_score:
        raise AutomationPolicyError("FIT_DECISION_NOT_ELIGIBLE")
    if fit.selected_cv_id not in policy.role_families:
        raise AutomationPolicyError("ROLE_FAMILY_NOT_PERMITTED")
    if not _geography_allowed(policy, job):
        raise AutomationPolicyError("GEOGRAPHY_NOT_PERMITTED")
    if fit.job_digest != job_content_digest(job):
        raise AutomationPolicyError("JOB_CHANGED")
    if (
        application.profile_version != policy.profile_version
        or fit.profile_version != policy.profile_version
    ):
        raise AutomationPolicyError("PROFILE_VERSION_CHANGED")
    if (
        fit.routing_config_digest != policy.routing_config_digest
        or fit.cv_manifest_digest != policy.cv_manifest_digest
        or fit.qualification_digest != policy.fit_qualification_digest
    ):
        raise AutomationPolicyError("FIT_QUALIFICATION_CHANGED")
    answer_revision = confirmed_answer_revision(db, profile_version=policy.profile_version)
    if not hmac.compare_digest(answer_revision, policy.confirmed_answer_revision):
        raise AutomationPolicyError("CONFIRMED_ANSWERS_CHANGED")
    if (
        application.selected_cv_hash is None
        or fit.selected_cv_hash is None
        or not hmac.compare_digest(application.selected_cv_hash, fit.selected_cv_hash)
    ):
        raise AutomationPolicyError("ATTACHMENT_UNVERIFIED")

    descriptor = adapter_for_url(job.apply_url or job.source_url)
    matching_scopes = (
        tuple(
            scope
            for scope in policy.qualified_form_contracts
            if descriptor is not None
            and scope.adapter_name == descriptor.platform
            and scope.adapter_version == descriptor.adapter_version
            and scope.selector_version == descriptor.selector_version
        )
        if descriptor is not None
        else ()
    )
    if (
        descriptor is None
        or not descriptor.allows_final_execution
        or descriptor.platform not in policy.permitted_adapters
        or not matching_scopes
        or not any(_scope_has_live_canary(db, scope) for scope in matching_scopes)
    ):
        raise AutomationPolicyError("ADAPTER_NOT_QUALIFIED")


def _usage_counts(
    db: Session,
    *,
    company_digest: str,
    now: datetime,
) -> tuple[int, int, int]:
    timestamp = _naive(now)
    local = _aware(now).astimezone(_JERUSALEM)
    day_start = datetime.combine(local.date(), time.min, tzinfo=_JERUSALEM).astimezone(UTC)
    hour_start = _aware(now) - timedelta(hours=1)
    company_start = _aware(now) - timedelta(days=14)
    base = db.query(ApplicationPolicyDecision.id).filter(
        ApplicationPolicyDecision.allowed.is_(True),
    )
    daily = base.filter(ApplicationPolicyDecision.evaluated_at >= _naive(day_start)).count()
    hourly = base.filter(ApplicationPolicyDecision.evaluated_at >= _naive(hour_start)).count()
    company = base.filter(
        ApplicationPolicyDecision.company_digest == company_digest,
        ApplicationPolicyDecision.evaluated_at >= _naive(company_start),
        ApplicationPolicyDecision.evaluated_at <= timestamp,
    ).count()
    return daily, hourly, company


def _decision_from_record(record: ApplicationPolicyDecision) -> AutoSubmitDecisionV1:
    try:
        reasons = json.loads(record.reason_codes_json)
        return AutoSubmitDecisionV1.model_validate(
            {
                "policy_id": record.policy_revision.policy_id,
                "policy_revision": record.policy_revision.revision,
                "policy_digest": record.policy_digest,
                "application_id": record.application_id,
                "application_revision": record.application_revision,
                "job_digest": record.job_digest,
                "company_digest": record.company_digest,
                "fit_decision_digest": record.fit_decision_digest,
                "form_plan_id": record.form_plan_public_id,
                "form_fingerprint": record.form_fingerprint,
                "form_contract_digest": record.form_contract_digest,
                "selected_cv_hash": record.selected_cv_hash,
                "profile_version": record.profile_version,
                "confirmed_answer_revision": record.confirmed_answer_revision,
                "adapter_name": record.adapter_name,
                "adapter_version": record.adapter_version,
                "selector_version": record.selector_version,
                "fit_score": record.fit_score,
                "allowed": record.allowed,
                "reason_codes": reasons,
                "evaluated_at": _aware(record.evaluated_at),
                "authority_expires_at": (
                    _aware(record.authority_expires_at)
                    if record.authority_expires_at is not None
                    else None
                ),
            }
        )
    except (TypeError, ValueError) as exc:
        raise AutomationPolicyError("AUTOMATION_DECISION_INVALID") from exc


def _persist_decision(
    db: Session,
    *,
    policy_record: AutomationPolicyRevisionRecord,
    application: Application,
    fit_record: JobFitDecisionRecord,
    plan: FormPlan,
    decision: AutoSubmitDecisionV1,
) -> ApplicationPolicyDecision:
    existing = (
        db.query(ApplicationPolicyDecision)
        .filter(
            ApplicationPolicyDecision.application_id == application.id,
            ApplicationPolicyDecision.policy_revision_id == policy_record.id,
            ApplicationPolicyDecision.application_revision == application.revision,
            ApplicationPolicyDecision.form_plan_id == plan.id,
            ApplicationPolicyDecision.decision_digest == decision.decision_digest,
        )
        .one_or_none()
    )
    if existing is not None:
        if _decision_from_record(existing) != decision:
            raise AutomationPolicyError("AUTOMATION_DECISION_DIGEST_CONFLICT")
        return existing
    row = ApplicationPolicyDecision(
        policy_revision_id=policy_record.id,
        application_id=application.id,
        application_revision=application.revision,
        fit_decision_id=fit_record.id,
        form_plan_id=plan.id,
        decision_digest=decision.decision_digest,
        policy_digest=decision.policy_digest,
        job_digest=decision.job_digest,
        company_digest=decision.company_digest,
        fit_decision_digest=decision.fit_decision_digest,
        form_plan_public_id=str(decision.form_plan_id),
        form_fingerprint=decision.form_fingerprint,
        form_contract_digest=decision.form_contract_digest,
        selected_cv_hash=decision.selected_cv_hash,
        profile_version=decision.profile_version,
        confirmed_answer_revision=decision.confirmed_answer_revision,
        adapter_name=decision.adapter_name,
        adapter_version=decision.adapter_version,
        selector_version=decision.selector_version,
        fit_score=decision.fit_score,
        allowed=decision.allowed,
        reason_codes_json=_compact(decision.reason_codes),
        authority_expires_at=(
            _naive(decision.authority_expires_at)
            if decision.authority_expires_at is not None
            else None
        ),
        evaluated_at=_naive(decision.evaluated_at),
    )
    db.add(row)
    db.flush()
    return row


def evaluate_auto_submit_policy(
    db: Session,
    *,
    application_id: int,
    form_plan_id: int,
    now: datetime | None = None,
    signing_key_path: str | Path | None = None,
) -> ApplicationPolicyDecision:
    """Evaluate and reserve authority while holding exact application/policy rows."""

    timestamp = _aware(now or datetime.now(UTC))
    lock_automation_authority_fence(db)
    application_query = db.query(Application).filter(Application.id == application_id)
    if db.bind.dialect.name == "postgresql":
        application_query = application_query.with_for_update()
    application = application_query.populate_existing().one_or_none()
    if application is None:
        raise AutomationPolicyError("APPLICATION_NOT_FOUND")
    plan = db.get(FormPlan, form_plan_id)
    if plan is None or plan.application_id != application.id:
        raise AutomationPolicyError("FORM_PLAN_NOT_FOUND")
    if application.job_fit_decision_id is None:
        raise AutomationPolicyError("FIT_DECISION_REQUIRED")
    fit_record = db.get(JobFitDecisionRecord, application.job_fit_decision_id)
    if fit_record is None or fit_record.job_id != application.job_id:
        raise AutomationPolicyError("FIT_DECISION_REQUIRED")
    active = current_signed_policy(
        db,
        now=timestamp,
        signing_key_path=signing_key_path,
        lock=True,
    )
    if active is None:
        raise AutomationPolicyError("AUTOMATION_POLICY_NOT_ACTIVE")
    policy_record, signed = active
    policy = signed.policy
    current_profile_version = latest_profile_version(db)
    try:
        _require_current_artifact_bindings(policy)
    except AutomationPolicyError:
        current_artifacts_changed = True
    else:
        current_artifacts_changed = False
    existing_allowed = (
        db.query(ApplicationPolicyDecision)
        .filter(
            ApplicationPolicyDecision.application_id == application.id,
            ApplicationPolicyDecision.allowed.is_(True),
        )
        .one_or_none()
    )
    if existing_allowed is not None:
        if (
            existing_allowed.policy_revision_id == policy_record.id
            and existing_allowed.application_revision == application.revision
            and existing_allowed.form_plan_id == plan.id
        ):
            if current_profile_version != policy.profile_version:
                raise AutomationPolicyError("PROFILE_VERSION_CHANGED")
            if current_artifacts_changed:
                raise AutomationPolicyError("FIT_QUALIFICATION_CHANGED")
            return existing_allowed
        raise AutomationPolicyError("AUTOMATION_AUTHORITY_ALREADY_RESERVED")
    fit = decision_from_record(fit_record)
    job = _job_data(application)
    contract_digest = form_contract_digest(plan)
    answer_revision = confirmed_answer_revision(db, profile_version=policy.profile_version)
    company_digest = company_identity_digest(job.company)
    reasons: list[str] = []

    if kill_switch_active(db, lock=True):
        reasons.append("KILL_SWITCH_ACTIVE")
    active_hours, active_deadline = _active_hours_deadline(timestamp)
    if not active_hours:
        reasons.append("OUTSIDE_ACTIVE_HOURS")
    if application.status != JobStatus.DRAFT:
        reasons.append("APPLICATION_NOT_ELIGIBLE")
    if application.material_eligible is not True:
        reasons.append("MATERIAL_NOT_ELIGIBLE")
    if not fit.quality_eligible:
        reasons.append("FIT_DECISION_NOT_ELIGIBLE")
    if fit.fit_score < policy.minimum_fit_score:
        reasons.append("FIT_SCORE_BELOW_POLICY")
    if fit.selected_cv_id not in policy.role_families:
        reasons.append("ROLE_FAMILY_NOT_PERMITTED")
    if not _geography_allowed(policy, job):
        reasons.append("GEOGRAPHY_NOT_PERMITTED")
    if fit.job_digest != job_content_digest(job):
        reasons.append("JOB_CHANGED")
    if (
        current_profile_version != policy.profile_version
        or fit.profile_version != policy.profile_version
        or application.profile_version != policy.profile_version
        or plan.profile_version != policy.profile_version
    ):
        reasons.append("PROFILE_VERSION_CHANGED")
    if (
        fit.routing_config_digest != policy.routing_config_digest
        or fit.cv_manifest_digest != policy.cv_manifest_digest
        or fit.qualification_digest != policy.fit_qualification_digest
        or current_artifacts_changed
    ):
        reasons.append("FIT_QUALIFICATION_CHANGED")
    if not hmac.compare_digest(answer_revision, policy.confirmed_answer_revision):
        reasons.append("CONFIRMED_ANSWERS_CHANGED")
    application_cv_hash = str(application.selected_cv_hash or "")
    if (
        not application_cv_hash
        or fit.selected_cv_hash is None
        or plan.selected_cv_hash != application_cv_hash
        or plan.attached_cv_hash != application_cv_hash
        or not hmac.compare_digest(fit.selected_cv_hash, application_cv_hash)
        or not plan.attachment_verified
    ):
        reasons.append("ATTACHMENT_UNVERIFIED")
    if (
        plan.invalidated_at is not None
        or plan.expires_at <= _naive(timestamp)
        or plan.application_revision != application.revision
    ):
        reasons.append("FORM_CHANGED")
    try:
        blockers = json.loads(plan.blockers_json)
    except (TypeError, ValueError):
        blockers = ["FORM_PLAN_BLOCKED"]
    if not isinstance(blockers, list) or blockers:
        reasons.append("FORM_PLAN_BLOCKED")
    descriptor = adapter_for_url(job.apply_url or job.source_url)
    if (
        descriptor is None
        or descriptor.platform not in policy.permitted_adapters
        or descriptor.platform != plan.adapter_name
        or descriptor.adapter_version != plan.adapter_version
        or descriptor.selector_version != plan.selector_version
        or not descriptor.allows_final_execution
        or not descriptor.qualifies_form_fingerprint(plan.fingerprint)
    ):
        reasons.append("ADAPTER_NOT_QUALIFIED")
    matching_scope = next(
        (
            scope
            for scope in policy.qualified_form_contracts
            if _scope_matches(scope, plan=plan, contract_digest=contract_digest)
        ),
        None,
    )
    if matching_scope is None or not _scope_has_live_canary(db, matching_scope):
        reasons.append("FORM_CONTRACT_NOT_QUALIFIED")
    daily, hourly, company = _usage_counts(
        db,
        company_digest=company_digest,
        now=timestamp,
    )
    if daily >= policy.daily_limit:
        reasons.append("AUTOMATION_DAILY_LIMIT_REACHED")
    if hourly >= policy.hourly_limit:
        reasons.append("AUTOMATION_HOURLY_LIMIT_REACHED")
    if company >= policy.company_limit:
        reasons.append("AUTOMATION_COMPANY_LIMIT_REACHED")

    unique_reasons = tuple(dict.fromkeys(reasons))
    allowed = not unique_reasons
    authority_expires_at = (
        min(policy.expires_at, active_deadline, timestamp + timedelta(minutes=5))
        if allowed
        else None
    )
    decision = AutoSubmitDecisionV1(
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        policy_digest=policy.payload_digest,
        application_id=application.id,
        application_revision=application.revision,
        job_digest=fit.job_digest,
        company_digest=company_digest,
        fit_decision_digest=fit.decision_digest,
        form_plan_id=UUID(plan.plan_id),
        form_fingerprint=plan.fingerprint,
        form_contract_digest=contract_digest,
        selected_cv_hash=application_cv_hash or _ZERO_DIGEST,
        profile_version=policy.profile_version,
        confirmed_answer_revision=answer_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        fit_score=fit.fit_score,
        allowed=allowed,
        reason_codes=unique_reasons,
        evaluated_at=timestamp,
        authority_expires_at=authority_expires_at,
    )
    row = _persist_decision(
        db,
        policy_record=policy_record,
        application=application,
        fit_record=fit_record,
        plan=plan,
        decision=decision,
    )
    if allowed:
        application.approved_at = _naive(timestamp)
        application.approval_source = "qualified_autopilot_policy"
        mark_application_prepared(application)
    else:
        review_reasons = tuple(
            reason for reason in unique_reasons if reason not in RETRYABLE_AUTOMATION_DENIALS
        )
        application.needs_review_reason = review_reasons[0] if review_reasons else None
    db.flush()
    return row


def validate_current_automation_decision(
    db: Session,
    *,
    decision_record: ApplicationPolicyDecision | None,
    now: datetime | None = None,
    signing_key_path: str | Path | None = None,
    lock: bool = False,
) -> AutoSubmitDecisionV1:
    """Recheck mutable stops and every signed binding before permit/commit."""

    if decision_record is None:
        raise AutomationPolicyError("AUTOMATION_DECISION_REQUIRED")
    if lock:
        lock_automation_authority_fence(db)
        query = db.query(ApplicationPolicyDecision).filter(
            ApplicationPolicyDecision.id == decision_record.id
        )
        if db.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        decision_record = query.populate_existing().one_or_none()
        if decision_record is None:
            raise AutomationPolicyError("AUTOMATION_DECISION_REQUIRED")
    decision = _decision_from_record(decision_record)
    if not decision.allowed:
        raise AutomationPolicyError("AUTOMATION_DECISION_DENIED")
    timestamp = _aware(now or datetime.now(UTC))
    if decision.authority_expires_at is None or decision.authority_expires_at <= timestamp:
        raise AutomationPolicyError("AUTOMATION_AUTHORITY_EXPIRED")
    active = current_signed_policy(
        db,
        now=timestamp,
        signing_key_path=signing_key_path,
        lock=lock,
    )
    if active is None:
        raise AutomationPolicyError("AUTOMATION_POLICY_NOT_ACTIVE")
    policy_record, signed = active
    policy = signed.policy
    if latest_profile_version(db) != policy.profile_version:
        raise AutomationPolicyError("PROFILE_VERSION_CHANGED")
    _require_current_artifact_bindings(policy)
    if (
        policy_record.id != decision_record.policy_revision_id
        or policy.payload_digest != decision.policy_digest
        or policy.policy_id != decision.policy_id
        or policy.revision != decision.policy_revision
    ):
        raise AutomationPolicyError("AUTOMATION_POLICY_CHANGED")
    if kill_switch_active(db, lock=lock):
        raise AutomationPolicyError("KILL_SWITCH_ACTIVE")
    active_hours, _deadline = _active_hours_deadline(timestamp)
    if not active_hours:
        raise AutomationPolicyError("OUTSIDE_ACTIVE_HOURS")
    application = decision_record.application
    plan = db.get(FormPlan, decision_record.form_plan_id)
    fit_record = db.get(JobFitDecisionRecord, decision_record.fit_decision_id)
    if plan is None or fit_record is None:
        raise AutomationPolicyError("AUTOMATION_DECISION_BINDING_MISMATCH")
    fit = decision_from_record(fit_record)
    answer_revision = confirmed_answer_revision(db, profile_version=policy.profile_version)
    bindings: Iterable[tuple[object, object, str]] = (
        (application.revision, decision.application_revision, "APPLICATION_REVISION_CHANGED"),
        (application.profile_version, decision.profile_version, "PROFILE_VERSION_CHANGED"),
        (application.selected_cv_hash, decision.selected_cv_hash, "ATTACHMENT_CHANGED"),
        (fit.decision_digest, decision.fit_decision_digest, "FIT_DECISION_CHANGED"),
        (plan.plan_id, str(decision.form_plan_id), "FORM_CHANGED"),
        (plan.fingerprint, decision.form_fingerprint, "FORM_CHANGED"),
        (form_contract_digest(plan), decision.form_contract_digest, "FORM_CHANGED"),
        (answer_revision, decision.confirmed_answer_revision, "CONFIRMED_ANSWERS_CHANGED"),
        (plan.adapter_name, decision.adapter_name, "ADAPTER_VERSION_CHANGED"),
        (plan.adapter_version, decision.adapter_version, "ADAPTER_VERSION_CHANGED"),
        (plan.selector_version, decision.selector_version, "SELECTOR_DRIFT"),
    )
    for expected, observed, reason in bindings:
        if not hmac.compare_digest(str(expected or ""), str(observed or "")):
            raise AutomationPolicyError(reason)
    if plan.invalidated_at is not None or plan.expires_at <= _naive(timestamp):
        raise AutomationPolicyError("FORM_CHANGED")
    scope = next(
        (
            item
            for item in policy.qualified_form_contracts
            if _scope_matches(
                item,
                plan=plan,
                contract_digest=decision.form_contract_digest,
            )
        ),
        None,
    )
    descriptor = adapter_for_platform(plan.adapter_name)
    if (
        scope is None
        or not _scope_has_live_canary(db, scope)
        or descriptor is None
        or not descriptor.allows_final_execution
        or not descriptor.qualifies_form_fingerprint(plan.fingerprint)
    ):
        raise AutomationPolicyError("ADAPTER_NOT_QUALIFIED")
    return decision


def policy_usage_status(
    db: Session,
    *,
    now: datetime | None = None,
    signing_key_path: str | Path | None = None,
) -> dict[str, Any]:
    timestamp = _aware(now or datetime.now(UTC))
    lock_automation_authority_fence(db)
    kill = latest_kill_switch_event(db)
    record = _active_policy_record(db)
    if record is None:
        return {
            "active": False,
            "reason_code": "AUTOMATION_POLICY_NOT_ACTIVE",
            "kill_switch_active": bool(kill and kill.active),
        }
    try:
        signed = _verified_policy(record, signing_key_path=signing_key_path)
    except AutomationPolicyError as exc:
        return {
            "active": False,
            "reason_code": exc.reason_code,
            "kill_switch_active": bool(kill and kill.active),
        }
    policy = signed.policy
    profile_changed = latest_profile_version(db) != policy.profile_version
    try:
        _require_current_artifact_bindings(policy)
    except AutomationPolicyError:
        artifact_bindings_changed = True
    else:
        artifact_bindings_changed = False
    answer_revision_changed = not hmac.compare_digest(
        confirmed_answer_revision(db, profile_version=policy.profile_version),
        policy.confirmed_answer_revision,
    )
    daily, hourly, _company = _usage_counts(
        db,
        company_digest=_ZERO_DIGEST,
        now=timestamp,
    )
    expired = policy.expires_at <= timestamp
    return {
        "active": (
            not expired
            and not profile_changed
            and not artifact_bindings_changed
            and not answer_revision_changed
            and not bool(kill and kill.active)
        ),
        "reason_code": (
            "KILL_SWITCH_ACTIVE"
            if kill and kill.active
            else "AUTOMATION_POLICY_EXPIRED"
            if expired
            else "PROFILE_VERSION_CHANGED"
            if profile_changed
            else "FIT_QUALIFICATION_CHANGED"
            if artifact_bindings_changed
            else "CONFIRMED_ANSWERS_CHANGED"
            if answer_revision_changed
            else None
        ),
        "policy_id": str(policy.policy_id),
        "revision": policy.revision,
        "activated_at": policy.activated_at.isoformat(),
        "expires_at": policy.expires_at.isoformat(),
        "minimum_fit_score": policy.minimum_fit_score,
        "daily_limit": policy.daily_limit,
        "hourly_limit": policy.hourly_limit,
        "daily_used": daily,
        "hourly_used": hourly,
        "daily_remaining": max(0, policy.daily_limit - daily),
        "hourly_remaining": max(0, policy.hourly_limit - hourly),
        "company_limit": policy.company_limit,
        "company_window_days": policy.company_window_days,
        "role_families": list(policy.role_families),
        "geographies": [item.value for item in policy.geographies],
        "permitted_adapters": list(policy.permitted_adapters),
        "qualified_form_contract_count": len(policy.qualified_form_contracts),
        "profile_version": policy.profile_version,
        "confirmed_answer_revision": policy.confirmed_answer_revision,
        "kill_switch_active": bool(kill and kill.active),
        "kill_switch_revision": kill.revision if kill else 0,
    }


__all__ = [
    "AutomationPolicyError",
    "RETRYABLE_AUTOMATION_DENIALS",
    "activate_auto_submit_policy",
    "company_identity_digest",
    "confirmed_answer_revision",
    "current_signed_policy",
    "evaluate_auto_submit_policy",
    "form_contract_digest",
    "kill_switch_active",
    "lock_automation_authority_fence",
    "latest_kill_switch_event",
    "policy_usage_status",
    "revoke_auto_submit_policy",
    "set_automation_kill_switch",
    "validate_automation_inspection_candidate",
    "validate_current_automation_decision",
    "verified_policy_for_decision",
]
