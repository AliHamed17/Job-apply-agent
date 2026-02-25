"""Jobvite submitter — uses the public Apply API / form POST.

Jobvite exposes a REST API for public job applications.
No API key required for submitting to public postings.

URL patterns:
  jobs.jobvite.com/{company}/job/{job-id}
  hire.jobvite.com/{company}/jobs/{job-id}
  {company}.jobs.jobvite.com/jobs/{job-id}
"""

from __future__ import annotations

import re

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_JOBVITE_RE = re.compile(r"jobvite\.com", re.IGNORECASE)

# Extract company slug and job ID
_JOB_PARSE_RE = re.compile(
    r"jobvite\.com/([^/]+)/(?:job|jobs)/([^/?#]+)", re.IGNORECASE
)

_API_BASE = "https://api.jobvite.com/api/v2"


class JobviteSubmitter(BaseSubmitter):
    """Submit applications via Jobvite public Apply API."""

    platform_name = "jobvite"

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "jobvite.com" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        url = job.apply_url or job.source_url or ""
        company, job_id = self._parse_url(url)
        if not job_id:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=f"Cannot extract Jobvite job ID from: {url}",
            )

        # Jobvite API requires auth tokens for most operations;
        # fall back to the public form-based submission which works without auth.
        return await self._submit_form(url, job_id, application, user_profile)

    async def _submit_form(
        self,
        job_url: str,
        job_id: str,
        application: GeneratedApplication,
        user_profile: dict,
    ) -> SubmissionResult:
        """Submit via Jobvite's public application form endpoint."""
        personal = user_profile.get("personal", {})
        name_parts = (personal.get("name") or "").split(maxsplit=1)
        links = user_profile.get("links", {})

        # Jobvite uses a multi-part form submission
        form_data = {
            "jvtoken": job_id,
            "firstname": name_parts[0] if name_parts else "",
            "lastname": name_parts[1] if len(name_parts) > 1 else "",
            "email": personal.get("email", ""),
            "phone": personal.get("phone", ""),
            "location": personal.get("location", ""),
            "linkedin": links.get("linkedin", ""),
            "website": links.get("portfolio") or links.get("website", ""),
            "coverletter": application.cover_letter or "",
            "source": "JobBoard",
        }

        submit_url = f"https://jobs.jobvite.com/{job_id}/apply"

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)"},
            ) as client:
                resp = await client.post(submit_url, data=form_data)

            if self.detect_captcha(resp.text):
                logger.warning("jobvite_captcha_detected", url=submit_url)
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="draft_only",
                    error="CAPTCHA detected — manual submission required",
                )

            if resp.status_code in (200, 201, 302):
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="submitted",
                    confirmation_url=str(resp.url),
                )
            else:
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=f"HTTP {resp.status_code}",
                )

        except Exception as exc:
            logger.error("jobvite_submit_error", error=str(exc))
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=str(exc),
            )

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = _JOB_PARSE_RE.search(url)
        if m:
            return m.group(1), m.group(2)
        return "", ""
