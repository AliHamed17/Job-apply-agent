"""Authenticated local control surface for signed qualified autopilot."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from core.automation_policy import (
    AutomationGeography,
    PolicyAuthoritySource,
    QualifiedFormContractV1,
)
from core.automation_policy_service import (
    AutomationPolicyError,
    activate_auto_submit_policy,
    policy_usage_status,
    revoke_auto_submit_policy,
    set_automation_kill_switch,
)
from core.automation_readiness import current_automation_readiness
from core.config import get_settings
from core.operations import readiness_report
from db.session import get_db

router = APIRouter(prefix="/automation", tags=["automation"])


class QualifiedFormContractRequest(BaseModel):
    adapter_name: str = Field(
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
        max_length=64,
    )
    adapter_version: str = Field(
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
        max_length=32,
    )
    selector_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        max_length=64,
    )
    form_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActivateAutomationPolicyRequest(BaseModel):
    acknowledgement: Literal["ACTIVATE_QUALIFIED_AUTOPILOT"]
    role_families: list[str] = Field(min_length=1, max_length=64)
    geographies: list[AutomationGeography] = Field(
        default=[AutomationGeography.ISRAEL, AutomationGeography.WORLDWIDE_REMOTE],
        min_length=1,
        max_length=3,
    )
    minimum_fit_score: float = Field(default=85.0, ge=85.0, le=100.0)
    daily_limit: int = Field(default=25, ge=1, le=25)
    hourly_limit: int = Field(default=5, ge=1, le=5)
    company_limit: int = Field(default=2, ge=1, le=2)
    permitted_adapters: list[str] = Field(min_length=1, max_length=5)
    qualified_form_contracts: list[QualifiedFormContractRequest] = Field(
        min_length=1,
        max_length=100,
    )
    expires_in_days: int = Field(default=30, ge=1, le=30)


class RevokeAutomationPolicyRequest(BaseModel):
    acknowledgement: Literal["REVOKE_QUALIFIED_AUTOPILOT"]
    reason_code: str = Field(
        default="AUTOMATION_POLICY_REVOKED",
        pattern=r"^[A-Z][A-Z0-9_]{1,63}$",
    )


class KillSwitchRequest(BaseModel):
    active: bool
    acknowledgement: Literal["ACTIVATE_KILL_SWITCH", "CLEAR_KILL_SWITCH"]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")

    @model_validator(mode="after")
    def acknowledgement_matches_state(self) -> KillSwitchRequest:
        expected = "ACTIVATE_KILL_SWITCH" if self.active else "CLEAR_KILL_SWITCH"
        if self.acknowledgement != expected:
            raise ValueError("kill-switch acknowledgement does not match state")
        return self


def _require_local_operator(request: Request) -> None:
    settings = get_settings()
    if not settings.operator_auth_configured:
        raise HTTPException(
            status_code=503,
            detail={"code": "OPERATOR_AUTH_REQUIRED"},
        )
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "OPERATOR_AUTH_REQUIRED"})
    supplied = header.removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, settings.secret_key):
        raise HTTPException(status_code=403, detail={"code": "OPERATOR_AUTH_REQUIRED"})


def _policy_error(exc: AutomationPolicyError) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": exc.reason_code})


@router.get("/policy")
def get_automation_policy(db: Session = Depends(get_db)) -> dict[str, object]:
    return policy_usage_status(db)


@router.post("/policy/activate", status_code=201)
def activate_automation_policy(
    body: ActivateAutomationPolicyRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_local_operator(request)
    settings = get_settings()
    try:
        activate_auto_submit_policy(
            db,
            settings=settings,
            role_families=body.role_families,
            geographies=body.geographies,
            permitted_adapters=body.permitted_adapters,
            qualified_form_contracts=[
                QualifiedFormContractV1.model_validate(item.model_dump())
                for item in body.qualified_form_contracts
            ],
            minimum_fit_score=body.minimum_fit_score,
            daily_limit=body.daily_limit,
            hourly_limit=body.hourly_limit,
            company_limit=body.company_limit,
            expires_in_days=body.expires_in_days,
        )
        db.commit()
    except AutomationPolicyError as exc:
        db.rollback()
        raise _policy_error(exc) from exc
    return policy_usage_status(db)


@router.post("/policy/revoke")
def revoke_automation_policy(
    body: RevokeAutomationPolicyRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_local_operator(request)
    try:
        revoke_auto_submit_policy(db, reason_code=body.reason_code)
        db.commit()
    except AutomationPolicyError as exc:
        db.rollback()
        raise _policy_error(exc) from exc
    return policy_usage_status(db)


@router.post("/kill-switch")
def update_automation_kill_switch(
    body: KillSwitchRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_local_operator(request)
    try:
        event, duplicate = set_automation_kill_switch(
            db,
            active=body.active,
            source=PolicyAuthoritySource.LOCAL_OPERATOR,
            reason_code=body.reason_code,
        )
        db.commit()
    except AutomationPolicyError as exc:
        db.rollback()
        raise _policy_error(exc) from exc
    return {
        "active": event.active,
        "revision": event.revision,
        "duplicate": duplicate,
        "source": event.source,
        "reason_code": event.reason_code,
    }


@router.get("/status")
def get_automation_status(db: Session = Depends(get_db)) -> dict[str, object]:
    settings = get_settings()
    report = readiness_report(settings)
    readiness = current_automation_readiness(
        settings=settings,
        dependency_report=report,
        db=db,
    )
    policy = policy_usage_status(db)
    submission = readiness.get("stages", {}).get("submission", {})
    submission_ready = bool(submission.get("ready") if isinstance(submission, Mapping) else False)
    return {
        "discovery_ready": bool(readiness.get("discovery_ready")),
        "preparation_ready": bool(readiness.get("preparation_ready")),
        "submission_ready": submission_ready,
        "qualified_autopilot_active": bool(policy.get("active")) and submission_ready,
        "legacy_auto_apply_is_prepare_only": True,
        "policy": policy,
    }


__all__ = ["router"]
