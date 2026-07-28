"""Inert compatibility shim for the retired SmartRecruiters one-step path.

The former implementation called a non-public/wrong candidate endpoint,
accepted any HTTP 2xx as submission truth, and silently switched to a browser
after API rejection. It is permanently quarantined. Versioned candidate
browser and protected OAuth API transports are separate modules.
"""

from __future__ import annotations

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.smartrecruiters_identity import (
    SmartRecruitersIdentityError,
    parse_smartrecruiters_candidate_identity,
)


class SmartRecruitersSubmitter(BaseSubmitter):
    """Compatibility-only object that cannot perform an external action."""

    platform_name = "smartrecruiters"

    def __init__(self, api_key: str = "") -> None:
        del api_key

    def can_submit(self, job: JobData) -> bool:
        try:
            parse_smartrecruiters_candidate_identity(
                job.apply_url or job.source_url,
            )
        except SmartRecruitersIdentityError:
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
            status="failed",
            error="SMARTRECRUITERS_LEGACY_TRANSPORT_DISABLED",
            reason_code="ADAPTER_NOT_QUALIFIED",
        )

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        """Compatibility helper returns company and public numeric ID only."""

        try:
            identity = parse_smartrecruiters_candidate_identity(url)
        except SmartRecruitersIdentityError:
            return "", ""
        return identity.company, identity.public_id
