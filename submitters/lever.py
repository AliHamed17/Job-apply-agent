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
            logger.info("lever_api_key_missing_trying_browser")
            try:
                return await self._submit_via_browser(job, application, user_profile, resume_path)
            except Exception as e:
                logger.error("lever_browser_fallback_failed", error=str(e))
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=f"No API key and browser fallback failed: {str(e)}",
                )

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
        """Fallback: Submit via browser if API key is missing."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError("Playwright not installed for browser fallback")

        job_url = job.apply_url or job.source_url or ""
        # Convert API URL to Job Board URL if needed
        # e.g. api.lever.co/v0/postings/company/id -> jobs.lever.co/company/id/apply
        if "api.lever.co" in job_url:
            company = self._extract_company(job_url)
            posting_id = self._extract_posting_id(job_url)
            if company and posting_id:
                job_url = f"https://jobs.lever.co/{company}/{posting_id}/apply"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, timeout=30000)

            # Check for CAPTCHA
            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False, platform="lever", status="captcha_blocked",
                    error="CAPTCHA detected on Lever application page"
                )

            personal = user_profile.get("personal", {})
            links = user_profile.get("links", {})

            # Fill standard fields
            await page.fill('input[name="name"]', personal.get("name", ""))
            await page.fill('input[name="email"]', personal.get("email", ""))
            await page.fill('input[name="phone"]', personal.get("phone", ""))
            await page.fill('input[name="org"]', "") # current company
            
            # Links
            if links.get("linkedin"):
                await page.fill('input[name="urls[LinkedIn]"]', links["linkedin"])
            if links.get("github"):
                await page.fill('input[name="urls[GitHub]"]', links["github"])
            if links.get("portfolio"):
                await page.fill('input[name="urls[Portfolio]"]', links["portfolio"])

            # Resume
            if resume_path:
                await page.set_input_files('input[type="file"]', resume_path)

            # Comments / Cover Letter
            await page.fill('textarea[name="comments"]', application.cover_letter)

            # Numeric inputs
            num_inputs = page.locator('input[type="number"]')
            for i in range(await num_inputs.count()):
                el = num_inputs.nth(i)
                if await el.is_visible() and await el.is_editable():
                    current = await el.input_value()
                    if not current:
                        await el.fill("0")

            # Custom text questions (Lever uses standard inputs/textareas)
            if application.qa_answers:
                text_inputs = page.locator('input[type="text"]:visible, textarea:visible')
                for i in range(await text_inputs.count()):
                    el = text_inputs.nth(i)
                    if not await el.is_editable():
                        continue
                    current = await el.input_value()
                    if current:
                        continue
                    el_id = await el.get_attribute("id") or ""
                    label_text = ""
                    if el_id:
                        lbl = page.locator(f'label[for="{el_id}"]').first
                        if await lbl.count() > 0:
                            label_text = (await lbl.inner_text()).strip().lower()
                    if not label_text:
                        label_text = (await el.get_attribute("aria-label") or "").lower()
                    if not label_text:
                        continue
                    best_answer = ""
                    for q_key, q_val in application.qa_answers.items():
                        if any(kw in label_text for kw in q_key.lower().split("_")):
                            best_answer = str(q_val)
                            break
                    if not best_answer:
                        best_answer = next((str(v) for v in application.qa_answers.values() if v), "")
                    if best_answer:
                        await el.fill(best_answer[:500])

            # Select dropdowns
            selects = page.locator('select:visible')
            for i in range(await selects.count()):
                sel = selects.nth(i)
                options = await sel.locator('option').all_text_contents()
                options_lower = [o.lower() for o in options]
                if "yes" in options_lower and "no" in options_lower:
                    await sel.select_option(label=next(o for o in options if o.lower() == "yes"))

            # Submit
            await page.click('button#btn-submit')
            await page.wait_for_timeout(3000)

            success = "thank-you" in page.url or "applied" in page.url.lower()
            
            await browser.close()
            return SubmissionResult(
                success=success,
                platform="lever",
                status="submitted" if success else "failed",
                error=None if success else "Redirected to unknown page after submission"
            )

    @staticmethod
    def _extract_posting_id(url: str) -> str | None:
        """Extract the Lever posting UUID from a URL."""
        import re
        # Pattern: lever.co/company/UUID or jobs.lever.co/company/UUID
        match = re.search(
            r"lever\.co/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            url,
        )
        return match.group(1) if match else None

    @staticmethod
    def _extract_company(url: str) -> str | None:
        """Extract the company slug from a Lever URL."""
        import re
        # Match company slug after lever.co/
        match = re.search(r"lever\.co/([^/]+)", url)
        return match.group(1) if match else None
