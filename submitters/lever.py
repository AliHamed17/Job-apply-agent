"""Lever Postings API submitter.

Uses the official Lever Postings API to submit applications.
Docs: https://hire.lever.co/developer/documentation
"""

from __future__ import annotations

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)


class LeverSubmitter(BaseSubmitter):
    """Submit applications via Lever Postings API."""

    platform_name = "lever"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.base_url = "https://api.lever.co/v0/postings"

    def can_submit(self, job: JobData) -> bool:
        """Check if this is a Lever job."""
        url = (job.apply_url or job.source_url).lower()
        return "lever.co" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Submit application via Lever Postings API.

        The Lever Postings API accepts form data with candidate info.
        """
        if not self.api_key:
            logger.info("lever_api_key_missing_falling_back_to_browser")
            return await self._submit_via_browser(job, application, user_profile, resume_path)

        try:
            posting_id = self._extract_posting_id(job.apply_url or job.source_url)
            company_slug = self._extract_company(job.apply_url or job.source_url)

            if not posting_id or not company_slug:
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error="Could not extract Lever posting ID from URL",
                )

            personal = user_profile.get("personal", {})
            links = user_profile.get("links", {})

            # Build form data
            form_data = {
                "name": personal.get("name", ""),
                "email": personal.get("email", ""),
                "phone": personal.get("phone", ""),
                "org": "",  # current company
                "urls[LinkedIn]": links.get("linkedin", ""),
                "urls[GitHub]": links.get("github", ""),
                "urls[Portfolio]": links.get("portfolio", ""),
                "comments": application.cover_letter,
            }

            files = {}
            if resume_path:
                files["resume"] = open(resume_path, "rb")

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    url = f"{self.base_url}/{company_slug}/{posting_id}"
                    resp = await client.post(
                        url,
                        data=form_data,
                        headers={"Authorization": f"Bearer {self.api_key}"},
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
                            error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                        )
            finally:
                if "resume" in files:
                    files["resume"].close()

        except Exception as exc:
            logger.error("lever_submit_error", error=str(exc))
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
        """Fallback browser-based submission for public Lever forms."""
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except Exception as exc:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=f"Playwright not available: {exc}",
            )

        apply_url = (job.apply_url or job.source_url or "").replace("api.lever.co", "jobs.lever.co")
        if not apply_url:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error="Missing Lever apply URL",
            )

        personal = user_profile.get("personal", {})

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)

                await page.locator('input[name="name"]').first.fill(personal.get("name", ""))
                await page.locator('input[name="email"]').first.fill(personal.get("email", ""))

                phone = page.locator('input[name="phone"], input[type="tel"]').first
                if await phone.count():
                    await phone.fill(personal.get("phone", ""))

                if application.cover_letter:
                    cover = page.locator('textarea[name="comments"], textarea').first
                    if await cover.count():
                        await cover.fill(application.cover_letter[:4000])

                if resume_path:
                    upload = page.locator('input[type="file"]').first
                    if await upload.count():
                        await upload.set_input_files(resume_path)

                await self._fill_common_screening_questions(page)
                await browser.close()

            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="requires_human_confirmation",
                error="Browser form filled (submission left for final human confirmation)",
            )
        except PlaywrightTimeoutError:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error="Timed out while loading Lever application form",
            )
        except Exception as exc:
            logger.error("lever_browser_submit_error", error=str(exc))
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=str(exc),
            )

    async def _fill_common_screening_questions(self, page) -> None:
        """Fill common work authorization and sponsorship questions."""
        yes_selectors = [
            'label:has-text("authorized") input[value="yes"]',
            'label:has-text("work authorization") input[value="yes"]',
        ]
        no_selectors = [
            'label:has-text("sponsorship") input[value="no"]',
            'label:has-text("visa") input[value="no"]',
        ]

        for selector in yes_selectors:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.check(force=True)

        for selector in no_selectors:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.check(force=True)

        exp_inputs = page.locator('input[type="number"][name*="experience" i]')
        count = await exp_inputs.count()
        for idx in range(count):
            await exp_inputs.nth(idx).fill("5")

    @staticmethod
    def _extract_posting_id(url: str) -> str | None:
        """Extract the Lever posting UUID from a URL."""
        import re
        # Pattern: lever.co/company/UUID
        match = re.search(
            r"lever\.co/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            url,
        )
        return match.group(1) if match else None

    @staticmethod
    def _extract_company(url: str) -> str | None:
        """Extract the company slug from a Lever URL."""
        import re
        match = re.search(r"lever\.co/([^/]+)", url)
        return match.group(1) if match else None
