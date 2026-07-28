"""Fixture-qualified SmartRecruiters candidate-browser adapter.

This module owns only deterministic domain and sanitized-HTML behavior. The
Playwright transport is injected by the private runner. The checked-in
descriptor has an empty qualified scope, so no irreversible action can run.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hmac import compare_digest
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
    DisclosureKind,
    DisclosureSource,
    FailedBeforeCommitOutcome,
    FieldType,
    FinalSubmitPermit,
    FormDisclosureV1,
    FormFieldConstraintsV1,
    FormFieldV1,
    FormOptionV1,
    FormPlanV1,
    NeedsReviewOutcome,
    PreflightOutcome,
    PreparedFinalActionV1,
    ReasonCode,
    SensitiveCategory,
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
)
from submitters.smartrecruiters_identity import (
    SmartRecruitersCandidateIdentity,
    SmartRecruitersIdentityError,
    SmartRecruitersResolvedIdentity,
    parse_smartrecruiters_candidate_identity,
    resolve_smartrecruiters_posting_identity,
)

SMARTRECRUITERS_V1_ADAPTER_VERSION = "1.0.0"
SMARTRECRUITERS_V1_SELECTOR_VERSION = "smartrecruiters-candidate-v1"
SMARTRECRUITERS_FORM_SELECTOR = (
    'form[data-qa="candidate-application-form"][data-company][data-public-id][data-posting-uuid]'
)
SMARTRECRUITERS_CONFIRMATION_SELECTOR = (
    'main[data-qa="application-confirmation"][data-application-id][data-posting-uuid]'
)
SMARTRECRUITERS_FINAL_SUBMIT_SELECTOR = 'button[data-qa="submit-application"][type="submit"]'
_MAX_HTML_BYTES = 256 * 1024
_MAX_FIELDS = 200
_MAX_RESUME_BYTES = 20 * 1024 * 1024
_FIELD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,500}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{6,160}$")
_SYSTEM_CONTROL_NAMES = frozenset({"csrf_token", "locale", "postingUuid", "publicPostingId"})
_DISCLOSURE_KINDS = {kind.value: kind for kind in DisclosureKind}
_DISCLOSURE_SOURCES = {source.value: source for source in DisclosureSource}
_NO_POLICY_SUMMARY = "No privacy policy was supplied by this candidate form."


class SmartRecruitersPageState(StrEnum):
    JOB = "job"
    FORM = "form"
    LOGIN = "login"
    MFA = "mfa"
    CHALLENGE = "challenge"
    CLOSED = "closed"
    ALREADY_APPLIED = "already_applied"
    CONFIRMATION = "confirmation"
    SELECTOR_DRIFT = "selector_drift"


@dataclass(frozen=True, slots=True)
class SmartRecruitersPageAssessment:
    state: SmartRecruitersPageState
    reason_code: ReasonCode | None = None


@dataclass(frozen=True, slots=True, repr=False)
class SmartRecruitersBrowserSnapshot:
    html: str
    url: str = ""
    locale: str = "en"

    def __post_init__(self) -> None:
        if len((self.html or "").encode("utf-8")) > _MAX_HTML_BYTES:
            raise ValueError("SMARTRECRUITERS_SNAPSHOT_TOO_LARGE")


@dataclass(frozen=True, slots=True, repr=False)
class SmartRecruitersAttachmentProof:
    cv_id: str
    cv_sha256: str
    upload_complete: bool
    receipt_sha256: str | None = None
    resume_control_sha256: str | None = None

    def matches(self, *, cv_id: str, cv_sha256: str) -> bool:
        return (
            self.upload_complete is True
            and self.cv_id == cv_id
            and compare_digest(self.cv_sha256, cv_sha256)
            and self.receipt_sha256 is not None
            and self.resume_control_sha256 is not None
            and _DIGEST_RE.fullmatch(self.receipt_sha256) is not None
            and _DIGEST_RE.fullmatch(self.resume_control_sha256) is not None
        )


@dataclass(frozen=True, slots=True)
class SmartRecruitersFinalActionProof:
    """Redacted proof of one retained native candidate request."""

    identity_sha256: str
    action_url_sha256: str
    form_fingerprint: str
    method: str
    encoding: str
    submitter_sha256: str
    actionability_sha256: str
    disclosures_sha256: str
    resume_control_sha256: str
    attached_cv_sha256: str
    payload_commitment_sha256: str
    user_field_count: int
    disclosure_count: int
    precommit_mutation_count: int

    def valid_for(
        self,
        *,
        identity: SmartRecruitersResolvedIdentity,
        plan: FormPlanV1,
    ) -> bool:
        expected_action = (
            f"https://{identity.candidate.hostname}/candidate-experience/"
            f"postings/{identity.posting_uuid}/applications"
        )
        digests = (
            self.identity_sha256,
            self.action_url_sha256,
            self.form_fingerprint,
            self.submitter_sha256,
            self.actionability_sha256,
            self.disclosures_sha256,
            self.resume_control_sha256,
            self.attached_cv_sha256,
            self.payload_commitment_sha256,
        )
        return (
            all(_DIGEST_RE.fullmatch(value) is not None for value in digests)
            and compare_digest(self.identity_sha256, _sha(identity.stable_key))
            and compare_digest(self.action_url_sha256, _sha(expected_action))
            and compare_digest(
                self.submitter_sha256,
                _sha(SMARTRECRUITERS_FINAL_SUBMIT_SELECTOR),
            )
            and compare_digest(self.form_fingerprint, plan.form_fingerprint)
            and self.method == "POST"
            and self.encoding == "multipart/form-data"
            and compare_digest(self.attached_cv_sha256, plan.selected_cv_hash)
            and compare_digest(
                self.disclosures_sha256,
                smartrecruiters_disclosures_digest(plan.disclosures),
            )
            and self.user_field_count == len(plan.fields)
            and self.disclosure_count == len(plan.disclosures)
            and self.precommit_mutation_count == 0
        )


class SmartRecruitersCandidateSession(Protocol):
    async def navigate(self, url: str) -> None: ...

    async def open_candidate_form(
        self,
        identity: SmartRecruitersCandidateIdentity,
    ) -> None: ...

    async def snapshot(self) -> SmartRecruitersBrowserSnapshot: ...

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> SmartRecruitersAttachmentProof: ...

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> SmartRecruitersAttachmentProof: ...

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None: ...

    async def prepare_final_action(
        self,
        *,
        identity: SmartRecruitersResolvedIdentity,
        fields: tuple[FormFieldV1, ...],
        disclosures: tuple[FormDisclosureV1, ...],
        decisions: tuple[AnswerDecisionV1, ...],
        form_fingerprint: str,
        attached_cv_sha256: str,
    ) -> SmartRecruitersFinalActionProof: ...

    async def click_final_action(
        self,
        proof: SmartRecruitersFinalActionProof,
    ) -> None: ...

    async def confirmation_reference(
        self,
        identity: SmartRecruitersResolvedIdentity,
    ) -> str | None: ...

    async def close(self) -> None: ...


SmartRecruitersBrowserFactory = Callable[[str], SmartRecruitersCandidateSession]


class SmartRecruitersAdapterBlockedError(RuntimeError):
    def __init__(self, reason_code: ReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


class SmartRecruitersFinalActionAmbiguousError(RuntimeError):
    """The exact candidate POST may have left the browser."""


@dataclass(slots=True, repr=False)
class _PreparedState:
    session: SmartRecruitersCandidateSession
    plan: FormPlanV1
    permit: FinalSubmitPermit
    identity: SmartRecruitersResolvedIdentity
    proof: SmartRecruitersFinalActionProof
    pre_action_html: str
    clicked: bool = False


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _compact(value: str) -> str:
    return " ".join((value or "").split())


def _visible(element: Tag) -> bool:
    current: Tag | None = element
    while current is not None:
        if current.has_attr("hidden"):
            return False
        if str(current.get("aria-hidden", "")).strip().casefold() == "true":
            return False
        style = str(current.get("style", "")).replace(" ", "").casefold()
        if any(
            marker in style
            for marker in (
                "display:none",
                "visibility:hidden",
                "visibility:collapse",
                "opacity:0",
                "content-visibility:hidden",
            )
        ):
            return False
        class_value = current.get("class") or ()
        classes = (
            {str(class_value).casefold()}
            if isinstance(class_value, str)
            else {str(item).casefold() for item in class_value}
        )
        if classes.intersection({"hidden", "sr-only", "visually-hidden", "d-none"}):
            return False
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return True


def _static_actionability_capture(
    element: Tag,
) -> tuple[dict[str, object], ...] | None:
    chain: list[dict[str, object]] = []
    current: Tag | None = element
    while current is not None:
        style = str(current.get("style", "")).replace(" ", "").casefold()
        opacity = re.search(r"(?:^|;)opacity:([0-9.]+)(?:;|$)", style)
        try:
            opacity_zero = bool(opacity and float(opacity.group(1)) <= 0)
        except ValueError:
            return None
        zero_area = any(
            re.search(
                rf"(?:^|;){dimension}:0(?:px|rem|em|%)?(?:;|$)",
                style,
            )
            is not None
            for dimension in ("width", "height")
        )
        entry = {
            "depth": len(chain),
            "tag": current.name,
            "disabled": current.has_attr("disabled"),
            "aria_disabled": (str(current.get("aria-disabled", "")).strip().casefold() == "true"),
            "inert": current.has_attr("inert"),
            "hidden": current.has_attr("hidden"),
            "aria_hidden": (str(current.get("aria-hidden", "")).strip().casefold() == "true"),
            "display_none": "display:none" in style,
            "visibility_hidden": any(
                marker in style for marker in ("visibility:hidden", "visibility:collapse")
            ),
            "opacity_zero": opacity_zero,
            "pointer_events_none": "pointer-events:none" in style,
            "content_visibility_hidden": "content-visibility:hidden" in style,
            "declared_zero_area": zero_area,
        }
        if any(value is True for key, value in entry.items() if key not in {"depth", "tag"}):
            return None
        chain.append(entry)
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return tuple(chain) if chain else None


def _candidate_action_url(
    form: Tag,
    identity: SmartRecruitersResolvedIdentity,
) -> str | None:
    action = str(form.get("action", "")).strip()
    try:
        parsed = urlsplit(action)
        port = parsed.port
    except ValueError:
        return None
    expected_path = f"/candidate-experience/postings/{identity.posting_uuid}/applications"
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != identity.candidate.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return (
        f"https://{identity.candidate.hostname}"
        f"/candidate-experience/postings/{identity.posting_uuid}/applications"
    )


def _exact_form(
    html: str,
    identity: SmartRecruitersResolvedIdentity,
) -> Tag | None:
    soup = BeautifulSoup(html or "", "html.parser")
    forms = [
        form
        for form in soup.select(SMARTRECRUITERS_FORM_SELECTOR)
        if isinstance(form, Tag)
        and _visible(form)
        and str(form.get("data-company", "")).strip() == identity.candidate.company
        and str(form.get("data-public-id", "")).strip() == identity.candidate.public_id
        and str(form.get("data-posting-uuid", "")).strip().casefold() == identity.posting_uuid
    ]
    return forms[0] if len(forms) == 1 else None


def _visible_confirmation_reference(
    html: str,
    identity: SmartRecruitersResolvedIdentity,
) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    matches = [
        node
        for node in soup.select(SMARTRECRUITERS_CONFIRMATION_SELECTOR)
        if isinstance(node, Tag)
        and _visible(node)
        and str(node.get("data-posting-uuid", "")).strip().casefold() == identity.posting_uuid
    ]
    if len(matches) != 1:
        return None
    reference = str(matches[0].get("data-application-id", "")).strip()
    return reference if _REFERENCE_RE.fullmatch(reference) else None


def assess_smartrecruiters_v1_snapshot(
    html: str,
    url: str = "",
    *,
    identity: SmartRecruitersResolvedIdentity | None = None,
) -> SmartRecruitersPageAssessment:
    if len((html or "").encode("utf-8")) > _MAX_HTML_BYTES:
        return SmartRecruitersPageAssessment(
            SmartRecruitersPageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        )
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.stripped_strings).casefold()
    low_url = (url or "").casefold()
    challenge = (
        '[data-qa="captcha"]',
        ".g-recaptcha",
        ".h-captcha",
        'iframe[src*="captcha"]',
    )
    if any(soup.select_one(selector) is not None for selector in challenge) or any(
        marker in text
        for marker in (
            "verify you are human",
            "security challenge",
            "complete the captcha",
        )
    ):
        return SmartRecruitersPageAssessment(
            SmartRecruitersPageState.CHALLENGE,
            ReasonCode.CHALLENGE_DETECTED,
        )
    if (
        soup.select_one('[data-qa="mfa-challenge"]') is not None
        or soup.select_one('input[autocomplete="one-time-code"]') is not None
    ):
        return SmartRecruitersPageAssessment(
            SmartRecruitersPageState.MFA,
            ReasonCode.MFA_REQUIRED,
        )
    if (
        soup.select_one('[data-qa="candidate-login"]') is not None
        or soup.select_one('input[type="password"]') is not None
        or any(marker in low_url for marker in ("/login", "/signin", "/sign-in"))
    ):
        return SmartRecruitersPageAssessment(
            SmartRecruitersPageState.LOGIN,
            ReasonCode.SESSION_EXPIRED,
        )
    if (
        soup.select_one('[data-qa="job-closed"]') is not None
        or "this job is no longer accepting applications" in text
    ):
        return SmartRecruitersPageAssessment(
            SmartRecruitersPageState.CLOSED,
            ReasonCode.JOB_CLOSED,
        )
    if (
        soup.select_one('[data-qa="already-applied"]') is not None
        or "you have already applied for this job" in text
    ):
        return SmartRecruitersPageAssessment(
            SmartRecruitersPageState.ALREADY_APPLIED,
            ReasonCode.ALREADY_APPLIED,
        )
    if identity is not None and _visible_confirmation_reference(html, identity):
        return SmartRecruitersPageAssessment(SmartRecruitersPageState.CONFIRMATION)
    if identity is not None and _exact_form(html, identity) is not None:
        return SmartRecruitersPageAssessment(SmartRecruitersPageState.FORM)
    visible_apply = [
        node
        for node in soup.select('a[data-qa="apply-link"][href]')
        if isinstance(node, Tag) and _visible(node)
    ]
    if len(visible_apply) == 1:
        return SmartRecruitersPageAssessment(SmartRecruitersPageState.JOB)
    return SmartRecruitersPageAssessment(
        SmartRecruitersPageState.SELECTOR_DRIFT,
        ReasonCode.SELECTOR_DRIFT,
    )


_IDENTITY_INDEPENDENT_TERMINAL_STATES = frozenset(
    {
        SmartRecruitersPageState.CHALLENGE,
        SmartRecruitersPageState.MFA,
        SmartRecruitersPageState.LOGIN,
        SmartRecruitersPageState.CLOSED,
        SmartRecruitersPageState.ALREADY_APPLIED,
    }
)


def _identity_independent_terminal_reason(
    html: str,
    url: str,
) -> ReasonCode | None:
    assessment = assess_smartrecruiters_v1_snapshot(html, url)
    if assessment.state in _IDENTITY_INDEPENDENT_TERMINAL_STATES:
        return assessment.reason_code
    return None


def _bounded_int(raw: object, *, minimum: int = 0) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if value < minimum:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _bounded_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if not math.isfinite(value):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _control_type(wrapper: Tag, control: Tag) -> tuple[FieldType, SensitiveCategory | None]:
    kind = str(wrapper.get("data-control-kind", "")).strip().casefold()
    section = str(wrapper.get("data-section", "")).strip().casefold()
    if kind == "consent":
        return FieldType.CONSENT, SensitiveCategory.CONSENT
    if kind == "attestation":
        return FieldType.ATTESTATION, SensitiveCategory.ATTESTATION
    sensitive = SensitiveCategory.DEMOGRAPHIC if section == "diversity" else None
    if control.name == "textarea":
        return FieldType.TEXTAREA, sensitive
    if control.name == "select":
        return (
            FieldType.MULTI_SELECT if control.has_attr("multiple") else FieldType.SELECT,
            sensitive,
        )
    raw = str(control.get("type", "text")).strip().casefold()
    field_type = {
        "": FieldType.TEXT,
        "text": FieldType.TEXT,
        "search": FieldType.TEXT,
        "checkbox": FieldType.CHECKBOX,
        "date": FieldType.DATE,
        "email": FieldType.EMAIL,
        "file": FieldType.FILE,
        "number": FieldType.NUMBER,
        "radio": FieldType.RADIO,
        "tel": FieldType.PHONE,
        "url": FieldType.URL,
    }.get(raw)
    if field_type is None:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    return field_type, sensitive


def _options(
    wrapper: Tag,
    control: Tag,
    field_type: FieldType,
) -> tuple[FormOptionV1, ...]:
    if field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
        nodes = [node for node in control.find_all("option") if isinstance(node, Tag)]
    elif field_type is FieldType.RADIO:
        nodes = [
            node for node in wrapper.select('input[type="radio"][value]') if isinstance(node, Tag)
        ]
    else:
        return ()
    result: list[FormOptionV1] = []
    seen_ids: set[str] = set()
    for node in nodes:
        value = str(node.get("value", "")).strip()
        if not value:
            continue
        label = (
            _compact(node.get_text(" ", strip=True))
            if node.name == "option"
            else _compact(str(node.get("data-option-label", "")))
        )
        option_id = str(node.get("data-option-id", "")).strip()
        if not label or _FIELD_ID_RE.fullmatch(option_id) is None or option_id in seen_ids:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        result.append(
            FormOptionV1(
                option_id=option_id,
                value=value,
                label=label,
                disabled=node.has_attr("disabled"),
            )
        )
        seen_ids.add(option_id)
    return tuple(result)


def observe_smartrecruiters_v1_fields(
    html: str,
    *,
    identity: SmartRecruitersResolvedIdentity,
) -> tuple[FormFieldV1, ...]:
    if len((html or "").encode("utf-8")) > _MAX_HTML_BYTES:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    form = _exact_form(html, identity)
    if form is None:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    wrappers = [
        node
        for node in form.select('[data-qa="application-field"][data-field-id]')
        if isinstance(node, Tag) and _visible(node)
    ]
    if not wrappers or len(wrappers) > _MAX_FIELDS:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    fields: list[FormFieldV1] = []
    seen_ids: set[str] = set()
    for position, wrapper in enumerate(wrappers):
        field_id = str(wrapper.get("data-field-id", "")).strip()
        if _FIELD_ID_RE.fullmatch(field_id) is None or field_id in seen_ids:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        controls = [
            control
            for control in wrapper.select("input,textarea,select")
            if isinstance(control, Tag) and str(control.get("type", "")).casefold() != "hidden"
        ]
        if not controls:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        control = controls[0]
        field_type, sensitive_category = _control_type(wrapper, control)
        names = {str(item.get("name", "")).strip() for item in controls}
        if (
            "" in names
            or (field_type is FieldType.RADIO and len(names) != 1)
            or (field_type is not FieldType.RADIO and len(controls) != 1)
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        label_node = wrapper.select_one("label,legend,[data-qa='field-label']")
        label = _compact(label_node.get_text(" ", strip=True)) if label_node else ""
        if not label:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        pattern = str(control.get("pattern", "")).strip() or None
        if pattern is not None and (
            len(pattern) > 128 or any(ord(character) < 32 for character in pattern)
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        accepted = tuple(
            item.strip() for item in str(control.get("accept", "")).split(",") if item.strip()
        )[:32]
        fields.append(
            FormFieldV1(
                field_id=field_id,
                canonical_name=(str(wrapper.get("data-canonical-name", "")).strip() or None),
                label=label,
                field_type=field_type,
                required=(
                    control.has_attr("required")
                    or str(wrapper.get("aria-required", "")).casefold() == "true"
                ),
                position=position,
                options=_options(wrapper, control, field_type),
                constraints=FormFieldConstraintsV1(
                    min_length=_bounded_int(control.get("minlength")),
                    max_length=_bounded_int(control.get("maxlength")),
                    min_value=_bounded_float(control.get("min")),
                    max_value=_bounded_float(control.get("max")),
                    pattern=pattern,
                    accepted_file_types=accepted,
                    multiple=control.has_attr("multiple"),
                ),
                sensitive_category=sensitive_category,
            )
        )
        seen_ids.add(field_id)
    return tuple(fields)


def _safe_disclosure_href(raw: str) -> str | None:
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or any(ord(character) > 127 for character in parsed.hostname)
    ):
        return None
    return raw


def observe_smartrecruiters_v1_disclosures(
    html: str,
    *,
    identity: SmartRecruitersResolvedIdentity | None = None,
) -> tuple[FormDisclosureV1, ...]:
    if len((html or "").encode("utf-8")) > _MAX_HTML_BYTES:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    soup = BeautifulSoup(html or "", "html.parser")
    scope = _exact_form(html, identity) if identity is not None else soup
    if scope is None:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    nodes = [
        node
        for node in scope.select(
            '[data-qa="form-disclosure"][data-disclosure-id]'
            "[data-disclosure-kind][data-disclosure-source]"
        )
        if isinstance(node, Tag) and _visible(node)
    ]
    disclosures: list[FormDisclosureV1] = []
    seen_ids: set[str] = set()
    for position, node in enumerate(nodes):
        disclosure_id = str(node.get("data-disclosure-id", "")).strip()
        kind = _DISCLOSURE_KINDS.get(str(node.get("data-disclosure-kind", "")).strip().casefold())
        source = _DISCLOSURE_SOURCES.get(
            str(node.get("data-disclosure-source", "")).strip().casefold()
        )
        summary_nodes = [
            item
            for item in node.select('[data-qa="disclosure-summary"]')
            if isinstance(item, Tag) and _visible(item)
        ]
        if (
            _FIELD_ID_RE.fullmatch(disclosure_id) is None
            or disclosure_id in seen_ids
            or kind is None
            or source not in {DisclosureSource.INLINE, DisclosureSource.LINK}
            or len(summary_nodes) != 1
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        summary = _compact(summary_nodes[0].get_text(" ", strip=True))
        if not summary or len(summary) > 2_000:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if source is DisclosureSource.LINK:
            links = [
                item
                for item in node.select('a[data-qa="disclosure-link"][href]')
                if isinstance(item, Tag) and _visible(item)
            ]
            if len(links) != 1:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
            reference = _safe_disclosure_href(str(links[0].get("href", "")).strip())
            if reference is None:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        else:
            if node.select_one('a[data-qa="disclosure-link"][href]') is not None:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
            reference = f"inline:{disclosure_id}:{kind.value}"
        disclosures.append(
            FormDisclosureV1(
                disclosure_id=disclosure_id,
                kind=kind,
                source=source,
                position=position,
                summary=summary,
                content_sha256=_sha(summary),
                reference_sha256=_sha(reference),
                acknowledgement_field_id=(
                    str(node.get("data-acknowledgement-field-id", "")).strip() or None
                ),
            )
        )
        seen_ids.add(disclosure_id)
    privacy_kinds = {
        DisclosureKind.PRIVACY_POLICY,
        DisclosureKind.NO_PRIVACY_POLICY_NOTICE,
    }
    observed_privacy = [
        disclosure for disclosure in disclosures if disclosure.kind in privacy_kinds
    ]
    if len(observed_privacy) > 1:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    if not observed_privacy:
        disclosures.append(
            FormDisclosureV1(
                disclosure_id="privacy-policy-absent",
                kind=DisclosureKind.NO_PRIVACY_POLICY_NOTICE,
                source=DisclosureSource.SYNTHETIC,
                position=len(disclosures),
                summary=_NO_POLICY_SUMMARY,
                content_sha256=_sha(_NO_POLICY_SUMMARY),
                reference_sha256=_sha("smartrecruiters:no-privacy-policy:v1"),
            )
        )
    return tuple(disclosures)


def smartrecruiters_v1_disclosure_runtime_material(
    html: str,
    *,
    identity: SmartRecruitersResolvedIdentity,
    disclosures: tuple[FormDisclosureV1, ...],
) -> str:
    """Return ephemeral exact DOM material for browser-side revalidation.

    The result may contain public disclosure link targets and must never be
    persisted, logged, returned by an API, or copied into diagnostics.
    """

    if (
        observe_smartrecruiters_v1_disclosures(
            html,
            identity=identity,
        )
        != disclosures
    ):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    form = _exact_form(html, identity)
    if form is None:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    material: list[dict[str, str]] = []
    for node in form.select(
        '[data-qa="form-disclosure"][data-disclosure-id]'
        "[data-disclosure-kind][data-disclosure-source]"
    ):
        if not isinstance(node, Tag) or not _visible(node):
            continue
        summaries = [
            item
            for item in node.select('[data-qa="disclosure-summary"]')
            if isinstance(item, Tag) and _visible(item)
        ]
        links = [
            item
            for item in node.select('a[data-qa="disclosure-link"][href]')
            if isinstance(item, Tag) and _visible(item)
        ]
        material.append(
            {
                "id": str(node.get("data-disclosure-id", "")).strip(),
                "kind": str(node.get("data-disclosure-kind", "")).strip(),
                "source": str(node.get("data-disclosure-source", "")).strip(),
                "acknowledgement": str(node.get("data-acknowledgement-field-id", "")).strip(),
                "summary": _compact(
                    summaries[0].get_text(" ", strip=True) if len(summaries) == 1 else ""
                ),
                "href": (str(links[0].get("href", "")).strip() if len(links) == 1 else ""),
            }
        )
    return json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def smartrecruiters_disclosures_digest(
    disclosures: tuple[FormDisclosureV1, ...],
) -> str:
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in disclosures],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return _sha(canonical)


def _wrapper_contracts(form: Tag) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen_fields: set[str] = set()
    seen_repeat_positions: set[tuple[str, int]] = set()
    for position, wrapper in enumerate(form.select('[data-qa="application-field"][data-field-id]')):
        if not isinstance(wrapper, Tag) or not _visible(wrapper):
            continue
        field_id = str(wrapper.get("data-field-id", "")).strip()
        repeat_group = str(wrapper.get("data-repeat-group", "")).strip() or None
        repeat_index = _bounded_int(wrapper.get("data-repeat-index"))
        conditional_parent = str(wrapper.get("data-conditional-parent", "")).strip() or None
        conditional_value = str(wrapper.get("data-conditional-value", "")).strip() or None
        if (
            (repeat_group is None) != (repeat_index is None)
            or (conditional_parent is None) != (conditional_value is None)
            or (conditional_parent is not None and conditional_parent not in seen_fields)
            or (
                repeat_group is not None
                and repeat_index is not None
                and (repeat_group, repeat_index) in seen_repeat_positions
            )
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if repeat_group is not None and repeat_index is not None:
            seen_repeat_positions.add((repeat_group, repeat_index))
        result.append(
            {
                "field_id": field_id,
                "position": position,
                "section": str(wrapper.get("data-section", "")).strip(),
                "repeat_group": repeat_group,
                "repeat_index": repeat_index,
                "conditional_parent": conditional_parent,
                "conditional_value": conditional_value,
            }
        )
        seen_fields.add(field_id)
    return result


def smartrecruiters_v1_final_action_binding(
    html: str,
    *,
    identity: SmartRecruitersResolvedIdentity,
    fields: tuple[FormFieldV1, ...],
    disclosures: tuple[FormDisclosureV1, ...],
) -> str:
    form = _exact_form(html, identity)
    if form is None:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    action = _candidate_action_url(form, identity)
    if (
        action is None
        or str(form.get("method", "")).strip().casefold() != "post"
        or str(form.get("enctype", "")).strip().casefold() != "multipart/form-data"
    ):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    submits = [
        node
        for node in form.select(SMARTRECRUITERS_FINAL_SUBMIT_SELECTOR)
        if isinstance(node, Tag) and _visible(node)
    ]
    if len(submits) != 1:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    submit = submits[0]
    actionability = _static_actionability_capture(submit)
    if (
        actionability is None
        or submit.has_attr("disabled")
        or str(submit.get("aria-disabled", "")).casefold() == "true"
        or any(
            submit.has_attr(attribute)
            for attribute in (
                "form",
                "formaction",
                "formmethod",
                "formenctype",
                "name",
            )
        )
    ):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    if (
        observe_smartrecruiters_v1_disclosures(
            html,
            identity=identity,
        )
        != disclosures
    ):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    field_by_id = {field.field_id: field for field in fields}
    if len(field_by_id) != len(fields):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    owners: dict[str, dict[str, str]] = {}
    mapped: set[str] = set()
    seen_system: set[str] = set()
    for control in form.select("[name]"):
        if not isinstance(control, Tag) or control is submit:
            continue
        name = str(control.get("name", "")).strip()
        if (
            not name
            or len(name.encode("utf-8")) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or control.has_attr("disabled")
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        wrapper = control.find_parent(attrs={"data-qa": "application-field", "data-field-id": True})
        input_type = str(control.get("type", "")).casefold()
        if wrapper is None:
            value = str(control.get("value", ""))
            if (
                control.name != "input"
                or input_type != "hidden"
                or name not in _SYSTEM_CONTROL_NAMES
                or name in seen_system
                or not value
                or len(value.encode("utf-8")) > 4096
                or (name == "postingUuid" and value.casefold() != identity.posting_uuid)
                or (name == "publicPostingId" and value != identity.candidate.public_id)
            ):
                raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
            seen_system.add(name)
            continue
        field_id = str(wrapper.get("data-field-id", "")).strip()
        field = field_by_id.get(field_id)
        if field is None or not _visible(wrapper) or input_type == "hidden":
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        prior = owners.get(name)
        if prior is not None and (
            prior["field_id"] != field_id or field.field_type is not FieldType.RADIO
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        owners[name] = {
            "field_id": field_id,
            "field_type": field.field_type.value,
        }
        mapped.add(field_id)
    if mapped != set(field_by_id):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    if (
        sum(field.field_type is FieldType.FILE for field in fields) != 1
        or sum(owner["field_type"] == FieldType.FILE.value for owner in owners.values()) != 1
    ):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    wrappers = _wrapper_contracts(form)
    if [item["field_id"] for item in wrappers] != [field.field_id for field in fields]:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    payload = {
        "identity_sha256": _sha(identity.stable_key),
        "resolver_evidence_sha256": identity.resolver_evidence_sha256,
        "action_sha256": _sha(action),
        "method": "POST",
        "encoding": "multipart/form-data",
        "submitter": "button:data-qa=submit-application:type=submit",
        "actionability_sha256": _sha(
            json.dumps(
                actionability,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        ),
        "disclosures_sha256": smartrecruiters_disclosures_digest(disclosures),
        "wrappers": wrappers,
        "owners": [
            {
                "control_name_sha256": _sha(name),
                **owner,
            }
            for name, owner in owners.items()
        ],
        "system_control_names": sorted(seen_system),
    }
    return _sha(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )


def smartrecruiters_v1_form_fingerprint(
    identity: SmartRecruitersResolvedIdentity,
    fields: tuple[FormFieldV1, ...],
    disclosures: tuple[FormDisclosureV1, ...],
    final_action_binding: str,
) -> str:
    if (
        not fields
        or len(fields) > _MAX_FIELDS
        or _DIGEST_RE.fullmatch(final_action_binding or "") is None
    ):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    payload = {
        "adapter_version": SMARTRECRUITERS_V1_ADAPTER_VERSION,
        "selector_version": SMARTRECRUITERS_V1_SELECTOR_VERSION,
        "identity_sha256": _sha(identity.stable_key),
        "resolver_evidence_sha256": identity.resolver_evidence_sha256,
        "final_action_binding": final_action_binding,
        "fields": [field.model_dump(mode="json") for field in fields],
        "disclosures": [disclosure.model_dump(mode="json") for disclosure in disclosures],
    }
    return _sha(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )


def _read_verified_resume_bytes(path: str, expected_sha256: str) -> bytes:
    with Path(path).open("rb") as handle:
        payload = handle.read(_MAX_RESUME_BYTES + 1)
    if (
        not payload
        or len(payload) > _MAX_RESUME_BYTES
        or not compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256)
    ):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    return payload


def _descriptor() -> AdapterDescriptor:
    descriptor = adapter_for_platform("smartrecruiters")
    if (
        descriptor is None
        or descriptor.adapter_version != SMARTRECRUITERS_V1_ADAPTER_VERSION
        or descriptor.selector_version != SMARTRECRUITERS_V1_SELECTOR_VERSION
        or descriptor.execution_contract_version != TWO_PHASE_EXECUTION_CONTRACT_VERSION
    ):
        raise RuntimeError("SMARTRECRUITERS_V1_DESCRIPTOR_MISMATCH")
    return descriptor


class SmartRecruitersBrowserV1:
    def __init__(
        self,
        *,
        browser_factory: SmartRecruitersBrowserFactory,
        answer_policy: AnswerPolicyV1 | None = None,
        descriptor: AdapterDescriptor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.descriptor = descriptor or _descriptor()
        self._browser_factory = browser_factory
        self._answer_policy = answer_policy or AnswerPolicyV1()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._prepared: dict[str, _PreparedState] = {}
        self._pending_preflight: dict[str, SmartRecruitersCandidateSession] = {}
        self._cleanup_preflight_id: ContextVar[str | None] = ContextVar(
            f"smartrecruiters-v1-cleanup-{id(self)}",
            default=None,
        )

    def can_inspect(self, job: JobData) -> bool:
        try:
            parse_smartrecruiters_candidate_identity(job.apply_url or job.source_url)
        except SmartRecruitersIdentityError:
            return False
        return True

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
        if (
            not resume_path
            or not selected_cv_id
            or application.cv_sha256 is None
            or application.profile_version is None
            or _DIGEST_RE.fullmatch(application.cv_sha256) is None
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            candidate = parse_smartrecruiters_candidate_identity(job.apply_url or job.source_url)
            resume_bytes = _read_verified_resume_bytes(
                resume_path,
                application.cv_sha256,
            )
            profile = UserProfile.model_validate(dict(user_profile))
        except SmartRecruitersIdentityError as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        except OSError as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        except (TypeError, ValueError) as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN) from exc
        session = self._browser_factory(candidate.job_url)
        try:
            await session.navigate(candidate.job_url)
            snapshot = await session.snapshot()
            terminal_reason = _identity_independent_terminal_reason(
                snapshot.html,
                snapshot.url,
            )
            if terminal_reason is not None:
                raise SmartRecruitersAdapterBlockedError(terminal_reason)
            try:
                identity = resolve_smartrecruiters_posting_identity(
                    snapshot.html,
                    candidate,
                )
            except SmartRecruitersIdentityError as exc:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT) from exc
            assessment = assess_smartrecruiters_v1_snapshot(
                snapshot.html,
                snapshot.url,
                identity=identity,
            )
            if assessment.state is SmartRecruitersPageState.JOB:
                await session.open_candidate_form(candidate)
                snapshot = await session.snapshot()
                terminal_reason = _identity_independent_terminal_reason(
                    snapshot.html,
                    snapshot.url,
                )
                if terminal_reason is not None:
                    raise SmartRecruitersAdapterBlockedError(terminal_reason)
                try:
                    resolved = resolve_smartrecruiters_posting_identity(
                        snapshot.html,
                        candidate,
                    )
                except SmartRecruitersIdentityError as exc:
                    raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
                if resolved != identity:
                    raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
                assessment = assess_smartrecruiters_v1_snapshot(
                    snapshot.html,
                    snapshot.url,
                    identity=identity,
                )
            if assessment.reason_code is not None:
                raise SmartRecruitersAdapterBlockedError(assessment.reason_code)
            if assessment.state is SmartRecruitersPageState.CONFIRMATION:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.ALREADY_APPLIED)
            if assessment.state is not SmartRecruitersPageState.FORM:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
            fields = observe_smartrecruiters_v1_fields(
                snapshot.html,
                identity=identity,
            )
            disclosures = observe_smartrecruiters_v1_disclosures(
                snapshot.html,
                identity=identity,
            )
            binding = smartrecruiters_v1_final_action_binding(
                snapshot.html,
                identity=identity,
                fields=fields,
                disclosures=disclosures,
            )
            fingerprint = smartrecruiters_v1_form_fingerprint(
                identity,
                fields,
                disclosures,
                binding,
            )
            attachment = await session.ensure_resume_attachment(
                resume_bytes=resume_bytes,
                cv_id=selected_cv_id,
                expected_sha256=application.cv_sha256,
            )
            attachment_verified = attachment.matches(
                cv_id=selected_cv_id,
                cv_sha256=application.cv_sha256,
            )
            context = AnswerPolicyContext(
                profile=profile,
                profile_version=application.profile_version,
                selected_cv_id=selected_cv_id,
                selected_cv_hash=application.cv_sha256,
                attached_cv_id=attachment.cv_id,
                attached_cv_hash=attachment.cv_sha256,
                attachment_verified=attachment_verified,
                adapter_name=self.descriptor.platform,
                adapter_version=self.descriptor.adapter_version,
                selector_version=self.descriptor.selector_version,
                form_fingerprint=fingerprint,
                locale=snapshot.locale,
            )
            policy = await (answer_policy or self._answer_policy).plan_fields(fields, context)
            if {decision.field_id for decision in policy.decisions} != {
                field.field_id for field in fields
            }:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
            blockers = list(dict.fromkeys(policy.blockers))
            if not attachment_verified:
                blockers.append(ReasonCode.ATTACHMENT_UNVERIFIED)
            if any(
                decision.disposition is not AnswerDisposition.RESOLVED
                for decision in policy.decisions
            ):
                blockers.append(ReasonCode.REQUIRED_FIELD_UNKNOWN)
            blockers = list(dict.fromkeys(blockers))
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
            audit = (
                policy.prompt_version,
                policy.model_provider,
                policy.model_name,
                policy.model_digest,
            )
            if any(value is not None for value in audit) and not all(
                isinstance(value, str) for value in audit
            ):
                raise SmartRecruitersAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
            return FormPlanV1(
                plan_id=uuid4(),
                application_id=application_id,
                application_revision=application_revision,
                adapter_name=self.descriptor.platform,
                adapter_version=self.descriptor.adapter_version,
                selector_version=self.descriptor.selector_version,
                form_fingerprint=fingerprint,
                selected_cv_id=selected_cv_id,
                selected_cv_hash=application.cv_sha256,
                attached_cv_id=attachment.cv_id,
                attached_cv_hash=attachment.cv_sha256,
                attachment_verified=attachment_verified,
                profile_version=application.profile_version,
                session_verified_at=now,
                created_at=now,
                expires_at=now + timedelta(minutes=30),
                fields=fields,
                disclosures=disclosures,
                decisions=policy.decisions,
                blockers=tuple(blockers),
                locale=snapshot.locale,
                answer_policy_version=context.policy_version,
                llm_prompt_version=audit[0],
                llm_model_provider=audit[1],
                llm_model_name=audit[2],
                llm_model_digest=audit[3],
            )
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
        now = self._clock()
        if not self._preflight_binding_valid(plan, permit, now=now):
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        if (
            context is None
            or context.selected_cv_id != plan.selected_cv_id
            or not compare_digest(
                context.selected_cv_hash,
                plan.selected_cv_hash,
            )
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            candidate = parse_smartrecruiters_candidate_identity(context.normalized_job_url)
            resume_bytes = _read_verified_resume_bytes(
                context.resume_path,
                plan.selected_cv_hash,
            )
            session = self._browser_factory(candidate.job_url)
        except (
            SmartRecruitersIdentityError,
            OSError,
            SmartRecruitersAdapterBlockedError,
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        cleanup_id = _sha(token_bytes(32).hex())
        self._pending_preflight[cleanup_id] = session
        self._cleanup_preflight_id.set(cleanup_id)
        try:
            await session.navigate(candidate.job_url)
            snapshot = await session.snapshot()
            terminal_reason = _identity_independent_terminal_reason(
                snapshot.html,
                snapshot.url,
            )
            if terminal_reason is ReasonCode.ALREADY_APPLIED:
                return AlreadyAppliedOutcome()
            if terminal_reason is not None:
                return NeedsReviewOutcome(reason_code=terminal_reason)
            identity = resolve_smartrecruiters_posting_identity(
                snapshot.html,
                candidate,
            )
            assessment = assess_smartrecruiters_v1_snapshot(
                snapshot.html,
                snapshot.url,
                identity=identity,
            )
            if assessment.state in {
                SmartRecruitersPageState.ALREADY_APPLIED,
                SmartRecruitersPageState.CONFIRMATION,
            }:
                return AlreadyAppliedOutcome()
            if assessment.state is SmartRecruitersPageState.JOB:
                await session.open_candidate_form(candidate)
                snapshot = await session.snapshot()
                terminal_reason = _identity_independent_terminal_reason(
                    snapshot.html,
                    snapshot.url,
                )
                if terminal_reason is ReasonCode.ALREADY_APPLIED:
                    return AlreadyAppliedOutcome()
                if terminal_reason is not None:
                    return NeedsReviewOutcome(reason_code=terminal_reason)
                if (
                    resolve_smartrecruiters_posting_identity(
                        snapshot.html,
                        candidate,
                    )
                    != identity
                ):
                    return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
                assessment = assess_smartrecruiters_v1_snapshot(
                    snapshot.html,
                    snapshot.url,
                    identity=identity,
                )
            if assessment.reason_code is not None:
                return NeedsReviewOutcome(reason_code=assessment.reason_code)
            if assessment.state is not SmartRecruitersPageState.FORM:
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            fields = observe_smartrecruiters_v1_fields(
                snapshot.html,
                identity=identity,
            )
            disclosures = observe_smartrecruiters_v1_disclosures(
                snapshot.html,
                identity=identity,
            )
            binding = smartrecruiters_v1_final_action_binding(
                snapshot.html,
                identity=identity,
                fields=fields,
                disclosures=disclosures,
            )
            if (
                fields != plan.fields
                or disclosures != plan.disclosures
                or smartrecruiters_v1_form_fingerprint(
                    identity,
                    fields,
                    disclosures,
                    binding,
                )
                != plan.form_fingerprint
                or len(plan.decisions) != len(fields)
                or any(
                    decision.disposition is not AnswerDisposition.RESOLVED
                    for decision in plan.decisions
                )
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            attachment = await session.ensure_resume_attachment(
                resume_bytes=resume_bytes,
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            if not attachment.matches(
                cv_id=plan.selected_cv_id,
                cv_sha256=plan.selected_cv_hash,
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            await session.fill(plan.decisions)
            filled = await session.snapshot()
            terminal_reason = _identity_independent_terminal_reason(
                filled.html,
                filled.url,
            )
            if terminal_reason is ReasonCode.ALREADY_APPLIED:
                return AlreadyAppliedOutcome()
            if terminal_reason is not None:
                return NeedsReviewOutcome(reason_code=terminal_reason)
            if (
                assess_smartrecruiters_v1_snapshot(
                    filled.html,
                    filled.url,
                    identity=identity,
                ).state
                is not SmartRecruitersPageState.FORM
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            filled_fields = observe_smartrecruiters_v1_fields(
                filled.html,
                identity=identity,
            )
            filled_disclosures = observe_smartrecruiters_v1_disclosures(
                filled.html,
                identity=identity,
            )
            filled_binding = smartrecruiters_v1_final_action_binding(
                filled.html,
                identity=identity,
                fields=filled_fields,
                disclosures=filled_disclosures,
            )
            if (
                filled_fields != fields
                or filled_disclosures != disclosures
                or smartrecruiters_v1_form_fingerprint(
                    identity,
                    filled_fields,
                    filled_disclosures,
                    filled_binding,
                )
                != plan.form_fingerprint
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            attachment = await session.verify_resume_attachment(
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            if not attachment.matches(
                cv_id=plan.selected_cv_id,
                cv_sha256=plan.selected_cv_hash,
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            proof = await session.prepare_final_action(
                identity=identity,
                fields=fields,
                disclosures=disclosures,
                decisions=plan.decisions,
                form_fingerprint=plan.form_fingerprint,
                attached_cv_sha256=plan.selected_cv_hash,
            )
            if not proof.valid_for(identity=identity, plan=plan) or not compare_digest(
                proof.resume_control_sha256,
                attachment.resume_control_sha256 or "",
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            pre_action = await session.snapshot()
            terminal_reason = _identity_independent_terminal_reason(
                pre_action.html,
                pre_action.url,
            )
            if terminal_reason is ReasonCode.ALREADY_APPLIED:
                return AlreadyAppliedOutcome()
            if terminal_reason is not None:
                return NeedsReviewOutcome(reason_code=terminal_reason)
            if (
                assess_smartrecruiters_v1_snapshot(
                    pre_action.html,
                    pre_action.url,
                    identity=identity,
                ).state
                is not SmartRecruitersPageState.FORM
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            action_nonce = _sha(token_bytes(32).hex())
            action = PreparedFinalActionV1(
                attempt_id=permit.attempt_id,
                adapter_name=plan.adapter_name,
                adapter_version=plan.adapter_version,
                selector_version=plan.selector_version,
                form_fingerprint=plan.form_fingerprint,
                attached_cv_hash=plan.attached_cv_hash,
                prepared_at=now,
                expires_at=min(
                    now + timedelta(minutes=2),
                    permit.expires_at,
                ),
                action_nonce=action_nonce,
            )
            self._prepared[action_nonce] = _PreparedState(
                session=session,
                plan=plan,
                permit=permit,
                identity=identity,
                proof=proof,
                pre_action_html=pre_action.html,
            )
            self._pending_preflight.pop(cleanup_id, None)
            return action
        except SmartRecruitersAdapterBlockedError as exc:
            return NeedsReviewOutcome(reason_code=exc.reason_code)
        except SmartRecruitersIdentityError:
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)

    async def commit(
        self,
        *,
        action: PreparedFinalActionV1,
        permit: FinalSubmitPermit,
    ) -> CommitOutcome:
        if not self.descriptor.allows_final_execution:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        state = self._prepared.get(action.action_nonce)
        now = self._clock()
        if state is None:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        if state.clicked:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_REPLAYED)
        try:
            valid = action.binds(state.plan, permit, at=now) and permit == state.permit
        except ValueError:
            valid = False
        if not valid:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)
        state.clicked = True
        final_action_at = now
        try:
            await state.session.click_final_action(state.proof)
            post_action = await state.session.snapshot()
            employer_reference = await state.session.confirmation_reference(state.identity)
        except SmartRecruitersAdapterBlockedError as exc:
            return FailedBeforeCommitOutcome(reason_code=exc.reason_code)
        except SmartRecruitersFinalActionAmbiguousError:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        assessment = assess_smartrecruiters_v1_snapshot(
            post_action.html,
            post_action.url,
            identity=state.identity,
        )
        if assessment.state is SmartRecruitersPageState.CHALLENGE:
            return UnknownOutcome(reason_code=ReasonCode.CHALLENGE_DETECTED)
        if assessment.state in {
            SmartRecruitersPageState.LOGIN,
            SmartRecruitersPageState.MFA,
        }:
            return UnknownOutcome(reason_code=assessment.reason_code or ReasonCode.SESSION_EXPIRED)
        if assessment.state is not SmartRecruitersPageState.CONFIRMATION or not employer_reference:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
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
                    rule_id="smartrecruiters-v1:visible-confirmation",
                    channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                    visible_selector=SMARTRECRUITERS_CONFIRMATION_SELECTOR,
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
            rule_id="smartrecruiters-v1:visible-confirmation",
            channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
            evidence_reference=_sha(employer_reference),
            observed_at=self._clock(),
            observed_after_final_action=True,
            was_present_before_action=bool(
                BeautifulSoup(
                    state.pre_action_html,
                    "html.parser",
                ).select(SMARTRECRUITERS_CONFIRMATION_SELECTOR)
            ),
            visible_selector=SMARTRECRUITERS_CONFIRMATION_SELECTOR,
            computed_visible=(
                _visible_confirmation_reference(
                    post_action.html,
                    state.identity,
                )
                == employer_reference
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
        sessions: list[SmartRecruitersCandidateSession] = []
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
            if id(session) not in closed:
                await session.close()
                closed.add(id(session))


def register_smartrecruiters_browser_v1(
    registry: SubmitterRegistry,
    *,
    browser_factory: SmartRecruitersBrowserFactory,
) -> SmartRecruitersBrowserV1:
    adapter = SmartRecruitersBrowserV1(
        browser_factory=browser_factory,
    )
    registry.register_two_phase(adapter)
    return adapter
