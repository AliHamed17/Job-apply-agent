"""Fixture-qualified, two-phase Workday candidate-browser adapter.

This module deliberately has no Playwright dependency and performs no network
requests by itself.  A private runner supplies a :class:`WorkdayCandidateSession`
that owns one browser page on one event loop.  The adapter observes, fills, and
verifies that page through a narrow protocol while keeping page HTML, answers,
CV paths, and cookies in memory only.

The checked-in descriptor is fixture-qualified with an empty live-canary
scope.  Consequently the production adapter can inspect forms but both the
registry and :meth:`WorkdayBrowserV2.commit` refuse the irreversible click.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from profile.models import UserProfile
from secrets import token_bytes
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup, Tag

from core.form_planning import AnswerPolicyContext, AnswerPolicyV1
from core.submission_domain import (
    AlreadyAppliedOutcome,
    AnswerDecisionV1,
    AnswerDisposition,
    CommitOutcome,
    ConfirmedSubmittedOutcome,
    FailedBeforeCommitOutcome,
    FieldType,
    FinalSubmitPermit,
    FormFieldConstraintsV1,
    FormFieldV1,
    FormOptionV1,
    FormPlanV1,
    NeedsReviewOutcome,
    PreflightOutcome,
    PreparedFinalActionV1,
    ReasonCode,
    UnknownOutcome,
)
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import AdapterPreflightContext, SubmitterRegistry
from submitters.confirmation import (
    AdapterEvidenceRule,
    EvidenceChannel,
    SubmissionEvidenceExpectation,
    SubmissionEvidenceObservation,
    verify_submission_evidence,
)
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    AdapterDescriptor,
    adapter_for_platform,
    adapter_for_url,
)

WORKDAY_V2_ADAPTER_VERSION = "2.0.0"
WORKDAY_V2_SELECTOR_VERSION = "workday-candidate-v2"
WORKDAY_CONFIRMATION_SELECTOR = 'main[data-automation-id="confirmationPage"][data-application-id]'
_MAX_FIXTURE_HTML_BYTES = 256 * 1024
_MAX_FIELD_COUNT = 200
_MAX_REVERSIBLE_STEPS = 12
_MAX_RESUME_BYTES = 20 * 1024 * 1024


def workday_public_hostname(
    url: str,
    *,
    expected_hostname: str | None = None,
) -> str:
    """Validate the network-independent portion of a Workday candidate URL.

    DNS is validated by the Playwright transport immediately before network
    use.  This earlier boundary still rejects clear SSRF inputs even when a
    test or qualification harness injects a non-network browser session.
    """

    candidate = (url or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or hostname != hostname.rstrip(".")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
        or any(ord(character) > 127 for character in hostname)
    ):
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    if (
        hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
        or (expected_hostname is not None and hostname != expected_hostname)
    ):
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    descriptor = adapter_for_url(candidate)
    if descriptor is None or descriptor.platform != "workday":
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    return hostname


class WorkdayPageState(StrEnum):
    """Bounded candidate-flow states recognized by the v2 selector contract."""

    JOB = "job"
    LOGIN = "login"
    MFA = "mfa"
    CHALLENGE = "challenge"
    CLOSED = "closed"
    ALREADY_APPLIED = "already_applied"
    FORM = "form"
    RESUME_UPLOAD = "resume_upload"
    REVIEW = "review"
    CONFIRMATION = "confirmation"
    SELECTOR_DRIFT = "selector_drift"


@dataclass(frozen=True, slots=True)
class WorkdayPageAssessment:
    state: WorkdayPageState
    reason_code: ReasonCode | None = None


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayBrowserSnapshot:
    """Ephemeral browser observation; callers must never persist this object."""

    html: str
    url: str = ""
    locale: str = "en"

    def __post_init__(self) -> None:
        if len(self.html.encode("utf-8")) > _MAX_FIXTURE_HTML_BYTES:
            raise ValueError("WORKDAY_SNAPSHOT_TOO_LARGE")


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayAttachmentProof:
    """Redacted, session-local proof of one fresh upload observation.

    ``receipt_sha256`` is derived from a new browser-side upload receipt and
    the selected CV digest.  It contains neither the local filename nor CV
    content and is never copied into a form plan or database record.
    """

    cv_id: str
    cv_sha256: str
    upload_complete: bool
    receipt_sha256: str | None = None

    def matches(self, *, cv_id: str, cv_sha256: str) -> bool:
        return (
            self.upload_complete is True
            and self.cv_id == cv_id
            and self.cv_sha256 == cv_sha256
            and self.receipt_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.receipt_sha256) is not None
        )


class WorkdayCandidateSession(Protocol):
    """One Workday page owned by the private runner's persistent browser."""

    async def navigate(self, url: str) -> None:
        """Navigate to the reviewed public job URL."""

    async def open_candidate_form(self) -> None:
        """Use only reversible Apply/last-application navigation."""

    async def snapshot(self) -> WorkdayBrowserSnapshot:
        """Return the current ephemeral DOM snapshot."""

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        """Upload immutable hash-verified bytes and prove completion."""

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        """Re-check the exact attachment without relying on its filename."""

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        """Apply reviewed decisions for only the currently observed step."""

    async def advance_reversible_step(self) -> None:
        """Advance exactly one reviewed non-final step."""

    async def click_final_action(self) -> None:
        """Perform the single irreversible click exactly once."""

    async def confirmation_reference(self) -> str | None:
        """Return one stable, actually-visible post-action employer reference."""

    async def close(self) -> None:
        """Release the page/session lease on its owning event loop."""


