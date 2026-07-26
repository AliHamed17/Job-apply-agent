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
from submitters.confirmation import browser_submission_result
from submitters.form_brain import FormBrain
from submitters.safe_fill import fill_form_safely, needs_review_error

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
            logger.info("greenhouse_api_key_missing_trying_browser")
            try:
                return await self._submit_via_browser(job, application, user_profile, resume_path)
            except Exception as e:
                logger.error("greenhouse_browser_fallback_failed", error=str(e))
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=f"No API key and browser fallback failed: {str(e)}",
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
            full_name = personal.get("name", "")
            name_parts = full_name.split() if full_name else []

            # Build candidate payload
            candidate_data = {
                "first_name": name_parts[0] if name_parts else "",
                "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
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
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {}
                    return SubmissionResult(
                        success=True,
                        platform=self.platform_name,
                        status="submitted",
                        confirmation_id=str(data.get("id", "")),
                        reason_code="SUBMITTED",
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

    async def _submit_via_browser(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Fallback: Submit via browser if API key is missing."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError("Playwright not installed for browser fallback")

        job_url = job.apply_url or job.source_url or ""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, timeout=30000)

            # Check for CAPTCHA
            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False,
                    platform="greenhouse",
                    status="captcha_blocked",
                    error="CAPTCHA detected on Greenhouse application page",
                )

            personal = user_profile.get("personal", {})
            full_name = personal.get("name", "")
            name_parts = full_name.split() if full_name else []
            first_name = name_parts[0] if name_parts else ""
            last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

            fn_loc = page.locator('input[name="first_name"], input[id*="first_name"]').first
            if await fn_loc.is_visible(timeout=3000):
                await fn_loc.fill(first_name)

            ln_loc = page.locator('input[name="last_name"], input[id*="last_name"]').first
            if await ln_loc.is_visible(timeout=3000):
                await ln_loc.fill(last_name)

            em_loc = page.locator('input[name="email"], input[type="email"]').first
            if await em_loc.is_visible(timeout=3000):
                await em_loc.fill(personal.get("email", ""))

            ph_loc = page.locator('input[name="phone"], input[type="tel"]').first
            if await ph_loc.is_visible(timeout=3000):
                await ph_loc.fill(personal.get("phone", ""))

            # Resume
            if resume_path:
                # Greenhouse often has a "Resume/CV" upload button
                try:
                    await page.set_input_files('input[type="file"]', resume_path)
                except Exception:
                    # Fallback to looking for file input by label
                    await page.click('button:has-text("Attach")')
                    await page.set_input_files('input[type="file"]', resume_path)

            # Cover Letter
            if application.cover_letter:
                try:
                    await page.fill('textarea[name="cover_letter"]', application.cover_letter)
                except Exception:
                    pass

            # Custom questions (text, number, and yes/no <select>) — resolved
            # per-question by FormBrain, which abstains instead of guessing.
            from profile.models import UserProfile

            profile_obj = UserProfile(**user_profile) if user_profile else UserProfile()
            brain = FormBrain(profile_obj)
            blocked = await fill_form_safely(page, brain, job, application.qa_answers)

            # A required question we could not answer truthfully: hand off to a
            # human rather than submit a made-up answer.
            if blocked:
                logger.info("greenhouse_needs_review", blocked=blocked)
                await browser.close()
                return SubmissionResult(
                    success=True,
                    platform="greenhouse",
                    status="draft_only",
                    error=needs_review_error(blocked),
                )

            submit_btn = page.locator("button#submit_app").first
            if not await submit_btn.is_visible(timeout=2000):
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
    def _extract_job_id(url: str) -> str | None:
        """Extract the Greenhouse job ID from a URL."""
        import re

        # Pattern: /jobs/12345 or /jobs/12345-...
        match = re.search(r"/jobs/(\d+)", url)
        return match.group(1) if match else None
