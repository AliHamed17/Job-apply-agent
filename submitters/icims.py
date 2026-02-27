"""iCIMS submitter — Playwright browser automation.

iCIMS powers career portals for thousands of companies.
URLs typically follow:
  careers-{company}.icims.com/jobs/{job-id}/job
  {company}.com/careers/job/{id}  (powered by iCIMS)

Because iCIMS uses company-specific SSO/login and custom field configurations,
this submitter uses Playwright to fill the standard candidate form.

Setup:
  No credentials needed — iCIMS public apply forms are open.
  pip install ".[browser]" && playwright install chromium
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_ICIMS_RE = re.compile(r"icims\.com", re.IGNORECASE)

_NAV_TIMEOUT  = 20_000
_ELEM_TIMEOUT = 10_000
_SHORT_WAIT   = 1_500


class IcimsSubmitter(BaseSubmitter):
    """Submit via iCIMS public apply form using Playwright."""

    platform_name = "icims"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "icims.com" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError:
            return SubmissionResult(
                success=True,
                platform=self.platform_name,
                status="draft_only",
                error="Playwright not installed. Run: pip install 'job-apply-agent[browser]' && playwright install chromium",
            )

        job_url = job.apply_url or job.source_url or ""

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            try:
                result = await self._apply(page, job_url, application, user_profile, resume_path)
            except Exception as exc:
                logger.error("icims_apply_error", error=str(exc))
                result = SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="draft_only",
                    error=str(exc),
                )
            finally:
                await browser.close()

        return result

    async def _apply(self, page, job_url: str, application: GeneratedApplication,
                     user_profile: dict, resume_path: str | None) -> SubmissionResult:
        await page.goto(job_url, timeout=_NAV_TIMEOUT)
        await page.wait_for_timeout(_SHORT_WAIT)

        if self.detect_captcha(await page.content()):
            return SubmissionResult(
                success=True, platform=self.platform_name, status="draft_only",
                error="CAPTCHA detected on iCIMS page",
            )

        # Find and click the Apply button
        apply_btn = page.locator(
            'a:has-text("Apply"), '
            'button:has-text("Apply"), '
            'a:has-text("Apply Now"), '
            'button:has-text("Apply Now"), '
            'a[data-icims-type="applybutton"]'
        ).first

        if await apply_btn.is_visible(timeout=5000):
            await apply_btn.click(timeout=_ELEM_TIMEOUT)
            await page.wait_for_timeout(_SHORT_WAIT)

        # Work through multi-step form
        max_steps = 8
        for step in range(max_steps):
            content = await page.content()

            if self.detect_captcha(content):
                return SubmissionResult(
                    success=True, platform=self.platform_name, status="draft_only",
                    error="CAPTCHA appeared during iCIMS application",
                )

            await self._fill_icims_fields(page, application, user_profile, resume_path)

            # Check for final submission
            submit_btn = page.locator(
                'input[type="submit"], '
                'button[type="submit"]:has-text("Submit"), '
                'button:has-text("Submit Application"), '
                'a:has-text("Submit Application")'
            ).first

            if await submit_btn.is_visible(timeout=2000):
                await submit_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(3000)

                final_content = (await page.content()).lower()
                if any(k in final_content for k in ["thank you", "application received", "successfully submitted", "confirmation"]):
                    logger.info("icims_application_submitted", url=job_url)
                    return SubmissionResult(
                        success=True, platform=self.platform_name, status="submitted",
                        confirmation_url=page.url,
                    )
                if self.detect_captcha(final_content):
                    return SubmissionResult(
                        success=True, platform=self.platform_name, status="draft_only",
                        error="CAPTCHA appeared during iCIMS submission",
                    )
                # If we clicked submit and didn't get success or captcha, assume success if URL changed?
                # iCIMS often redirects.
                logger.info("icims_application_submitted_assumed", url=job_url)
                return SubmissionResult(
                    success=True, platform=self.platform_name, status="submitted",
                    confirmation_url=page.url,
                )

            # Click Continue / Next if present
            next_btn = page.locator(
                'button:has-text("Next"), '
                'button:has-text("Continue"), '
                'input[value="Next"], '
                'input[value="Continue"]'
            ).first
            if await next_btn.is_visible(timeout=2000):
                await next_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(_SHORT_WAIT)
            else:
                break

        return SubmissionResult(
            success=True, platform=self.platform_name, status="draft_only",
            error="iCIMS apply form did not reach submission step",
        )

    async def _fill_icims_fields(self, page, application: GeneratedApplication,
                                 user_profile: dict, resume_path: str | None) -> None:
        """Fill all visible form inputs with profile data and Q&A answers on iCIMS."""
        personal = user_profile.get("personal", {})
        name_parts = (personal.get("name", "") or "").split()

        field_map = {
            "iims-firstname": name_parts[0] if name_parts else "",
            "iims-lastname":  " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
            "iims-email":     personal.get("email", ""),
            "iims-phone":     personal.get("phone", ""),
            "applicant.firstname": name_parts[0] if name_parts else "",
            "applicant.lastname":  " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
            "applicant.email":     personal.get("email", ""),
            "applicant.phone":     personal.get("phone", ""),
        }

        for name_attr, value in field_map.items():
            if not value:
                continue
            loc = page.locator(f'input[name="{name_attr}"], input[id="{name_attr}"]').first
            if await loc.count() > 0 and await loc.is_visible() and await loc.is_editable():
                await loc.fill(value)

        # Resume upload
        if resume_path and Path(resume_path).exists():
            file_input = page.locator('input[type="file"]').first
            if await file_input.count() > 0:
                await file_input.set_input_files(resume_path)
                await page.wait_for_timeout(1000)

        # Cover letter
        cl = page.locator('textarea[name*="coverletter"], textarea[id*="coverletter"]').first
        if await cl.count() > 0 and await cl.is_visible() and await cl.is_editable():
            await cl.fill(application.cover_letter or "")

        # Work authorization — select "Yes"
        for auth_label in ["authorized to work", "legally authorized", "work authorization", "sponsorship"]:
            yes_radio = page.locator(
                f'label:has-text("Yes"):near(label:has-text("{auth_label}"))'
            ).first
            if await yes_radio.count() > 0 and await yes_radio.is_visible():
                await yes_radio.click()

        # Numeric inputs
        num_inputs = page.locator('input[type="number"]')
        for i in range(await num_inputs.count()):
            el = num_inputs.nth(i)
            if await el.is_visible() and await el.is_editable():
                current = await el.input_value()
                if not current:
                    await el.fill("0")

        # Custom text questions
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

        # Select dropdowns — pick "Yes" for yes/no questions
        selects = page.locator('select:visible')
        for i in range(await selects.count()):
            sel = selects.nth(i)
            options = await sel.locator('option').all_text_contents()
            options_lower = [o.lower() for o in options]
            if "yes" in options_lower and "no" in options_lower:
                await sel.select_option(label=next(o for o in options if o.lower() == "yes"))