WorkdayBrowserFactory = Callable[[str], WorkdayCandidateSession]


class WorkdayAdapterBlockedError(RuntimeError):
    """Fail-closed inspection error containing only a bounded reason code."""

    def __init__(self, reason_code: ReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


@dataclass(slots=True)
class _PreparedState:
    session: WorkdayCandidateSession
    plan: FormPlanV1
    permit: FinalSubmitPermit
    pre_action_html: str
    clicked: bool = False


def _visible(element: Tag) -> bool:
    current: Tag | None = element
    while current is not None:
        if current.has_attr("hidden"):
            return False
        if str(current.get("aria-hidden", "")).strip().casefold() == "true":
            return False
        style = str(current.get("style", "")).replace(" ", "").casefold()
        if any(marker in style for marker in ("display:none", "visibility:hidden", "opacity:0")):
            return False
        class_value = current.get("class")
        classes = (
            {str(class_value).casefold()}
            if isinstance(class_value, str)
            else {str(item).casefold() for item in (class_value or ())}
        )
        if classes.intersection({"hidden", "sr-only", "visually-hidden", "d-none"}):
            return False
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return True


def _has_one_visible(soup: BeautifulSoup, selector: str) -> bool:
    try:
        return len([element for element in soup.select(selector) if _visible(element)]) == 1
    except Exception:
        return False


def _has_one_visible_confirmation_reference(soup: BeautifulSoup) -> bool:
    try:
        matches = [
            element
            for element in soup.select(WORKDAY_CONFIRMATION_SELECTOR)
            if _visible(element) and bool(str(element.get("data-application-id", "")).strip())
        ]
    except Exception:
        return False
    return len(matches) == 1


def assess_workday_v2_snapshot(html: str, url: str = "") -> WorkdayPageAssessment:
    """Classify one sanitized Workday snapshot using adapter-specific markers."""

    if len((html or "").encode("utf-8")) > _MAX_FIXTURE_HTML_BYTES:
        return WorkdayPageAssessment(
            WorkdayPageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        )
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.stripped_strings).casefold()
    low_url = (url or "").casefold()

    challenge_selectors = (
        '[data-automation-id="captcha"]',
        ".g-recaptcha",
        ".h-captcha",
        'iframe[src*="captcha"]',
    )
    if any(soup.select_one(selector) is not None for selector in challenge_selectors) or any(
        marker in text
        for marker in (
            "verify you are human",
            "security challenge",
            "complete the captcha",
        )
    ):
        return WorkdayPageAssessment(
            WorkdayPageState.CHALLENGE,
            ReasonCode.CHALLENGE_DETECTED,
        )

    if (
        soup.select_one('[data-automation-id="mfaChallenge"]') is not None
        or soup.select_one('input[autocomplete="one-time-code"]') is not None
    ):
        return WorkdayPageAssessment(WorkdayPageState.MFA, ReasonCode.MFA_REQUIRED)

    if (
        soup.select_one('[data-automation-id="signInPage"]') is not None
        or soup.select_one('input[type="password"]') is not None
        or any(marker in low_url for marker in ("/login", "/signin", "/sign-in"))
    ):
        return WorkdayPageAssessment(
            WorkdayPageState.LOGIN,
            ReasonCode.SESSION_EXPIRED,
        )

    if soup.select_one('[data-automation-id="jobPostingNotAvailable"]') is not None or any(
        marker in text
        for marker in (
            "this job is no longer available",
            "this position has been filled",
            "the job posting has expired",
        )
    ):
        return WorkdayPageAssessment(WorkdayPageState.CLOSED, ReasonCode.JOB_CLOSED)

    if soup.select_one('[data-automation-id="alreadyApplied"]') is not None or any(
        marker in text
        for marker in (
            "you have already applied for this job",
            "you've already applied for this job",
        )
    ):
        return WorkdayPageAssessment(
            WorkdayPageState.ALREADY_APPLIED,
            ReasonCode.ALREADY_APPLIED,
        )

    if _has_one_visible_confirmation_reference(soup):
        return WorkdayPageAssessment(WorkdayPageState.CONFIRMATION)

    review = soup.select_one('[data-automation-id="reviewPage"]')
    submit = soup.select_one('button[data-automation-id="submitApplication"]')
    if review is not None and submit is not None and _visible(submit):
        return WorkdayPageAssessment(WorkdayPageState.REVIEW)

    if soup.select_one(
        '[data-automation-id="resumeUpload"] input[type="file"], '
        'input[data-automation-id="file-upload-input"][type="file"]'
    ):
        return WorkdayPageAssessment(WorkdayPageState.RESUME_UPLOAD)

    if soup.select_one('[data-automation-id="formField"][data-field-id]') is not None:
        return WorkdayPageAssessment(WorkdayPageState.FORM)

    if soup.select_one('[data-automation-id="jobPostingApplyButton"]') is not None:
        return WorkdayPageAssessment(WorkdayPageState.JOB)

    return WorkdayPageAssessment(
        WorkdayPageState.SELECTOR_DRIFT,
        ReasonCode.SELECTOR_DRIFT,
    )


