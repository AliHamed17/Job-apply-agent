"""Persistence boundary for immutable, browser-observed application plans."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hmac import compare_digest
from profile.models import canonical_fact_key
from typing import cast

from core.application_audit import record_application_event
from core.form_planning import option_set_hash, reusable_field_contract_fingerprint
from core.operational_metrics import record_form_plan_metrics
from core.submission_domain import (
    AnswerDisposition,
    AnswerProvenance,
    FormPlanV1,
    ReasonCode,
)
from db.models import Application, FormPlan, OperatorApprovedAnswer
from submitters.platforms import adapter_for_url

ATTACHMENT_VERIFICATION_SOURCE = "candidate_browser_upload_complete"


class FormPlanPersistenceError(ValueError):
    """A browser observation no longer matches authoritative application state."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FormPlanPersistenceError("FORM_PLAN_TIME_INVALID")
    return value.astimezone(UTC).replace(tzinfo=None)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _validate_reusable_answer_evidence(db, plan: FormPlanV1) -> None:
    """Recheck reusable evidence after browser work and before persistence.

    Inspection can spend minutes outside the application transaction. A
    reusable answer may be revoked or superseded during that interval, so the
    plan cannot trust the row observed by its request-scoped answer policy.
    """

    fields = {field.field_id: field for field in plan.fields}
    prefix = "operator-approved-answer:"
    for decision in plan.decisions:
        if decision.provenance is not AnswerProvenance.OPERATOR_APPROVED_REUSABLE:
            continue
        field = fields.get(decision.field_id)
        if (
            field is None
            or decision.disposition is not AnswerDisposition.RESOLVED
            or len(decision.evidence_refs) != 1
            or not decision.evidence_refs[0].startswith(prefix)
        ):
            raise FormPlanPersistenceError(ReasonCode.FORM_CHANGED.value)
        try:
            evidence_id = int(decision.evidence_refs[0].removeprefix(prefix))
        except ValueError as exc:
            raise FormPlanPersistenceError(ReasonCode.FORM_CHANGED.value) from exc
        if evidence_id < 1:
            raise FormPlanPersistenceError(ReasonCode.FORM_CHANGED.value)

        # populate_existing prevents a row cached during browser inspection
        # from hiding a concurrent revocation committed before this recheck.
        row = (
            db.query(OperatorApprovedAnswer)
            .filter(OperatorApprovedAnswer.id == evidence_id)
            .populate_existing()
            .one_or_none()
        )
        try:
            recorded_answer = json.loads(row.answer_json) if row is not None else None
        except (TypeError, ValueError):
            recorded_answer = None
        if (
            row is None
            or row.revoked_at is not None
            or not field.canonical_name
            or row.canonical_field != canonical_fact_key(field.canonical_name)
            or row.field_type != field.field_type.value
            or row.option_set_hash != option_set_hash(field)
            or row.locale != plan.locale
            or row.profile_version != plan.profile_version
            or row.selected_cv_id != plan.selected_cv_id
            or not compare_digest(row.selected_cv_hash, plan.selected_cv_hash)
            or row.adapter_name != plan.adapter_name
            or row.adapter_version != plan.adapter_version
            or row.selector_version != plan.selector_version
            or row.field_contract_fingerprint is None
            or not compare_digest(
                row.field_contract_fingerprint,
                reusable_field_contract_fingerprint(
                    field,
                    adapter_name=plan.adapter_name,
                    adapter_version=plan.adapter_version,
                    selector_version=plan.selector_version,
                ),
            )
            or row.policy_version != plan.answer_policy_version
            or row.evidence_source != "operator_confirmation"
            or _json(recorded_answer) != _json(decision.value)
        ):
            raise FormPlanPersistenceError(ReasonCode.FORM_CHANGED.value)


