"""Automatic reversible form inspection before qualified-autopilot evaluation."""

from __future__ import annotations

import inspect as python_inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from profile.cv_content_cache import (
    CVArtifactBindingError,
    get_selected_cv_artifact_by_id,
    require_current_selected_cv_artifact,
)
from profile.cv_routing import parse_required_skills
from profile.versioned_snapshot import ProfileSnapshotError, load_versioned_profile_snapshot
from uuid import uuid4

from celery import shared_task
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from core.application_audit import record_application_event
from core.application_mutations import (
    ApplicationMutationBlockedError,
    ApplicationMutationIntent,
    lock_application_for_mutation,
)
from core.automation_policy_service import (
    AutomationPolicyError,
    current_signed_policy,
    validate_automation_inspection_candidate,
)
from core.config import get_settings
from core.form_plan_persistence import FormPlanPersistenceError, persist_inspected_form_plan
from core.form_planning import AnswerPolicyV1
from core.utils import run_async
from db.models import Application, AutopilotInspectionRun, JobStatus
from db.session import get_session_factory
from jobs.models import JobData
from llm.contracts import is_qualified_material_identity
from llm.generation import GeneratedApplication
from worker.autopilot import AutopilotDispatchResult, dispatch_qualified_autopilot


class AutopilotInspectionError(ValueError):
    def __init__(self, reason_code: str):
        bounded = _bounded_reason(reason_code)
        super().__init__(bounded)
        self.reason_code = bounded


_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_INSPECTION_LEASE = timedelta(minutes=15)
_SCAN_BATCH_SIZE = 25


@dataclass(frozen=True, slots=True)
class AutopilotInspectionEnqueueResult:
    run_id: int | None
    state: str
    reason_code: str | None = None
    replayed: bool = False


def _bounded_reason(value: object) -> str:
    candidate = str(value or "")
    return candidate if _REASON_RE.fullmatch(candidate) else "FORM_INSPECTION_FAILED"


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def enqueue_qualified_autopilot_inspection(
    db,
    *,
    application_id: int,
    now: datetime | None = None,
) -> AutopilotInspectionEnqueueResult:
    """Persist one reversible inspection command for an exact signed policy."""

    timestamp = now or datetime.now(UTC)
    try:
        validate_automation_inspection_candidate(
            db,
            application_id=application_id,
            now=timestamp,
        )
        active = current_signed_policy(db, now=timestamp)
        if active is None:
            raise AutomationPolicyError("AUTOMATION_POLICY_NOT_ACTIVE")
        policy_record, _signed = active
        application = db.get(Application, application_id)
        if application is None:
            raise AutomationPolicyError("APPLICATION_NOT_FOUND")
        existing = (
            db.query(AutopilotInspectionRun)
            .filter(
                AutopilotInspectionRun.application_id == application.id,
                AutopilotInspectionRun.application_revision == application.revision,
                AutopilotInspectionRun.policy_revision_id == policy_record.id,
            )
            .one_or_none()
        )
        if existing is not None:
            db.rollback()
            return AutopilotInspectionEnqueueResult(
                run_id=existing.id,
                state=existing.state,
                reason_code=existing.reason_code,
                replayed=True,
            )
        row = AutopilotInspectionRun(
            application_id=application.id,
            application_revision=application.revision,
            policy_revision_id=policy_record.id,
            state="queued",
        )
        db.add(row)
        db.commit()
        return AutopilotInspectionEnqueueResult(run_id=row.id, state="queued")
    except AutomationPolicyError as exc:
        db.rollback()
        return AutopilotInspectionEnqueueResult(
            run_id=None,
            state="not_queued",
            reason_code=exc.reason_code,
        )
    except IntegrityError:
        db.rollback()
        application = db.get(Application, application_id)
        active = current_signed_policy(db, now=timestamp)
        if application is None or active is None:
            return AutopilotInspectionEnqueueResult(
                run_id=None,
                state="not_queued",
                reason_code="AUTOPILOT_INSPECTION_CONFLICT",
            )
        existing = (
            db.query(AutopilotInspectionRun)
            .filter(
                AutopilotInspectionRun.application_id == application.id,
                AutopilotInspectionRun.application_revision == application.revision,
                AutopilotInspectionRun.policy_revision_id == active[0].id,
            )
            .one_or_none()
        )
        if existing is None:
            return AutopilotInspectionEnqueueResult(
                run_id=None,
                state="not_queued",
                reason_code="AUTOPILOT_INSPECTION_CONFLICT",
            )
        return AutopilotInspectionEnqueueResult(
            run_id=existing.id,
            state=existing.state,
            reason_code=existing.reason_code,
            replayed=True,
        )


