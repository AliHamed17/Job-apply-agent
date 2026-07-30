"""Redacted runtime identity and submission capability endpoint."""

from __future__ import annotations

from collections.abc import Mapping
from profile.loader import get_profile
from profile.models import UserProfile
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from core.automation_readiness import build_automation_readiness
from core.config import get_settings
from core.operations import readiness_report
from core.runtime_identity import build_runtime_capabilities
from db.models import UserProfileVersion
from db.session import get_session_factory

router = APIRouter(tags=["runtime"])


class ReleaseCapabilities(BaseModel):
    build_sha: str
    ui_asset_digest: str
    source_digest: str
    release_id: str
    protocol_version: str
    boot_id: str
    started_at: str


class ModeCapabilities(BaseModel):
    name: str
    dry_run: bool
    draft_only: bool
    live_submit_enabled: bool
    discovery_enabled: bool
    auto_prepare_enabled: bool
    qualified_autopilot_enabled: bool
    legacy_auto_apply_alias: bool


class ReadinessCapabilities(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, bool]


class SubmissionCapabilities(BaseModel):
    allowed: bool
    reasons: list[str]


class WorkerCapabilities(BaseModel):
    build_sha: str | None
    source_digest: str | None
    release_id: str | None
    protocol_version: str | None
    compatible: bool


class LLMCapabilities(BaseModel):
    provider: str
    model: str
    local: bool
    digest: str | None
    ollama_server_version: str | None = Field(
        default=None,
        max_length=64,
        pattern=(
            r"^[0-9]+(?:\.[0-9]+){1,3}"
            r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]{0,31})?$"
        ),
    )
    ready: bool
    reason_code: str | None


class AutomationStageCapabilities(BaseModel):
    ready: bool
    reason_codes: list[str]


class AutomationReadinessCapabilities(BaseModel):
    discovery_ready: bool
    preparation_ready: bool
    submission_ready: bool
    stages: dict[
        Literal["discovery", "preparation", "submission"],
        AutomationStageCapabilities,
    ]


class RuntimeCapabilitiesResponse(BaseModel):
    release: ReleaseCapabilities
    mode: ModeCapabilities
    readiness: ReadinessCapabilities
    automation: AutomationReadinessCapabilities
    submission: SubmissionCapabilities
    worker: WorkerCapabilities
    llm: LLMCapabilities | None = None


def _current_profile_context() -> tuple[UserProfile, int | None]:
    """Read local profile state without blocking the API event loop."""

    db = get_session_factory()()
    try:
        try:
            latest_profile = (
                db.query(UserProfileVersion.version)
                .order_by(UserProfileVersion.version.desc())
                .first()
            )
            profile_version = int(latest_profile[0]) if latest_profile is not None else None
        except Exception:
            db.rollback()
            profile_version = None
    finally:
        db.close()
    try:
        profile = get_profile()
    except (OSError, ValueError):
        profile = UserProfile()
    return profile, profile_version


@router.get(
    "/runtime/capabilities",
    response_model=RuntimeCapabilitiesResponse,
    response_model_exclude_none=True,
)
async def get_runtime_capabilities() -> RuntimeCapabilitiesResponse:
    """Return only bounded process, dependency, and final-send guard data."""

    settings = get_settings()
    report = await run_in_threadpool(readiness_report, settings)
    profile, profile_version = await run_in_threadpool(_current_profile_context)
    automation = build_automation_readiness(
        settings=settings,
        dependency_report=report,
        profile=profile,
        profile_version=profile_version,
    )
    capabilities = build_runtime_capabilities(
        settings,
        report,
        automation_readiness=automation,
    )
    checks = report.get("checks")
    llm = checks.get("llm") if isinstance(checks, Mapping) else None
    if isinstance(llm, Mapping):
        capabilities["readiness"]["checks"]["llm"] = bool(llm.get("ok"))
        capabilities["llm"] = {
            "provider": str(llm.get("provider") or "unknown")[:32],
            "model": str(llm.get("model") or "unknown")[:128],
            "local": bool(llm.get("local")),
            "digest": (str(llm["digest"])[:71] if isinstance(llm.get("digest"), str) else None),
            "ollama_server_version": (
                str(llm["ollama_server_version"])[:64]
                if isinstance(llm.get("ollama_server_version"), str)
                else None
            ),
            "ready": bool(llm.get("ok")),
            "reason_code": (
                str(llm["reason_code"])[:64] if isinstance(llm.get("reason_code"), str) else None
            ),
        }
    return RuntimeCapabilitiesResponse.model_validate(capabilities)
