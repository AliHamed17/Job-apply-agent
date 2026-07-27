"""Fail-closed compatibility shim for the retired Greenhouse submitter.

The historical implementation mixed a privileged Harvest API transport with an
unqualified browser fallback.  V4 submission execution is database-authoritative
and resolves only versioned two-phase adapters, so this legacy import remains
available solely for old broker messages, tests, and downstream imports.

This module deliberately performs no HTTP request, browser launch, credential
use, form fill, file read, or external action.
"""

from __future__ import annotations

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.greenhouse_identity import (
    GreenhouseIdentityError,
    parse_greenhouse_candidate_url,
)

_DISABLED_REASON = "ADAPTER_NOT_QUALIFIED"


def _disabled_result() -> SubmissionResult:
    """Return one bounded result without reflecting private caller inputs."""

    return SubmissionResult(
        success=False,
        platform="greenhouse",
        status="failed",
        error=_DISABLED_REASON,
        reason_code=_DISABLED_REASON,
        diagnostic_details={"external_action_started": False},
    )


class GreenhouseSubmitter(BaseSubmitter):
    """Backwards-compatible, network-incapable legacy adapter."""

    platform_name = "greenhouse"

    def __init__(self, api_key: str = "") -> None:
        # Accept the historical constructor signature, but never retain or use
        # the credential. Authorized employer API transport requires a separate
        # tenant-bound adapter and qualification program.
        del api_key

    def can_submit(self, job: JobData) -> bool:
        """Never advertise this retired one-step adapter as executable."""

        del job
        return False

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Refuse execution before inspecting any private application input."""

        del job, application, user_profile, resume_path
        return _disabled_result()

    async def _submit_via_browser(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Preserve the old method surface while refusing browser execution."""

        del job, application, user_profile, resume_path
        return _disabled_result()

    @staticmethod
    def _extract_job_id(url: str) -> str | None:
        """Retain the historical helper for exact hosted Greenhouse job URLs."""

        try:
            return parse_greenhouse_candidate_url(url).identity.job_token
        except GreenhouseIdentityError:
            return None