def wake_qualified_autopilot_inspection(run_id: int, *, eager: bool) -> bool:
    """Best-effort wake; the database queue remains authoritative."""

    try:
        if eager:
            execute_qualified_autopilot_inspection_task.apply(args=[run_id])
        else:
            execute_qualified_autopilot_inspection_task.delay(run_id)
    except Exception:
        return False
    return True


def enqueue_and_wake_qualified_autopilot_inspection(
    db,
    *,
    application_id: int,
    now: datetime | None = None,
) -> AutopilotInspectionEnqueueResult:
    result = enqueue_qualified_autopilot_inspection(
        db,
        application_id=application_id,
        now=now,
    )
    if result.run_id is not None and result.state in {"queued", "running"}:
        wake_qualified_autopilot_inspection(
            result.run_id,
            eager=get_settings().tasks_always_eager,
        )
    return result


def _answers(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _material_blockers(value: str | None) -> list[object]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return ["MATERIAL_AUDIT_INVALID"]
    return parsed if isinstance(parsed, list) else ["MATERIAL_AUDIT_INVALID"]


def _mark_quarantined(db, *, application_id: int, reason_code: str) -> None:
    db.rollback()
    try:
        locked = lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=ApplicationMutationIntent.CONTENT,
        )
    except ApplicationMutationBlockedError:
        db.rollback()
        return
    if locked is None:
        db.rollback()
        return
    locked.application.needs_review_reason = reason_code[:64]
    record_application_event(
        db,
        application_id,
        "qualified_autopilot_quarantined",
        actor="qualified_autopilot",
        details={
            "reason_code": reason_code[:64],
            "external_action_queued": False,
        },
    )
    db.commit()


def _finalize_autopilot_dispatch_result(
    db,
    *,
    application_id: int,
    result: AutopilotDispatchResult,
) -> dict[str, object]:
    """Persist dispatch quarantine state before returning the worker result."""

    reason_code = result.reason_code
    if result.state == "quarantined":
        reason_code = _bounded_reason(reason_code)
        _mark_quarantined(
            db,
            application_id=application_id,
            reason_code=reason_code,
        )
    return {
        "state": result.state,
        "reason_code": reason_code,
        "policy_decision_id": result.policy_decision_id,
        "attempt_id": result.attempt_id,
        "command_id": result.command_id,
        "replayed": result.replayed,
    }


