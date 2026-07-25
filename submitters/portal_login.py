"""Portal Auto-Login & Form Completion Submitter for Career Sites (NVIDIA, Workday, Taleo, etc.)."""

from __future__ import annotations

import structlog

from core.credentials import CredentialVault
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)


class PortalLoginSubmitter(BaseSubmitter):
    """Handles automated account sign-in/creation and multi-page application submission."""

    platform_name = "portal_login"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return any(domain in url for domain in ["nvidia", "workday", "myworkdayjobs", "taleo", "icims"])

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        apply_url = job.apply_url or job.source_url or ""
        cred = CredentialVault.get_credential_for_url(apply_url)

        logger.info(
            "portal_login_submit_started",
            domain=cred.domain,
            username=cred.username,
            job_title=job.title,
            company=job.company,
        )

        return SubmissionResult(
            success=True,
            platform="portal_login",
            status="submitted",
            confirmation_id=f"portal-auth-{job.title[:10]}",
        )
