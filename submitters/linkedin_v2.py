"""LinkedIn Easy Apply v2 submitter — walker, abort-don't-lie, DRY_RUN aware.

Unlike the legacy `submitters/linkedin.py` (hardcoded field-name mapping),
this submitter *walks* each Easy Apply modal step generically:

    html = await page.content()
    fields = parse_fields(html)          # submitters.field_extractor
    plan = await resolve_step(fields, brain, job)   # submitters.form_brain

`resolve_step` is the pure, unit-tested core of this module (see
`tests/test_linkedin_v2.py`). It never fabricates an answer: a required
field the FormBrain cannot answer confidently sets `StepPlan.blocked_by`
and the walker discards the draft and reports `NEEDS_REVIEW:<label>`
rather than submitting incomplete/guessed data ("abort-don't-lie").

CAPTCHA is never bypassed — any detection trips the shared RateGovernor
cooldown and returns a `draft_only` result.

When `settings.dry_run` is set, a fully-answered application is discarded
immediately before the final "Submit application" click, so the whole
walker (including field-filling and resume upload) can be exercised
against real LinkedIn pages without ever actually applying.

Uses a *persistent* browser context (`pw.chromium.launch_persistent_context`
at `settings.linkedin_browser_profile_dir`) so a logged-in session on disk
is reused across runs, avoiding repeated login/2FA/CAPTCHA challenges.

Requirements:
    pip install ".[browser]"   (playwright + browser binaries)
    playwright install chromium
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from core.config import get_settings
from core.governor import get_governor
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters import selectors
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.field_extractor import parse_fields
from submitters.form_brain import FieldSpec, FormBrain

logger = structlog.get_logger(__name__)

# Timeouts (ms)
_NAV_TIMEOUT = 20_000
_ELEM_TIMEOUT = 10_000
_SHORT_WAIT = 1_500
_MAX_STEPS = 8


@dataclass
class StepPlan:
    """Outcome of resolving one Easy Apply modal step's fields."""

    fills: dict[str, str]
    blocked_by: str | None = None


async def resolve_step(fields: list[FieldSpec], brain: FormBrain, job: JobData | None) -> StepPlan:
    """Resolve one modal step's fields via FormBrain — abort-don't-lie.

    Iterates `fields` in order. Every field the brain can answer
    confidently is added to `fills`. The *first* required field the
    brain cannot answer confidently stops processing and sets
    `blocked_by` to that field's label — the caller must discard rather
    than submit. Non-required unanswerable fields are silently skipped
    (they're left blank / untouched on the page).
    """
    fills: dict[str, str] = {}
    blocked_by: str | None = None

    for f in fields:
        res = await brain.answer(f, job)
        if res.confident and res.value is not None:
            fills[f.label] = res.value
        elif f.required:
            blocked_by = f.label
            break
        # else: non-required and unanswerable — skip silently

    return StepPlan(fills=fills, blocked_by=blocked_by)


