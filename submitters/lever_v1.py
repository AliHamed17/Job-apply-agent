"""Fixture-qualified two-phase Lever candidate-browser adapter.

The module has no network dependency. A private runner supplies one candidate
session and retains all URLs, answers, CV bytes, cookies, and page content in
memory. The checked-in descriptor has an empty live scope, so neither the
registry nor this adapter can perform a final external action.
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
from pathlib import Path
from profile.models import UserProfile
from secrets import token_bytes
from typing import Any, Protocol
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
from submitters.lever_identity import (
    LeverIdentityError,
    LeverPostingIdentity,
    parse_lever_posting_identity,
)
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    AdapterDescriptor,
    adapter_for_platform,
)

LEVER_V1_ADAPTER_VERSION = "1.0.0"
# v2 assumed markup (data-qa="application-form", data-field-id wrappers,
# authenticity_token) that a real, completed Lever submission on 2026-08-06
# (jobs.lever.co/collate, capture.json committed alongside this change)
# proved never exists on a real page: every v2 inspection would SELECTOR_DRIFT
# before a real form was ever read. v3 is a genuinely different, evidence-
# backed contract, not a patch -- bumping the version is what keeps any old
# fixture-qualified evidence for v2 from silently vouching for v3's behavior.
LEVER_V1_SELECTOR_VERSION = "lever-candidate-v3"
LEVER_FORM_SELECTOR = "form#application-form"
# Unverified: the real capture's confirmation-candidate search found no match
# on the actual post-submit page (confirmation_selector: None in the captured
# report), so this selector is still the old, unproven assumption. Left
# unchanged rather than guessed; see the P1 plan doc for the open question.
LEVER_CONFIRMATION_SELECTOR = (
    'main[data-qa="application-confirmation"][data-application-id][data-posting-id]'
)
_MAX_FIXTURE_HTML_BYTES = 256 * 1024
_MAX_FIELD_COUNT = 200
_MAX_RESUME_BYTES = 20 * 1024 * 1024
_FIELD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,500}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{6,160}$")
# Real hidden fields observed on a live, completed Lever submission. None of
# these existed in the v2 assumption (authenticity_token/csrf_token/_csrf/utf8
# -- a Rails-form convention Lever does not use).
_SYSTEM_CONTROL_NAMES = frozenset(
    {
        "accountId",
        "linkedInData",
        "origin",
        "referer",
        "timezone",
        "socialReferralKey",
        "socialSource",
        "resumeStorageId",
        "h-captcha-response",
        "source",
    }
)
_FIELD_WRAPPER_SELECTOR = "li.application-question"
_NAME_TO_FIELD_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def _field_id_from_name(name: str) -> str:
    """Derive a stable field_id from a control's name attribute.

    Real Lever markup has no per-field equivalent of a stable data-field-id:
    data-qa exists only on some controls (name-input, email-input, ...) and
    is absent on others that are just as real (urls[LinkedIn], ...). name is
    the one attribute every real control has, so it is the identifier this
    adapter keys on, transformed only enough to satisfy _FIELD_ID_RE (e.g.
    "urls[LinkedIn]" -> "urls_LinkedIn_").
    """
    return _NAME_TO_FIELD_ID_RE.sub("_", name)


class LeverPageState(StrEnum):
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
class LeverPageAssessment:
    state: LeverPageState
    reason_code: ReasonCode | None = None


@dataclass(frozen=True, slots=True, repr=False)
class LeverBrowserSnapshot:
    html: str
    url: str = ""
    locale: str = "en"

    def __post_init__(self) -> None:
        if len((self.html or "").encode("utf-8")) > _MAX_FIXTURE_HTML_BYTES:
            raise ValueError("LEVER_SNAPSHOT_TOO_LARGE")


@dataclass(frozen=True, slots=True, repr=False)
class LeverAttachmentProof:
    cv_id: str
    cv_sha256: str
    upload_complete: bool
    receipt_sha256: str | None = None
    resume_control_sha256: str | None = None

    def matches(self, *, cv_id: str, cv_sha256: str) -> bool:
        return (
            self.upload_complete is True
            and self.cv_id == cv_id
            and self.cv_sha256 == cv_sha256
            and self.receipt_sha256 is not None
            and self.resume_control_sha256 is not None
            and _DIGEST_RE.fullmatch(self.receipt_sha256) is not None
            and _DIGEST_RE.fullmatch(self.resume_control_sha256) is not None
        )


@dataclass(frozen=True, slots=True)
class LeverFinalActionProof:
    """Redacted proof of the exact retained native form boundary."""

    identity_sha256: str
    action_url_sha256: str
    form_fingerprint: str
    method: str
    encoding: str
    submitter_sha256: str
    actionability_sha256: str
    resume_control_sha256: str
    attached_cv_sha256: str
    payload_commitment_sha256: str
    user_field_count: int
    precommit_mutation_count: int

    def valid_for(
        self,
        *,
        identity: LeverPostingIdentity,
        plan: FormPlanV1,
    ) -> bool:
        values = (
            self.identity_sha256,
            self.action_url_sha256,
            self.form_fingerprint,
            self.submitter_sha256,
            self.actionability_sha256,
            self.resume_control_sha256,
            self.attached_cv_sha256,
            self.payload_commitment_sha256,
        )
        return (
            all(_DIGEST_RE.fullmatch(value) is not None for value in values)
            and self.identity_sha256 == _sha(identity.stable_key)
            and self.action_url_sha256 == _sha(identity.apply_url)
            and self.form_fingerprint == plan.form_fingerprint
            and self.method == "POST"
            and self.encoding == "multipart/form-data"
            and self.attached_cv_sha256 == plan.selected_cv_hash
            and self.user_field_count == len(plan.fields)
            and self.precommit_mutation_count == 0
        )


class LeverCandidateSession(Protocol):
    async def navigate(self, url: str) -> None: ...

    async def open_candidate_form(self, identity: LeverPostingIdentity) -> None: ...

    async def snapshot(self) -> LeverBrowserSnapshot: ...

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> LeverAttachmentProof: ...

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> LeverAttachmentProof: ...

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None: ...

    async def prepare_final_action(
        self,
        *,
        identity: LeverPostingIdentity,
        fields: tuple[FormFieldV1, ...],
        decisions: tuple[AnswerDecisionV1, ...],
        form_fingerprint: str,
        attached_cv_sha256: str,
    ) -> LeverFinalActionProof: ...

    async def click_final_action(self, proof: LeverFinalActionProof) -> None: ...

    async def confirmation_reference(
        self,
        identity: LeverPostingIdentity,
    ) -> str | None: ...

    async def close(self) -> None: ...


LeverBrowserFactory = Callable[[str], LeverCandidateSession]


class LeverAdapterBlockedError(RuntimeError):
    def __init__(self, reason_code: ReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


@dataclass(slots=True)
class _PreparedState:
    session: LeverCandidateSession
    plan: FormPlanV1
    permit: FinalSubmitPermit
    identity: LeverPostingIdentity
    proof: LeverFinalActionProof
    pre_action_html: str
    clicked: bool = False


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        classes = current.get("class") or ()
        normalized = (
            {str(classes).casefold()}
            if isinstance(classes, str)
            else {str(value).casefold() for value in classes}
        )
        if normalized.intersection({"hidden", "sr-only", "visually-hidden", "d-none"}):
            return False
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return True


def _static_actionability_capture(element: Tag) -> tuple[dict[str, object], ...] | None:
    """Conservatively reject non-actionable checked-in markup and ancestors."""

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


def _visible_confirmation_reference(
    html: str,
    identity: LeverPostingIdentity,
) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    try:
        matches = [
            node
            for node in soup.select(LEVER_CONFIRMATION_SELECTOR)
            if _visible(node)
            and str(node.get("data-posting-id", "")).strip().casefold() == identity.posting_id
        ]
    except Exception:
        return None
    if len(matches) != 1:
        return None
    reference = str(matches[0].get("data-application-id", "")).strip()
    return reference if _REFERENCE_RE.fullmatch(reference) else None


def _exact_form(
    html: str,
    identity: LeverPostingIdentity,
) -> Tag | None:
    """Real Lever markup carries no data-posting-id/data-site on the form
    itself -- confirmed on a live, completed submission -- so posting
    identity cannot be re-verified from form attributes the way v2 assumed.
    identity is accepted for signature/call-site stability and because the
    caller has already navigated to identity.apply_url before snapshotting;
    that navigation, not a form attribute, is what scopes this page to the
    right posting."""
    del identity
    soup = BeautifulSoup(html or "", "html.parser")
    forms = [form for form in soup.select(LEVER_FORM_SELECTOR) if _visible(form)]
    return forms[0] if len(forms) == 1 else None


def assess_lever_v1_snapshot(
    html: str,
    url: str = "",
    *,
    identity: LeverPostingIdentity | None = None,
) -> LeverPageAssessment:
    if len((html or "").encode("utf-8")) > _MAX_FIXTURE_HTML_BYTES:
        return LeverPageAssessment(LeverPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT)
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.stripped_strings).casefold()
    low_url = (url or "").casefold()
    # No structural captcha-widget check (e.g. .h-captcha, iframe[src*=captcha]):
    # a real, completed Lever submission (jobs.lever.co/collate, 2026-08-06;
    # request transcript in .capture/lever/capture.json) showed hCaptcha's
    # widget div AND its iframe (newassets.hcaptcha.com/.../static/...) are
    # both already present at the very first form_load, alongside only
    # passive resources (secure-api.js, checksiteconfig) -- normal page
    # furniture on every real Lever posting checked this session, not a
    # signal anything is blocking. The actual active challenge (real puzzle
    # images, the image_label_area_select endpoint) only appeared in the
    # transcript after the operator clicked submit, as part of the normal
    # submit flow the qualification ladder already requires a human for.
    # Checking for the widget's mere existence gave a 100% false-positive
    # rate on real pages -- assess_lever_v1_snapshot could never reach FORM
    # state at all. Text markers are kept as the only signal for now; this
    # is evidence that the old structural check was wrong, not proof these
    # markers alone catch every real active-challenge presentation, since no
    # capture has observed the DOM during that moment.
    if any(
        marker in text
        for marker in ("verify you are human", "security challenge", "complete the captcha")
    ):
        return LeverPageAssessment(LeverPageState.CHALLENGE, ReasonCode.CHALLENGE_DETECTED)
    if (
        soup.select_one('input[autocomplete="one-time-code"]') is not None
        or soup.select_one('[data-qa="mfa-challenge"]') is not None
    ):
        return LeverPageAssessment(LeverPageState.MFA, ReasonCode.MFA_REQUIRED)
    if (
        soup.select_one('input[type="password"]') is not None
        or soup.select_one('[data-qa="candidate-login"]') is not None
        or any(marker in low_url for marker in ("/login", "/signin", "/sign-in"))
    ):
        return LeverPageAssessment(LeverPageState.LOGIN, ReasonCode.SESSION_EXPIRED)
    if (
        soup.select_one('[data-qa="posting-closed"]') is not None
        or "this job is no longer accepting applications" in text
    ):
        return LeverPageAssessment(LeverPageState.CLOSED, ReasonCode.JOB_CLOSED)
    if (
        soup.select_one('[data-qa="already-applied"]') is not None
        or "you have already applied for this job" in text
    ):
        return LeverPageAssessment(LeverPageState.ALREADY_APPLIED, ReasonCode.ALREADY_APPLIED)
    if identity is not None and _visible_confirmation_reference(html, identity) is not None:
        return LeverPageAssessment(LeverPageState.CONFIRMATION)
    if identity is not None and _exact_form(html, identity) is not None:
        return LeverPageAssessment(LeverPageState.FORM)
    visible_apply = [node for node in soup.select('a[data-qa="btn-apply"][href]') if _visible(node)]
    if len(visible_apply) == 1:
        return LeverPageAssessment(LeverPageState.JOB)
    return LeverPageAssessment(LeverPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT)


def _bounded_int(raw: object, *, minimum: int = 0) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if value < minimum:
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _bounded_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if not math.isfinite(value):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _control_type(wrapper: Tag, control: Tag) -> tuple[FieldType, SensitiveCategory | None]:
    kind = str(wrapper.get("data-control-kind", "")).strip().casefold()
    if kind == "consent":
        return FieldType.CONSENT, SensitiveCategory.CONSENT
    if kind == "attestation":
        return FieldType.ATTESTATION, SensitiveCategory.ATTESTATION
    if control.name == "textarea":
        return FieldType.TEXTAREA, None
    if control.name == "select":
        return (FieldType.MULTI_SELECT if control.has_attr("multiple") else FieldType.SELECT), None
    raw = str(control.get("type", "text")).casefold()
    return (
        {
            "checkbox": FieldType.CHECKBOX,
            "date": FieldType.DATE,
            "email": FieldType.EMAIL,
            "file": FieldType.FILE,
            "number": FieldType.NUMBER,
            "radio": FieldType.RADIO,
            "tel": FieldType.PHONE,
            "url": FieldType.URL,
        }.get(raw, FieldType.TEXT),
        None,
    )


def _options(wrapper: Tag, control: Tag, field_type: FieldType) -> tuple[FormOptionV1, ...]:
    if field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
        nodes = [node for node in control.find_all("option") if isinstance(node, Tag)]
    elif field_type is FieldType.RADIO:
        nodes = [
            node for node in wrapper.select('input[type="radio"][value]') if isinstance(node, Tag)
        ]
    else:
        return ()
    result: list[FormOptionV1] = []
    for index, node in enumerate(nodes):
        value = str(node.get("value", "")).strip()
        if not value:
            continue
        label = (
            node.get_text(" ", strip=True)
            if node.name == "option"
            else str(node.get("data-option-label", "")).strip()
        )
        if not label:
            raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        result.append(
            FormOptionV1(
                option_id=str(node.get("data-option-id", f"option-{index}")).strip(),
                value=value,
                label=label,
                disabled=node.has_attr("disabled"),
            )
        )
    return tuple(result)


def observe_lever_v1_fields(
    html: str,
    *,
    identity: LeverPostingIdentity,
) -> tuple[FormFieldV1, ...]:
    if len((html or "").encode("utf-8")) > _MAX_FIXTURE_HTML_BYTES:
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
    form = _exact_form(html, identity)
    if form is None:
        raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    wrappers = [node for node in form.select(_FIELD_WRAPPER_SELECTOR) if _visible(node)]
    if not wrappers or len(wrappers) > _MAX_FIELD_COUNT:
        raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    fields: list[FormFieldV1] = []
    seen_ids: set[str] = set()
    for position, wrapper in enumerate(wrappers):
        controls = [
            control
            for control in wrapper.select("input, textarea, select")
            if str(control.get("type", "")).casefold() != "hidden"
        ]
        if not controls:
            raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        control = controls[0]
        field_type, sensitive_category = _control_type(wrapper, control)
        names = {str(node.get("name", "")).strip() for node in controls}
        if (
            "" in names
            or (field_type is FieldType.RADIO and len(names) != 1)
            or (field_type is not FieldType.RADIO and len(controls) != 1)
        ):
            raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        field_id = _field_id_from_name(str(control.get("name", "")).strip())
        if not _FIELD_ID_RE.fullmatch(field_id) or field_id in seen_ids:
            raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        label_node = wrapper.select_one("label, legend, [data-qa='field-label']")
        label = label_node.get_text(" ", strip=True) if label_node is not None else ""
        if not label:
            raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        accepted = tuple(
            item.strip() for item in str(control.get("accept", "")).split(",") if item.strip()
        )[:32]
        pattern = str(control.get("pattern", "")).strip() or None
        if pattern is not None and (
            len(pattern) > 128 or any(ord(character) < 32 for character in pattern)
        ):
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        fields.append(
            FormFieldV1(
                field_id=field_id,
                canonical_name=str(wrapper.get("data-canonical-name", "")).strip() or None,
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


def _wrapper_field_ids(
    form: Tag,
    field_by_id: Mapping[str, FormFieldV1],
) -> dict[int, str]:
    """Map each field wrapper (by Python object id) to the field_id
    observe_lever_v1_fields would have assigned it, via the same
    primary-control rule. Used so a hidden companion control sharing a
    mapped field's wrapper -- the location autocomplete's selectedLocation,
    confirmed on a live submission -- is recognised as belonging to that
    field instead of being rejected as an unexplained control."""
    result: dict[int, str] = {}
    for wrapper in form.select(_FIELD_WRAPPER_SELECTOR):
        visible_controls = [
            c
            for c in wrapper.select("input, textarea, select")
            if str(c.get("type", "")).casefold() != "hidden"
        ]
        if not visible_controls:
            continue
        name = str(visible_controls[0].get("name", "")).strip()
        if not name:
            continue
        candidate_id = _field_id_from_name(name)
        if candidate_id in field_by_id:
            result[id(wrapper)] = candidate_id
    return result


def lever_v1_final_action_binding(
    html: str,
    *,
    identity: LeverPostingIdentity,
    fields: tuple[FormFieldV1, ...],
) -> str:
    """Hash exact successful-control ownership and the native POST boundary."""

    form = _exact_form(html, identity)
    if form is None:
        raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    # No action-URL check: real markup has no action attribute at all (a
    # browser posts an action-less form to its own page, which is already
    # identity.apply_url because the session navigated there before
    # snapshotting -- confirmed on a live submission).
    if (
        str(form.get("method", "")).strip().casefold() != "post"
        or str(form.get("enctype", "")).strip().casefold() != "multipart/form-data"
    ):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
    # type="button", not type="submit": the visible button does not natively
    # submit the form. hCaptcha's own script gates a second, hidden,
    # nameless type="submit" button -- confirmed on a live submission. The
    # adapter still only ever clicks the one visible, named data-qa button.
    submits = [node for node in form.select('button[data-qa="btn-submit"]') if _visible(node)]
    if len(submits) != 1:
        raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    submit = submits[0]
    actionability_capture = _static_actionability_capture(submit)
    if (
        actionability_capture is None
        or submit.has_attr("disabled")
        or str(submit.get("aria-disabled", "")).casefold() == "true"
        or any(
            submit.has_attr(attribute)
            for attribute in ("form", "formaction", "formmethod", "formenctype", "name")
        )
    ):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)

    field_by_id = {field.field_id: field for field in fields}
    if len(field_by_id) != len(fields):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
    wrapper_field_id = _wrapper_field_ids(form, field_by_id)
    owners: dict[str, dict[str, object]] = {}
    seen_system: set[str] = set()
    mapped_fields: set[str] = set()
    for control in form.select("[name]"):
        if control is submit:
            continue
        name = str(control.get("name", "")).strip()
        if (
            not name
            or len(name.encode("utf-8")) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or control.has_attr("disabled")
        ):
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        input_type = str(control.get("type", "")).casefold()
        wrapper = control.find_parent("li", class_="application-question")
        wrapper_key = id(wrapper) if wrapper is not None else None
        if wrapper_key is None or wrapper_key not in wrapper_field_id:
            if (
                control.name != "input"
                or input_type != "hidden"
                or name not in _SYSTEM_CONTROL_NAMES
            ):
                raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
            # Lever's own system fields are legitimately allowed to start
            # empty (e.g. origin/referer/socialSource on a direct visit) --
            # unlike v2's authenticity_token assumption, presence of a
            # nonempty value is not required, only that the name is exactly
            # one of the confirmed real system fields and appears once.
            if name in seen_system:
                raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
            seen_system.add(name)
            continue
        if not _visible(wrapper):
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        field_id = wrapper_field_id[wrapper_key]
        field = field_by_id[field_id]
        is_primary_control = _field_id_from_name(name) == field_id and input_type != "hidden"
        if is_primary_control:
            prior = owners.get(name)
            if prior is not None and (
                prior["field_id"] != field_id or field.field_type is not FieldType.RADIO
            ):
                raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
            owners[name] = {"field_id": field_id, "field_type": field.field_type.value}
        elif input_type != "hidden":
            # A second non-hidden, non-primary control inside a mapped
            # wrapper is a shape observe_lever_v1_fields did not account for.
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        mapped_fields.add(field_id)
    if mapped_fields != set(field_by_id):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
    if (
        sum(field.field_type is FieldType.FILE for field in fields) != 1
        or sum(1 for owner in owners.values() if owner["field_type"] == FieldType.FILE.value) != 1
    ):
        raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    payload = {
        "identity_sha256": _sha(identity.stable_key),
        "action_sha256": _sha(identity.apply_url),
        "method": "POST",
        "encoding": "multipart/form-data",
        "submitter": "button:data-qa=btn-submit:type=button",
        "actionability_sha256": _sha(
            json.dumps(
                actionability_capture,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        ),
        "owners": [
            {
                "control_name_sha256": _sha(name),
                "field_id": owner["field_id"],
                "field_type": owner["field_type"],
            }
            for name, owner in owners.items()
        ],
        "system_control_names": sorted(seen_system),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _sha(canonical)


def lever_v1_form_fingerprint(
    identity: LeverPostingIdentity,
    fields: tuple[FormFieldV1, ...],
    final_action_binding: str,
) -> str:
    if (
        not fields
        or len(fields) > _MAX_FIELD_COUNT
        or _DIGEST_RE.fullmatch(final_action_binding or "") is None
    ):
        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
    payload = {
        "adapter_version": LEVER_V1_ADAPTER_VERSION,
        "selector_version": LEVER_V1_SELECTOR_VERSION,
        "posting_identity_sha256": _sha(identity.stable_key),
        "final_action_binding": final_action_binding,
        "fields": [field.model_dump(mode="json") for field in fields],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _sha(canonical)


def _read_verified_resume_bytes(path: str, expected_sha256: str) -> bytes:
    with Path(path).open("rb") as handle:
        payload = handle.read(_MAX_RESUME_BYTES + 1)
    if (
        not payload
        or len(payload) > _MAX_RESUME_BYTES
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    return payload


def _descriptor() -> AdapterDescriptor:
    descriptor = adapter_for_platform("lever")
    if (
        descriptor is None
        or descriptor.adapter_version != LEVER_V1_ADAPTER_VERSION
        or descriptor.selector_version != LEVER_V1_SELECTOR_VERSION
        or descriptor.execution_contract_version != TWO_PHASE_EXECUTION_CONTRACT_VERSION
    ):
        raise RuntimeError("LEVER_V1_DESCRIPTOR_MISMATCH")
    return descriptor


class LeverBrowserV1:
    def __init__(
        self,
        *,
        browser_factory: LeverBrowserFactory,
        answer_policy: AnswerPolicyV1 | None = None,
        descriptor: AdapterDescriptor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.descriptor = descriptor or _descriptor()
        self._browser_factory = browser_factory
        self._answer_policy = answer_policy or AnswerPolicyV1()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._prepared: dict[str, _PreparedState] = {}
        self._pending_preflight: dict[str, LeverCandidateSession] = {}
        self._cleanup_preflight_id: ContextVar[str | None] = ContextVar(
            f"lever-v1-cleanup-{id(self)}",
            default=None,
        )

    def can_inspect(self, job: JobData) -> bool:
        try:
            parse_lever_posting_identity(job.apply_url or job.source_url)
        except LeverIdentityError:
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
            raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            identity = parse_lever_posting_identity(job.apply_url or job.source_url)
            resume_bytes = _read_verified_resume_bytes(resume_path, application.cv_sha256)
            profile = UserProfile.model_validate(dict(user_profile))
        except LeverIdentityError as exc:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        except OSError as exc:
            raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        except (TypeError, ValueError) as exc:
            raise LeverAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN) from exc
        session = self._browser_factory(identity.apply_url)
        try:
            await session.navigate(identity.apply_url)
            snapshot = await session.snapshot()
            assessment = assess_lever_v1_snapshot(
                snapshot.html,
                snapshot.url,
                identity=identity,
            )
            if assessment.state is LeverPageState.JOB:
                await session.open_candidate_form(identity)
                snapshot = await session.snapshot()
                assessment = assess_lever_v1_snapshot(
                    snapshot.html,
                    snapshot.url,
                    identity=identity,
                )
            if assessment.reason_code is not None:
                raise LeverAdapterBlockedError(assessment.reason_code)
            if assessment.state is LeverPageState.CONFIRMATION:
                raise LeverAdapterBlockedError(ReasonCode.ALREADY_APPLIED)
            if assessment.state is not LeverPageState.FORM:
                raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

            fields = observe_lever_v1_fields(snapshot.html, identity=identity)
            final_action_binding = lever_v1_final_action_binding(
                snapshot.html,
                identity=identity,
                fields=fields,
            )
            fingerprint = lever_v1_form_fingerprint(
                identity,
                fields,
                final_action_binding,
            )
            proof = await session.ensure_resume_attachment(
                resume_bytes=resume_bytes,
                cv_id=selected_cv_id,
                expected_sha256=application.cv_sha256,
            )
            attachment_verified = proof.matches(
                cv_id=selected_cv_id,
                cv_sha256=application.cv_sha256,
            )
            context = AnswerPolicyContext(
                profile=profile,
                profile_version=application.profile_version,
                selected_cv_id=selected_cv_id,
                selected_cv_hash=application.cv_sha256,
                attached_cv_id=proof.cv_id,
                attached_cv_hash=proof.cv_sha256,
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
                raise LeverAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
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
                raise LeverAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
            audit = (
                policy.prompt_version,
                policy.model_provider,
                policy.model_name,
                policy.model_digest,
            )
            if any(value is not None for value in audit) and not all(
                isinstance(value, str) for value in audit
            ):
                raise LeverAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
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
                attached_cv_id=proof.cv_id,
                attached_cv_hash=proof.cv_sha256,
                attachment_verified=attachment_verified,
                profile_version=application.profile_version,
                session_verified_at=now,
                created_at=now,
                expires_at=now + timedelta(minutes=30),
                fields=fields,
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
            or context.selected_cv_hash != plan.selected_cv_hash
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            identity = parse_lever_posting_identity(context.normalized_job_url)
            resume_bytes = _read_verified_resume_bytes(context.resume_path, plan.selected_cv_hash)
            session = self._browser_factory(identity.apply_url)
        except (LeverIdentityError, OSError, LeverAdapterBlockedError):
            return NeedsReviewOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        cleanup_id = _sha(token_bytes(32).hex())
        self._pending_preflight[cleanup_id] = session
        self._cleanup_preflight_id.set(cleanup_id)
        try:
            await session.navigate(identity.apply_url)
            snapshot = await session.snapshot()
            assessment = assess_lever_v1_snapshot(
                snapshot.html,
                snapshot.url,
                identity=identity,
            )
            if assessment.state is LeverPageState.ALREADY_APPLIED:
                return AlreadyAppliedOutcome()
            if assessment.reason_code is not None:
                return NeedsReviewOutcome(reason_code=assessment.reason_code)
            if assessment.state is not LeverPageState.FORM:
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            fields = observe_lever_v1_fields(snapshot.html, identity=identity)
            final_action_binding = lever_v1_final_action_binding(
                snapshot.html,
                identity=identity,
                fields=fields,
            )
            if (
                fields != plan.fields
                or lever_v1_form_fingerprint(
                    identity,
                    fields,
                    final_action_binding,
                )
                != plan.form_fingerprint
                or len(plan.decisions) != len(fields)
                or any(
                    decision.disposition is not AnswerDisposition.RESOLVED
                    for decision in plan.decisions
                )
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            proof = await session.ensure_resume_attachment(
                resume_bytes=resume_bytes,
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            await session.fill(plan.decisions)
            filled_snapshot = await session.snapshot()
            filled_assessment = assess_lever_v1_snapshot(
                filled_snapshot.html,
                filled_snapshot.url,
                identity=identity,
            )
            if filled_assessment.state is not LeverPageState.FORM:
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            filled_fields = observe_lever_v1_fields(
                filled_snapshot.html,
                identity=identity,
            )
            filled_action_binding = lever_v1_final_action_binding(
                filled_snapshot.html,
                identity=identity,
                fields=filled_fields,
            )
            if (
                filled_fields != fields
                or lever_v1_form_fingerprint(
                    identity,
                    filled_fields,
                    filled_action_binding,
                )
                != plan.form_fingerprint
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            proof = await session.verify_resume_attachment(
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            if not proof.matches(
                cv_id=plan.selected_cv_id,
                cv_sha256=plan.selected_cv_hash,
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            action_proof = await session.prepare_final_action(
                identity=identity,
                fields=fields,
                decisions=plan.decisions,
                form_fingerprint=plan.form_fingerprint,
                attached_cv_sha256=plan.selected_cv_hash,
            )
            if (
                not action_proof.valid_for(identity=identity, plan=plan)
                or action_proof.resume_control_sha256 != proof.resume_control_sha256
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            pre_action_snapshot = await session.snapshot()
            if (
                assess_lever_v1_snapshot(
                    pre_action_snapshot.html,
                    pre_action_snapshot.url,
                    identity=identity,
                ).state
                is not LeverPageState.FORM
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
                expires_at=min(now + timedelta(minutes=2), permit.expires_at),
                action_nonce=action_nonce,
            )
            self._prepared[action_nonce] = _PreparedState(
                session=session,
                plan=plan,
                permit=permit,
                identity=identity,
                proof=action_proof,
                pre_action_html=pre_action_snapshot.html,
            )
            self._pending_preflight.pop(cleanup_id, None)
            return action
        except LeverAdapterBlockedError as exc:
            return NeedsReviewOutcome(reason_code=exc.reason_code)
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
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        assessment = assess_lever_v1_snapshot(
            post_action.html,
            post_action.url,
            identity=state.identity,
        )
        if assessment.state is LeverPageState.CHALLENGE:
            return UnknownOutcome(reason_code=ReasonCode.CHALLENGE_DETECTED)
        if assessment.state in {LeverPageState.LOGIN, LeverPageState.MFA}:
            return UnknownOutcome(reason_code=assessment.reason_code or ReasonCode.SESSION_EXPIRED)
        if assessment.state is not LeverPageState.CONFIRMATION or not employer_reference:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        redacted_reference = _sha(employer_reference)
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
                    rule_id="lever-v1:visible-confirmation",
                    channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                    visible_selector=LEVER_CONFIRMATION_SELECTOR,
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
            rule_id="lever-v1:visible-confirmation",
            channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
            evidence_reference=redacted_reference,
            observed_at=self._clock(),
            observed_after_final_action=True,
            was_present_before_action=bool(
                BeautifulSoup(state.pre_action_html, "html.parser").select(
                    LEVER_CONFIRMATION_SELECTOR
                )
            ),
            visible_selector=LEVER_CONFIRMATION_SELECTOR,
            computed_visible=(
                _visible_confirmation_reference(post_action.html, state.identity)
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
        sessions: list[LeverCandidateSession] = []
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


def register_lever_browser_v1(
    registry: SubmitterRegistry,
    *,
    browser_factory: LeverBrowserFactory,
    answer_policy: AnswerPolicyV1 | None = None,
    descriptor: AdapterDescriptor | None = None,
) -> LeverBrowserV1:
    adapter = LeverBrowserV1(
        browser_factory=browser_factory,
        answer_policy=answer_policy,
        descriptor=descriptor,
    )
    registry.register_two_phase(adapter)
    return adapter
