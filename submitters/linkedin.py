"""LinkedIn Easy Apply submitter — Playwright browser automation.

Uses a stored LinkedIn session (cookies) or email/password credentials to
log in and submit applications via LinkedIn Easy Apply.

Setup (choose one):
  Option A — Cookie file (recommended, more stable):
    1. Log into LinkedIn in Chrome/Firefox
    2. Export cookies to JSON using a browser extension
       (e.g. "Cookie-Editor" → Export → Netscape/JSON format)
    3. Set LINKEDIN_COOKIES_FILE=/path/to/linkedin_cookies.json in .env

  Option B — Email + Password:
    Set LINKEDIN_EMAIL=you@example.com and LINKEDIN_PASSWORD=secret in .env
    Note: LinkedIn may trigger 2FA or CAPTCHA — the cookie method is safer.

Requirements:
    pip install ".[browser]"   (playwright + browser binaries)
    playwright install chromium
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_LI_URL_RE = re.compile(r"linkedin\.com/jobs", re.IGNORECASE)
_JOB_ID_RE = re.compile(r"(?:view|currentJobId)[=/](\d+)", re.IGNORECASE)

# Timeouts (ms)
_NAV_TIMEOUT  = 20_000
_ELEM_TIMEOUT = 10_000
_SHORT_WAIT   = 1_500


class LinkedInSubmitter(BaseSubmitter):
    """Submit via LinkedIn Easy Apply using Playwright.

    Falls back to draft_only if:
    - Playwright not installed
    - No session credentials configured
    - CAPTCHA detected
    - Easy Apply not available for this job
    """

    platform_name = "linkedin"

    def __init__(
        self,
        cookies_file: str = "",
        email: str = "",
        password: str = "",
    ):
        self.cookies_file = cookies_file or os.getenv("LINKEDIN_COOKIES_FILE", "")
        self.email    = email    or os.getenv("LINKEDIN_EMAIL", "")
        self.password = password or os.getenv("LINKEDIN_PASSWORD", "")

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "linkedin.com/jobs" in url

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

        if not self.cookies_file and not (self.email and self.password):
            return SubmissionResult(
                success=True,
                platform=self.platform_name,
                status="draft_only",
                error="LinkedIn credentials not configured. Set LINKEDIN_COOKIES_FILE or LINKEDIN_EMAIL+LINKEDIN_PASSWORD in .env",
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

            # ── Load session ──────────────────────────────
            if self.cookies_file:
                result = await self._load_cookies(ctx)
                if not result:
                    await browser.close()
                    return SubmissionResult(
                        success=False,
                        platform=self.platform_name,
                        status="failed",
                        error=f"Failed to load LinkedIn cookies from {self.cookies_file}",
                    )
            else:
                result = await self._login(ctx)
                if not result:
                    await browser.close()
                    return SubmissionResult(
                        success=False,
                        platform=self.platform_name,
                        status="failed",
                        error="LinkedIn login failed — check credentials or use cookie method",
                    )

            page = await ctx.new_page()

            try:
                result = await self._apply(page, job_url, application, user_profile, resume_path)
            except Exception as exc:
                logger.error("linkedin_apply_error", error=str(exc))
                result = SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=str(exc),
                )
            finally:
                await browser.close()

        return result

    # ── Session helpers ───────────────────────────────────────────────────────

    async def _load_cookies(self, ctx) -> bool:
        """Load LinkedIn session from a cookie JSON file."""
        try:
            raw = Path(self.cookies_file).read_text(encoding="utf-8")
            cookies_data = json.loads(raw)

            # Normalise — supports both Netscape and JSON format
            cookies = []
            for c in cookies_data:
                if isinstance(c, dict):
                    cookies.append({
                        "name":   c.get("name", ""),
                        "value":  c.get("value", ""),
                        "domain": c.get("domain", ".linkedin.com"),
                        "path":   c.get("path", "/"),
                        "httpOnly": c.get("httpOnly", False),
                        "secure":   c.get("secure", True),
                    })

            await ctx.add_cookies(cookies)
            logger.info("linkedin_cookies_loaded", count=len(cookies))
            return True
        except Exception as exc:
            logger.error("linkedin_cookie_load_failed", error=str(exc))
            return False

    async def _login(self, ctx) -> bool:
        """Log into LinkedIn using email/password."""
        page = await ctx.new_page()
        try:
            await page.goto("https://www.linkedin.com/login", timeout=_NAV_TIMEOUT)
            await page.fill("#username", self.email, timeout=_ELEM_TIMEOUT)
            await page.fill("#password", self.password, timeout=_ELEM_TIMEOUT)
            await page.click('button[type="submit"]', timeout=_ELEM_TIMEOUT)
            await page.wait_for_timeout(3000)

            # Check for CAPTCHA / challenge
            if self.detect_captcha(await page.content()):
                logger.warning("linkedin_login_captcha")
                return False

            # Check we're logged in (feed page or redirect)
            if "feed" in page.url or "checkpoint" not in page.url:
                logger.info("linkedin_logged_in")
                return True

            logger.warning("linkedin_login_challenge", url=page.url)
            return False

        except Exception as exc:
            logger.error("linkedin_login_error", error=str(exc))
            return False
        finally:
            await page.close()

    # ── Application flow ──────────────────────────────────────────────────────

    async def _apply(self, page, job_url: str, application: GeneratedApplication,
                     user_profile: dict, resume_path: str | None) -> SubmissionResult:
        """Navigate to the job and complete Easy Apply."""
        personal = user_profile.get("personal", {})

        await page.goto(job_url, timeout=_NAV_TIMEOUT)
        await page.wait_for_timeout(_SHORT_WAIT)

        # Detect CAPTCHA on job page
        if self.detect_captcha(await page.content()):
            return SubmissionResult(
                success=True,
                platform=self.platform_name,
                status="draft_only",
                error="CAPTCHA detected on LinkedIn job page",
            )

        # Find the Easy Apply button
        easy_apply_btn = page.locator(
            'button.jobs-apply-button, '
            'button[aria-label*="Easy Apply"], '
            'button[data-control-name="jobdetails_topcard_inapply"]'
        ).first

        if not await easy_apply_btn.is_visible(timeout=5000):
            return SubmissionResult(
                success=True,
                platform=self.platform_name,
                status="draft_only",
                error="Easy Apply button not found — job may require external application",
            )

        await easy_apply_btn.click(timeout=_ELEM_TIMEOUT)
        await page.wait_for_timeout(_SHORT_WAIT)

        # Work through the multi-step form
        max_steps = 8
        for step in range(max_steps):
            content = await page.content()

            if self.detect_captcha(content):
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="draft_only",
                    error="CAPTCHA appeared during Easy Apply",
                )

            # Fill visible form fields
            await self._fill_form_fields(page, application, user_profile, resume_path)

            # Check if there's a "Submit application" button
            submit_btn = page.locator(
                'button[aria-label*="Submit application"], '
                'button[data-control-name="submit_unify"]'
            ).first

            if await submit_btn.is_visible(timeout=2000):
                await submit_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(2000)
                logger.info("linkedin_application_submitted", url=job_url)
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="submitted",
                    confirmation_url=job_url,
                )

            # Click Next / Continue / Review
            next_btn = page.locator(
                'button[aria-label*="Continue to next step"], '
                'button[aria-label*="Review your application"], '
                'button[data-control-name="continue_unify"]'
            ).first

            if await next_btn.is_visible(timeout=2000):
                await next_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(_SHORT_WAIT)
            else:
                break  # No more buttons — unexpected state

        return SubmissionResult(
            success=True,
            platform=self.platform_name,
            status="draft_only",
            error="Easy Apply form did not reach submission step",
        )

    async def _fill_form_fields(self, page, application: GeneratedApplication,
                                user_profile: dict, resume_path: str | None) -> None:
        """Fill all visible form inputs with profile data and Q&A answers."""
        personal = user_profile.get("personal", {})
        links    = user_profile.get("links", {})
        name_parts = (personal.get("name", "") or "").split()

        field_map = {
            "phone":       personal.get("phone", ""),
            "phoneNumber": personal.get("phone", ""),
            "city":        personal.get("location", "").split(",")[0].strip(),
            "email":       personal.get("email", ""),
            "firstName":   name_parts[0] if name_parts else "",
            "lastName":    " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
            "linkedin":    links.get("linkedin", ""),
            "website":     links.get("portfolio") or links.get("website", ""),
            "coverLetter": application.cover_letter or "",
            "summary":     application.cover_letter or "",
        }

        for name, value in field_map.items():
            if not value:
                continue
            locator = page.locator(f'input[name="{name}"], textarea[name="{name}"]')
            if await locator.count() > 0:
                el = locator.first
                if await el.is_visible() and await el.is_editable():
                    await el.fill(value)

        # Resume upload
        if resume_path and Path(resume_path).exists():
            file_input = page.locator('input[type="file"]').first
            if await file_input.count() > 0:
                await file_input.set_input_files(resume_path)
                await page.wait_for_timeout(1000)

        # Cover letter textarea
        cover_letter_textarea = page.locator(
            'textarea[name="coverLetter"], '
            'textarea[aria-label*="cover letter"], '
            '.jobs-easy-apply-content textarea'
        ).first
        if await cover_letter_textarea.count() > 0:
            if await cover_letter_textarea.is_visible() and await cover_letter_textarea.is_editable():
                await cover_letter_textarea.fill(application.cover_letter or "")

        # Work authorization — select "Yes" on any auth-related radio group
        for auth_label in ["authorized to work", "legally authorized", "work authorization", "sponsorship"]:
            yes_radio = page.locator(
                f'label:has-text("Yes"):near(label:has-text("{auth_label}"))'
            ).first
            if await yes_radio.count() > 0 and await yes_radio.is_visible():
                await yes_radio.click()

        # Numeric inputs (years of experience) — fill with value from qa_answers or 0
        exp_inputs = page.locator('input[type="number"]')
        for i in range(await exp_inputs.count()):
            el = exp_inputs.nth(i)
            if await el.is_visible() and await el.is_editable():
                current = await el.input_value()
                if not current:
                    await el.fill("0")

        # Custom open-text questions — match labels to qa_answers
        if application.qa_answers:
            text_inputs = page.locator(
                '.jobs-easy-apply-form-element input[type="text"], '
                '.jobs-easy-apply-form-element textarea'
            )
            count = await text_inputs.count()
            for i in range(count):
                el = text_inputs.nth(i)
                if not await el.is_visible():
                    continue
                if not await el.is_editable():
                    continue
                current = await el.input_value()
                if current:
                    continue
                # Try to find the label text near this element
                label_el = page.locator(f'label[for="{await el.get_attribute("id") or ""}"]').first
                label_text = ""
                if await label_el.count() > 0:
                    label_text = (await label_el.inner_text()).strip().lower()
                if not label_text:
                    # Try aria-label
                    label_text = (await el.get_attribute("aria-label") or "").lower()
                if not label_text:
                    continue
                # Find the best matching answer from qa_answers
                best_answer = ""
                for q_key, q_val in application.qa_answers.items():
                    if any(kw in label_text for kw in q_key.lower().split("_")):
                        best_answer = str(q_val)
                        break
                if not best_answer:
                    # Fallback: use the first non-empty answer
                    best_answer = next((str(v) for v in application.qa_answers.values() if v), "")
                if best_answer:
                    await el.fill(best_answer[:500])

        # Select dropdowns — handle common yes/no questions
        selects = page.locator('select')
        for i in range(await selects.count()):
            sel = selects.nth(i)
            if not await sel.is_visible():
                continue
            options = await sel.locator('option').all_text_contents()
            options_lower = [o.lower() for o in options]
            # For "Yes/No" dropdowns on authorization questions, pick "Yes"
            if "yes" in options_lower and "no" in options_lower:
                await sel.select_option(label=next(o for o in options if o.lower() == "yes"))
