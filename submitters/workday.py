"""Authenticated Workday application flow with verified, fail-closed outcomes.

The operator signs in once with ``scripts.portal_session_bootstrap``. Workers
reuse that dedicated browser profile; they never read a browser password store
or receive a plaintext password.

The flow prefers Workday's "Use My Last Application" path, resolves remaining
questions through FormBrain, and only records success after an authoritative
Workday confirmation. Unknown post-click outcomes are never retried
automatically.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import structlog
from bs4 import BeautifulSoup

from core.config import get_settings
from core.portal_sessions import (
    PortalSessionError,
    PortalSessionLease,
    portal_session_for_url,
)
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.browser_trace import RedactedTrace
from submitters.employer_workflows import (
    EmployerWorkflow,
    load_employer_workflows,
    workflow_for_url,
)
from submitters.field_extractor import parse_fields
from submitters.form_brain import (
    FieldSpec,
    FormBrain,
    is_sensitive_question,
    normalize_question,
)

logger = structlog.get_logger(__name__)

WORKDAY_SELECTOR_VERSION = "workday-candidate-v1"
_WORKDAY_KEYWORDS = ("myworkday.com", "myworkdayjobs.com", "workday.com/en-us")
_NAV_TIMEOUT = 45_000
_ACTION_TIMEOUT = 8_000
_SHORT_WAIT = 1_200
_MAX_STEPS = 12

_ACTION_SELECTORS: dict[str, tuple[str, ...]] = {
    "apply": (
        '[data-automation-id="jobPostingApplyButton"]',
        'a[data-automation-id="jobPostingApplyButton"]',
        'button:has-text("Apply")',
        'a:has-text("Apply")',
    ),
    "use_last_application": (
        'button:has-text("Use My Last Application")',
        'a:has-text("Use My Last Application")',
        '[data-automation-id="useMyLastApplication"]',
    ),
    "autofill_resume": (
        'button:has-text("Autofill with Resume")',
        'a:has-text("Autofill with Resume")',
    ),
    "apply_manually": (
        'button:has-text("Apply Manually")',
        'a:has-text("Apply Manually")',
    ),
    "continue": (
        'button[data-automation-id="bottom-navigation-next-button"]',
        'button:has-text("Save and Continue")',
        'button:has-text("Next")',
    ),
    "submit": (
        'button[data-automation-id="bottom-navigation-next-button"]:has-text("Submit")',
        'button[data-automation-id="submitApplication"]',
        'button:has-text("Submit")',
    ),
}


@dataclass(frozen=True)
class WorkdayPageAssessment:
    state: str
    terminal_reason: str | None = None


def assess_workday_page(html: str, url: str = "") -> WorkdayPageAssessment:
    """Classify a sanitized snapshot without retaining page or field content."""
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.stripped_strings).casefold()
    low_url = (url or "").casefold()

    challenge_markers = (
        "captcha",
        "recaptcha",
        "hcaptcha",
        "verify you are human",
        "security challenge",
    )
    if any(marker in text for marker in challenge_markers):
        return WorkdayPageAssessment("challenge", "CHALLENGE_DETECTED")

    has_password = soup.find("input", attrs={"type": "password"}) is not None
    if has_password or any(marker in low_url for marker in ("/login", "/signin", "/sign-in")):
        return WorkdayPageAssessment("session_expired", "SESSION_EXPIRED")

    if any(
        marker in text
        for marker in (
            "you've already applied",
            "you have already applied",
            "already applied for this job",
        )
    ):
        return WorkdayPageAssessment("already_applied", "ALREADY_APPLIED")

    confirmation = soup.find(attrs={"data-automation-id": "confirmationPage"})
    if confirmation is not None or (
        "application submitted" in text
        and any(marker in text for marker in ("successfully", "thank you", "received"))
    ):
        return WorkdayPageAssessment("submitted", "SUBMITTED")

    if (
        soup.find(attrs={"data-automation-id": "reviewPage"}) is not None
        or "review your application" in text
        or (
            re.search(r"\breview\b", text)
            and any(
                re.search(r"\bsubmit\b", button.get_text(" ", strip=True), re.I)
                for button in soup.find_all("button")
            )
        )
    ):
        return WorkdayPageAssessment("review")

    if "use my last application" in text:
        return WorkdayPageAssessment("entry_options")
    if soup.find(attrs={"data-automation-id": "jobPostingApplyButton"}) is not None:
        return WorkdayPageAssessment("job")
    if soup.find(attrs={"data-automation-id": "bottom-navigation-next-button"}) is not None:
        return WorkdayPageAssessment("form")
    if soup.find(["input", "textarea", "select"]) is not None:
        return WorkdayPageAssessment("form")
    return WorkdayPageAssessment("unknown", "SELECTOR_DRIFT")


class WorkdaySubmitter(BaseSubmitter):
    """Workday browser adapter using tenant-isolated persistent sessions."""

    platform_name = "workday"

    def __init__(self, db=None, trace: RedactedTrace | None = None):
        self.db = db
        self.trace = trace or RedactedTrace(selector_version=WORKDAY_SELECTOR_VERSION)

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url or "").lower()
        return any(keyword in url for keyword in _WORKDAY_KEYWORDS)

    def _terminal_result(
        self,
        *,
        success: bool,
        status: str,
        reason_code: str,
        error: str | None = None,
        confirmation_id: str | None = None,
        confirmation_url: str | None = None,
    ) -> SubmissionResult:
        self.trace.record("terminal", terminal_reason=reason_code)
        return SubmissionResult(
            success=success,
            platform=self.platform_name,
            status=status,
            confirmation_id=confirmation_id,
            confirmation_url=confirmation_url,
            error=error,
            reason_code=reason_code,
            diagnostic_details={
                "selector_version": WORKDAY_SELECTOR_VERSION,
                "terminal_reason": reason_code,
                "step_count": sum(
                    1 for event in self.trace.events if event.get("event") == "step_resolved"
                ),
                "events": self.trace.events[-30:],
            },
        )

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        job_url = job.apply_url or job.source_url or ""
        settings = get_settings()

        try:
            session = portal_session_for_url(
                job_url,
                settings.portal_browser_profile_root,
            )
        except PortalSessionError:
            return self._terminal_result(
                success=False,
                status="failed",
                reason_code="PORTAL_URL_INVALID",
                error="PORTAL_URL_INVALID",
            )

        if not session.ready:
            return self._terminal_result(
                success=True,
                status="draft_only",
                reason_code="PORTAL_SESSION_REQUIRED",
                error="NEEDS_REVIEW:PORTAL_SESSION_REQUIRED",
            )

        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError:
            return self._terminal_result(
                success=True,
                status="draft_only",
                reason_code="BROWSER_UNAVAILABLE",
                error="NEEDS_REVIEW:BROWSER_UNAVAILABLE",
            )

        from profile.models import UserProfile  # noqa: PLC0415

        profile = UserProfile(**user_profile) if user_profile else UserProfile()
        brain = FormBrain(
            profile,
            db=self.db,
            selected_cv_id=None,
        )
        policy = workflow_for_url(
            job_url,
            load_employer_workflows(settings.employer_workflow_path),
        )

        try:
            with PortalSessionLease(
                session,
                stale_minutes=settings.portal_session_lock_minutes,
            ):
                async with async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(
                        str(session.profile_dir),
                        headless=settings.portal_browser_headless,
                        viewport={"width": 1280, "height": 900},
                    )
                    try:
                        page = context.pages[0] if context.pages else await context.new_page()
                        return await self._apply(
                            page=page,
                            job_url=job_url,
                            job=job,
                            application=application,
                            brain=brain,
                            resume_path=resume_path,
                            policy=policy,
                            settings=settings,
                        )
                    finally:
                        await context.close()
        except PortalSessionError as exc:
            code = str(exc) if str(exc) == "PORTAL_SESSION_BUSY" else "PORTAL_SESSION_ERROR"
            return self._terminal_result(
                success=True,
                status="draft_only",
                reason_code=code,
                error=f"NEEDS_REVIEW:{code}",
            )
        except Exception as exc:
            logger.warning("workday_browser_failed", error=type(exc).__name__)
            return self._terminal_result(
                success=False,
                status="failed",
                reason_code="BROWSER_FLOW_FAILED",
                error="BROWSER_FLOW_FAILED",
            )

    async def _apply(
        self,
        *,
        page,
        job_url: str,
        job: JobData,
        application: GeneratedApplication,
        brain: FormBrain,
        resume_path: str | None,
        policy: EmployerWorkflow,
        settings,
    ) -> SubmissionResult:
        self.trace.record("navigation_started")
        await page.goto(job_url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(_SHORT_WAIT)

        assessment = await self._assessment(page)
        terminal = self._terminal_from_assessment(assessment, job_url)
        if terminal:
            return terminal

        if assessment.state == "job":
            if not await self._click_action(page, "apply"):
                return self._terminal_result(
                    success=True,
                    status="draft_only",
                    reason_code="SELECTOR_DRIFT",
                    error="NEEDS_REVIEW:WORKDAY_APPLY_BUTTON_UNAVAILABLE",
                )
            self.trace.record("apply_opened")
            await page.wait_for_timeout(_SHORT_WAIT)

        used_last_application = False
        assessment = await self._assessment(page)
        terminal = self._terminal_from_assessment(assessment, job_url)
        if terminal:
            return terminal

        if (
            policy.prefer_last_application
            and settings.portal_reuse_last_application
            and await self._click_action(page, "use_last_application")
        ):
            used_last_application = True
            self.trace.record("entry_selected", step=0, resolver_sources=["last_application"])
            await page.wait_for_timeout(_SHORT_WAIT)
        elif (
            resume_path
            and Path(resume_path).is_file()
            and await self._click_action(page, "autofill_resume")
        ):
            self.trace.record("entry_selected", step=0, resolver_sources=["selected_cv"])
            await page.wait_for_timeout(_SHORT_WAIT)
            await self._upload_resume(page, resume_path)
        elif await self._click_action(page, "apply_manually"):
            self.trace.record("entry_selected", step=0, resolver_sources=["profile"])
            await page.wait_for_timeout(_SHORT_WAIT)

        source_completed = False
        for step_number in range(1, _MAX_STEPS + 1):
            assessment = await self._assessment(page)
            terminal = self._terminal_from_assessment(assessment, job_url)
            if terminal:
                return terminal

            if assessment.state == "review" or await self._action_visible(page, "submit"):
                if settings.dry_run:
                    return self._terminal_result(
                        success=True,
                        status="draft_only",
                        reason_code="DRY_RUN_REVIEW_READY",
                        error="DRY_RUN_REVIEW_READY",
                    )
                if not settings.portal_final_submit_enabled:
                    return self._terminal_result(
                        success=True,
                        status="draft_only",
                        reason_code="REVIEW_READY",
                        error="REVIEW_READY",
                    )

                try:
                    clicked = await self._click_action(page, "submit")
                except Exception:
                    # Playwright can time out after dispatching a click. Once
                    # the final control may have fired, retrying is unsafe.
                    return self._terminal_result(
                        success=False,
                        status="unknown",
                        reason_code="SUBMIT_UNCONFIRMED",
                        error="SUBMIT_OUTCOME_UNKNOWN",
                    )
                if not clicked:
                    return self._terminal_result(
                        success=True,
                        status="draft_only",
                        reason_code="SELECTOR_DRIFT",
                        error="NEEDS_REVIEW:WORKDAY_SUBMIT_BUTTON_UNAVAILABLE",
                    )
                self.trace.record("final_submit_clicked", step=step_number)
                try:
                    await page.wait_for_timeout(3_000)
                    final = await self._assessment(page)
                except Exception:
                    return self._terminal_result(
                        success=False,
                        status="unknown",
                        reason_code="SUBMIT_UNCONFIRMED",
                        error="SUBMIT_OUTCOME_UNKNOWN",
                    )
                if final.state in {"submitted", "already_applied"}:
                    return self._confirmed_result(job_url, final.state)
                if final.state == "challenge":
                    return self._terminal_result(
                        success=False,
                        status="unknown",
                        reason_code="CHALLENGE_AFTER_SUBMIT",
                        error="SUBMIT_OUTCOME_UNKNOWN",
                    )
                return self._terminal_result(
                    success=False,
                    status="unknown",
                    reason_code="SUBMIT_UNCONFIRMED",
                    error="Submit clicked but no success confirmation appeared",
                )

            html = await page.content()
            blocked, sources, field_types, selector_failed = await self._resolve_and_fill_step(
                page=page,
                html=html,
                job=job,
                application=application,
                brain=brain,
                resume_path=resume_path,
                used_last_application=used_last_application,
            )

            if not source_completed and self._has_source_prompt(html):
                if not policy.source_path:
                    blocked = blocked or "How did you hear about us?"
                else:
                    source_completed = await self._fill_source_path(
                        page,
                        policy.source_path,
                    )
                    if not source_completed:
                        selector_failed = True
                    else:
                        sources.append("employer_workflow")

            self.trace.record(
                "step_resolved",
                step=step_number,
                field_types=sorted(field_types),
                resolver_sources=sorted(set(sources)),
            )

            if blocked:
                return self._terminal_result(
                    success=True,
                    status="draft_only",
                    reason_code="REQUIRED_FIELD_UNKNOWN",
                    error=f"NEEDS_REVIEW:{blocked[:120]}",
                )
            if selector_failed:
                return self._terminal_result(
                    success=True,
                    status="draft_only",
                    reason_code="SELECTOR_DRIFT",
                    error="NEEDS_REVIEW:WORKDAY_FIELD_SELECTOR_DRIFT",
                )

            if await self._click_action(page, "continue"):
                await page.wait_for_timeout(_SHORT_WAIT)
                continue

            return self._terminal_result(
                success=True,
                status="draft_only",
                reason_code="SELECTOR_DRIFT",
                error="NEEDS_REVIEW:WORKDAY_STEP_CONTROL_UNAVAILABLE",
            )

        return self._terminal_result(
            success=True,
            status="draft_only",
            reason_code="STEP_LIMIT_REACHED",
            error="NEEDS_REVIEW:WORKDAY_STEP_LIMIT_REACHED",
        )

    async def _assessment(self, page) -> WorkdayPageAssessment:
        return assess_workday_page(await page.content(), getattr(page, "url", ""))

    def _terminal_from_assessment(
        self,
        assessment: WorkdayPageAssessment,
        job_url: str,
    ) -> SubmissionResult | None:
        if assessment.state in {"submitted", "already_applied"}:
            return self._confirmed_result(job_url, assessment.state)
        if assessment.state == "challenge":
            return self._terminal_result(
                success=True,
                status="draft_only",
                reason_code="CHALLENGE_DETECTED",
                error="NEEDS_REVIEW:CHALLENGE_DETECTED",
            )
        if assessment.state == "session_expired":
            return self._terminal_result(
                success=True,
                status="draft_only",
                reason_code="SESSION_EXPIRED",
                error="NEEDS_REVIEW:SESSION_EXPIRED",
            )
        return None

    def _confirmed_result(self, job_url: str, state: str) -> SubmissionResult:
        match = re.search(r"\b(?:JR|REQ|R)[-_]?\d+\b", job_url, re.I)
        reason = "ALREADY_APPLIED" if state == "already_applied" else "SUBMITTED"
        return self._terminal_result(
            success=True,
            status="submitted",
            reason_code=reason,
            confirmation_id=match.group(0) if match else None,
            confirmation_url=job_url,
        )

    async def _resolve_and_fill_step(
        self,
        *,
        page,
        html: str,
        job: JobData,
        application: GeneratedApplication,
        brain: FormBrain,
        resume_path: str | None,
        used_last_application: bool,
    ) -> tuple[str | None, list[str], set[str], bool]:
        fields = [
            field for field in parse_fields(html) if field.kind != "file" and field.label.strip()
        ]
        sources: list[str] = []
        field_types = {field.kind for field in fields}
        selector_failed = False

        for field in fields[:50]:
            current = await self._current_value(page, field)
            if current:
                continue

            answer = self._generated_answer(field, application.qa_answers)
            source = "generated_qa" if answer else None
            if answer is None:
                resolved = await brain.answer(field, job)
                if resolved.confident and resolved.value is not None:
                    answer = str(resolved.value)
                    source = resolved.source
                elif field.required:
                    return field.label, sources, field_types, selector_failed
                else:
                    continue

            filled = await self._fill_by_label(page, field, answer)
            if not filled and field.required:
                selector_failed = True
                break
            if filled and source:
                sources.append(source)

        if not used_last_application and resume_path and Path(resume_path).is_file():
            uploaded = await self._upload_resume(page, resume_path)
            if uploaded:
                field_types.add("file")
                sources.append("selected_cv")
        return None, sources, field_types, selector_failed

    @staticmethod
    def _generated_answer(field: FieldSpec, answers: dict[str, str]) -> str | None:
        if is_sensitive_question(field.label):
            return None
        normalized = normalize_question(field.label)
        for key, value in (answers or {}).items():
            key_tokens = [
                token
                for token in normalize_question(str(key).replace("_", " ")).split()
                if len(token) > 3
            ]
            if key_tokens and all(token in normalized for token in key_tokens) and value:
                return str(value)
        return None

    async def _current_value(self, page, field: FieldSpec) -> bool:
        if field.kind in {"radio", "checkbox"}:
            container = await self._field_container(page, field.label)
            if container is None:
                return False
            try:
                return await container.locator("input:checked").count() > 0
            except Exception:
                return False

        locator = page.get_by_label(field.label, exact=True)
        element = await self._first_visible(locator)
        if element is None:
            return False
        try:
            return bool((await element.input_value()).strip())
        except Exception:
            return False

    async def _fill_by_label(self, page, field: FieldSpec, value: str) -> bool:
        if field.kind == "radio":
            container = await self._field_container(page, field.label)
            if container is None:
                return False
            option = container.get_by_role("radio", name=value, exact=True)
            element = await self._first_visible(option)
            if element is not None:
                await element.check()
                return True
            return False

        if field.kind == "checkbox":
            container = await self._field_container(page, field.label)
            if container is None:
                return False
            option = container.get_by_role("checkbox", name=value, exact=True)
            element = await self._first_visible(option)
            if element is None and value.casefold() in {"yes", "true", "agree", "accepted"}:
                element = await self._first_visible(container.get_by_role("checkbox"))
            if element is not None:
                await element.check()
                return True
            return False

        target = await self._first_visible(page.get_by_label(field.label, exact=True))
        if target is None:
            target = await self._first_visible(
                page.get_by_role("combobox", name=field.label, exact=True)
            )
            if target is not None:
                await target.click()
                option = await self._first_visible(
                    page.get_by_role("option", name=value, exact=True)
                )
                if option is None:
                    return False
                await option.click()
                return True
            return False

        try:
            tag = await target.evaluate("element => element.tagName.toLowerCase()")
            if tag == "select":
                await target.select_option(label=value)
            else:
                await target.fill(value[:500])
            return True
        except Exception:
            return False

    async def _field_container(self, page, label: str):
        label_element = await self._first_visible(page.get_by_text(label, exact=True))
        if label_element is None:
            return None
        for xpath in (
            "xpath=ancestor::fieldset[1]",
            'xpath=ancestor::*[@data-automation-id="formField"][1]',
        ):
            try:
                container = label_element.locator(xpath)
                if await container.count() > 0:
                    return container.first
            except Exception:
                continue
        return None

    async def _fill_source_path(self, page, source_path: list[str]) -> bool:
        prompt_selectors = (
            '[data-automation-id="formField"]:has-text("How Did You Hear") button',
            '[data-automation-id="formField"]:has-text("hear about us") button',
            'button[aria-label*="How Did You Hear"]',
            'button[aria-label*="hear about us"]',
        )
        prompt = await self._first_visible_from_selectors(page, prompt_selectors)
        if prompt is None:
            return False
        await prompt.click()
        await page.wait_for_timeout(300)
        for value in source_path:
            option = await self._first_visible(page.get_by_text(value, exact=True))
            if option is None:
                return False
            await option.click()
            await page.wait_for_timeout(300)
        return True

    @staticmethod
    def _has_source_prompt(html: str) -> bool:
        text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True).casefold()
        return "how did you hear" in text or "hear about us" in text

    async def _upload_resume(self, page, resume_path: str) -> bool:
        inputs = page.locator('input[type="file"]')
        count = min(await inputs.count(), 5)
        for index in range(count):
            item = inputs.nth(index)
            try:
                await item.set_input_files(resume_path)
                return True
            except Exception:
                continue
        return False

    async def _click_action(self, page, action: str) -> bool:
        element = await self._first_visible_from_selectors(
            page,
            _ACTION_SELECTORS[action],
        )
        if element is None:
            return False
        await element.click(timeout=_ACTION_TIMEOUT)
        return True

    async def _action_visible(self, page, action: str) -> bool:
        return (
            await self._first_visible_from_selectors(
                page,
                _ACTION_SELECTORS[action],
            )
            is not None
        )

    async def _first_visible_from_selectors(self, page, selectors: tuple[str, ...]):
        for selector in selectors:
            element = await self._first_visible(page.locator(selector))
            if element is not None:
                return element
        return None

    @staticmethod
    async def _first_visible(locator):
        try:
            count = min(await locator.count(), 10)
        except Exception:
            return None
        for index in range(count):
            element = locator.nth(index)
            try:
                if await element.is_visible(timeout=500):
                    return element
            except Exception:
                continue
        return None


def serialize_workday_trace(trace: RedactedTrace) -> str:
    """Test/documentation helper returning only the bounded redacted trace."""
    return json.dumps(trace.events, separators=(",", ":"), sort_keys=True)
