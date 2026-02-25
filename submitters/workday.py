"""Workday submitter — always DRAFT_ONLY.

Per HANDOVER_PLAN.md Phase 9:
  "Investigate Workday's candidate APIs (requires cautious handling due to
   authentication variability)."

Workday does not provide a public, unauthenticated candidate submission API.
Submissions require:
  - Tenant-specific OAuth 2.0 / SSO tokens, OR
  - Form-based submission through an authenticated browser session.

Because we cannot safely obtain those credentials programmatically without
risking account compromise or ToS violations, this submitter is intentionally
DRAFT_ONLY.  It detects Workday URLs and logs a clear reason so the operator
knows a manual application is required.

Future work: If a company shares a Workday Recruiting API key via their own
integration, this can be upgraded to a real submitter for that tenant only.
"""

from __future__ import annotations

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_WORKDAY_KEYWORDS = ("myworkday.com", "myworkdayjobs.com", "workday.com/en-US")


class WorkdaySubmitter(BaseSubmitter):
    """Workday-aware submitter that always records a draft.

    Acknowledges the Workday platform and surfaces a human-action note
    without attempting automated submission (auth variability risk).
    """

    platform_name = "workday"

    def can_submit(self, job: JobData) -> bool:
        """Return True for any Workday-hosted job URL."""
        url = (job.apply_url or job.source_url or "").lower()
        return any(kw in url for kw in _WORKDAY_KEYWORDS)

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Record a draft and instruct the operator to apply manually.

        Workday submission requires authenticated browser interaction or a
        tenant-specific API key.  We never attempt to bypass authentication.
        """
        logger.info(
            "workday_draft_recorded",
            job=job.title,
            company=job.company,
            apply_url=job.apply_url,
            reason=(
                "Workday requires SSO/OAuth or browser-based form submission. "
                "Please apply manually using the generated cover letter."
            ),
        )

        return SubmissionResult(
            success=True,
            platform=self.platform_name,
            status="draft_only",
            error=(
                "Workday automated submission is not supported due to "
                "authentication variability. Apply manually at: "
                f"{job.apply_url or job.source_url}"
            ),
        )
