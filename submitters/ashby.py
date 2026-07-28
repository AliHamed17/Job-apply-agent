"""Legacy Ashby compatibility shim, permanently preparation-only.

The former implementation posted to an undocumented
``posting-public/application/create`` route and treated HTTP 200/201 as proof
of submission. That transport is quarantined. Versioned browser inspection is
implemented separately by :mod:`submitters.ashby_v1`.
"""

from __future__ import annotations

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.ashby_identity import AshbyIdentityError, parse_ashby_candidate_url
from submitters.base import BaseSubmitter, SubmissionResult


class AshbySubmitter(BaseSubmitter):
    """Compatibility-only shim that can never perform an external action."""

    platform_name = "ashby"

    def can_submit(self, job: JobData) -> bool:
        try:
            parse_ashby_candidate_url(job.apply_url or job.source_url or "")
        except AshbyIdentityError:
            return False
        return True

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        del job, application, user_profile, resume_path
        return SubmissionResult(
            success=False,
            platform=self.platform_name,
            status="draft_only",
            error="NEEDS_REVIEW:ASHBY_VERSIONED_BROWSER_REQUIRED",
            reason_code="ADAPTER_NOT_QUALIFIED",
        )

    @staticmethod
    def _extract_posting_id(url: str) -> str | None:
        try:
            return parse_ashby_candidate_url(url).identity.posting_id
        except AshbyIdentityError:
            return None
