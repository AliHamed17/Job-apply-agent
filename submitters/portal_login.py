"""Fail-closed compatibility adapter for legacy career-portal routing."""

from __future__ import annotations

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.platforms import detect_platform
from submitters.workday import WorkdaySubmitter


class PortalLoginSubmitter(BaseSubmitter):
    """Delegate Workday safely; never invent success for unsupported portals."""

    platform_name = "portal_login"

    def __init__(self, db=None):
        self.db = db

    def can_submit(self, job: JobData) -> bool:
        url = job.apply_url or job.source_url or ""
        return detect_platform(url) in {"workday", "icims"} or any(
            marker in url.lower() for marker in ("taleo.net", "nvidia")
        )

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        url = job.apply_url or job.source_url or ""
        if detect_platform(url) == "workday":
            return await WorkdaySubmitter(db=self.db).submit(
                job,
                application,
                user_profile,
                resume_path,
            )
        return SubmissionResult(
            success=True,
            platform=self.platform_name,
            status="draft_only",
            error="NEEDS_REVIEW:PORTAL_ADAPTER_REQUIRED",
            reason_code="PORTAL_ADAPTER_REQUIRED",
        )
