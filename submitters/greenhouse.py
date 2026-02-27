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
                    success=False, platform="greenhouse", status="captcha_blocked",
                    error="CAPTCHA detected on Greenhouse application page"
                )

            personal = user_profile.get("personal", {})
            full_name = personal.get("name", "")
            name_parts = full_name.split() if full_name else []
            first_name = name_parts[0] if name_parts else ""
            last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

            # Greenhouse fields often have IDs like 'first_name', 'last_name', etc.
            # or name attributes.
            await page.fill('input[name="first_name"]', first_name)
            await page.fill('input[name="last_name"]', last_name)
            await page.fill('input[name="email"]', personal.get("email", ""))
            await page.fill('input[name="phone"]', personal.get("phone", ""))

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

            # Numeric inputs
            num_inputs = page.locator('input[type="number"]')
            for i in range(await num_inputs.count()):
                el = num_inputs.nth(i)
                if await el.is_visible() and await el.is_editable():
                    current = await el.input_value()
                    if not current:
                        await el.fill("0")

            # Custom text questions (Greenhouse handles custom questions with standard inputs/textareas)
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

            # Select dropdowns — Greenhouse uses standard <select> elements for yes/no
            selects = page.locator('select:visible')
            for i in range(await selects.count()):
                sel = selects.nth(i)
                options = await sel.locator('option').all_text_contents()
                options_lower = [o.lower() for o in options]
                if "yes" in options_lower and "no" in options_lower:
                    await sel.select_option(label=next(o for o in options if o.lower() == "yes"))

            # Submit
            await page.click('button#submit_app')
            await page.wait_for_timeout(3000)

            success = "confirmation" in page.url or "applied" in page.url.lower() or "success" in page.url.lower()
            
            await browser.close()
            return SubmissionResult(
                success=success,
                platform="greenhouse",
                status="submitted" if success else "failed",
                error=None if success else "Redirected to unknown page after submission"
            )

    @staticmethod
    def _extract_job_id(url: str) -> str | None:
        """Extract the Greenhouse job ID from a URL."""
        import re
        # Pattern: /jobs/12345 or /jobs/12345-...
        match = re.search(r"/jobs/(\d+)", url)
        return match.group(1) if match else None
