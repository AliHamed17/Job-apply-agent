"""Fail-closed qualified-autopilot evaluation and command admission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from celery import shared_task
from sqlalchemy.orm import Session

from core.automation_policy_service import (
    AutomationPolicyError,
    evaluate_auto_submit_policy,
)
from core.config import Settings, get_settings
from core.submission_service import (
    ClientReleaseIdentity,
    DescriptorResolver,
    SessionChecker,
    SubmissionAdmissionError,
    SubmissionCommandRequest,
    create_submission_commands,
)
from db.models import ApplicationPolicyDecision, FormPlan
from db.session import get_session_factory
from submitters.platforms import adapter_for_url


@dataclass(frozen=True, slots=True)
class AutopilotDispatchResult:
    state: str
    reason_code: str | None
    policy_decision_id: int | None = None
    attempt_id: int | None = None
    command_id: int | None = None
    replayed: bool = False


def _client_release(capabilities) -> ClientReleaseIdentity:
    release = capabilities.get("release") if isinstance(capabilities, dict) else None
    if not isinstance(release, dict):
        raise AutomationPolicyError("RUNTIME_NOT_READY")
    values = {
        key: str(release.get(key) or "")
        for key in (
            "build_sha",
            "ui_asset_digest",
            "source_digest",
            "protocol_version",
            "boot_id",
        )
    }
    if any(not value for value in values.values()):
        raise AutomationPolicyError("RUNTIME_NOT_READY")
    return ClientReleaseIdentity(**values)


def dispatch_qualified_autopilot(
    db: Session,
    *,
    application_id: int,
    form_plan_id: int,
    settings: Settings | None = None,
    capabilities=None,
    descriptor_resolver: DescriptorResolver = adapter_for_url,
    session_checker: SessionChecker | None = None,
    now: datetime | None = None,
) -> AutopilotDispatchResult:
    """Reserve one policy decision and atomically create one durable command."""

    timestamp = now or datetime.now(UTC)
    try:
        decision = evaluate_auto_submit_policy(
            db,
            application_id=application_id,
            form_plan_id=form_plan_id,
            now=timestamp,
        )
    except AutomationPolicyError as exc:
        db.rollback()
        return AutopilotDispatchResult(state="quarantined", reason_code=exc.reason_code)
    if not decision.allowed:
        reasons = json.loads(decision.reason_codes_json)
        db.commit()
        return AutopilotDispatchResult(
            state="quarantined",
            reason_code=str(reasons[0]) if reasons else "AUTOMATION_DECISION_DENIED",
            policy_decision_id=decision.id,
        )

    resolved_settings = settings or get_settings()
    if capabilities is None:
        from core.submission_service import _runtime_capabilities

        capabilities = _runtime_capabilities(resolved_settings, db)
    plan = db.get(FormPlan, form_plan_id)
    if plan is None:
        db.rollback()
        return AutopilotDispatchResult(
            state="quarantined",
            reason_code="FORM_PLAN_NOT_FOUND",
        )
    request = SubmissionCommandRequest(
        application_id=application_id,
        client_idempotency_key=f"autopilot:{decision.decision_digest}",
        application_revision=decision.application_revision,
        form_plan_id=plan.plan_id,
        client_release=_client_release(capabilities),
        authority_expires_at=decision.authority_expires_at,
        authority_kind="qualified_autopilot",
        automation_policy_decision_id=decision.id,
    )
    try:
        create_kwargs = {
            "settings": resolved_settings,
            "capabilities": capabilities,
            "descriptor_resolver": descriptor_resolver,
            "now": (
                timestamp.astimezone(UTC).replace(tzinfo=None)
                if timestamp.tzinfo is not None
                else timestamp
            ),
        }
        if session_checker is not None:
            create_kwargs["session_checker"] = session_checker
        [created] = create_submission_commands(db, [request], **create_kwargs)
    except SubmissionAdmissionError as exc:
        db.rollback()
        return AutopilotDispatchResult(
            state="quarantined",
            reason_code=exc.reason_code,
            policy_decision_id=decision.id,
        )
    if not created.replayed:
        try:
            from worker.submission_commands import execute_submission_command_task

            execute_submission_command_task.delay(created.command_id)
        except Exception:
            pass
    return AutopilotDispatchResult(
        state="queued",
        reason_code=None,
        policy_decision_id=decision.id,
        attempt_id=created.attempt_id,
        command_id=created.command_id,
        replayed=created.replayed,
    )


@shared_task(name="worker.autopilot.evaluate", bind=True, max_retries=0)
def evaluate_qualified_autopilot_task(
    self,
    application_id: int,
    form_plan_id: int,
) -> dict[str, object]:
    del self
    db = get_session_factory()()
    try:
        result = dispatch_qualified_autopilot(
            db,
            application_id=application_id,
            form_plan_id=form_plan_id,
        )
        return {
            "state": result.state,
            "reason_code": result.reason_code,
            "policy_decision_id": result.policy_decision_id,
            "attempt_id": result.attempt_id,
            "command_id": result.command_id,
            "replayed": result.replayed,
        }
    finally:
        db.close()


def latest_policy_decision(
    db: Session,
    *,
    application_id: int,
) -> ApplicationPolicyDecision | None:
    return (
        db.query(ApplicationPolicyDecision)
        .filter(ApplicationPolicyDecision.application_id == application_id)
        .order_by(
            ApplicationPolicyDecision.evaluated_at.desc(),
            ApplicationPolicyDecision.id.desc(),
        )
        .first()
    )


__all__ = [
    "AutopilotDispatchResult",
    "dispatch_qualified_autopilot",
    "evaluate_qualified_autopilot_task",
    "latest_policy_decision",
]
