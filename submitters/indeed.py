"""Indeed Apply submitter — Playwright browser automation.

Indeed Apply (Instant Apply) lets candidates apply directly on Indeed
without leaving the site.  This submitter uses Playwright to:
  1. Log in with stored cookies or email/password
  2. Navigate to the job listing
  3. Click "Apply now" / "Easily apply"
  4. Fill out the application form
  5. Submit

Setup (choose one):
  Option A — Cookie file (recommended):
    1. Log into indeed.com in your browser
    2. Export cookies to JSON (e.g. "Cookie-Editor" extension)
    3. Set INDEED_COOKIES_FILE=/path/to/indeed_cookies.json in .env

  Option B — Email + Password:
    Set INDEED_EMAIL=you@example.com and INDEED_PASSWORD=secret in .env

Requirements:
    pip install ".[browser]"
    playwright install chromium
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult

logger = structlog.get_logger(__name__)

_INDEED_URL_RE = re.compile(r"indeed\.com", re.IGNORECASE)
_JOB_ID_RE = re.compile(r"jk=([a-f0-9]+)", re.IGNORECASE)

_NAV_TIMEOUT  = 20_000
_ELEM_TIMEOUT = 10_000
_SHORT_WAIT   = 1_500


class IndeedSubmitter(BaseSubmitter):
    """Submit via Indeed Instant Apply using Playwright."""

    platform_name = "indeed"

    def __init__(
        self,
        cookies_file: str = "",
        email: str = "",
        password: str = "",
    ):
        self.cookies_file = cookies_file or os.getenv("INDEED_COOKIES_FILE", "")
        self.email    = email    or os.getenv("INDEED_EMAIL", "")
        self.password = password or os.getenv("INDEED_PASSWORD", "")

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return "indeed.com" in url

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
                error="Indeed credentials not configured. Set INDEED_COOKIES_FILE or INDEED_EMAIL+INDEED_PASSWORD in .env",
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
                ok = await self._load_cookies(ctx)
            else:
                ok = await self._login(ctx)

            if not ok:
                await browser.close()
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error="Indeed authentication failed",
                )

            page = await ctx.new_page()
            try:
                result = await self._apply(page, job_url, application, user_profile, resume_path)
            except Exception as exc:
                logger.error("indeed_apply_error", error=str(exc))
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
        try:
            raw = Path(self.cookies_file).read_text(encoding="utf-8")
            cookies_data = json.loads(raw)
            cookies = [
                {
                    "name":   c.get("name", ""),
                    "value":  c.get("value", ""),
                    "domain": c.get("domain", ".indeed.com"),
                    "path":   c.get("path", "/"),
                    "httpOnly": c.get("httpOnly", False),
                    "secure":   c.get("secure", True),
                }
                for c in cookies_data if isinstance(c, dict)
            ]
            await ctx.add_cookies(cookies)
            logger.info("indeed_cookies_loaded", count=len(cookies))
            return True
        except Exception as exc:
            logger.error("indeed_cookie_load_failed", error=str(exc))
            return False

    async def _login(self, ctx) -> bool:
        page = await ctx.new_page()
        try:
            await page.goto("https://secure.indeed.com/auth", timeout=_NAV_TIMEOUT)
            await page.wait_for_timeout(2000)

            # Fill email
            email_input = page.locator('input[type="email"], input[name="__email"]').first
            if await email_input.is_visible(timeout=5000):
                await email_input.fill(self.email)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(1500)

            # Fill password
            pwd_input = page.locator('input[type="password"]').first
            if await pwd_input.is_visible(timeout=5000):
                await pwd_input.fill(self.password)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(3000)

            if self.detect_captcha(await page.content()):
                logger.warning("indeed_login_captcha")
                return False

            logged_in = "indeed.com/jobs" in page.url or "dashboard" in page.url
            if not logged_in:
                # Some redirects are fine
                logger.info("indeed_login_redirected", url=page.url)
            return True

        except Exception as exc:
            logger.error("indeed_login_error", error=str(exc))
            return False
        finally:
            await page.close()

    # ── Application flow ──────────────────────────────────────────────────────

    async def _apply(self, page, job_url: str, application: GeneratedApplication,
                     user_profile: dict, resume_path: str | None) -> SubmissionResult:
        personal = user_profile.get("personal", {})

        await page.goto(job_url, timeout=_NAV_TIMEOUT)
        await page.wait_for_timeout(_SHORT_WAIT)

        if self.detect_captcha(await page.content()):
            return SubmissionResult(
                success=True, platform=self.platform_name, status="draft_only",
                error="CAPTCHA detected on Indeed job page",
            )

        # Click "Apply now" or "Easily apply"
        apply_btn = page.locator(
            'button:has-text("Apply now"), '
            'a:has-text("Apply now"), '
            'button[id*="applyButton"], '
            'a[data-testid="applyButton"]'
        ).first

        if not await apply_btn.is_visible(timeout=5000):
            return SubmissionResult(
                success=True, platform=self.platform_name, status="draft_only",
                error="Indeed apply button not found — job may redirect to external site",
            )

        await apply_btn.click(timeout=_ELEM_TIMEOUT)
        await page.wait_for_timeout(_SHORT_WAIT)

        # Work through multi-step form
        max_steps = 10
        for step in range(max_steps):
            content = await page.content()

            if self.detect_captcha(content):
                return SubmissionResult(
                    success=True, platform=self.platform_name, status="draft_only",
                    error="CAPTCHA appeared during Indeed Apply",
                )

            await self._fill_indeed_fields(page, application, user_profile, resume_path)

            # Check for final submission
            submit_btn = page.locator(
                'button:has-text("Submit your application"), '
                'button[data-testid="SubmitButton"]'
            ).first
            if await submit_btn.is_visible(timeout=2000):
                await submit_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(2000)
                logger.info("indeed_application_submitted", url=job_url)
                return SubmissionResult(
                    success=True, platform=self.platform_name, status="submitted",
                    confirmation_url=job_url,
                )

            # Click Continue / Next
            continue_btn = page.locator(
                'button:has-text("Continue"), '
                'button:has-text("Next"), '
                'button[data-testid="ContinueButton"]'
            ).first
            if await continue_btn.is_visible(timeout=2000):
                await continue_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(_SHORT_WAIT)
            else:
                break

        return SubmissionResult(
            success=True, platform=self.platform_name, status="draft_only",
            error="Indeed Apply form did not reach submission step",
        )

    async def _fill_indeed_fields(self, page, application: GeneratedApplication,
                                  user_profile: dict, resume_path: str | None) -> None:
        """Fill visible Indeed form fields."""
        personal = user_profile.get("personal", {})
        links    = user_profile.get("links", {})

        field_map = {
            "name":        personal.get("name", ""),
            "email":       personal.get("email", ""),
            "phone":       personal.get("phone", ""),
            "city":        personal.get("location", "").split(",")[0].strip(),
            "website":     links.get("portfolio") or links.get("website", ""),
            "linkedinUrl": links.get("linkedin", ""),
        }

        for name, value in field_map.items():
            if not value:
                continue
            locator = page.locator(f'input[name="{name}"]').first
            if await locator.count() > 0 and await locator.is_visible() and await locator.is_editable():
                await locator.fill(value)

        # Resume upload
        if resume_path and Path(resume_path).exists():
            file_input = page.locator('input[type="file"]').first
            if await file_input.count() > 0:
                await file_input.set_input_files(resume_path)
                await page.wait_for_timeout(1000)

        # Cover letter (if a textarea is visible)
        cl_field = page.locator(
            'textarea[name="coverletter"], '
            'textarea[data-testid="cover-letter-textarea"], '
            'div[contenteditable="true"]'
        ).first
        if await cl_field.count() > 0 and await cl_field.is_visible():
            if await cl_field.is_editable():
                await cl_field.fill(application.cover_letter or "")

        # Work authorization — try to select "Yes"
        auth_radios = page.locator('input[type="radio"][value="YES"], input[type="radio"][value="yes"]')
        for i in range(await auth_radios.count()):
            r = auth_radios.nth(i)
            if await r.is_visible():
                await r.check()

        # Numeric fields (years of experience) — fill safely
        num_inputs = page.locator('input[type="number"]')
        for i in range(await num_inputs.count()):
            el = num_inputs.nth(i)
            if await el.is_visible() and await el.is_editable():
                val = await el.input_value()
                if not val:
                    await el.fill("0")