def _bounded_int(raw: object, *, minimum: int = 0) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if value < minimum:
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _bounded_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if not math.isfinite(value):
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _bounded_pattern(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or len(value) > 128 or any(ord(character) < 32 for character in value):
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _control_type(control: Tag) -> FieldType:
    if control.name == "textarea":
        return FieldType.TEXTAREA
    if control.name == "select":
        return FieldType.MULTI_SELECT if control.has_attr("multiple") else FieldType.SELECT
    raw = str(control.get("type", "text")).casefold()
    return {
        "checkbox": FieldType.CHECKBOX,
        "date": FieldType.DATE,
        "email": FieldType.EMAIL,
        "file": FieldType.FILE,
        "number": FieldType.NUMBER,
        "radio": FieldType.RADIO,
        "tel": FieldType.PHONE,
        "url": FieldType.URL,
    }.get(raw, FieldType.TEXT)


def _field_options(wrapper: Tag, control: Tag, field_type: FieldType) -> tuple[FormOptionV1, ...]:
    option_nodes: list[Tag]
    if field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
        option_nodes = [node for node in control.find_all("option") if isinstance(node, Tag)]
    elif field_type is FieldType.RADIO:
        option_nodes = [
            node for node in wrapper.select('input[type="radio"][value]') if isinstance(node, Tag)
        ]
    else:
        return ()

    options: list[FormOptionV1] = []
    for index, node in enumerate(option_nodes):
        value = str(node.get("value", "")).strip()
        if not value:
            continue
        label = (
            node.get_text(" ", strip=True)
            if node.name == "option"
            else str(node.get("data-option-label", value)).strip()
        )
        options.append(
            FormOptionV1(
                option_id=str(node.get("data-option-id", f"option-{index}")).strip(),
                value=value,
                label=label or value,
                disabled=node.has_attr("disabled"),
            )
        )
    return tuple(options)


def observe_workday_v2_fields(html: str) -> tuple[FormFieldV1, ...]:
    """Extract a bounded, deterministic field contract from sanitized markup."""

    if len((html or "").encode("utf-8")) > _MAX_FIXTURE_HTML_BYTES:
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
    soup = BeautifulSoup(html or "", "html.parser")
    wrappers = soup.select('[data-automation-id="formField"][data-field-id]')
    if len(wrappers) > _MAX_FIELD_COUNT:
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)

    fields: list[FormFieldV1] = []
    seen_ids: set[str] = set()
    for position, wrapper in enumerate(wrappers):
        field_id = str(wrapper.get("data-field-id", "")).strip()
        if (
            not field_id
            or len(field_id) > 500
            or field_id in seen_ids
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", field_id)
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        controls = wrapper.select("input, textarea, select")
        if not controls:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        control = controls[0]
        field_type = _control_type(control)
        if field_type is FieldType.RADIO:
            radio_names = {
                str(node.get("name", "")).strip()
                for node in controls
                if str(node.get("type", "")).casefold() == "radio"
            }
            if len(radio_names) != 1 or "" in radio_names:
                raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        elif len(controls) != 1:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

        label_node = wrapper.select_one("label, legend, [data-automation-id='fieldLabel']")
        label = label_node.get_text(" ", strip=True) if label_node is not None else ""
        if not label:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        canonical = str(wrapper.get("data-canonical-name", "")).strip() or None
        accepted = tuple(
            item.strip() for item in str(control.get("accept", "")).split(",") if item.strip()
        )[:32]
        fields.append(
            FormFieldV1(
                field_id=field_id,
                canonical_name=canonical,
                label=label,
                field_type=field_type,
                required=(
                    control.has_attr("required")
                    or str(wrapper.get("aria-required", "")).casefold() == "true"
                ),
                position=position,
                options=_field_options(wrapper, control, field_type),
                constraints=FormFieldConstraintsV1(
                    min_length=_bounded_int(control.get("minlength")),
                    max_length=_bounded_int(control.get("maxlength")),
                    min_value=_bounded_float(control.get("min")),
                    max_value=_bounded_float(control.get("max")),
                    pattern=_bounded_pattern(control.get("pattern")),
                    accepted_file_types=accepted,
                    multiple=control.has_attr("multiple"),
                ),
            )
        )
        seen_ids.add(field_id)
    return tuple(fields)


def workday_v2_form_fingerprint(
    fields: tuple[FormFieldV1, ...],
    step_field_counts: tuple[int, ...] | None = None,
) -> str:
    """Hash the exact ordered, step-bounded form contract.

    Step boundaries are part of the contract so moving an unchanged field to a
    later Workday page invalidates a reviewed plan.  The one-step default keeps
    the fixture observer useful without creating an alternate hash format.
    """

    counts = step_field_counts if step_field_counts is not None else (len(fields),)
    if (
        not counts
        or len(counts) > _MAX_REVERSIBLE_STEPS
        or any(type(count) is not int or count < 1 for count in counts)
        or sum(counts) != len(fields)
    ):
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
    offset = 0
    steps: list[list[dict[str, Any]]] = []
    for count in counts:
        steps.append([field.model_dump(mode="json") for field in fields[offset : offset + count]])
        offset += count

    payload = {
        "adapter_version": WORKDAY_V2_ADAPTER_VERSION,
        "selector_version": WORKDAY_V2_SELECTOR_VERSION,
        "steps": steps,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _globalize_step_fields(
    fields: tuple[FormFieldV1, ...],
    *,
    prior_fields: tuple[FormFieldV1, ...],
) -> tuple[FormFieldV1, ...]:
    if not fields:
        raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    prior_ids = {field.field_id for field in prior_fields}
    if prior_ids.intersection(field.field_id for field in fields):
        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
    offset = len(prior_fields)
    return tuple(
        field.model_copy(update={"position": offset + index}) for index, field in enumerate(fields)
    )


def _read_verified_resume_bytes(path: str, expected_sha256: str) -> bytes:
    with Path(path).open("rb") as handle:
        payload = handle.read(_MAX_RESUME_BYTES + 1)
    if (
        not payload
        or len(payload) > _MAX_RESUME_BYTES
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    return payload


def _descriptor() -> AdapterDescriptor:
    descriptor = adapter_for_platform("workday")
    if (
        descriptor is None
        or descriptor.adapter_version != WORKDAY_V2_ADAPTER_VERSION
        or descriptor.selector_version != WORKDAY_V2_SELECTOR_VERSION
        or descriptor.execution_contract_version != TWO_PHASE_EXECUTION_CONTRACT_VERSION
    ):
        raise RuntimeError("WORKDAY_V2_DESCRIPTOR_MISMATCH")
    return descriptor


class WorkdayBrowserV2:
    """Two-phase Workday adapter with an immutable final-action boundary."""

    def __init__(
        self,
        *,
        browser_factory: WorkdayBrowserFactory,
        answer_policy: AnswerPolicyV1 | None = None,
        descriptor: AdapterDescriptor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.descriptor = descriptor or _descriptor()
        self._browser_factory = browser_factory
        self._answer_policy = answer_policy or AnswerPolicyV1()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._prepared: dict[str, _PreparedState] = {}
        self._pending_preflight: dict[str, WorkdayCandidateSession] = {}
        self._cleanup_preflight_id: ContextVar[str | None] = ContextVar(
            f"workday-v2-cleanup-{id(self)}",
            default=None,
        )

    def can_inspect(self, job: JobData) -> bool:
        url = job.apply_url or job.source_url
        try:
            workday_public_hostname(url)
        except WorkdayAdapterBlockedError:
            return False
        candidate = adapter_for_url(url)
        return bool(
            candidate is not None
            and candidate.platform == "workday"
            and candidate.adapter_version == self.descriptor.adapter_version
            and candidate.selector_version == self.descriptor.selector_version
        )

    @staticmethod
    def _inspection_reason(assessment: WorkdayPageAssessment) -> ReasonCode | None:
        return assessment.reason_code or (
            ReasonCode.ALREADY_APPLIED
            if assessment.state is WorkdayPageState.CONFIRMATION
            else None
        )

    async def inspect(
        self,
        *,
        application_id: int,
        application_revision: int,
        job: JobData,
        application: GeneratedApplication,
        user_profile: Mapping[str, Any],
        resume_path: str | None,
        selected_cv_id: str | None = None,
        answer_policy: AnswerPolicyV1 | None = None,
    ) -> FormPlanV1:
        """Observe every reversible step and plan without a final action.

        Inspection may upload the selected CV and fill reversible fields in a
        private draft so later pages can be observed.  It never clicks Submit.
        If a field needs operator evidence, it returns an immutable partial
        plan with ``FORM_PLAN_INCOMPLETE`` and never advances that page.
        Reinspection must reach Workday Review before the global blocker is
        cleared.
        """

        if (
            not self.can_inspect(job)
            or not resume_path
            or not selected_cv_id
            or application.cv_sha256 is None
            or application.profile_version is None
            or not re.fullmatch(r"[0-9a-f]{64}", application.cv_sha256)
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            resume_bytes = _read_verified_resume_bytes(
                resume_path,
                application.cv_sha256,
            )
        except OSError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        try:
            profile = UserProfile.model_validate(dict(user_profile))
        except (TypeError, ValueError) as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN) from exc

        job_url = job.apply_url or job.source_url
        planner = answer_policy or self._answer_policy
        session = self._browser_factory(job_url)
        try:
            await session.navigate(job_url)
            snapshot = await session.snapshot()
            assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            if assessment.state is WorkdayPageState.JOB:
                await session.open_candidate_form()
                snapshot = await session.snapshot()
                assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            fields: tuple[FormFieldV1, ...] = ()
            decisions: tuple[AnswerDecisionV1, ...] = ()
            step_field_counts: tuple[int, ...] = ()
            proof: WorkdayAttachmentProof | None = None
            audit_identity: tuple[str, str, str, str] | None = None
            plan_blockers: tuple[ReasonCode, ...] = ()
            inspection_complete = False
            locale = snapshot.locale

            for _step in range(_MAX_REVERSIBLE_STEPS):
                reason = self._inspection_reason(assessment)
                if reason is not None:
                    raise WorkdayAdapterBlockedError(reason)
                if assessment.state is WorkdayPageState.REVIEW:
                    if not fields or proof is None:
                        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    final_proof = await session.verify_resume_attachment(
                        cv_id=selected_cv_id,
                        expected_sha256=application.cv_sha256,
                    )
                    if not final_proof.matches(
                        cv_id=selected_cv_id,
                        cv_sha256=application.cv_sha256,
                    ):
                        raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                    proof = final_proof
                    inspection_complete = True
                    break
                if assessment.state not in {
                    WorkdayPageState.FORM,
                    WorkdayPageState.RESUME_UPLOAD,
                }:
                    raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

                raw_step_fields = observe_workday_v2_fields(snapshot.html)
                step_fields = _globalize_step_fields(
                    raw_step_fields,
                    prior_fields=fields,
                )
                candidate_fields = (*fields, *step_fields)
                candidate_counts = (*step_field_counts, len(step_fields))
                if any(field.field_type is FieldType.FILE for field in step_fields):
                    proof = await session.ensure_resume_attachment(
                        resume_bytes=resume_bytes,
                        cv_id=selected_cv_id,
                        expected_sha256=application.cv_sha256,
                    )
                    if not proof.matches(
                        cv_id=selected_cv_id,
                        cv_sha256=application.cv_sha256,
                    ):
                        raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

                step_fingerprint = workday_v2_form_fingerprint(
                    tuple(candidate_fields),
                    tuple(candidate_counts),
                )
                context = AnswerPolicyContext(
                    profile=profile,
                    profile_version=application.profile_version,
                    selected_cv_id=selected_cv_id,
                    selected_cv_hash=application.cv_sha256,
                    attached_cv_id=proof.cv_id if proof is not None else None,
                    attached_cv_hash=proof.cv_sha256 if proof is not None else None,
                    attachment_verified=(
                        proof.matches(
                            cv_id=selected_cv_id,
                            cv_sha256=application.cv_sha256,
                        )
                        if proof is not None
                        else False
                    ),
                    adapter_name=self.descriptor.platform,
                    adapter_version=self.descriptor.adapter_version,
                    selector_version=self.descriptor.selector_version,
                    form_fingerprint=step_fingerprint,
                    locale=snapshot.locale,
                )
                policy = await planner.plan_fields(step_fields, context)
                if {decision.field_id for decision in policy.decisions} != {
                    field.field_id for field in step_fields
                }:
                    raise WorkdayAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
                candidate_audit = (
                    policy.prompt_version,
                    policy.model_provider,
                    policy.model_name,
                    policy.model_digest,
                )
                if any(value is not None for value in candidate_audit):
                    if not all(isinstance(value, str) for value in candidate_audit):
                        raise WorkdayAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
                    bounded_audit = (
                        str(candidate_audit[0]),
                        str(candidate_audit[1]),
                        str(candidate_audit[2]),
                        str(candidate_audit[3]),
                    )
                    if audit_identity is not None and bounded_audit != audit_identity:
                        raise WorkdayAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
                    audit_identity = bounded_audit

                fields = tuple(candidate_fields)
                decisions = (*decisions, *policy.decisions)
                step_field_counts = tuple(candidate_counts)
                locale = snapshot.locale
                if policy.blockers:
                    plan_blockers = tuple(dict.fromkeys(policy.blockers))
                    break
                await session.fill(policy.decisions)
                await session.advance_reversible_step()
                snapshot = await session.snapshot()
                assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            else:
                raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

            if not fields:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
            attachment_verified = bool(
                proof is not None
                and proof.matches(
                    cv_id=selected_cv_id,
                    cv_sha256=application.cv_sha256,
                )
            )
            if not inspection_complete and not attachment_verified:
                plan_blockers = tuple(
                    dict.fromkeys((*plan_blockers, ReasonCode.ATTACHMENT_UNVERIFIED))
                )
            if not inspection_complete:
                plan_blockers = tuple(
                    dict.fromkeys((*plan_blockers, ReasonCode.FORM_PLAN_INCOMPLETE))
                )
            if inspection_complete and not attachment_verified:
                raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
            fingerprint = workday_v2_form_fingerprint(fields, step_field_counts)
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise WorkdayAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
            plan = FormPlanV1(
                plan_id=uuid4(),
                application_id=application_id,
                application_revision=application_revision,
                adapter_name=self.descriptor.platform,
                adapter_version=self.descriptor.adapter_version,
                selector_version=self.descriptor.selector_version,
                form_fingerprint=fingerprint,
                selected_cv_id=selected_cv_id,
                selected_cv_hash=application.cv_sha256,
                attached_cv_id=proof.cv_id if proof is not None else selected_cv_id,
                attached_cv_hash=(proof.cv_sha256 if proof is not None else application.cv_sha256),
                attachment_verified=attachment_verified,
                profile_version=application.profile_version,
                session_verified_at=now,
                created_at=now,
                expires_at=now + timedelta(minutes=30),
                fields=fields,
                decisions=decisions,
                blockers=plan_blockers,
                locale=locale,
                answer_policy_version=context.policy_version,
                llm_prompt_version=audit_identity[0] if audit_identity is not None else None,
                llm_model_provider=audit_identity[1] if audit_identity is not None else None,
                llm_model_name=audit_identity[2] if audit_identity is not None else None,
                llm_model_digest=audit_identity[3] if audit_identity is not None else None,
            )
            return plan
        finally:
            await session.close()

    def _preflight_binding_valid(
        self,
        plan: FormPlanV1,
        permit: FinalSubmitPermit,
        *,
        now: datetime,
    ) -> bool:
        return (
            self.descriptor.allows_final_execution
            and self.descriptor.qualifies_form_fingerprint(plan.form_fingerprint)
            and plan.adapter_name == self.descriptor.platform
            and plan.adapter_version == self.descriptor.adapter_version
            and plan.selector_version == self.descriptor.selector_version
            and plan.ready_for_permit_at(now)
            and not permit.is_expired(now)
            and permit.binds(plan)
        )

    async def preflight(
        self,
        *,
        plan: FormPlanV1,
        permit: FinalSubmitPermit,
        context: AdapterPreflightContext | None = None,
    ) -> PreflightOutcome:
        """Fill and prepare the exact Submit button, but never click it."""

        now = self._clock()
        if not self._preflight_binding_valid(plan, permit, now=now):
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        if (
            context is None
            or context.selected_cv_id != plan.selected_cv_id
            or context.selected_cv_hash != plan.selected_cv_hash
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            workday_public_hostname(context.normalized_job_url)
        except WorkdayAdapterBlockedError:
            return NeedsReviewOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        try:
            resume_bytes = _read_verified_resume_bytes(
                context.resume_path,
                plan.selected_cv_hash,
            )
        except (OSError, WorkdayAdapterBlockedError):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)

        try:
            session = self._browser_factory(context.normalized_job_url)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        cleanup_id = hashlib.sha256(token_bytes(32)).hexdigest()
        self._pending_preflight[cleanup_id] = session
        self._cleanup_preflight_id.set(cleanup_id)

        try:
            await session.navigate(context.normalized_job_url)
            snapshot = await session.snapshot()
            assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            if assessment.state is WorkdayPageState.JOB:
                await session.open_candidate_form()
                snapshot = await session.snapshot()
                assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            if assessment.state is WorkdayPageState.ALREADY_APPLIED:
                return AlreadyAppliedOutcome()
            if assessment.reason_code is not None:
                return NeedsReviewOutcome(reason_code=assessment.reason_code)

            observed_fields: tuple[FormFieldV1, ...] = ()
            step_field_counts: tuple[int, ...] = ()
            proof: WorkdayAttachmentProof | None = None
            decisions_by_id = {decision.field_id: decision for decision in plan.decisions}

            for _step in range(_MAX_REVERSIBLE_STEPS):
                if assessment.state is WorkdayPageState.ALREADY_APPLIED:
                    return AlreadyAppliedOutcome()
                if assessment.reason_code is not None:
                    return NeedsReviewOutcome(reason_code=assessment.reason_code)
                if assessment.state is WorkdayPageState.REVIEW:
                    break
                if assessment.state not in {
                    WorkdayPageState.FORM,
                    WorkdayPageState.RESUME_UPLOAD,
                }:
                    return NeedsReviewOutcome(reason_code=ReasonCode.SELECTOR_DRIFT)

                raw_step_fields = observe_workday_v2_fields(snapshot.html)
                step_fields = _globalize_step_fields(
                    raw_step_fields,
                    prior_fields=observed_fields,
                )
                offset = len(observed_fields)
                expected_step = plan.fields[offset : offset + len(step_fields)]
                if tuple(step_fields) != tuple(expected_step):
                    return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
                step_decisions = tuple(
                    decisions_by_id[field.field_id]
                    for field in step_fields
                    if field.field_id in decisions_by_id
                )
                if len(step_decisions) != len(step_fields) or any(
                    decision.disposition is not AnswerDisposition.RESOLVED
                    for decision in step_decisions
                ):
                    return NeedsReviewOutcome(reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN)
                if any(field.field_type is FieldType.FILE for field in step_fields):
                    proof = await session.ensure_resume_attachment(
                        resume_bytes=resume_bytes,
                        cv_id=plan.selected_cv_id,
                        expected_sha256=plan.selected_cv_hash,
                    )
                    if not proof.matches(
                        cv_id=plan.selected_cv_id,
                        cv_sha256=plan.selected_cv_hash,
                    ):
                        return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)

                await session.fill(step_decisions)
                observed_fields = (*observed_fields, *step_fields)
                step_field_counts = (*step_field_counts, len(step_fields))
                await session.advance_reversible_step()
                snapshot = await session.snapshot()
                assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            else:
                return NeedsReviewOutcome(reason_code=ReasonCode.SELECTOR_DRIFT)

            if (
                assessment.state is not WorkdayPageState.REVIEW
                or tuple(observed_fields) != plan.fields
                or workday_v2_form_fingerprint(
                    observed_fields,
                    step_field_counts,
                )
                != plan.form_fingerprint
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            if proof is None:
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            proof = await session.verify_resume_attachment(
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            if not proof.matches(cv_id=plan.selected_cv_id, cv_sha256=plan.selected_cv_hash):
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            pre_action = snapshot

            action_nonce = hashlib.sha256(token_bytes(32)).hexdigest()
            action = PreparedFinalActionV1(
                attempt_id=permit.attempt_id,
                adapter_name=plan.adapter_name,
                adapter_version=plan.adapter_version,
                selector_version=plan.selector_version,
                form_fingerprint=plan.form_fingerprint,
                attached_cv_hash=plan.attached_cv_hash,
                prepared_at=now,
                expires_at=min(now + timedelta(minutes=2), permit.expires_at),
                action_nonce=action_nonce,
            )
            self._prepared[action_nonce] = _PreparedState(
                session=session,
                plan=plan,
                permit=permit,
                pre_action_html=pre_action.html,
            )
            self._pending_preflight.pop(cleanup_id, None)
            return action
        except WorkdayAdapterBlockedError as exc:
            return NeedsReviewOutcome(reason_code=exc.reason_code)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)

    async def commit(
        self,
        *,
        action: PreparedFinalActionV1,
        permit: FinalSubmitPermit,
    ) -> CommitOutcome:
        """Click once and require new, adapter-specific employer evidence."""

        if not self.descriptor.allows_final_execution:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        state = self._prepared.get(action.action_nonce)
        now = self._clock()
        if state is None:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        if state.clicked:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_REPLAYED)
        try:
            binding_valid = action.binds(state.plan, permit, at=now) and permit == state.permit
        except ValueError:
            binding_valid = False
        if not binding_valid:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)

        state.clicked = True
        final_action_at = now
        try:
            await state.session.click_final_action()
            post_action = await state.session.snapshot()
            employer_reference = await state.session.confirmation_reference()
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        assessment = assess_workday_v2_snapshot(post_action.html, post_action.url)
        if assessment.state is WorkdayPageState.ALREADY_APPLIED:
            return AlreadyAppliedOutcome()
        if assessment.state is WorkdayPageState.CHALLENGE:
            return UnknownOutcome(reason_code=ReasonCode.CHALLENGE_DETECTED)
        if assessment.state is WorkdayPageState.LOGIN:
            return UnknownOutcome(reason_code=ReasonCode.SESSION_EXPIRED)
        if assessment.state is not WorkdayPageState.CONFIRMATION:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if not employer_reference or not employer_reference.strip():
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        pre_soup = BeautifulSoup(state.pre_action_html, "html.parser")
        post_soup = BeautifulSoup(post_action.html, "html.parser")
        redacted_reference = hashlib.sha256(employer_reference.strip().encode("utf-8")).hexdigest()
        expectation = SubmissionEvidenceExpectation(
            attempt_id=action.attempt_id,
            platform=self.descriptor.platform,
            adapter_version=self.descriptor.adapter_version,
            selector_version=self.descriptor.selector_version,
            form_fingerprint=action.form_fingerprint,
            attached_cv_hash=action.attached_cv_hash,
            attachment_verified=state.plan.attachment_verified,
            post_action_nonce=action.action_nonce,
            final_action_at=final_action_at,
            allowed_rules=(
                AdapterEvidenceRule(
                    rule_id="workday-v2:visible-confirmation",
                    channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                    visible_selector=WORKDAY_CONFIRMATION_SELECTOR,
                ),
            ),
        )
        observation = SubmissionEvidenceObservation(
            attempt_id=action.attempt_id,
            platform=self.descriptor.platform,
            adapter_version=self.descriptor.adapter_version,
            selector_version=self.descriptor.selector_version,
            form_fingerprint=action.form_fingerprint,
            attached_cv_hash=action.attached_cv_hash,
            post_action_nonce=action.action_nonce,
            rule_id="workday-v2:visible-confirmation",
            channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
            evidence_reference=redacted_reference,
            observed_at=self._clock(),
            observed_after_final_action=True,
            was_present_before_action=bool(pre_soup.select(WORKDAY_CONFIRMATION_SELECTOR)),
            visible_selector=WORKDAY_CONFIRMATION_SELECTOR,
            computed_visible=(
                _has_one_visible_confirmation_reference(post_soup)
                and bool(employer_reference.strip())
            ),
        )
        confirmation = verify_submission_evidence(
            expectation,
            observation,
            pre_action_html=state.pre_action_html,
            post_action_html=post_action.html,
        )
        if not confirmation.confirmed or confirmation.evidence is None:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        return ConfirmedSubmittedOutcome(evidence=confirmation.evidence)

    async def cleanup_prepared_action(
        self,
        *,
        action: PreparedFinalActionV1 | None,
    ) -> None:
        """Close the page on the same loop after every terminal command path."""

        sessions: list[WorkdayCandidateSession] = []
        if action is not None:
            prepared = self._prepared.pop(action.action_nonce, None)
            if prepared is not None:
                sessions.append(prepared.session)
        else:
            cleanup_id = self._cleanup_preflight_id.get()
            if cleanup_id is not None:
                session = self._pending_preflight.pop(cleanup_id, None)
                if session is not None:
                    sessions.append(session)
        self._cleanup_preflight_id.set(None)
        closed: set[int] = set()
        for session in sessions:
            identity = id(session)
            if identity not in closed:
                await session.close()
                closed.add(identity)


def register_workday_browser_v2(
    registry: SubmitterRegistry,
    *,
    browser_factory: WorkdayBrowserFactory,
    answer_policy: AnswerPolicyV1 | None = None,
) -> WorkdayBrowserV2:
    """Register the fixture-qualified inspector without authorizing final action."""

    adapter = WorkdayBrowserV2(
        browser_factory=browser_factory,
        answer_policy=answer_policy,
    )
    registry.register_two_phase(adapter)
    return adapter
