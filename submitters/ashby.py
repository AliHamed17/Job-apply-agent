"""Ashby HQ submitter — uses the public Posting API.

No API key required. Works for any public Ashby job posting.
Docs: https://developers.ashbyhq.com/reference/applicationformsubmit
"""

from __future__ import annotations

import re

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

# Ashby URL patterns:
#   jobs.ashbyhq.com/{company}/{posting-id}
#   {company}.ashbyhq.com/jobs/{posting-id}
#   greenhouse-hosted pages that redirect to ashby (skip those)
_ASHBY_HOSTS = re.compile(r"ashbyhq\.com", re.IGNORECASE)
_POSTING_ID_RE = re.compile(
    r"ashbyhq\.com/(?:[^/]+/)?(?:jobs?/)?([0-9a-f\-]{36})", re.IGNORECASE
)

_API_BASE = "https://api.ashbyhq.com/posting-public"


class AshbySubmitter(BaseSubmitter):
    """Submit applications via Ashby HQ public Posting API."""

    platform_name = "ashby"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "ashbyhq.com" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        url = job.apply_url or job.source_url or ""
        posting_id = self._extract_posting_id(url)
        if not posting_id:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=f"Cannot extract Ashby posting ID from URL: {url}",
            )

        personal = user_profile.get("personal", {})
        name_parts = (personal.get("name") or "").split(maxsplit=1)
        first = name_parts[0] if name_parts else ""
        last = name_parts[1] if len(name_parts) > 1 else ""

        # Build field submissions — Ashby uses a flexible form field system
        field_submissions = [
            {"path": "_systemfield_name", "value": personal.get("name", "")},
            {"path": "_systemfield_email", "value": personal.get("email", "")},
            {"path": "_systemfield_phone", "value": personal.get("phone", "")},
        ]
        if application.cover_letter:
            field_submissions.append(
                {"path": "coverLetter", "value": application.cover_letter}
            )

        links = user_profile.get("links", {})
        if links.get("linkedin"):
            field_submissions.append(
                {"path": "_systemfield_linkedin", "value": links["linkedin"]}
            )
        if links.get("portfolio") or links.get("website"):
            field_submissions.append(
                {"path": "_systemfield_website",
                 "value": links.get("portfolio") or links.get("website", "")}
            )

        payload = {
            "postingId": posting_id,
            "applicationForm": {"fieldSubmissions": field_submissions},
            "source": "Job Board",
            "origin": "applied",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{_API_BASE}/application/create",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            if resp.status_code in (200, 201):
                data = resp.json()
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="submitted",
                    confirmation_id=str(data.get("applicationId", "")),
                )
            else:
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=f"HTTP {resp.status_code}: {resp.text[:400]}",
                )

        except Exception as exc:
            logger.error("ashby_submit_error", error=str(exc))
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=str(exc),
            )

    @staticmethod
    def _extract_posting_id(url: str) -> str | None:
        m = _POSTING_ID_RE.search(url)
        return m.group(1) if m else None
