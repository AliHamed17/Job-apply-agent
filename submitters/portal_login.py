"""Submitter for career portals behind a login (Workday, Taleo, iCIMS, NVIDIA).

Two authentication paths, tried in order:

1. **Saved browser session** (preferred). The user signs in once themselves in
   a real browser; Playwright reuses that persisted profile via
   ``PORTAL_BROWSER_PROFILE_DIR``. Nothing has to hold a password, and it is
   the only approach that survives the SSO and MFA these portals front.
2. **Stored credentials** from ``core.credentials.CredentialVault``, used only
   when no session is configured, so behaviour degrades to the previous
   implementation rather than to nothing. This path frequently fails against
   SSO and bot detection, and repeated failed sign-ins can lock the account.

Whichever path runs, the result is reported honestly. The previous version was
not: it filled the form, uploaded the CV, called ``browser.close()`` **without
ever clicking submit**, and returned ``status="submitted"`` with a fabricated
confirmation id — and its ``except`` handler returned ``status="submitted"``
as well, so a navigation timeout was also recorded as a successful
application. Every job routed there appeared on the dashboard as applied-to
having never been submitted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from core.config import get_settings
from core.credentials import CredentialVault
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.form_brain import FormBrain
from submitters.safe_fill import fill_form_safely, needs_review_error

logger = structlog.get_logger(__name__)

_NAV_TIMEOUT = 45000
_PORTAL_DOMAINS = ("nvidia", "workday", "myworkdayjobs", "taleo", "icims")

_APPLY_ENTRY_SELECTORS = (
    "a:has-text('Apply Manually')",
    "button:has-text('Apply Manually')",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
)
_SUBMIT_SELECTORS = (
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "button[type='submit']",
    "input[type='submit']",
    "a:has-text('Submit')",
)
_SUCCESS_MARKERS = (
    "application received",
    "thank you for applying",
    "your application has been submitted",
    "successfully submitted",
    "application submitted",
)


class PortalLoginSubmitter(BaseSubmitter):
    """Apply on login-walled career portals."""

    platform_name = "portal_login"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        if not url:
            return False
        return any(domain in url for domain in _PORTAL_DOMAINS)

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
            return self._draft(
                "Playwright not installed. Run: "
                "pip install 'job-apply-agent[browser]' && playwright install chromium"
            )

        settings = get_settings()
        apply_url = job.apply_url or job.source_url or ""
        if not apply_url:
            return self._draft("No apply URL on the job")

        profile_dir = getattr(settings, "portal_browser_profile_dir", "") or ""
        use_session = bool(profile_dir) and Path(profile_dir).exists()

        from profile.models import UserProfile

        profile_obj = UserProfile(**user_profile) if user_profile else UserProfile()
        brain = FormBrain(profile_obj)

        async with async_playwright() as pw:
            if use_session:
                logger.info("portal_using_saved_session", profile_dir=profile_dir)
                ctx = await pw.chromium.launch_persistent_context(
                    profile_dir, headless=True, viewport={"width": 1280, "height": 800}
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                closer = ctx.close
            else:
                logger.info("portal_no_saved_session_using_credentials")
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    )
                )
                page = await ctx.new_page()
                closer = browser.close

            try:
                return await self._run(
                    page, job, application, profile_obj, brain,
                    apply_url, resume_path, settings, use_session,
                )
            except Exception as exc:
                # Report the failure. The previous version returned
                # status="submitted" from here, so every timeout was recorded
                # as a successful application.
                logger.error("portal_login_submit_error", error=str(exc))
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=str(exc)[:300],
                )
            finally:
                await closer()

    async def _run(
        self, page, job, application, profile_obj, brain,
        apply_url, resume_path, settings, use_session,
    ) -> SubmissionResult:
        await page.goto(apply_url, timeout=_NAV_TIMEOUT)
        await asyncio.sleep(2)

        if self.detect_captcha(await page.content()):
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="captcha_blocked",
                error="CAPTCHA detected on the portal",
            )

        await self._click_first(page, _APPLY_ENTRY_SELECTORS, wait=2)

        if await self._needs_login(page):
            if use_session:
                # The saved session expired. Do not fall back to typing a
                # password: the user signs in themselves, and a scripted retry
                # here is what trips account lockout.
                return self._draft(
                    "Portal session expired — sign in again manually, then retry."
                )
            if not await self._login_with_credentials(page, apply_url):
                return self._draft(
                    "Portal sign-in did not complete. Set PORTAL_BROWSER_PROFILE_DIR "
                    "and sign in once manually — these portals front SSO/MFA, which "
                    "scripted login cannot pass."
                )

        personal = profile_obj.personal
        name_parts = (personal.name or "").split()
        await self._fill_first(
            page,
            ("input[name*='first'], input[id*='first'], input[aria-label*='First Name']",),
            name_parts[0] if name_parts else "",
        )
        await self._fill_first(
            page,
            ("input[name*='last'], input[id*='last'], input[aria-label*='Last Name']",),
            " ".join(name_parts[1:]),
        )
        await self._fill_first(
            page,
            ("input[type='tel'], input[name*='phone'], input[id*='phone']",),
            personal.phone or "",
        )
        await self._fill_first(
            page, ("input[type='email'], input[name*='email']",), personal.email or ""
        )

        if resume_path and Path(resume_path).exists():
            try:
                await page.locator("input[type='file']").first.set_input_files(resume_path)
                await asyncio.sleep(2)
            except Exception as exc:
                logger.warning("portal_resume_upload_failed", error=str(exc))

        blocked = await fill_form_safely(page, brain, job, application.qa_answers)
        if blocked:
            logger.info("portal_login_needs_review", blocked=blocked)
            return self._draft(needs_review_error(blocked))

        if settings.dry_run:
            return self._draft("DRY_RUN: form filled but not submitted")

        # The previous version stopped here and claimed success. Without this
        # click, nothing is ever actually submitted.
        if not await self._click_first(page, _SUBMIT_SELECTORS, wait=3):
            return self._draft("No submit control found on the portal form")

        content = (await page.content()).lower()
        if not any(marker in content for marker in _SUCCESS_MARKERS):
            return SubmissionResult(
                success=False,
                platform=self.platform_name,
                status="failed",
                error="Submit clicked but no confirmation appeared",
            )

        logger.info("portal_login_submitted", job=job.title, company=job.company)
        return SubmissionResult(
            success=True,
            platform=self.platform_name,
            status="submitted",
            confirmation_url=page.url,
        )

    # ── helpers ───────────────────────────────────────────────────────

    def _draft(self, reason: str) -> SubmissionResult:
        """A truthful non-submission: the application is kept for review."""
        return SubmissionResult(
            success=True,
            platform=self.platform_name,
            status="draft_only",
            error=reason,
        )

    async def _login_with_credentials(self, page, apply_url: str) -> bool:
        """Fallback sign-in using the stored vault credential."""
        cred = CredentialVault.get_credential_for_url(apply_url)
        # Deliberately not logging the username — it is the account identifier.
        logger.info("portal_credential_login_attempt", domain=cred.domain)
        try:
            email = page.locator(
                "input[type='email'], input[name='username'], "
                "input[id*='email'], input[id*='user']"
            ).first
            password = page.locator("input[type='password'], input[name='password']").first
            if not (await email.count() and await password.count()):
                return False
            await email.fill(cred.username)
            await password.fill(cred.password)
            await self._click_first(
                page,
                (
                    "button[type='submit']",
                    "button:has-text('Sign In')",
                    "button:has-text('Log In')",
                    "input[type='submit']",
                ),
                wait=3,
            )
        except Exception as exc:
            logger.warning("portal_credential_login_failed", error=str(exc))
            return False
        # Confirm it worked rather than assuming it did.
        return not await self._needs_login(page)

    @staticmethod
    async def _needs_login(page) -> bool:
        try:
            if await page.locator("input[type='password']").count() > 0:
                return True
        except Exception:
            pass
        url = (page.url or "").lower()
        return any(marker in url for marker in ("login", "signin", "sso", "auth"))

    @staticmethod
    async def _click_first(page, selectors: tuple[str, ...], wait: int = 0) -> bool:
        for selector in selectors:
            try:
                el = page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    if wait:
                        await asyncio.sleep(wait)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _fill_first(page, selectors: tuple[str, ...], value: str) -> None:
        if not value:
            return
        for selector in selectors:
            try:
                el = page.locator(selector).first
                if await el.count() > 0 and await el.is_editable():
                    await el.fill(value)
                    return
            except Exception:
                continue
