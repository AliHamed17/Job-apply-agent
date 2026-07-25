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
from submitters.confirmation import browser_submission_result
from submitters.form_brain import FormBrain
from submitters.safe_fill import fill_form_safely, needs_review_error

logger = structlog.get_logger(__name__)

_JOBVITE_RE = re.compile(r"jobvite\.com", re.IGNORECASE)

# Extract company slug and job ID
_JOB_PARSE_RE = re.compile(r"jobvite\.com/([^/]+)/(?:job|jobs)/([^/?#]+)", re.IGNORECASE)

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
        return await self._submit_form(job, url, job_id, application, user_profile, resume_path)

    async def _submit_form(
        self,
        job: JobData,
        job_url: str,
        job_id: str,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
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

            if resp.status_code in (200, 201, 302):
                return browser_submission_result(
                    platform=self.platform_name,
                    page_url=str(resp.url),
                    html=resp.text,
                )
            if 400 <= resp.status_code < 500:
                return await self._submit_via_browser(
                    job_url, application, user_profile, resume_path, job=job
                )
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="unknown",
                error="JOBVITE_FORM_OUTCOME_UNKNOWN",
                reason_code="SUBMIT_UNCONFIRMED",
            )

        except Exception as exc:
            logger.warning("jobvite_form_outcome_unknown", error=type(exc).__name__)
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="unknown",
                error="JOBVITE_FORM_OUTCOME_UNKNOWN",
                reason_code="SUBMIT_UNCONFIRMED",
            )

    async def _submit_via_browser(
        self,
        job_url: str,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
        job: JobData | None = None,
    ) -> SubmissionResult:
        """Fallback: Submit via browser using Playwright."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error="Playwright not installed for browser fallback",
            )

        if not job_url.endswith("/apply"):
            job_url = job_url.rstrip("/") + "/apply"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, timeout=30000)

            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="captcha_blocked",
                    error="CAPTCHA detected on Jobvite page",
                )

            # Wait a moment for form to load
            await page.wait_for_timeout(2000)

            personal = user_profile.get("personal", {})
            name_parts = (personal.get("name") or "").split(maxsplit=1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Fields
            await page.fill('input[name="firstName"], input[name="firstname"]', first_name)
            await page.fill('input[name="lastName"], input[name="lastname"]', last_name)
            await page.fill('input[name="email"], input[type="email"]', personal.get("email", ""))

            phone_input = page.locator('input[type="tel"], input[name="phone"]').first
            if await phone_input.count() > 0:
                await phone_input.fill(personal.get("phone", ""))

            # Resume
            if resume_path:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    await file_input.set_input_files(resume_path)
                    await page.wait_for_timeout(1000)

            # Custom questions (Jobvite Q&A blocks) and dropdowns — resolved per
            # question through FormBrain, which abstains instead of guessing, so
            # an unmatched field is left blank rather than answered falsely.
            from profile.models import UserProfile

            profile_obj = UserProfile(**user_profile) if user_profile else UserProfile()
            brain = FormBrain(profile_obj)
            blocked = await fill_form_safely(page, brain, job, application.qa_answers)

            # A required question we cannot answer truthfully stops the submission:
            # worker/tasks.py routes the NEEDS_REVIEW: prefix to human review.
            if blocked:
                logger.warning("jobvite_needs_review", url=job_url, questions=blocked[:5])
                await browser.close()
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="draft_only",
                    error=needs_review_error(blocked),
                )

            # Submit application
            submit_btn = page.locator(
                'button[type="submit"]:has-text("Submit"), button[type="submit"]:has-text("Apply")'
            ).first
            if not await submit_btn.is_visible():
                await browser.close()
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="draft_only",
                    error="NEEDS_REVIEW:SUBMIT_BUTTON_UNAVAILABLE",
                    reason_code="SELECTOR_DRIFT",
                )
            try:
                await submit_btn.click()
                await page.wait_for_timeout(3000)
                result = browser_submission_result(
                    platform=self.platform_name,
                    page_url=page.url,
                    html=await page.content(),
                )
            except Exception:
                result = SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="unknown",
                    error="SUBMIT_OUTCOME_UNKNOWN",
                    reason_code="SUBMIT_UNCONFIRMED",
                )
            await browser.close()
            return result

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = _JOB_PARSE_RE.search(url)
        if m:
            return m.group(1), m.group(2)
        return "", ""
