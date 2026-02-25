"""SmartRecruiters submitter — uses the public Candidate API.

SmartRecruiters exposes a public candidate creation endpoint that doesn't
require an API key for jobs posted on smartrecruiters.com.

Docs: https://dev.smartrecruiters.com/customer-api/live-docs/application-api/
URL patterns:
  jobs.smartrecruiters.com/{company}/{posting-id}
  careers.{company}.com (custom domain, harder to detect)
"""

from __future__ import annotations

import re

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_SR_URL_RE = re.compile(r"smartrecruiters\.com", re.IGNORECASE)

# URL: jobs.smartrecruiters.com/{company-identifier}/{job-id}
_SR_PARSE_RE = re.compile(
    r"smartrecruiters\.com/([^/]+)/([^/?#]+)", re.IGNORECASE
)

_API_BASE = "https://api.smartrecruiters.com/v1"


class SmartRecruitersSubmitter(BaseSubmitter):
    """Submit applications via SmartRecruiters public Candidate API."""

    platform_name = "smartrecruiters"

    def __init__(self, api_key: str = ""):
        # Optional: company API key for higher rate limits / custom workflows
        self.api_key = api_key

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "smartrecruiters.com" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        url = job.apply_url or job.source_url or ""
        company_id, posting_id = self._parse_url(url)
        if not company_id or not posting_id:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=f"Cannot extract SmartRecruiters company/posting IDs from: {url}",
            )

        personal = user_profile.get("personal", {})
        name_parts = (personal.get("name") or "").split(maxsplit=1)
        links = user_profile.get("links", {})

        candidate = {
            "firstName": name_parts[0] if name_parts else "",
            "lastName": name_parts[1] if len(name_parts) > 1 else "",
            "email": personal.get("email", ""),
            "phoneNumber": personal.get("phone", ""),
            "location": {"country": personal.get("country", "GB")},
            "web": {
                "linkedIn": links.get("linkedin", ""),
                "portfolio": links.get("portfolio", "") or links.get("website", ""),
            },
            "tags": {
                "public": ["source:job-agent"],
            },
            "sourceDetails": {
                "sourceType": "DIRECT",
                "sourceSubType": "JOB_BOARD",
            },
        }

        # Cover letter goes as an attachment or in the notes
        if application.cover_letter:
            candidate["coverLetter"] = application.cover_letter

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-SmartToken"] = self.api_key

        endpoint = f"{_API_BASE}/companies/{company_id}/postings/{posting_id}/candidates"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json=candidate, headers=headers)

            if resp.status_code in (200, 201):
                data = resp.json()
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="submitted",
                    confirmation_id=str(data.get("id", "")),
                )
            elif resp.status_code == 401:
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error="SmartRecruiters requires a company API token. Set SMARTRECRUITERS_API_KEY.",
                )
            else:
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=f"HTTP {resp.status_code}: {resp.text[:400]}",
                )

        except Exception as exc:
            logger.error("smartrecruiters_submit_error", error=str(exc))
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=str(exc),
            )

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = _SR_PARSE_RE.search(url)
        if m:
            return m.group(1), m.group(2)
        return "", ""
