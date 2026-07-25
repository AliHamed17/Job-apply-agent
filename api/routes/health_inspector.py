"""Submitters health inspector API router."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from submitters.inspector import inspect_submitter_health

router = APIRouter(tags=["submitters"])


class SubmitterHealthResponse(BaseModel):
    playwright_installed: bool
    live_auto_apply_active: bool
    auto_apply_threshold: float
    cv_alignment_enabled: bool
    registered_platforms: list[str] = Field(default_factory=list)


@router.get("/submitters/health", response_model=SubmitterHealthResponse)
async def get_submitter_health():
    """Return browser automation readiness and active platform submitters status."""
    report = inspect_submitter_health()
    return SubmitterHealthResponse(
        playwright_installed=report.playwright_installed,
        live_auto_apply_active=report.live_auto_apply_active,
        auto_apply_threshold=report.auto_apply_threshold,
        cv_alignment_enabled=report.cv_alignment_enabled,
        registered_platforms=report.registered_platforms,
    )
