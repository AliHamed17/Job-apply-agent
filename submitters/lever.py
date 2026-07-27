"""Inert compatibility shim for the quarantined legacy Lever submitter.

The former implementation mixed an unaudited API request with a browser
fallback and treated generic HTTP success as proof of submission. That path is
permanently disabled. New code must use the versioned two-phase browser
adapter, while authorized API support remains a distinct disabled capability.
"""

from __future__ import annotations

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.lever_identity import (
    LeverIdentityError,
    parse_lever_posting_identity,
)


class LeverSubmitter(BaseSubmitter):
    """Compatibility-only object that cannot perform an external action."""

    platform_name = "lever"

    def __init__(self, api_key: str = "") -> None:
        # Accept the historical argument without retaining credential material.
        del api_key

    def can_submit(self, job: JobData) -> bool:
        try:
            parse_lever_posting_identity(job.apply_url or job.source_url)
        except LeverIdentityError:
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
            error="LEVER_LEGACY_TRANSPORT_DISABLED",
            reason_code="ADAPTER_NOT_QUALIFIED",
        )

    @staticmethod
    def _extract_posting_id(url: str) -> str | None:
        try:
            return parse_lever_posting_identity(url).posting_id
        except LeverIdentityError:
            return None

    @staticmethod
    def _extract_company(url: str) -> str | None:
        try:
            return parse_lever_posting_identity(url).site
        except LeverIdentityError:
            return None