def persist_inspected_form_plan(
    db,
    *,
    application: Application,
    plan: FormPlanV1,
    observed_at: datetime | None = None,
) -> FormPlan:
    """Persist one exact observation after the caller re-locks the application.

    The browser can spend time navigating without holding database locks.  This
    boundary therefore rechecks every private identity and adapter version
    before invalidating an older plan or writing the new immutable snapshot.
    """

    if application.job is None:
        raise FormPlanPersistenceError("JOB_NOT_FOUND")
    if plan.application_id != application.id:
        raise FormPlanPersistenceError("APPLICATION_CHANGED")
    if plan.application_revision != int(application.revision or 1):
        raise FormPlanPersistenceError("APPLICATION_REVISION_CHANGED")
    if not application.selected_cv_id or not application.selected_cv_hash:
        raise FormPlanPersistenceError("SELECTED_CV_UNAVAILABLE")
    if (
        plan.selected_cv_id != application.selected_cv_id
        or not compare_digest(plan.selected_cv_hash, cast(str, application.selected_cv_hash))
        or plan.profile_version != application.profile_version
    ):
        raise FormPlanPersistenceError("FORM_CHANGED")

    descriptor = adapter_for_url(
        application.job.apply_url or application.job.source_url or "",
    )
    if (
        descriptor is None
        or descriptor.platform != plan.adapter_name
        or descriptor.adapter_version != plan.adapter_version
        or descriptor.selector_version != plan.selector_version
        or descriptor.execution_contract_version is None
    ):
        raise FormPlanPersistenceError("ADAPTER_VERSION_CHANGED")

    now = observed_at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise FormPlanPersistenceError("FORM_PLAN_TIME_INVALID")
    if plan.is_expired(now.astimezone(UTC)):
        raise FormPlanPersistenceError("FORM_PLAN_EXPIRED")

    _validate_reusable_answer_evidence(db, plan)

    duplicate = (
        db.query(FormPlan)
        .filter(
            FormPlan.application_id == application.id,
            FormPlan.plan_id == str(plan.plan_id),
        )
        .one_or_none()
    )
    if duplicate is not None:
        return duplicate

    invalidated_at = now.astimezone(UTC).replace(tzinfo=None)
    preparation_invalidated = bool(
        application.prepared_revision is not None
        or application.approved_at is not None
        or application.approval_source is not None
    )
    # Preparation is bound to the exact latest immutable plan.  A fresh
    # observation can have the same application revision while a form, selector,
    # or attachment changed, so atomically revoke preparation before storing it.
    application.prepared_revision = None
    application.approved_at = None
    application.approval_source = None
    for previous in (
        db.query(FormPlan)
        .filter(
            FormPlan.application_id == application.id,
            FormPlan.invalidated_at.is_(None),
        )
        .all()
    ):
        previous.invalidated_at = invalidated_at
        previous.invalidation_reason = "FORM_REINSPECTED"

    row = FormPlan(
        plan_id=str(plan.plan_id),
        application_id=application.id,
        application_revision=plan.application_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        fingerprint=plan.form_fingerprint,
        selected_cv_id=plan.selected_cv_id,
        selected_cv_hash=plan.selected_cv_hash,
        attached_cv_id=plan.attached_cv_id,
        attached_cv_hash=plan.attached_cv_hash,
        attachment_verified=plan.attachment_verified,
        attachment_verification_source=(
            ATTACHMENT_VERIFICATION_SOURCE if plan.attachment_verified else None
        ),
        attachment_verified_at=(_naive_utc(plan.created_at) if plan.attachment_verified else None),
        profile_version=plan.profile_version,
        fields_json=_json([item.model_dump(mode="json") for item in plan.fields]),
        disclosures_json=_json([item.model_dump(mode="json") for item in plan.disclosures]),
        decisions_json=_json([item.model_dump(mode="json") for item in plan.decisions]),
        blockers_json=_json([item.value for item in plan.blockers]),
        locale=plan.locale,
        answer_policy_version=plan.answer_policy_version,
        llm_prompt_version=plan.llm_prompt_version,
        llm_model_provider=plan.llm_model_provider,
        llm_model_name=plan.llm_model_name,
        llm_model_digest=plan.llm_model_digest,
        session_verified_at=_naive_utc(plan.session_verified_at),
        created_at=_naive_utc(plan.created_at),
        expires_at=_naive_utc(plan.expires_at),
    )
    db.add(row)
    db.flush()
    record_form_plan_metrics(
        db,
        plan=plan,
        occurred_at=_naive_utc(plan.created_at),
    )
    record_application_event(
        db,
        application_id=application.id,
        event_type="form_plan_inspected",
        actor="operator",
        details={
            "platform": plan.adapter_name,
            "reason_code": (
                sorted(item.value for item in plan.blockers)[0]
                if plan.blockers
                else "FORM_PLAN_READY"
            ),
            "external_action_queued": False,
            "attachment_verified": plan.attachment_verified,
            "attachment_verification_source": (
                ATTACHMENT_VERIFICATION_SOURCE if plan.attachment_verified else None
            ),
            "preparation_invalidated": preparation_invalidated,
        },
    )
    return row
