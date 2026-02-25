"""Greenhouse Harvest API submitter.

Uses the official Greenhouse Harvest API to submit applications.
Requires a Greenhouse API key (set in environment).
Docs: https://developers.greenhouse.io/harvest.html
"""

from __future__ import annotations

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)


class GreenhouseSubmitter(BaseSubmitter):
    """Submit applications via Greenhouse Harvest API."""

    platform_name = "greenhouse"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.base_url = "https://harvest.greenhouse.io/v1"

    def can_submit(self, job: JobData) -> bool:
        """Check if this is a Greenhouse job."""
        url = (job.apply_url or job.source_url).lower()
        return "greenhouse.io" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Submit application via Greenhouse API.

        Note: Greenhouse Harvest API requires the job_id and candidate data.
        This implementation submits the candidate to Greenhouse.
        """
        if not self.api_key:
            logger.warning("greenhouse_no_api_key")
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error="Greenhouse API key not configured",
            )

        try:
            # Extract job ID from URL
            job_id = self._extract_job_id(job.apply_url or job.source_url)
            if not job_id:
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error="Could not extract Greenhouse job ID from URL",
                )

            personal = user_profile.get("personal", {})

            # Build candidate payload
            candidate_data = {
                "first_name": personal.get("name", "").split()[0] if personal.get("name") else "",
                "last_name": " ".join(personal.get("name", "").split()[1:]) if personal.get("name") else "",
                "email_addresses": [{"value": personal.get("email", ""), "type": "personal"}],
                "phone_numbers": [{"value": personal.get("phone", ""), "type": "mobile"}],
                "applications": [{"job_id": int(job_id)}],
            }

            # Add cover letter if available
            if application.cover_letter:
                candidate_data["applications"][0]["cover_letter"] = application.cover_letter

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.base_url}/candidates",
                    json=candidate_data,
                    headers={
                        "Authorization": f"Basic {self.api_key}",
                        "Content-Type": "application/json",
                        "On-Behalf-Of": personal.get("email", ""),
                    },
                )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    return SubmissionResult(
                        success=True,
                        platform=self.platform_name,
                        status="submitted",
                        confirmation_id=str(data.get("id", "")),
                    )
                else:
                    return SubmissionResult(
                        success=False,
                        platform=self.platform_name,
                        status="failed",
                        error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                    )

        except Exception as exc:
            logger.error("greenhouse_submit_error", error=str(exc))
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=str(exc),
            )

    @staticmethod
    def _extract_job_id(url: str) -> str | None:
        """Extract the Greenhouse job ID from a URL."""
        import re
        # Pattern: /jobs/12345 or /jobs/12345-...
        match = re.search(r"/jobs/(\d+)", url)
        return match.group(1) if match else None
