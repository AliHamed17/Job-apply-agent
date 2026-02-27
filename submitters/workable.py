"""Workable submitter — uses the public Apply API.

Workable exposes a candidate creation endpoint for each job shortcode.
No API key required for external candidate submissions.

URL patterns:
  apply.workable.com/{company}/j/{shortcode}
  {company}.workable.com/jobs/{shortcode}
  {company}.workable.com/j/{shortcode}
"""

from __future__ import annotations

import re

import httpx
import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_WORKABLE_RE = re.compile(r"workable\.com", re.IGNORECASE)

# Extract: company slug + shortcode
# apply.workable.com/{company}/j/{shortcode}
# {company}.workable.com/j/{shortcode}
_SHORTCODE_RE = re.compile(
    r"(?:apply\.workable\.com/([^/]+)|([^.]+)\.workable\.com)"
    r"/j(?:obs)?/([A-Z0-9]+)",
    re.IGNORECASE,
)

_API_BASE = "https://apply.workable.com/api/v3"


class WorkableSubmitter(BaseSubmitter):
    """Submit applications via Workable public Apply API."""

    platform_name = "workable"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "workable.com" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        url = job.apply_url or job.source_url or ""
        company, shortcode = self._parse_url(url)
        if not shortcode:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error=f"Cannot extract Workable job shortcode from: {url}",
            )

        personal = user_profile.get("personal", {})
        name_parts = (personal.get("name") or "").split(maxsplit=1)
        links = user_profile.get("links", {})

        candidate = {
            "firstname": name_parts[0] if name_parts else "",
            "lastname": name_parts[1] if len(name_parts) > 1 else "",
            "email": personal.get("email", ""),
            "phone": personal.get("phone", ""),
            "address": personal.get("location", ""),
            "coverLetter": application.cover_letter or "",
            "summary": user_profile.get("resume", {}).get("text", "")[:2000],
            "socialProfiles": [],
        }

        if links.get("linkedin"):
            candidate["socialProfiles"].append(
                {"type": "linkedin", "url": links["linkedin"]}
            )
        if links.get("github"):
            candidate["socialProfiles"].append(
                {"type": "github", "url": links["github"]}
            )
        if links.get("portfolio") or links.get("website"):
            candidate["socialProfiles"].append(
                {"type": "website",
                 "url": links.get("portfolio") or links.get("website", "")}
            )

        payload = {"candidate": candidate, "sourced": False}

        endpoint = f"{_API_BASE}/jobs/{shortcode}/candidates"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
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
                logger.warning("workable_api_failed_trying_browser", status=resp.status_code)
                return await self._submit_via_browser(job, application, user_profile, resume_path)

        except Exception as exc:
            logger.warning("workable_api_error_trying_browser", error=str(exc))
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
        # Workable standard application URL pattern
        if "/apply" not in job_url:
            company, shortcode = self._parse_url(job_url)
            if company and shortcode:
                # E.g. apply.workable.com/company/j/shortcode/apply
                job_url = f"https://apply.workable.com/{company}/j/{shortcode}/apply"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, timeout=30000)

            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False, platform=self.platform_name, status="captcha_blocked",
                    error="CAPTCHA detected on Workable page"
                )

            # Wait a moment for Workable React app to mount
            await page.wait_for_timeout(2000)

            # Accept cookies if the popup exists
            cookie_btn = page.locator('button:has-text("Accept"), button:has-text("Allow")').first
            if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
                try:
                    await cookie_btn.click(timeout=1000)
                except Exception:
                    pass

            personal = user_profile.get("personal", {})
            name_parts = (personal.get("name") or "").split(maxsplit=1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Standard fields
            await page.fill('input[name="firstname"]', first_name)
            await page.fill('input[name="lastname"]', last_name)
            await page.fill('input[name="email"], input[type="email"]', personal.get("email", ""))
            
            # Phone field is sometimes split or uses 'phone'
            phone_input = page.locator('input[name="phone"], input[type="tel"]').first
            if await phone_input.count() > 0:
                await phone_input.fill(personal.get("phone", ""))

            # Resume
            if resume_path:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    await file_input.set_input_files(resume_path)
                    await page.wait_for_timeout(1000)

            # Cover letter
            if application.cover_letter:
                cl_locator = page.locator('textarea[name="cover_letter"], textarea[name="coverLetter"]').first
                if await cl_locator.count() > 0:
                    await cl_locator.fill(application.cover_letter)

            # Custom text questions (Workable Q&A)
            if application.qa_answers:
                text_inputs = page.locator('input[type="text"]:visible, textarea:visible')
                for i in range(await text_inputs.count()):
                    el = text_inputs.nth(i)
                    if not await el.is_editable(): continue
                    current = await el.input_value()
                    if current: continue
                    label_text = ""
                    el_id = await el.get_attribute("id") or ""
                    if el_id:
                        lbl = page.locator(f'label[for="{el_id}"]')
                        if await lbl.count() > 0:
                            label_text = (await lbl.inner_text()).strip().lower()
                    if not label_text:
                        label_text = (await el.get_attribute("aria-label") or "").lower()
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

            # Submit application
            submit_btn = page.locator('button[type="submit"]').first
            if await submit_btn.is_visible():
                await submit_btn.click()
                await page.wait_for_timeout(3000)

            # Check success
            success_indicators = ["success", "applied", "thank"]
            final_content = (await page.content()).lower()
            success = any(ind in page.url.lower() or ind in final_content for ind in success_indicators)
            
            await browser.close()
            return SubmissionResult(
                success=success, platform=self.platform_name,
                status="submitted" if success else "failed",
                error=None if success else "Workable browser submission failed"
            )


    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = _SHORTCODE_RE.search(url)
        if m:
            company = m.group(1) or m.group(2) or ""
            shortcode = m.group(3)
            return company, shortcode
        return "", ""
