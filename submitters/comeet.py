"""Comeet job board submitter (comeet.com / comeet.co).

Comeet does not expose a public application API, so we use
Playwright browser automation to fill and submit the apply form.
"""

from __future__ import annotations

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.confirmation import browser_submission_result
from submitters.form_brain import FormBrain
from submitters.safe_fill import fill_form_safely, needs_review_error

logger = structlog.get_logger(__name__)


class ComeetSubmitter(BaseSubmitter):
    """Submit applications via Comeet browser automation."""

    platform_name = "comeet"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "comeet.com" in url or "comeet.co" in url

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error="Playwright not installed",
            )

        job_url = job.apply_url or job.source_url or ""
        # Navigate to apply URL (Comeet often has /apply suffix or an apply button)
        if not job_url.endswith("/apply"):
            apply_url = job_url.rstrip("/") + "/apply"
        else:
            apply_url = job_url

        personal = user_profile.get("personal", {})
        links = user_profile.get("links", {})
        full_name = personal.get("name", "")
        name_parts = full_name.split() if full_name else []
        first_name = name_parts[0] if name_parts else ""
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

        from profile.models import UserProfile

        profile_obj = UserProfile(**user_profile) if user_profile else UserProfile()
        brain = FormBrain(profile_obj)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()

            # Try /apply URL first; fall back to job URL (which may have an Apply button)
            try:
                await page.goto(apply_url, timeout=30000)
            except Exception:
                await page.goto(job_url, timeout=30000)
                # Click Apply button if present
                try:
                    await page.click('a:has-text("Apply"), button:has-text("Apply")', timeout=5000)
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass

            # CAPTCHA check
            if self.detect_captcha(await page.content()):
                await browser.close()
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="captcha_blocked",
                    error="CAPTCHA detected on Comeet application page",
                )

            # ── Fill form fields ─────────────────────────────────────────────
            # Name fields — try split first name/last name, then full name
            await self._try_fill(
                page,
                'input[name*="first"], input[id*="first"]',
                first_name,
            )
            await self._try_fill(
                page,
                'input[name*="last"], input[id*="last"]',
                last_name,
            )
            await self._try_fill(
                page,
                (
                    'input[name*="full"], '
                    'input[name*="name"]:not([name*="first"]):not([name*="last"])'
                ),
                full_name,
            )

            # Contact
            await self._try_fill(
                page,
                'input[type="email"], input[name*="email"]',
                personal.get("email", ""),
            )
            await self._try_fill(
                page,
                'input[type="tel"], input[name*="phone"]',
                personal.get("phone", ""),
            )

            # Links
            if links.get("linkedin"):
                await self._try_fill(
                    page,
                    'input[name*="linkedin"], input[placeholder*="LinkedIn"]',
                    links["linkedin"],
                )
            if links.get("github"):
                await self._try_fill(
                    page,
                    'input[name*="github"], input[placeholder*="GitHub"]',
                    links["github"],
                )
            if links.get("portfolio") or links.get("website"):
                url_val = links.get("portfolio") or links.get("website", "")
                await self._try_fill(
                    page,
                    (
                        'input[name*="portfolio"], input[name*="website"], '
                        'input[placeholder*="website"]'
                    ),
                    url_val,
                )

            # Resume
            if resume_path:
                try:
                    await page.set_input_files('input[type="file"]', resume_path)
                except Exception:
                    pass

            # Cover letter / message
            if application.cover_letter:
                await self._try_fill(
                    page,
                    (
                        'textarea[name*="cover"], textarea[name*="letter"], '
                        'textarea[name*="message"], textarea[placeholder*="cover"], '
                        "textarea"
                    ),
                    application.cover_letter,
                )

            # Custom questions (text, numeric, dropdowns) — label-aware.
            # FormBrain abstains rather than guess, so a question we cannot
            # answer truthfully is left blank instead of filled with "0"/"Yes".
            blocked = await fill_form_safely(page, brain, job, application.qa_answers)
            if blocked:
                # A required question has no confident answer — never submit a
                # made-up one; hand the application to a human instead.
                await browser.close()
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="draft_only",
                    error=needs_review_error(blocked),
                )

            # ── Submit ────────────────────────────────────────────────────────
            submit_clicked = False
            for submit_sel in (
                'button[type="submit"]',
                'button:has-text("Submit")',
                'button:has-text("Send Application")',
                'input[type="submit"]',
            ):
                btn = page.locator(submit_sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    submit_clicked = True
                    break

            if not submit_clicked:
                await browser.close()
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error="Could not find submit button on Comeet form",
                )

            await page.wait_for_timeout(3000)

            result = browser_submission_result(
                platform=self.platform_name,
                page_url=page.url,
                html=await page.content(),
            )
            await browser.close()
            return result

    @staticmethod
    async def _try_fill(page, selector: str, value: str) -> None:
        """Fill first matching visible, editable input — silently skip if not found."""
        if not value:
            return
        try:
            el = page.locator(f"{selector}:visible").first
            if await el.count() > 0 and await el.is_editable():
                await el.fill(value)
        except Exception:
            pass
