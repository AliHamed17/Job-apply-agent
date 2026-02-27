"""SmartRecruiters submitter — uses the public Candidate API.

SmartRecruiters exposes a public candidate creation endpoint that doesn't
require an API key for jobs posted on smartrecruiters.com.

Docs: https://dev.smartrecruiters.com/customer-api/live-docs/application-api/
URL patterns:
  jobs.smartrecruiters.com/{company}/{posting-id}
  careers.{company}.com (custom domain, harder to detect)
"""

from __future__ import annotations

import re

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_SR_URL_RE = re.compile(r"smartrecruiters\.com", re.IGNORECASE)

# URL: jobs.smartrecruiters.com/{company-identifier}/{job-id}
_SR_PARSE_RE = re.compile(
    r"smartrecruiters\.com/([^/]+)/([^/?#]+)", re.IGNORECASE
)

_API_BASE = "https://api.smartrecruiters.com/v1"


class SmartRecruitersSubmitter(BaseSubmitter):
    """Submit applications via SmartRecruiters public Candidate API."""

    platform_name = "smartrecruiters"

    def __init__(self, api_key: str = ""):
        # Optional: company API key for higher rate limits / custom workflows
        self.api_key = api_key

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "smartrecruiters.com" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        url = job.apply_url or job.source_url or ""
        company_id, posting_id = self._parse_url(url)
        if not company_id or not posting_id:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=f"Cannot extract SmartRecruiters company/posting IDs from: {url}",
            )

        personal = user_profile.get("personal", {})
        name_parts = (personal.get("name") or "").split(maxsplit=1)
        links = user_profile.get("links", {})

        candidate = {
            "firstName": name_parts[0] if name_parts else "",
            "lastName": name_parts[1] if len(name_parts) > 1 else "",
            "email": personal.get("email", ""),
            "phoneNumber": personal.get("phone", ""),
            "location": {"country": personal.get("country", "GB")},
            "web": {
                "linkedIn": links.get("linkedin", ""),
                "portfolio": links.get("portfolio", "") or links.get("website", ""),
            },
            "tags": {
                "public": ["source:job-agent"],
            },
            "sourceDetails": {
                "sourceType": "DIRECT",
                "sourceSubType": "JOB_BOARD",
            },
        }

        # Cover letter goes as an attachment or in the notes
        if application.cover_letter:
            candidate["coverLetter"] = application.cover_letter

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-SmartToken"] = self.api_key

        endpoint = f"{_API_BASE}/companies/{company_id}/postings/{posting_id}/candidates"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json=candidate, headers=headers)

            if resp.status_code in (200, 201):
                data = resp.json()
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="submitted",
                    confirmation_id=str(data.get("id", "")),
                )
            elif resp.status_code == 401:
                logger.warning("smartrecruiters_api_auth_failed_trying_browser")
                return await self._submit_via_browser(job, application, user_profile, resume_path)
            else:
                logger.warning("smartrecruiters_api_failed_trying_browser", status=resp.status_code)
                return await self._submit_via_browser(job, application, user_profile, resume_path)

        except Exception as exc:
            logger.warning("smartrecruiters_api_error_trying_browser", error=str(exc))
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

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, timeout=30000)

            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False, platform=self.platform_name, status="captcha_blocked",
                    error="CAPTCHA detected on SmartRecruiters page"
                )

            # SmartRecruiters often has an initial "Apply" or "Easy Apply" button to launch the form
            apply_btn = page.locator('button:has-text("Apply"), a:has-text("Apply")').first
            if await apply_btn.count() > 0 and await apply_btn.is_visible():
                await apply_btn.click(timeout=3000)
                await page.wait_for_timeout(2000)

            # Accept cookies if the popup exists
            cookie_btn = page.locator('button:has-text("Accept")').first
            if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
                try:
                    await cookie_btn.click(timeout=1000)
                except Exception:
                    pass

            personal = user_profile.get("personal", {})
            name_parts = (personal.get("name") or "").split(maxsplit=1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Fields
            await page.fill('input[name="firstName"], input[id*="firstName"]', first_name)
            await page.fill('input[name="lastName"], input[id*="lastName"]', last_name)
            await page.fill('input[type="email"]', personal.get("email", ""))
            
            phone_input = page.locator('input[type="tel"], input[name="phoneNumber"]').first
            if await phone_input.count() > 0:
                await phone_input.fill(personal.get("phone", ""))

            # Resume
            if resume_path:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    await file_input.set_input_files(resume_path)
                    await page.wait_for_timeout(1000)

            # Cover letter / Messages (Textarea)
            if application.cover_letter:
                msg_input = page.locator('textarea').first
                if await msg_input.count() > 0 and await msg_input.is_visible():
                    await msg_input.fill(application.cover_letter)

            # Custom Q&A answers
            if application.qa_answers:
                text_inputs = page.locator('input[type="text"]:visible')
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
            submit_btn = page.locator('button[type="submit"]:has-text("Submit"), button:has-text("Submit")').first
            if await submit_btn.is_visible():
                await submit_btn.click()
                await page.wait_for_timeout(3000)

            # Check success
            success_indicators = ["success", "applied", "thank", "confirmation"]
            final_content = (await page.content()).lower()
            success = any(ind in page.url.lower() or ind in final_content for ind in success_indicators)
            
            await browser.close()
            return SubmissionResult(
                success=success, platform=self.platform_name,
                status="submitted" if success else "failed",
                error=None if success else "SmartRecruiters browser submission failed"
            )


    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = _SR_PARSE_RE.search(url)
        if m:
            return m.group(1), m.group(2)
        return "", ""
