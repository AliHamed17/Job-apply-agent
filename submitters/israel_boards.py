"""Submitter for the Israeli job boards (Drushim, AllJobs, JobMaster).

Replaces a stub that returned ``status="submitted"`` with a fabricated
confirmation id without opening a browser or touching the network. Anything
routed to it would have been recorded as a completed application — the
dashboard would show "submitted" for a job nobody applied to.

Follows the same shape as the other browser submitters here: CAPTCHA is
detected and never bypassed, questions go through FormBrain via safe_fill so
nothing is answered untruthfully, a required question we cannot answer aborts
to human review, and success is verified against the page rather than assumed.
"""

from __future__ import annotations

import structlog

from core.config import get_settings
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.form_brain import FormBrain
from submitters.safe_fill import fill_form_safely, needs_review_error

logger = structlog.get_logger(__name__)

_BOARD_DOMAINS = ("drushim.co.il", "alljobs.co.il", "jobmaster.co.il", "jobs.co.il")

# Hebrew and English apply-button text, most- to least-specific.
_APPLY_BUTTON_SELECTORS = (
    "button:has-text('הגשת מועמדות')",
    "a:has-text('הגשת מועמדות')",
    "button:has-text('שלח קורות חיים')",
    "button:has-text('הגש מועמדות')",
    "button:has-text('שליחה')",
    "button[type='submit']",
    "input[type='submit']",
)

# Page text that indicates the application actually went through.
_SUCCESS_MARKERS = (
    "מועמדותך נשלחה",
    "המועמדות נשלחה",
    "נשלח בהצלחה",
    "תודה על פנייתך",
    "application received",
    "thank you",
)


class DrushimSubmitter(BaseSubmitter):
    """Submit applications on Israeli job boards via browser automation."""

    platform_name = "drushim"

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return any(domain in url for domain in _BOARD_DOMAINS)

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
                success=True,
                platform=self.platform_name,
                status="draft_only",
                error=(
                    "Playwright not installed. Run: "
                    "pip install 'job-apply-agent[browser]' && playwright install chromium"
                ),
            )

        settings = get_settings()
        job_url = job.apply_url or job.source_url or ""
        if not job_url:
            return SubmissionResult(
                success=True,
                platform=self.platform_name,
                status="draft_only",
                error="No apply URL on the job",
            )

        from profile.models import UserProfile

        profile_obj = UserProfile(**user_profile) if user_profile else UserProfile()
        personal = user_profile.get("personal", {}) if user_profile else {}
        brain = FormBrain(profile_obj)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(job_url, timeout=30000)
                await page.wait_for_timeout(1500)

                if self.detect_captcha(await page.content()):
                    return SubmissionResult(
                        success=False,
                        platform=self.platform_name,
                        status="captcha_blocked",
                        error="CAPTCHA detected on the application page",
                    )

                # These boards label inputs in Hebrew, so match on type/name
                # attributes rather than visible label text.
                await self._try_fill(
                    page,
                    'input[type="email"], input[name*="mail"], input[id*="mail"]',
                    personal.get("email", ""),
                )
                await self._try_fill(
                    page,
                    'input[type="tel"], input[name*="phone"], input[name*="mobile"]',
                    personal.get("phone", ""),
                )
                await self._try_fill(
                    page,
                    'input[name*="name"], input[id*="name"]',
                    personal.get("name", ""),
                )

                if resume_path:
                    try:
                        await page.set_input_files('input[type="file"]', resume_path)
                    except Exception as exc:
                        # A CV is the whole point of applying on these boards;
                        # do not submit without one.
                        logger.warning("drushim_resume_upload_failed", error=str(exc))
                        return SubmissionResult(
                            success=True,
                            platform=self.platform_name,
                            status="draft_only",
                            error="Could not attach the CV to the form",
                        )

                blocked = await fill_form_safely(page, brain, job, application.qa_answers)
                if blocked:
                    logger.info("drushim_needs_review", blocked=blocked)
                    return SubmissionResult(
                        success=True,
                        platform=self.platform_name,
                        status="draft_only",
                        error=needs_review_error(blocked),
                    )

                if settings.dry_run:
                    return SubmissionResult(
                        success=True,
                        platform=self.platform_name,
                        status="draft_only",
                        error="DRY_RUN: form filled but not submitted",
                    )

                if not await self._click_apply(page):
                    return SubmissionResult(
                        success=True,
                        platform=self.platform_name,
                        status="draft_only",
                        error="No apply button found on the page",
                    )

                await page.wait_for_timeout(3000)
                content = (await page.content()).lower()
                confirmed = any(m.lower() in content for m in _SUCCESS_MARKERS)
                if not confirmed:
                    # Report honestly rather than claim a submission we have
                    # no evidence for.
                    return SubmissionResult(
                        success=False,
                        platform=self.platform_name,
                        status="failed",
                        error="Submit clicked but no confirmation appeared",
                    )

                logger.info("drushim_submitted", job=job.title, company=job.company)
                return SubmissionResult(
                    success=True,
                    platform=self.platform_name,
                    status="submitted",
                    confirmation_url=page.url,
                )
            except Exception as exc:
                logger.error("drushim_submit_error", error=str(exc))
                return SubmissionResult(
                    success=False,
                    platform=self.platform_name,
                    status="failed",
                    error=str(exc)[:300],
                )
            finally:
                await browser.close()

    @staticmethod
    async def _try_fill(page, selector: str, value: str) -> None:
        """Fill the first match, ignoring absent or non-editable fields."""
        if not value:
            return
        try:
            el = page.locator(selector).first
            if await el.count() > 0 and await el.is_editable():
                await el.fill(value)
        except Exception:
            pass

    @staticmethod
    async def _click_apply(page) -> bool:
        for selector in _APPLY_BUTTON_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    return True
            except Exception:
                continue
        return False
