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
from submitters.form_brain import FormBrain
from submitters.safe_fill import fill_form_safely, needs_review_error

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
                logger.warning("ashby_api_failed_trying_browser", status=resp.status_code)
                return await self._submit_via_browser(job, application, user_profile, resume_path)

        except Exception as exc:
            logger.warning("ashby_api_error_trying_browser", error=str(exc))
            return await self._submit_via_browser(job, application, user_profile, resume_path)

    async def _submit_via_browser(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Fallback: Submit via browser using Playwright."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return SubmissionResult(
                success=False, platform=self.platform_name, status="failed",
                error="Playwright not installed for browser fallback"
            )

        job_url = job.apply_url or job.source_url or ""
        if "/apply" not in job_url and not job_url.endswith("/application"):
            # Try appending /application (Ashby's common path)
            job_url = job_url.rstrip("/") + "/application"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, timeout=30000)

            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False, platform=self.platform_name, status="captcha_blocked",
                    error="CAPTCHA detected on Ashby page"
                )

            personal = user_profile.get("personal", {})

            # Check if there is an "Apply" button instead of the form directly
            apply_btn = page.locator('a:has-text("Apply"), button:has-text("Apply")').first
            if await apply_btn.count() > 0 and await apply_btn.is_visible():
                await apply_btn.click()
                await page.wait_for_timeout(2000)

            # Standard fields (Ashby uses name="name", "email", "phone" etc.)
            await page.fill('input[name="name"]', personal.get("name", ""))
            await page.fill('input[name="email"], input[type="email"]', personal.get("email", ""))
            await page.fill('input[name="phone"], input[type="tel"]', personal.get("phone", ""))

            # Resume
            if resume_path:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    await file_input.set_input_files(resume_path)
                    await page.wait_for_timeout(1000)

            # Custom questions (Ashby custom fields) — resolved through
            # FormBrain, which abstains rather than guessing. Nothing is typed
            # into a field whose question we could not actually answer.
            from profile.models import UserProfile

            profile_obj = UserProfile(**user_profile) if user_profile else UserProfile()
            brain = FormBrain(profile_obj)
            blocked = await fill_form_safely(page, brain, job, application.qa_answers)

            # A required question we cannot answer truthfully means no
            # submission — hand it to a human instead of inventing an answer.
            if blocked:
                logger.warning("ashby_needs_review", questions=blocked[:5])
                await browser.close()
                return SubmissionResult(
                    success=False, platform=self.platform_name, status="failed",
                    error=needs_review_error(blocked)
                )

            # Submit
            submit_btn = page.locator('button[type="submit"]').first
            if await submit_btn.is_visible():
                await submit_btn.click()
                await page.wait_for_timeout(3000)

            success = (
                "confirmation" in page.url
                or "success" in page.url.lower()
                or "applied" in page.url.lower()
            )
            await browser.close()

            return SubmissionResult(
                success=success, platform=self.platform_name,
                status="submitted" if success else "failed",
                error=None if success else "Ashby browser submission failed"
            )


    @staticmethod
    def _extract_posting_id(url: str) -> str | None:
        m = _POSTING_ID_RE.search(url)
        return m.group(1) if m else None
