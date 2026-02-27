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

logger = structlog.get_logger(__name__)

_JOBVITE_RE = re.compile(r"jobvite\.com", re.IGNORECASE)

# Extract company slug and job ID
_JOB_PARSE_RE = re.compile(
    r"jobvite\.com/([^/]+)/(?:job|jobs)/([^/?#]+)", re.IGNORECASE
)

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
        return await self._submit_form(url, job_id, application, user_profile)

    async def _submit_form(
        self,
        job_url: str,
        job_id: str,
        application: GeneratedApplication,
        user_profile: dict,
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

            if self.detect_captcha(resp.text):
                logger.warning("jobvite_captcha_detected", url=submit_url)
                return await self._submit_via_browser(job_url, application, user_profile, resume_path)

            if resp.status_code in (200, 201, 302):
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="submitted",
                    confirmation_url=str(resp.url),
                )
            else:
                return await self._submit_via_browser(job_url, application, user_profile, resume_path)

        except Exception as exc:
            logger.warning("jobvite_submit_error_trying_browser", error=str(exc))
            return await self._submit_via_browser(job_url, application, user_profile, resume_path)

    async def _submit_via_browser(
        self,
        job_url: str,
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

        if not job_url.endswith("/apply"):
            job_url = job_url.rstrip("/") + "/apply"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, timeout=30000)

            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False, platform=self.platform_name, status="captcha_blocked",
                    error="CAPTCHA detected on Jobvite page"
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

            # Custom questions (Jobvite Q&A blocks)
            if application.qa_answers:
                text_inputs = page.locator('input[type="text"]:visible, textarea:visible')
                for i in range(await text_inputs.count()):
                    el = text_inputs.nth(i)
                    if not await el.is_editable(): continue
                    current = await el.input_value()
                    if current: continue
                    label_text = (await el.get_attribute("aria-label") or "").lower()
                    el_id = await el.get_attribute("id") or ""
                    if not label_text and el_id:
                        lbl = page.locator(f'label[for="{el_id}"]')
                        if await lbl.count() > 0:
                            label_text = (await lbl.inner_text()).strip().lower()
                    if not label_text: continue
                    best_answer = next((str(v) for k, v in application.qa_answers.items() if any(kw in label_text for kw in k.lower().split("_"))), next((str(v) for v in application.qa_answers.values() if v), ""))
                    if best_answer:
                        await el.fill(best_answer[:500])

            # Select dropdowns
            selects = page.locator('select:visible')
            for i in range(await selects.count()):
                sel = selects.nth(i)
                options = await sel.locator('option').all_text_contents()
                options_lower = [o.lower() for o in options]
                if "yes" in options_lower and "no" in options_lower:
                    try:
                        await sel.select_option(label=next(o for o in options if o.lower() == "yes"))
                    except Exception:
                        pass

            # Acknowledge / Checkboxes
            checkboxes = page.locator('input[type="checkbox"]:visible')
            for i in range(await checkboxes.count()):
                cb = checkboxes.nth(i)
                try:
                    await cb.check()
                except Exception:
                    pass

            # Submit application
            submit_btn = page.locator('button[type="submit"]:has-text("Submit"), button[type="submit"]:has-text("Apply")').first
            if await submit_btn.is_visible():
                await submit_btn.click()
                await page.wait_for_timeout(3000)

            # Check success (Jobvite usually redirects or shows a confirmation)
            success_indicators = ["success", "applied", "thank", "confirmation"]
            final_content = (await page.content()).lower()
            success = any(ind in page.url.lower() or ind in final_content for ind in success_indicators)
            
            await browser.close()
            return SubmissionResult(
                success=success, platform=self.platform_name,
                status="submitted" if success else "failed",
                error=None if success else "Jobvite browser submission failed"
            )

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = _JOB_PARSE_RE.search(url)
        if m:
            return m.group(1), m.group(2)
        return "", ""
