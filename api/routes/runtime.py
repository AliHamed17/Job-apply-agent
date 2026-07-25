"""Redacted runtime identity and submission capability endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from core.config import get_settings
from core.operations import readiness_report
from core.runtime_identity import build_runtime_capabilities

router = APIRouter(tags=["runtime"])


class ReleaseCapabilities(BaseModel):
    build_sha: str
    ui_asset_digest: str
    protocol_version: str
    boot_id: str
    started_at: str


class ModeCapabilities(BaseModel):
    name: str
    dry_run: bool
    draft_only: bool
    live_submit_enabled: bool


class ReadinessCapabilities(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, bool]


class SubmissionCapabilities(BaseModel):
    allowed: bool
    reasons: list[str]


class WorkerCapabilities(BaseModel):
    build_sha: str | None
    protocol_version: str | None
    compatible: bool


class RuntimeCapabilitiesResponse(BaseModel):
    release: ReleaseCapabilities
    mode: ModeCapabilities
    readiness: ReadinessCapabilities
    submission: SubmissionCapabilities
    worker: WorkerCapabilities


@router.get("/runtime/capabilities", response_model=RuntimeCapabilitiesResponse)
async def get_runtime_capabilities() -> RuntimeCapabilitiesResponse:
    """Return only bounded process, dependency, and final-send guard data."""

    settings = get_settings()
    report = readiness_report(settings)
    return RuntimeCapabilitiesResponse.model_validate(build_runtime_capabilities(settings, report))
