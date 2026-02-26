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
            logger.info("greenhouse_api_key_missing_falling_back_to_browser")
            return await self._submit_via_browser(job, application, user_profile, resume_path)

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


    async def _submit_via_browser(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Fallback browser-based form fill for public Greenhouse pages."""
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

        apply_url = job.apply_url or job.source_url
        if not apply_url:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error="Missing Greenhouse apply URL",
            )

        personal = user_profile.get("personal", {})

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)

                names = personal.get("name", "").split()
                await page.locator('input[name="first_name"], input[id*="first_name"]').first.fill(
                    names[0] if names else ""
                )
                await page.locator('input[name="last_name"], input[id*="last_name"]').first.fill(
                    " ".join(names[1:])
                )
                await page.locator('input[name="email"], input[type="email"]').first.fill(
                    personal.get("email", "")
                )

                phone = page.locator('input[name="phone"], input[type="tel"]').first
                if await phone.count():
                    await phone.fill(personal.get("phone", ""))

                if application.cover_letter:
                    cover = page.locator(
                        'textarea[name*="cover_letter" i], textarea[id*="cover_letter" i]'
                    ).first
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
                error="Timed out while loading Greenhouse application form",
            )
        except Exception as exc:
            logger.error("greenhouse_browser_submit_error", error=str(exc))
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=str(exc),
            )

    async def _fill_common_screening_questions(self, page) -> None:
        """Fill common work authorization and sponsorship questions."""
        yes_inputs = page.locator(
            'label:has-text("authorized") input[type="radio"][value="1"], '
            'label:has-text("work authorization") input[type="radio"][value="yes" i]'
        )
        for idx in range(await yes_inputs.count()):
            await yes_inputs.nth(idx).check(force=True)

        no_inputs = page.locator(
            'label:has-text("sponsorship") input[type="radio"][value="0"], '
            'label:has-text("visa") input[type="radio"][value="no" i]'
        )
        for idx in range(await no_inputs.count()):
            await no_inputs.nth(idx).check(force=True)

        exp_inputs = page.locator(
            'input[type="number"][name*="experience" i], input[type="number"][id*="experience" i]'
        )
        for idx in range(await exp_inputs.count()):
            await exp_inputs.nth(idx).fill("5")

    @staticmethod
    def _extract_job_id(url: str) -> str | None:
        """Extract the Greenhouse job ID from a URL."""
        import re
        # Pattern: /jobs/12345 or /jobs/12345-...
        match = re.search(r"/jobs/(\d+)", url)
        return match.group(1) if match else None
