"""Strict, privacy-safe ATS qualification authority.

Committed fixture reports prove only offline parser behavior. A guarded real-URL
dry run can authorize later inspection for the same adapter code identity, and
one employer-confirmed canary can authorize final execution for the observed
semantic form-contract class. Legacy BrowserQualificationRun rows remain
telemetry and are never consulted for authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from core.automation_authority_fence import lock_automation_authority_fence
from core.automation_policy_service import form_contract_digest
from core.runtime_identity import get_runtime_identity, runtime_source_is_current
from core.submission_service import reconstruct_persisted_form_plan
from db.models import (
    AdapterQualificationRecord,
    Application,
    BrowserQualificationRun,
    FormPlan,
    QualificationCanaryAuthorization,
    Submission,
    SubmissionEvidence,
)
from discovery.contracts import stable_digest
from ingestion.url_utils import normalize_url, url_hash
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    AdapterDescriptor,
    QualificationTier,
    adapter_for_platform,
    adapter_for_url,
    registered_adapters,
)

_ROOT = Path(__file__).resolve().parents[1]
_QUALIFICATION_DIR = _ROOT / "docs" / "qualification"
_REPORT_STEMS = {
    "workday": "workday-browser-v2",
    "greenhouse": "greenhouse-browser-v1",
    "lever": "lever-browser-v1",
    "ashby": "ashby-browser-v1",
    "smartrecruiters": "smartrecruiters-browser-v1",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_CANARY_TTL = timedelta(minutes=5)


class AdapterQualificationError(ValueError):
    """Bounded qualification failure safe for an authenticated local operator."""

    def __init__(self, reason_code: str):
        bounded = (
            reason_code
            if _REASON_RE.fullmatch(reason_code or "") is not None
            else "ADAPTER_QUALIFICATION_INVALID"
        )
        super().__init__(bounded)
        self.reason_code = bounded


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _aware(value).replace(tzinfo=None)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
        result[key] = value
    return result


def _current_fixture_digest(descriptor: AdapterDescriptor) -> str:
    """Validate and return the committed fixture manifest for one adapter."""

    stem = _REPORT_STEMS.get(descriptor.platform)
    if stem is None:
        raise AdapterQualificationError("ADAPTER_NOT_QUALIFIED")
    try:
        report = json.loads(
            (_QUALIFICATION_DIR / f"{stem}.json").read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _token: (_ for _ in ()).throw(
                AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
            ),
        )
    except AdapterQualificationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED") from exc
    if not isinstance(report, dict):
        raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
    adapter = report.get("adapter")
    gates = report.get("qualification_gates")
    evidence = report.get("fixture_evidence")
    safety = report.get("safety_observations")
    if (
        not isinstance(adapter, dict)
        or not isinstance(gates, dict)
        or not isinstance(evidence, dict)
        or not isinstance(safety, dict)
    ):
        raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
    expected_adapter = {
        "adapter_name": descriptor.platform,
        "adapter_version": descriptor.adapter_version,
        "execution_contract_version": descriptor.execution_contract_version,
        "selector_version": descriptor.selector_version,
        "transport": descriptor.transport,
    }
    if (
        report.get("schema_version") != "ats-browser-qualification-report-v1"
        or report.get("achieved_tier") != QualificationTier.FIXTURE_QUALIFIED.value
        or adapter != expected_adapter
        or gates
        != {
            "final_external_action_enabled": False,
            "fixture_contract": "passed",
            "live_canary": "pending",
            "qualified_form_scope": [],
            "real_url_dry_run": "pending",
        }
    ):
        raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
    if any(
        safety.get(flag) is not False
        for flag in (
            "external_network_used",
            "final_action_performed",
            "private_data_used",
            "real_application_used",
        )
    ):
        raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
    cases = evidence.get("cases")
    count = evidence.get("fixture_count")
    digest = evidence.get("fixture_digest")
    if (
        not isinstance(cases, list)
        or not isinstance(count, int)
        or count <= 0
        or count != len(cases)
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
    ):
        raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
    observed_files: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
        filename = case.get("file")
        case_digest = case.get("sha256")
        if (
            not isinstance(filename, str)
            or not filename
            or filename in observed_files
            or not isinstance(case_digest, str)
            or _SHA256_RE.fullmatch(case_digest) is None
        ):
            raise AdapterQualificationError("FIXTURE_EVIDENCE_CHANGED")
        observed_files.add(filename)
    return digest


def fixture_evidence_digest(adapter_name: str) -> str:
    """Return the validated committed fixture digest for one adapter code identity."""

    descriptor = adapter_for_platform(adapter_name)
    if descriptor is None:
        raise AdapterQualificationError("ADAPTER_NOT_QUALIFIED")
    return _current_fixture_digest(descriptor)


def _descriptor_matches_plan(descriptor: AdapterDescriptor, plan: FormPlan) -> bool:
    return (
        descriptor.platform == plan.adapter_name
        and descriptor.adapter_version == plan.adapter_version
        and descriptor.selector_version == plan.selector_version
        and descriptor.execution_contract_version == TWO_PHASE_EXECUTION_CONTRACT_VERSION
        and descriptor.transport == "browser"
    )


def _qualification_trace(plan: FormPlan, *, terminal_reason: str) -> dict[str, object]:
    domain = reconstruct_persisted_form_plan(plan)
    return {
        "schema_version": "ats-qualification-trace.v1",
        "terminal_reason": terminal_reason,
        "selector_version": domain.selector_version,
        "form_fingerprint": domain.form_fingerprint,
        "field_types": [field.field_type.value for field in domain.fields],
        "resolver_sources": [decision.provenance.value for decision in domain.decisions],
        "attachment_verified": domain.attachment_verified,
        "blocker_codes": [blocker.value for blocker in domain.blockers],
    }


def _trace_digest(trace: dict[str, object]) -> str:
    return stable_digest(trace)


def _current_runner_release() -> str | None:
    """Return the stable local release only while its source remains unchanged."""

    identity = get_runtime_identity()
    if (
        identity.release_id in {"", "unknown", "unavailable"}
        or len(identity.release_id) > 64
        or not runtime_source_is_current(identity)
    ):
        return None
    return identity.release_id


def _qualification_query(
    db: Session,
    *,
    descriptor: AdapterDescriptor,
    fixture_digest: str,
    runner_release: str,
    tier: str | None = None,
):
    query = db.query(AdapterQualificationRecord).filter(
        AdapterQualificationRecord.adapter_name == descriptor.platform,
        AdapterQualificationRecord.adapter_version == descriptor.adapter_version,
        AdapterQualificationRecord.selector_version == descriptor.selector_version,
        AdapterQualificationRecord.execution_contract_version
        == descriptor.execution_contract_version,
        AdapterQualificationRecord.fixture_digest == fixture_digest,
        AdapterQualificationRecord.runner_release == runner_release,
        AdapterQualificationRecord.invalidated_at.is_(None),
    )
    if tier is not None:
        query = query.filter(AdapterQualificationRecord.qualification_tier == tier)
    return query


def record_dry_run_qualification(
    db: Session,
    *,
    application: Application,
    plan: FormPlan,
    job_url: str,
    runner_release: str,
    now: datetime | None = None,
) -> AdapterQualificationRecord:
    """Record one fully resolved real-URL inspection; never perform a final action."""

    timestamp = _aware(now or datetime.now(UTC))
    lock_automation_authority_fence(db)
    descriptor = adapter_for_url(job_url)
    if descriptor is None or not _descriptor_matches_plan(descriptor, plan):
        raise AdapterQualificationError("ADAPTER_VERSION_CHANGED")
    fixture_digest = _current_fixture_digest(descriptor)
    try:
        normalized_url = normalize_url(job_url)
        domain = reconstruct_persisted_form_plan(plan)
        contract_digest = form_contract_digest(plan)
    except Exception as exc:
        raise AdapterQualificationError("FORM_PLAN_BLOCKED") from exc
    if (
        application.id != plan.application_id
        or application.revision != plan.application_revision
        or plan.invalidated_at is not None
        or not domain.ready_for_permit_at(timestamp)
    ):
        raise AdapterQualificationError("FORM_PLAN_BLOCKED")
    if not runner_release or len(runner_release) > 64:
        raise AdapterQualificationError("BUILD_MISMATCH")
    existing = (
        _qualification_query(
            db,
            descriptor=descriptor,
            fixture_digest=fixture_digest,
            runner_release=runner_release,
            tier=QualificationTier.DRY_RUN_QUALIFIED.value,
        )
        .filter(
            AdapterQualificationRecord.application_id == application.id,
            AdapterQualificationRecord.application_revision == application.revision,
            AdapterQualificationRecord.form_plan_id == plan.id,
            AdapterQualificationRecord.form_fingerprint == plan.fingerprint,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    trace = _qualification_trace(plan, terminal_reason="DRY_RUN_QUALIFIED")
    evidence_digest = _trace_digest(trace)
    record = AdapterQualificationRecord(
        qualification_tier=QualificationTier.DRY_RUN_QUALIFIED.value,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        execution_contract_version=descriptor.execution_contract_version,
        form_fingerprint=plan.fingerprint,
        form_contract_digest=contract_digest,
        fixture_digest=fixture_digest,
        application_id=application.id,
        application_revision=application.revision,
        form_plan_id=plan.id,
        attempt_id=None,
        job_url_hash=url_hash(normalized_url),
        evidence_digest=evidence_digest,
        runner_release=runner_release,
        qualified_at=_naive(timestamp),
    )
    db.add(record)
    db.add(
        BrowserQualificationRun(
            selector_version=descriptor.selector_version,
            terminal_reason="DRY_RUN_QUALIFIED",
            qualified=True,
            trace_json=json.dumps(trace, ensure_ascii=True, separators=(",", ":")),
            adapter_name=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            qualification_tier=QualificationTier.DRY_RUN_QUALIFIED.value,
            form_fingerprint=plan.fingerprint,
            form_contract_digest=contract_digest,
            fixture_digest=fixture_digest,
        )
    )
    db.flush()
    return record


def effective_inspection_descriptor(db: Session, job_url: str) -> AdapterDescriptor | None:
    """Return a descriptor only after this adapter build passed a real-URL dry run."""

    descriptor = adapter_for_url(job_url)
    if descriptor is None:
        return None
    try:
        fixture_digest = _current_fixture_digest(descriptor)
    except AdapterQualificationError:
        return None
    runner_release = _current_runner_release()
    if runner_release is None:
        return None
    records = (
        _qualification_query(
            db,
            descriptor=descriptor,
            fixture_digest=fixture_digest,
            runner_release=runner_release,
        )
        .order_by(AdapterQualificationRecord.qualified_at.desc())
        .all()
    )
    if not records:
        return None
    live = any(
        row.qualification_tier == QualificationTier.LIVE_CANARY_QUALIFIED.value for row in records
    )
    fingerprints = tuple(dict.fromkeys(row.form_fingerprint for row in records))
    return replace(
        descriptor,
        qualification=(
            QualificationTier.LIVE_CANARY_QUALIFIED if live else QualificationTier.DRY_RUN_QUALIFIED
        ),
        qualified_form_scope=fingerprints,
    )


def effective_live_descriptor_for_plan(
    db: Session,
    *,
    job_url: str,
    plan: FormPlan,
) -> AdapterDescriptor | None:
    """Resolve final authority for one current semantic form-contract class."""

    descriptor = adapter_for_url(job_url)
    if descriptor is None or not _descriptor_matches_plan(descriptor, plan):
        return None
    try:
        fixture_digest = _current_fixture_digest(descriptor)
        contract_digest = form_contract_digest(plan)
    except Exception:
        return None
    runner_release = _current_runner_release()
    if runner_release is None:
        return None
    record = (
        _qualification_query(
            db,
            descriptor=descriptor,
            fixture_digest=fixture_digest,
            runner_release=runner_release,
            tier=QualificationTier.LIVE_CANARY_QUALIFIED.value,
        )
        .filter(AdapterQualificationRecord.form_contract_digest == contract_digest)
        .order_by(AdapterQualificationRecord.qualified_at.desc())
        .first()
    )
    if record is None:
        return None
    return replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(plan.fingerprint,),
    )


def scope_has_live_qualification(
    db: Session,
    *,
    adapter_name: str,
    adapter_version: str,
    selector_version: str,
    form_contract_digest_value: str,
) -> bool:
    """Prove one semantic form scope from strict, current canary authority."""

    descriptor = adapter_for_platform(adapter_name)
    if (
        descriptor is None
        or descriptor.adapter_version != adapter_version
        or descriptor.selector_version != selector_version
        or descriptor.execution_contract_version != TWO_PHASE_EXECUTION_CONTRACT_VERSION
    ):
        return False
    try:
        fixture_digest = _current_fixture_digest(descriptor)
    except AdapterQualificationError:
        return False
    runner_release = _current_runner_release()
    if runner_release is None:
        return False
    return (
        _qualification_query(
            db,
            descriptor=descriptor,
            fixture_digest=fixture_digest,
            runner_release=runner_release,
            tier=QualificationTier.LIVE_CANARY_QUALIFIED.value,
        )
        .filter(AdapterQualificationRecord.form_contract_digest == form_contract_digest_value)
        .first()
        is not None
    )


def effective_registered_descriptors(db: Session) -> tuple[AdapterDescriptor, ...]:
    """Return a redaction-safe inventory with qualification derived from authority rows."""

    effective: list[AdapterDescriptor] = []
    runner_release = _current_runner_release()
    if runner_release is None:
        return registered_adapters()
    for descriptor in registered_adapters():
        try:
            fixture_digest = _current_fixture_digest(descriptor)
        except AdapterQualificationError:
            effective.append(descriptor)
            continue
        records = _qualification_query(
            db,
            descriptor=descriptor,
            fixture_digest=fixture_digest,
            runner_release=runner_release,
        ).all()
        live_fingerprints = tuple(
            dict.fromkeys(
                row.form_fingerprint
                for row in records
                if row.qualification_tier == QualificationTier.LIVE_CANARY_QUALIFIED.value
            )
        )
        inspected_fingerprints = tuple(dict.fromkeys(row.form_fingerprint for row in records))
        if live_fingerprints:
            effective.append(
                replace(
                    descriptor,
                    qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
                    qualified_form_scope=live_fingerprints,
                )
            )
        elif inspected_fingerprints:
            effective.append(
                replace(
                    descriptor,
                    qualification=QualificationTier.DRY_RUN_QUALIFIED,
                    qualified_form_scope=inspected_fingerprints,
                )
            )
        else:
            effective.append(descriptor)
    return tuple(effective)


def _authorization_payload(row: QualificationCanaryAuthorization) -> dict[str, object]:
    return {
        "schema_version": "qualification-canary-authorization.v1",
        "nonce_hash": row.nonce_hash,
        "application_id": row.application_id,
        "application_revision": row.application_revision,
        "form_plan_id": row.form_plan_id,
        "dry_run_qualification_id": row.dry_run_qualification_id,
        "adapter_name": row.adapter_name,
        "adapter_version": row.adapter_version,
        "selector_version": row.selector_version,
        "execution_contract_version": row.execution_contract_version,
        "form_fingerprint": row.form_fingerprint,
        "form_contract_digest": row.form_contract_digest,
        "selected_cv_hash": row.selected_cv_hash,
        "job_url_hash": row.job_url_hash,
        "runner_release": row.runner_release,
        "issued_at": _aware(row.issued_at).isoformat(),
        "expires_at": _aware(row.expires_at).isoformat(),
    }


def qualification_canary_authorization_digest(row: QualificationCanaryAuthorization) -> str:
    return stable_digest(_authorization_payload(row))


def mint_qualification_canary_authorization(
    db: Session,
    *,
    application: Application,
    plan: FormPlan,
    job_url: str,
    runner_release: str,
    now: datetime | None = None,
) -> QualificationCanaryAuthorization:
    """Create one local five-minute authority bound to a qualified dry-run plan."""

    timestamp = _aware(now or datetime.now(UTC))
    lock_automation_authority_fence(db)
    descriptor = adapter_for_url(job_url)
    if descriptor is None or not _descriptor_matches_plan(descriptor, plan):
        raise AdapterQualificationError("ADAPTER_VERSION_CHANGED")
    try:
        fixture_digest = _current_fixture_digest(descriptor)
        domain = reconstruct_persisted_form_plan(plan)
        normalized_url = normalize_url(job_url)
        contract_digest = form_contract_digest(plan)
    except Exception as exc:
        raise AdapterQualificationError("FORM_PLAN_BLOCKED") from exc
    if (
        application.id != plan.application_id
        or application.revision != plan.application_revision
        or plan.invalidated_at is not None
        or not domain.ready_for_permit_at(timestamp)
        or application.selected_cv_hash != plan.selected_cv_hash
    ):
        raise AdapterQualificationError("FORM_PLAN_BLOCKED")
    dry_run = (
        _qualification_query(
            db,
            descriptor=descriptor,
            fixture_digest=fixture_digest,
            runner_release=runner_release,
            tier=QualificationTier.DRY_RUN_QUALIFIED.value,
        )
        .filter(
            AdapterQualificationRecord.application_id == application.id,
            AdapterQualificationRecord.application_revision == application.revision,
            AdapterQualificationRecord.form_plan_id == plan.id,
            AdapterQualificationRecord.form_fingerprint == plan.fingerprint,
            AdapterQualificationRecord.form_contract_digest == contract_digest,
            AdapterQualificationRecord.job_url_hash == url_hash(normalized_url),
        )
        .one_or_none()
    )
    if dry_run is None:
        raise AdapterQualificationError("REAL_URL_DRY_RUN_REQUIRED")
    if not runner_release or len(runner_release) > 64:
        raise AdapterQualificationError("BUILD_MISMATCH")
    active = (
        db.query(QualificationCanaryAuthorization.id)
        .filter(
            QualificationCanaryAuthorization.application_id == application.id,
            QualificationCanaryAuthorization.consumed_at.is_(None),
            QualificationCanaryAuthorization.revoked_at.is_(None),
            QualificationCanaryAuthorization.expires_at > _naive(timestamp),
        )
        .first()
    )
    if active is not None:
        raise AdapterQualificationError("CANARY_AUTHORIZATION_ACTIVE")
    expires_at = min(_aware(plan.expires_at), timestamp + _CANARY_TTL)
    if expires_at <= timestamp:
        raise AdapterQualificationError("CANARY_AUTHORIZATION_EXPIRED")
    nonce_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    row = QualificationCanaryAuthorization(
        authorization_digest="0" * 64,
        nonce_hash=nonce_hash,
        application_id=application.id,
        application_revision=application.revision,
        form_plan_id=plan.id,
        dry_run_qualification_id=dry_run.id,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        execution_contract_version=descriptor.execution_contract_version,
        form_fingerprint=plan.fingerprint,
        form_contract_digest=contract_digest,
        selected_cv_hash=plan.selected_cv_hash,
        job_url_hash=url_hash(normalized_url),
        runner_release=runner_release,
        issued_at=_naive(timestamp),
        expires_at=_naive(expires_at),
    )
    row.authorization_digest = qualification_canary_authorization_digest(row)
    db.add(row)
    db.flush()
    return row


def validate_qualification_canary_authorization(
    db: Session,
    *,
    authorization_id: int,
    authorization_digest: str,
    application: Application,
    plan: FormPlan,
    job_url: str,
    runner_release: str,
    consumed: bool,
    now: datetime | None = None,
    lock: bool = False,
) -> QualificationCanaryAuthorization:
    """Revalidate every canary binding at admission and at commit time."""

    timestamp = _aware(now or datetime.now(UTC))
    query = db.query(QualificationCanaryAuthorization).filter(
        QualificationCanaryAuthorization.id == authorization_id,
        QualificationCanaryAuthorization.authorization_digest == authorization_digest,
    )
    if lock and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    row = query.populate_existing().one_or_none()
    if row is None:
        raise AdapterQualificationError("CANARY_AUTHORIZATION_REQUIRED")
    expected_digest = qualification_canary_authorization_digest(row)
    if not hmac.compare_digest(expected_digest, str(row.authorization_digest)):
        raise AdapterQualificationError("CANARY_AUTHORIZATION_CHANGED")
    if timestamp < _aware(row.issued_at):
        raise AdapterQualificationError("CANARY_AUTHORIZATION_NOT_YET_VALID")
    if row.revoked_at is not None or _aware(row.expires_at) <= timestamp:
        raise AdapterQualificationError("CANARY_AUTHORIZATION_EXPIRED")
    if (row.consumed_at is not None) is not consumed:
        state_reason = (
            "CANARY_AUTHORIZATION_REPLAYED"
            if row.consumed_at is not None
            else "CANARY_NOT_CONSUMED"
        )
        raise AdapterQualificationError(state_reason)
    descriptor = adapter_for_url(job_url)
    if descriptor is None or not _descriptor_matches_plan(descriptor, plan):
        raise AdapterQualificationError("ADAPTER_VERSION_CHANGED")
    try:
        fixture_digest = _current_fixture_digest(descriptor)
        normalized_url = normalize_url(job_url)
        contract_digest = form_contract_digest(plan)
    except Exception as exc:
        raise AdapterQualificationError("FORM_PLAN_BLOCKED") from exc
    bindings = (
        (row.application_id, application.id),
        (row.application_revision, application.revision),
        (row.application_revision, plan.application_revision),
        (row.form_plan_id, plan.id),
        (row.adapter_name, descriptor.platform),
        (row.adapter_name, plan.adapter_name),
        (row.adapter_version, descriptor.adapter_version),
        (row.adapter_version, plan.adapter_version),
        (row.selector_version, descriptor.selector_version),
        (row.selector_version, plan.selector_version),
        (row.execution_contract_version, descriptor.execution_contract_version),
        (row.form_fingerprint, plan.fingerprint),
        (row.form_contract_digest, contract_digest),
        (row.selected_cv_hash, plan.selected_cv_hash),
        (row.selected_cv_hash, application.selected_cv_hash),
        (row.job_url_hash, url_hash(normalized_url)),
        (row.runner_release, runner_release),
    )
    if any(
        not hmac.compare_digest(str(expected), str(observed)) for expected, observed in bindings
    ):
        raise AdapterQualificationError("CANARY_AUTHORIZATION_CHANGED")
    dry_run = db.get(AdapterQualificationRecord, row.dry_run_qualification_id)
    if (
        dry_run is None
        or dry_run.invalidated_at is not None
        or dry_run.qualification_tier != QualificationTier.DRY_RUN_QUALIFIED.value
        or dry_run.fixture_digest != fixture_digest
        or dry_run.form_plan_id != plan.id
        or dry_run.form_contract_digest != contract_digest
    ):
        raise AdapterQualificationError("REAL_URL_DRY_RUN_REQUIRED")
    return row


def consume_qualification_canary_authorization(
    row: QualificationCanaryAuthorization,
    *,
    now: datetime | None = None,
) -> None:
    if row.consumed_at is not None:
        raise AdapterQualificationError("CANARY_AUTHORIZATION_REPLAYED")
    row.consumed_at = _naive(now or datetime.now(UTC))


def effective_canary_descriptor(
    db: Session,
    *,
    attempt: Submission,
    plan: FormPlan,
    job_url: str,
    runner_release: str,
    now: datetime | None = None,
    lock: bool = False,
) -> AdapterDescriptor | None:
    """Return exact one-shot executor authority for a consumed canary grant."""

    if (
        attempt.authority_kind != "qualification_canary"
        or attempt.qualification_canary_authorization_id is None
        or attempt.qualification_canary_authorization_digest is None
    ):
        return None
    try:
        validate_qualification_canary_authorization(
            db,
            authorization_id=attempt.qualification_canary_authorization_id,
            authorization_digest=attempt.qualification_canary_authorization_digest,
            application=attempt.application,
            plan=plan,
            job_url=job_url,
            runner_release=runner_release,
            consumed=True,
            now=now,
            lock=lock,
        )
    except AdapterQualificationError:
        return None
    return descriptor_for_validated_canary(
        attempt.qualification_canary_authorization,
        job_url=job_url,
        plan=plan,
    )


def descriptor_for_validated_canary(
    authorization: QualificationCanaryAuthorization,
    *,
    job_url: str,
    plan: FormPlan,
) -> AdapterDescriptor | None:
    """Elevate only the exact descriptor already bound by validated authority."""

    descriptor = adapter_for_url(job_url)
    if (
        descriptor is None
        or not _descriptor_matches_plan(descriptor, plan)
        or authorization.adapter_name != descriptor.platform
        or authorization.adapter_version != descriptor.adapter_version
        or authorization.selector_version != descriptor.selector_version
        or authorization.execution_contract_version != descriptor.execution_contract_version
        or authorization.form_fingerprint != plan.fingerprint
    ):
        return None
    return replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(plan.fingerprint,),
    )


def record_live_canary_confirmation(
    db: Session,
    *,
    attempt: Submission,
    plan: FormPlan,
    evidence_digest: str,
    runner_release: str,
    now: datetime | None = None,
) -> AdapterQualificationRecord:
    """Promote only an employer-confirmed exact canary to live qualification."""

    timestamp = _aware(now or datetime.now(UTC))
    if _SHA256_RE.fullmatch(evidence_digest or "") is None:
        raise AdapterQualificationError("EVIDENCE_INVALID")
    status = getattr(attempt.status, "value", attempt.status)
    if (
        attempt.stage != "finished"
        or attempt.outcome != "confirmed_submitted"
        or status != "success"
        or attempt.submitted_at is None
        or attempt.final_action_at is None
        or attempt.verification_kind
        not in {
            "employer_application_id",
            "api_receipt",
            "candidate_portal_record",
            "visible_post_click_confirmation",
        }
        or attempt.evidence_digest is None
        or not hmac.compare_digest(str(attempt.evidence_digest), evidence_digest)
        or _aware(attempt.final_action_at) > _aware(attempt.submitted_at)
        or _aware(attempt.submitted_at) > timestamp
    ):
        raise AdapterQualificationError("EMPLOYER_EVIDENCE_REQUIRED")
    evidence = (
        db.query(SubmissionEvidence.id)
        .filter(
            SubmissionEvidence.attempt_id == attempt.id,
            SubmissionEvidence.evidence_digest == evidence_digest,
            SubmissionEvidence.evidence_type == attempt.verification_kind,
            SubmissionEvidence.form_fingerprint == plan.fingerprint,
            SubmissionEvidence.cv_hash == plan.attached_cv_hash,
        )
        .first()
    )
    if evidence is None:
        raise AdapterQualificationError("EMPLOYER_EVIDENCE_REQUIRED")
    job = attempt.application.job
    job_url = ((job.apply_url or job.source_url) if job is not None else "") or ""
    descriptor = effective_canary_descriptor(
        db,
        attempt=attempt,
        plan=plan,
        job_url=job_url,
        runner_release=runner_release,
        now=timestamp,
        lock=True,
    )
    if descriptor is None:
        raise AdapterQualificationError("CANARY_AUTHORIZATION_CHANGED")
    fixture_digest = _current_fixture_digest(descriptor)
    contract_digest = form_contract_digest(plan)
    existing = (
        db.query(AdapterQualificationRecord)
        .filter(AdapterQualificationRecord.attempt_id == attempt.id)
        .one_or_none()
    )
    if existing is not None:
        return existing
    trace = _qualification_trace(plan, terminal_reason="LIVE_CANARY_CONFIRMED")
    trace["evidence_type"] = str(attempt.verification_kind or "unknown")[:64]
    record = AdapterQualificationRecord(
        qualification_tier=QualificationTier.LIVE_CANARY_QUALIFIED.value,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        execution_contract_version=descriptor.execution_contract_version,
        form_fingerprint=plan.fingerprint,
        form_contract_digest=contract_digest,
        fixture_digest=fixture_digest,
        application_id=attempt.application_id,
        application_revision=attempt.application_revision,
        form_plan_id=plan.id,
        attempt_id=attempt.id,
        job_url_hash=attempt.qualification_canary_authorization.job_url_hash,
        evidence_digest=evidence_digest,
        runner_release=runner_release,
        qualified_at=_naive(timestamp),
    )
    db.add(record)
    db.add(
        BrowserQualificationRun(
            selector_version=descriptor.selector_version,
            terminal_reason="LIVE_CANARY_CONFIRMED",
            qualified=True,
            trace_json=json.dumps(trace, ensure_ascii=True, separators=(",", ":")),
            adapter_name=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            qualification_tier=QualificationTier.LIVE_CANARY_QUALIFIED.value,
            form_fingerprint=plan.fingerprint,
            form_contract_digest=contract_digest,
            fixture_digest=fixture_digest,
        )
    )
    db.flush()
    return record


def invalidate_stale_qualification_records(
    db: Session,
    *,
    now: datetime | None = None,
) -> int:
    """Persist automatic revocation when adapter or fixture evidence drifts."""

    timestamp = _naive(now or datetime.now(UTC))
    runner_release = _current_runner_release()
    changed = 0
    for row in (
        db.query(AdapterQualificationRecord)
        .filter(AdapterQualificationRecord.invalidated_at.is_(None))
        .all()
    ):
        descriptor = adapter_for_platform(row.adapter_name)
        reason: str | None = None
        if runner_release is None or not hmac.compare_digest(
            str(row.runner_release),
            runner_release,
        ):
            reason = "BUILD_MISMATCH"
        elif (
            descriptor is None
            or descriptor.adapter_version != row.adapter_version
            or descriptor.selector_version != row.selector_version
            or descriptor.execution_contract_version != row.execution_contract_version
        ):
            reason = "ADAPTER_VERSION_CHANGED"
        else:
            try:
                if not hmac.compare_digest(
                    _current_fixture_digest(descriptor),
                    str(row.fixture_digest),
                ):
                    reason = "FIXTURE_EVIDENCE_CHANGED"
            except AdapterQualificationError:
                reason = "FIXTURE_EVIDENCE_CHANGED"
        if reason is not None:
            row.invalidated_at = timestamp
            row.invalidation_reason = reason
            changed += 1
    return changed


__all__ = [
    "AdapterQualificationError",
    "consume_qualification_canary_authorization",
    "descriptor_for_validated_canary",
    "effective_canary_descriptor",
    "effective_inspection_descriptor",
    "effective_live_descriptor_for_plan",
    "effective_registered_descriptors",
    "fixture_evidence_digest",
    "invalidate_stale_qualification_records",
    "mint_qualification_canary_authorization",
    "qualification_canary_authorization_digest",
    "record_dry_run_qualification",
    "record_live_canary_confirmation",
    "scope_has_live_qualification",
    "validate_qualification_canary_authorization",
]