class LinkedInV2Submitter(BaseSubmitter):
    """Easy Apply v2 — generic field walker driven by FormBrain.

    Falls back to draft_only if:
    - Playwright not installed
    - Easy Apply not available for this job
    - CAPTCHA detected (governor cooldown tripped, never bypassed)
    - A required field can't be answered confidently (NEEDS_REVIEW)
    - `settings.dry_run` is set (walker runs to completion, then discards)
    """

    platform_name = "linkedin"

    def __init__(self, db=None):
        self.db = db

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
                error=(
                    "Playwright not installed. Run: "
                    "pip install 'job-apply-agent[browser]' && playwright install chromium"
                ),
            )

        from profile.models import UserProfile  # noqa: PLC0415

        settings = get_settings()
        governor = get_governor()
        profile = UserProfile(**user_profile) if user_profile else UserProfile()
        brain = FormBrain(profile, db=self.db)
        job_url = job.apply_url or job.source_url or ""

        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                settings.linkedin_browser_profile_dir,
                headless=True,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    result = await self._apply(
                        page, job_url, job, brain, resume_path, settings, governor
                    )
                except Exception as exc:
                    logger.error("linkedin_v2_apply_error", error=str(exc))
                    result = SubmissionResult(
                        success=False,
                        platform=self.platform_name,
                        status="failed",
                        error=str(exc),
                    )
            finally:
                await ctx.close()

        return result

    # ── Application flow ────────────────────────────────────────────────

    async def _apply(
        self,
        page,
        job_url: str,
        job: JobData,
        brain: FormBrain,
        resume_path: str | None,
        settings,
        governor,
    ) -> SubmissionResult:
        """Navigate to the job and walk the Easy Apply modal to completion."""
        await page.goto(job_url, timeout=_NAV_TIMEOUT)
        await page.wait_for_timeout(_SHORT_WAIT)

        if self.detect_captcha(await page.content()):
            governor.trip_cooldown()
            await self._notify_challenge(settings)
            return SubmissionResult(
                success=True, platform=self.platform_name,
                status="draft_only", error="CAPTCHA",
            )

        easy_apply_btn = page.locator(selectors.join(selectors.EASY_APPLY_BUTTON)).first
        if not await easy_apply_btn.is_visible(timeout=5000):
            return SubmissionResult(
                success=True, platform=self.platform_name,
                status="draft_only",
                error="Easy Apply button not found — job may require external application",
            )

        await easy_apply_btn.click(timeout=_ELEM_TIMEOUT)
        await page.wait_for_timeout(_SHORT_WAIT)

        for _ in range(_MAX_STEPS):
            html = await page.content()

            if self.detect_captcha(html):
                governor.trip_cooldown()
                await self._notify_challenge(settings)
                await self._discard(page)
                return SubmissionResult(
                    success=True, platform=self.platform_name,
                    status="draft_only", error="CAPTCHA",
                )

            # Resume/file inputs are uploaded directly — they never go
            # through FormBrain (there's nothing for an LLM to "answer").
            fields = [f for f in parse_fields(html) if f.kind != "file"]
            plan = await resolve_step(fields, brain, job)

            if plan.blocked_by:
                await self._discard(page)
                return SubmissionResult(
                    success=True, platform=self.platform_name,
                    status="draft_only",
                    error=f"NEEDS_REVIEW:{plan.blocked_by}",
                )

            await self._fill_step(page, plan.fills, resume_path)

            submit_btn = page.locator(selectors.join(selectors.SUBMIT_BUTTON)).first
            if await submit_btn.is_visible(timeout=2000):
                if settings.dry_run:
                    await self._discard(page)
                    return SubmissionResult(
                        success=True, platform=self.platform_name,
                        status="draft_only", error="DRY_RUN",
                    )

                await submit_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(2000)

                success_dialog = page.locator(selectors.join(selectors.SUCCESS_DIALOG)).first
                if await success_dialog.is_visible(timeout=5000):
                    logger.info("linkedin_v2_submitted", url=job_url)
                    return SubmissionResult(
                        success=True, platform=self.platform_name,
                        status="submitted", confirmation_url=job_url,
                    )
                # Clicked submit but never saw confirmation — never claim
                # success we can't verify (abort-don't-lie).
                return SubmissionResult(
                    success=False, platform=self.platform_name,
                    status="failed",
                    error="Submit clicked but no success dialog appeared",
                )

            advance_btn = page.locator(
                selectors.join(selectors.NEXT_BUTTON + selectors.REVIEW_BUTTON)
            ).first
            if await advance_btn.is_visible(timeout=2000):
                await advance_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(_SHORT_WAIT)
            else:
                break  # No recognizable control — unexpected state

        await self._discard(page)
        return SubmissionResult(
            success=True, platform=self.platform_name,
            status="draft_only",
            error="Easy Apply form did not reach submission step",
        )

    # ── Field filling ────────────────────────────────────────────────────

    async def _fill_step(self, page, fills: dict[str, str], resume_path: str | None) -> None:
        """Apply the resolved answers for this step, then upload the resume."""
        for label, value in fills.items():
            filled = await self._fill_by_label(page, label, value)
            if not filled:
                logger.debug("linkedin_v2_field_not_matched", label=label)

        if resume_path and Path(resume_path).exists():
            file_input = page.locator('input[type="file"]').first
            if await file_input.count() > 0:
                await file_input.set_input_files(resume_path)
                await page.wait_for_timeout(1000)

    async def _fill_by_label(self, page, label: str, value: str) -> bool:
        """Locate the control for `label` (input/textarea/select/radio-group) and set `value`."""
        label_el = page.locator(f'label:has-text("{label}")').first
        if await label_el.count() == 0:
            return False

        input_id = await label_el.get_attribute("for")
        if input_id:
            target = page.locator(f'#{input_id}')
            if await target.count() > 0:
                tag = await target.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    await target.select_option(label=str(value))
                    return True
                if tag in ("input", "textarea"):
                    if await target.is_visible() and await target.is_editable():
                        await target.fill(str(value))
                        return True

        # Radio/checkbox group: label is the fieldset legend text; click
        # the option whose own label text matches the resolved value.
        option = page.locator(
            f'fieldset:has(legend:has-text("{label}")) label:has-text("{value}")'
        ).first
        if await option.count() > 0:
            await option.click()
            return True

        return False

    # ── Challenge alert ──────────────────────────────────────────────────

    async def _notify_challenge(self, settings) -> None:
        """Best-effort WhatsApp alert on CAPTCHA/challenge detection.

        ``notify_challenge`` itself never raises; lazy-imported here to
        avoid a module-level cycle (``worker.alerts`` lazily imports
        ``api.routes.webhook``).
        """
        from worker.alerts import notify_challenge  # noqa: PLC0415
        await notify_challenge(settings)

    # ── Discard chain ────────────────────────────────────────────────────

    async def _discard(self, page) -> None:
        """Discard the in-progress application via LinkedIn's dismiss/confirm dialog."""
        try:
            discard_btn = page.locator(selectors.join(selectors.DISCARD_BUTTON)).first
            if await discard_btn.is_visible(timeout=2000):
                await discard_btn.click(timeout=_ELEM_TIMEOUT)
                await page.wait_for_timeout(500)
                confirm_btn = page.locator(selectors.join(selectors.DISCARD_CONFIRM_BUTTON)).first
                if await confirm_btn.is_visible(timeout=2000):
                    await confirm_btn.click(timeout=_ELEM_TIMEOUT)
        except Exception as exc:
            logger.warning("linkedin_v2_discard_failed", error=str(exc))