def inspect_and_dispatch_qualified_autopilot(application_id: int) -> dict[str, object]:
    """Inspect outside locks, persist exact plan, then evaluate signed authority."""

    settings = get_settings()
    db = get_session_factory()()
    try:
        try:
            locked = lock_application_for_mutation(
                db,
                application_id=application_id,
                intent=ApplicationMutationIntent.CONTENT,
            )
        except ApplicationMutationBlockedError as exc:
            db.rollback()
            return {"state": "quarantined", "reason_code": exc.reason_code}
        if locked is None or locked.job is None:
            db.rollback()
            return {"state": "quarantined", "reason_code": "APPLICATION_NOT_FOUND"}
        app = locked.application
        job = locked.job
        validate_automation_inspection_candidate(
            db,
            application_id=application_id,
        )
        if app.status != JobStatus.DRAFT or job.status != JobStatus.DRAFT:
            raise AutopilotInspectionError("APPLICATION_NOT_ELIGIBLE")
        blockers = _material_blockers(app.material_blockers_json)
        if (
            app.material_eligible is not True
            or blockers
            or not app.selected_cv_id
            or not app.selected_cv_hash
            or not isinstance(app.profile_version, int)
            or app.profile_version < 1
            or not is_qualified_material_identity(
                provider=app.material_model_provider,
                model=app.material_model_name,
                local=True,
                digest=app.material_model_digest,
                prompt_version=app.material_prompt_version,
            )
        ):
            raise AutopilotInspectionError("MATERIAL_NOT_ELIGIBLE")
        selected = get_selected_cv_artifact_by_id(
            app.selected_cv_id,
            cv_routing_path=settings.cv_routing_path,
            cv_directory=settings.cv_directory,
        )
        if selected is None:
            raise AutopilotInspectionError("SELECTED_CV_UNAVAILABLE")
        selected = require_current_selected_cv_artifact(
            selected,
            expected_sha256=app.selected_cv_hash,
        )
        snapshot = load_versioned_profile_snapshot(db, version=app.profile_version)
        application_revision = int(app.revision or 1)
        selected_cv_id = app.selected_cv_id
        selected_cv_hash = app.selected_cv_hash
        profile_version = app.profile_version
        job_data = JobData(
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
        generated = GeneratedApplication(
            cover_letter=app.cover_letter or "",
            recruiter_message=app.recruiter_message or "",
            qa_answers=_answers(app.qa_answers),
            cv_sha256=selected_cv_hash,
            profile_version=profile_version,
        )
        from submitters.registry import get_two_phase_registry

        inspector = get_two_phase_registry().get_inspector(job_data)
        if inspector is None:
            raise AutopilotInspectionError("ADAPTER_NOT_QUALIFIED")
        profile_payload = snapshot.profile.model_dump(mode="python")
        resume_path = selected.resolved_path
        db.rollback()

        inspect_kwargs: dict[str, object] = {
            "application_id": application_id,
            "application_revision": application_revision,
            "job": job_data,
            "application": generated,
            "user_profile": profile_payload,
            "resume_path": resume_path,
            "selected_cv_id": selected_cv_id,
        }
        try:
            parameters: Mapping[str, python_inspect.Parameter] = python_inspect.signature(
                inspector.inspect
            ).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "answer_policy" in parameters:
            inspect_kwargs["answer_policy"] = AnswerPolicyV1(db=db)
        try:
            domain_plan = run_async(inspector.inspect(**inspect_kwargs))
        except Exception as exc:
            reason = getattr(exc, "reason_code", None)
            reason_code = _bounded_reason(getattr(reason, "value", reason))
            raise AutopilotInspectionError(reason_code) from exc

        db.rollback()
        try:
            locked = lock_application_for_mutation(
                db,
                application_id=application_id,
                intent=ApplicationMutationIntent.CONTENT,
                expected_revision=application_revision,
            )
        except ApplicationMutationBlockedError as exc:
            raise AutopilotInspectionError(exc.reason_code) from exc
        if locked is None:
            raise AutopilotInspectionError("APPLICATION_NOT_FOUND")
        app = locked.application
        if (
            app.selected_cv_id != selected_cv_id
            or app.selected_cv_hash != selected_cv_hash
            or app.profile_version != profile_version
        ):
            raise AutopilotInspectionError("FORM_CHANGED")
        try:
            plan = persist_inspected_form_plan(
                db,
                application=app,
                plan=domain_plan,
            )
        except FormPlanPersistenceError as exc:
            raise AutopilotInspectionError(exc.reason_code) from exc
        db.commit()
        result = dispatch_qualified_autopilot(
            db,
            application_id=application_id,
            form_plan_id=plan.id,
        )
        return _finalize_autopilot_dispatch_result(
            db,
            application_id=application_id,
            result=result,
        )
    except (
        AutopilotInspectionError,
        AutomationPolicyError,
        CVArtifactBindingError,
        ProfileSnapshotError,
    ) as exc:
        reason_code = _bounded_reason(getattr(exc, "reason_code", None) or str(exc))
        _mark_quarantined(
            db,
            application_id=application_id,
            reason_code=reason_code,
        )
        return {"state": "quarantined", "reason_code": reason_code}
    finally:
        db.close()


def _claim_inspection_run(
    db,
    *,
    run_id: int,
    now: datetime,
) -> tuple[int, str] | None:
    query = db.query(AutopilotInspectionRun).filter(AutopilotInspectionRun.id == run_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    row = query.populate_existing().one_or_none()
    if row is None or row.state == "finished":
        db.rollback()
        return None
    timestamp = _naive(now)
    if row.state == "running" and row.lease_expires_at is not None:
        if row.lease_expires_at > timestamp:
            db.rollback()
            return None
    try:
        validate_automation_inspection_candidate(
            db,
            application_id=row.application_id,
            now=now,
        )
        active = current_signed_policy(db, now=now)
        if active is None or active[0].id != row.policy_revision_id:
            raise AutomationPolicyError("AUTOMATION_POLICY_CHANGED")
        application = db.get(Application, row.application_id)
        if application is None or application.revision != row.application_revision:
            raise AutomationPolicyError("APPLICATION_REVISION_CHANGED")
    except AutomationPolicyError as exc:
        row.state = "finished"
        row.claimed_at = row.claimed_at or timestamp
        row.lease_expires_at = None
        row.claim_token = None
        row.finished_at = timestamp
        row.reason_code = exc.reason_code
        db.commit()
        return None
    claim_token = str(uuid4())
    row.state = "running"
    row.claimed_at = timestamp
    row.lease_expires_at = timestamp + _INSPECTION_LEASE
    row.claim_token = claim_token
    row.finished_at = None
    row.reason_code = None
    application_id = row.application_id
    db.commit()
    return application_id, claim_token


def _finish_inspection_run(
    db,
    *,
    run_id: int,
    claim_token: str,
    reason_code: str,
    now: datetime,
) -> bool:
    query = db.query(AutopilotInspectionRun).filter(AutopilotInspectionRun.id == run_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    row = query.populate_existing().one_or_none()
    if row is None or row.state != "running" or row.claim_token != claim_token:
        db.rollback()
        return False
    row.state = "finished"
    row.reason_code = _bounded_reason(reason_code)
    row.lease_expires_at = None
    row.claim_token = None
    row.finished_at = _naive(now)
    db.commit()
    return True


def execute_qualified_autopilot_inspection(run_id: int) -> dict[str, object]:
    """Claim one reversible inspection lease, execute it, and close the lease."""

    db = get_session_factory()()
    now = datetime.now(UTC)
    try:
        claimed = _claim_inspection_run(db, run_id=run_id, now=now)
    finally:
        db.close()
    if claimed is None:
        return {"state": "not_claimed", "run_id": run_id}
    application_id, claim_token = claimed
    try:
        result = inspect_and_dispatch_qualified_autopilot(application_id)
    except Exception:
        result = {"state": "quarantined", "reason_code": "FORM_INSPECTION_FAILED"}
    terminal_reason = (
        "COMMAND_QUEUED"
        if result.get("state") == "queued"
        else _bounded_reason(result.get("reason_code"))
    )
    finish_db = get_session_factory()()
    try:
        finished = _finish_inspection_run(
            finish_db,
            run_id=run_id,
            claim_token=claim_token,
            reason_code=terminal_reason,
            now=datetime.now(UTC),
        )
    finally:
        finish_db.close()
    return {**result, "run_id": run_id, "lease_finished": finished}


def scan_qualified_autopilot_inspections(*, batch_size: int = _SCAN_BATCH_SIZE) -> dict[str, int]:
    """Recover queued/stale inspections and enqueue new exact candidates."""

    bounded_size = max(1, min(int(batch_size), _SCAN_BATCH_SIZE))
    now = datetime.now(UTC)
    timestamp = _naive(now)
    db = get_session_factory()()
    try:
        active = current_signed_policy(db, now=now)
        if active is None:
            db.rollback()
            return {"queued": 0, "woken": 0}
        policy_record, _signed = active
        recoverable_ids = [
            row_id
            for (row_id,) in (
                db.query(AutopilotInspectionRun.id)
                .filter(
                    AutopilotInspectionRun.policy_revision_id == policy_record.id,
                    or_(
                        AutopilotInspectionRun.state == "queued",
                        and_(
                            AutopilotInspectionRun.state == "running",
                            AutopilotInspectionRun.lease_expires_at <= timestamp,
                        ),
                    ),
                )
                .order_by(AutopilotInspectionRun.created_at.asc())
                .limit(bounded_size)
                .all()
            )
        ]
        remaining = bounded_size - len(recoverable_ids)
        new_application_ids: list[int] = []
        if remaining > 0:
            joined = and_(
                AutopilotInspectionRun.application_id == Application.id,
                AutopilotInspectionRun.application_revision == Application.revision,
                AutopilotInspectionRun.policy_revision_id == policy_record.id,
            )
            new_application_ids = [
                application_id
                for (application_id,) in (
                    db.query(Application.id)
                    .outerjoin(AutopilotInspectionRun, joined)
                    .filter(
                        Application.status == JobStatus.DRAFT,
                        Application.material_eligible.is_(True),
                        Application.needs_review_reason.is_(None),
                        Application.job_fit_decision_id.is_not(None),
                        AutopilotInspectionRun.id.is_(None),
                    )
                    .order_by(Application.id.asc())
                    .limit(remaining)
                    .all()
                )
            ]
        db.rollback()
        run_ids = list(recoverable_ids)
        queued = 0
        for application_id in new_application_ids:
            result = enqueue_qualified_autopilot_inspection(
                db,
                application_id=application_id,
                now=now,
            )
            if result.run_id is not None:
                run_ids.append(result.run_id)
                queued += int(not result.replayed)
        eager = get_settings().tasks_always_eager
        woken = sum(
            1
            for run_id in dict.fromkeys(run_ids)
            if wake_qualified_autopilot_inspection(run_id, eager=eager)
        )
        return {"queued": queued, "woken": woken}
    finally:
        db.close()


@shared_task(name="worker.autopilot.inspect_and_evaluate", bind=True, max_retries=0)
def inspect_and_dispatch_qualified_autopilot_task(
    self,
    application_id: int,
) -> dict[str, object]:
    del self
    return inspect_and_dispatch_qualified_autopilot(application_id)


@shared_task(name="worker.autopilot.execute_inspection", bind=True, max_retries=0)
def execute_qualified_autopilot_inspection_task(
    self,
    run_id: int,
) -> dict[str, object]:
    del self
    return execute_qualified_autopilot_inspection(run_id)


@shared_task(name="worker.autopilot.scan_inspections", bind=True, max_retries=0)
def scan_qualified_autopilot_inspections_task(self) -> dict[str, int]:
    del self
    return scan_qualified_autopilot_inspections()


__all__ = [
    "AutopilotInspectionError",
    "AutopilotInspectionEnqueueResult",
    "enqueue_and_wake_qualified_autopilot_inspection",
    "enqueue_qualified_autopilot_inspection",
    "execute_qualified_autopilot_inspection",
    "execute_qualified_autopilot_inspection_task",
    "inspect_and_dispatch_qualified_autopilot",
    "inspect_and_dispatch_qualified_autopilot_task",
    "scan_qualified_autopilot_inspections",
    "scan_qualified_autopilot_inspections_task",
    "wake_qualified_autopilot_inspection",
]
