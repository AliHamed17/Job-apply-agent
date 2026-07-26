"""Submitter for Israeli job platforms (Drushim.co.il & Jobs.co.il)."""

from __future__ import annotations

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)


class DrushimSubmitter(BaseSubmitter):
    """Recognizes Israeli boards and creates a reviewable handoff."""

    platform_name = "drushim"

    def can_submit(self, job: JobData) -> bool:
        # JobData has no `platform` attribute — reading it here raised
        # AttributeError for any job whose URL didn't already match
        # drushim/jobs.co.il, crashing the whole submitter cascade in
        # worker/tasks.py for that job (every submitter's can_submit runs).
        url = (job.apply_url or job.source_url or "").lower()
        domains = ("drushim", "jobs.co.il", "alljobs.co.il", "jobmaster.co.il")
        return any(domain in url for domain in domains)

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        logger.info("drushim_manual_handoff", job_title=job.title, company=job.company)
        return SubmissionResult(
            success=True,
            platform="drushim",
            status="draft_only",
            error="NEEDS_REVIEW:DRUSHIM_BROWSER_ADAPTER_REQUIRED",
            reason_code="PORTAL_ADAPTER_REQUIRED",
        )
