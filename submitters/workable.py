"""Workable submitter — uses the public Apply API.

Workable exposes a candidate creation endpoint for each job shortcode.
No API key required for external candidate submissions.

URL patterns:
  apply.workable.com/{company}/j/{shortcode}
  {company}.workable.com/jobs/{shortcode}
  {company}.workable.com/j/{shortcode}
"""

from __future__ import annotations

import re

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_WORKABLE_RE = re.compile(r"workable\.com", re.IGNORECASE)

# Extract: company slug + shortcode
# apply.workable.com/{company}/j/{shortcode}
# {company}.workable.com/j/{shortcode}
_SHORTCODE_RE = re.compile(
    r"(?:apply\.workable\.com/([^/]+)|([^.]+)\.workable\.com)"
    r"/j(?:obs)?/([A-Z0-9]+)",
    re.IGNORECASE,
)

_API_BASE = "https://apply.workable.com/api/v3"


class WorkableSubmitter(BaseSubmitter):
    """Submit applications via Workable public Apply API."""

    platform_name = "workable"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "workable.com" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        url = job.apply_url or job.source_url or ""
        company, shortcode = self._parse_url(url)
        if not shortcode:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=f"Cannot extract Workable job shortcode from: {url}",
            )

        personal = user_profile.get("personal", {})
        name_parts = (personal.get("name") or "").split(maxsplit=1)
        links = user_profile.get("links", {})

        candidate = {
            "firstname": name_parts[0] if name_parts else "",
            "lastname": name_parts[1] if len(name_parts) > 1 else "",
            "email": personal.get("email", ""),
            "phone": personal.get("phone", ""),
            "address": personal.get("location", ""),
            "coverLetter": application.cover_letter or "",
            "summary": user_profile.get("resume", {}).get("text", "")[:2000],
            "socialProfiles": [],
        }

        if links.get("linkedin"):
            candidate["socialProfiles"].append(
                {"type": "linkedin", "url": links["linkedin"]}
            )
        if links.get("github"):
            candidate["socialProfiles"].append(
                {"type": "github", "url": links["github"]}
            )
        if links.get("portfolio") or links.get("website"):
            candidate["socialProfiles"].append(
                {"type": "website",
                 "url": links.get("portfolio") or links.get("website", "")}
            )

        payload = {"candidate": candidate, "sourced": False}

        endpoint = f"{_API_BASE}/jobs/{shortcode}/candidates"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
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
                    error=f"HTTP {resp.status_code}: {resp.text[:400]}",
                )

        except Exception as exc:
            logger.error("workable_submit_error", error=str(exc))
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=str(exc),
            )

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = _SHORTCODE_RE.search(url)
        if m:
            company = m.group(1) or m.group(2) or ""
            shortcode = m.group(3)
            return company, shortcode
        return "", ""
