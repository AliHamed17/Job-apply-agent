"""Fixture-qualified Ashby candidate-browser domain adapter.

The module is deliberately transport-neutral. It observes sanitized candidate
markup, creates an immutable reviewed plan, and binds a later browser session
to one exact HTML form action. It does not call Ashby's private React requests
or treat an HTTP response as submission evidence.

The checked-in descriptor has an empty qualified scope. Production routing and
the adapter's own preflight/commit gates therefore refuse every final action
until a later dry-run and explicitly approved live-canary release.
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
from urllib.parse import urljoin
from uuid import uuid4

from bs4 import BeautifulSoup, Tag

from core.form_planning import AnswerPolicyContext, AnswerPolicyV1
from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
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
from submitters.ashby_identity import (
    AshbyApplicationIdentity,
    AshbyIdentityError,
    canonical_ashby_application_url,
    parse_ashby_candidate_url,
)
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

ASHBY_V1_ADAPTER_VERSION = "1.0.0"
ASHBY_V1_SELECTOR_VERSION = "ashby-candidate-v1"
ASHBY_FORM_SELECTOR = "form[data-ashby-application-form]"
ASHBY_FIELD_SELECTOR = "[data-ashby-field][data-field-id]"
ASHBY_FINAL_CONTROL_SELECTOR = (
    f'{ASHBY_FORM_SELECTOR} button[data-ashby-submit-application][type="submit"]'
)
ASHBY_CONFIRMATION_SELECTOR = "main[data-ashby-application-confirmation][data-submission-id]"
_MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_FIELD_COUNT = 200
_MAX_REACT_PASSES = 8
_MAX_RESUME_BYTES = 20 * 1024 * 1024
_FIELD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,500}$")
_CONTROL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:[\]-]{1,256}$")
_EVIDENCE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{5,200}$")


class AshbyPageState(StrEnum):
    """Bounded candidate states recognized by the v1 selector contract."""

    JOB = "job"
    FORM = "form"
    CONFIRMATION = "confirmation"
    ALREADY_APPLIED = "already_applied"
    CLOSED = "closed"
    LOGIN = "login"
    MFA = "mfa"
    CHALLENGE = "challenge"
    SELECTOR_DRIFT = "selector_drift"


@dataclass(frozen=True, slots=True)
class AshbyPageAssessment:
    state: AshbyPageState
    reason_code: ReasonCode | None = None


@dataclass(frozen=True, slots=True, repr=False)
class AshbyBrowserSnapshot:
    """Ephemeral candidate DOM; it must never be persisted or logged."""

    html: str
    url: str
    locale: str = "en"

    def __post_init__(self) -> None:
        if len(self.html.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("ASHBY_SNAPSHOT_TOO_LARGE")


@dataclass(frozen=True, slots=True, repr=False)
class AshbyAttachmentProof:
    """Redacted proof that exact CV bytes reached one exact file control."""

    field_id: str
    control_name: str
    cv_id: str
    cv_sha256: str
    upload_complete: bool
    receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        cv_id_valid = (
            isinstance(self.cv_id, str)
            and self.cv_id == self.cv_id.strip()
            and 0 < len(self.cv_id) <= 256
            and all(31 < ord(character) != 127 for character in self.cv_id)
        )
        receipt_valid = (
            isinstance(self.receipt_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.receipt_sha256) is not None
        )
        if (
            not isinstance(self.field_id, str)
            or _FIELD_ID_RE.fullmatch(self.field_id) is None
            or not isinstance(self.control_name, str)
            or _CONTROL_NAME_RE.fullmatch(self.control_name) is None
            or not cv_id_valid
            or not isinstance(self.cv_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.cv_sha256) is None
            or not isinstance(self.upload_complete, bool)
            or (self.upload_complete and not receipt_valid)
            or (not self.upload_complete and self.receipt_sha256 is not None)
        ):
            raise ValueError("ASHBY_ATTACHMENT_PROOF_INVALID")

    def matches(
        self,
        *,
        field_id: str,
        control_name: str,
        cv_id: str,
        cv_sha256: str,
    ) -> bool:
        return (
            isinstance(cv_sha256, str)
            and self.upload_complete is True
            and self.field_id == field_id
            and self.control_name == control_name
            and self.cv_id == cv_id
            and compare_digest(self.cv_sha256, cv_sha256)
            and self.receipt_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.receipt_sha256) is not None
        )


@dataclass(frozen=True, slots=True, repr=False)
class AshbyFinalRequestContract:
    """Exact main-frame candidate form target and reviewed control manifest."""

    identity: AshbyApplicationIdentity
    target_url: str
    method: str
    enctype: str
    field_controls: tuple[tuple[str, FieldType, tuple[str, ...]], ...]
    system_controls: tuple[str, ...]
    submit_control: tuple[str, str]
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, AshbyApplicationIdentity):
            raise ValueError("ASHBY_FINAL_REQUEST_CONTRACT_INVALID")
        field_ids: set[str] = set()
        reviewed_names: set[str] = set()
        manifest_valid = (
            isinstance(self.field_controls, tuple)
            and 0 < len(self.field_controls) <= _MAX_FIELD_COUNT
        )
        if manifest_valid:
            for entry in self.field_controls:
                if not isinstance(entry, tuple) or len(entry) != 3:
                    manifest_valid = False
                    break
                field_id, field_type, names = entry
                if (
                    not isinstance(field_id, str)
                    or _FIELD_ID_RE.fullmatch(field_id) is None
                    or field_id in field_ids
                    or not isinstance(field_type, FieldType)
                    or not isinstance(names, tuple)
                    or len(names) != 1
                    or not isinstance(names[0], str)
                    or _CONTROL_NAME_RE.fullmatch(names[0]) is None
                    or names[0] in reviewed_names
                ):
                    manifest_valid = False
                    break
                field_ids.add(field_id)
                reviewed_names.add(names[0])

        system_valid = (
            isinstance(self.system_controls, tuple)
            and len(self.system_controls) <= _MAX_FIELD_COUNT
        )
        system_names: set[str] = set()
        if system_valid:
            for name in self.system_controls:
                if (
                    not isinstance(name, str)
                    or _CONTROL_NAME_RE.fullmatch(name) is None
                    or name in system_names
                    or name in reviewed_names
                ):
                    system_valid = False
                    break
                system_names.add(name)

        submit_valid = (
            isinstance(self.submit_control, tuple)
            and len(self.submit_control) == 2
            and isinstance(self.submit_control[0], str)
            and _CONTROL_NAME_RE.fullmatch(self.submit_control[0]) is not None
            and self.submit_control[0] not in reviewed_names
            and self.submit_control[0] not in system_names
            and isinstance(self.submit_control[1], str)
            and 0 < len(self.submit_control[1]) <= 256
            and all(31 < ord(character) != 127 for character in self.submit_control[1])
        )
        if not manifest_valid or not system_valid or not submit_valid:
            raise ValueError("ASHBY_FINAL_REQUEST_CONTRACT_INVALID")

        payload = {
            "identity": {
                "board": self.identity.board_token,
                "posting": self.identity.posting_id,
            },
            "target_url": self.target_url,
            "method": self.method,
            "enctype": self.enctype,
            "field_controls": [
                {
                    "field_id": field_id,
                    "field_type": field_type.value,
                    "names": names,
                }
                for field_id, field_type, names in self.field_controls
            ],
            "system_controls": self.system_controls,
            "submit_control": self.submit_control,
        }
        expected = _canonical_digest(payload)
        if (
            self.method != "POST"
            or self.enctype != "multipart/form-data"
            or self.target_url
            != (
                f"https://jobs.ashbyhq.com/{self.identity.board_token}/"
                f"{self.identity.posting_id}/application"
            )
            or not self.field_controls
            or re.fullmatch(r"[0-9a-f]{64}", self.digest or "") is None
            or not compare_digest(expected, self.digest)
        ):
            raise ValueError("ASHBY_FINAL_REQUEST_CONTRACT_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class AshbyAnswerBinding:
    """One reviewed answer represented only by a type-aware digest."""

    field_id: str
    field_type: FieldType
    value_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.field_id, str)
            or _FIELD_ID_RE.fullmatch(self.field_id) is None
            or not isinstance(self.field_type, FieldType)
            or re.fullmatch(r"[0-9a-f]{64}", self.value_sha256 or "") is None
        ):
            raise ValueError("ASHBY_ANSWER_BINDING_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class AshbyFinalCommitExpectation:
    """Transport-local truth contract rechecked at the final action boundary."""

    identity: AshbyApplicationIdentity
    form_fingerprint: str
    observed_fields: tuple[FormFieldV1, ...]
    answer_bindings: tuple[AshbyAnswerBinding, ...]
    selected_cv_id: str
    selected_cv_hash: str
    attachment_receipt_sha256: str
    pre_action_digest: str
    request_contract: AshbyFinalRequestContract

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_contract, AshbyFinalRequestContract)
            or not isinstance(self.observed_fields, tuple)
            or not isinstance(self.answer_bindings, tuple)
        ):
            raise ValueError("ASHBY_FINAL_COMMIT_EXPECTATION_INVALID")
        try:
            fingerprint_matches = compare_digest(
                ashby_v1_form_fingerprint(
                    self.observed_fields,
                    self.request_contract.digest,
                ),
                self.form_fingerprint,
            )
        except (TypeError, ValueError):
            fingerprint_matches = False
        observed_identity = tuple(
            (field.field_id, field.field_type) for field in self.observed_fields
        )
        request_identity = tuple(
            (field_id, field_type)
            for field_id, field_type, _names in self.request_contract.field_controls
        )
        cv_id_valid = (
            isinstance(self.selected_cv_id, str)
            and self.selected_cv_id == self.selected_cv_id.strip()
            and 0 < len(self.selected_cv_id) <= 256
            and all(31 < ord(character) != 127 for character in self.selected_cv_id)
        )
        if (
            self.identity != self.request_contract.identity
            or not cv_id_valid
            or not self.observed_fields
            or len(self.observed_fields) > _MAX_FIELD_COUNT
            or any(
                re.fullmatch(r"[0-9a-f]{64}", value or "") is None
                for value in (
                    self.form_fingerprint,
                    self.selected_cv_hash,
                    self.attachment_receipt_sha256,
                    self.pre_action_digest,
                )
            )
            or tuple((binding.field_id, binding.field_type) for binding in self.answer_bindings)
            != observed_identity
            or request_identity != observed_identity
            or not fingerprint_matches
        ):
            raise ValueError("ASHBY_FINAL_COMMIT_EXPECTATION_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class AshbyFinalActionReceipt:
    """Redacted receipt that one exact validated request may have left."""

    request_contract_digest: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if any(
            re.fullmatch(r"[0-9a-f]{64}", value or "") is None
            for value in (self.request_contract_digest, self.payload_sha256)
        ):
            raise ValueError("ASHBY_FINAL_ACTION_RECEIPT_INVALID")


class AshbyFinalActionAmbiguousError(RuntimeError):
    """The final candidate request may have left; automatic retry is unsafe."""


class AshbyAdapterBlockedError(RuntimeError):
    """Fail-closed error containing only a bounded reason code."""

    def __init__(self, reason_code: ReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


_ASHBY_REVIEW_REASONS = frozenset(
    {
        ReasonCode.RUNTIME_NOT_READY,
        ReasonCode.BUILD_MISMATCH,
        ReasonCode.ADAPTER_NOT_QUALIFIED,
        ReasonCode.SESSION_EXPIRED,
        ReasonCode.MFA_REQUIRED,
        ReasonCode.CHALLENGE_DETECTED,
        ReasonCode.FORM_CHANGED,
        ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ReasonCode.ATTACHMENT_UNVERIFIED,
        ReasonCode.JOB_CLOSED,
        ReasonCode.SELECTOR_DRIFT,
        ReasonCode.UNSUPPORTED_CONTROL,
    }
)
_ASHBY_FAILED_BEFORE_COMMIT_REASONS = frozenset(
    {
        ReasonCode.PERMIT_MISSING,
        ReasonCode.PERMIT_EXPIRED,
        ReasonCode.PERMIT_REPLAYED,
        ReasonCode.PERMIT_BINDING_MISMATCH,
        ReasonCode.COMMAND_EXPIRED,
        ReasonCode.COMMAND_REPLAYED,
        ReasonCode.GOVERNOR_DENIED,
        ReasonCode.OPERATOR_CANCELLED,
        ReasonCode.NETWORK_ERROR,
        ReasonCode.INTERNAL_ERROR,
    }
)


def _typed_pre_request_block(
    reason_code: ReasonCode,
) -> NeedsReviewOutcome | FailedBeforeCommitOutcome:
    if reason_code in _ASHBY_REVIEW_REASONS:
        return NeedsReviewOutcome(reason_code=reason_code)
    if reason_code in _ASHBY_FAILED_BEFORE_COMMIT_REASONS:
        return FailedBeforeCommitOutcome(reason_code=reason_code)
    return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)


class AshbyCandidateSession(Protocol):
    """One candidate page owned by the private local runner."""

    async def navigate(self, url: str) -> None: ...

    async def open_application_form(self) -> None: ...

    async def snapshot(self) -> AshbyBrowserSnapshot: ...

    async def ensure_resume_attachment(
        self,
        *,
        field_id: str,
        control_name: str,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> AshbyAttachmentProof: ...

    async def verify_resume_attachment(
        self,
        *,
        field_id: str,
        control_name: str,
        cv_id: str,
        expected_sha256: str,
    ) -> AshbyAttachmentProof: ...

    async def fill_once(self, decisions: tuple[AnswerDecisionV1, ...]) -> None: ...

    async def settle_react(self) -> None: ...

    async def commit_final_action(
        self,
        expectation: AshbyFinalCommitExpectation,
    ) -> AshbyFinalActionReceipt: ...

    async def confirmation_reference(self) -> str | None: ...

    async def close(self) -> None: ...


AshbyBrowserFactory = Callable[[str], AshbyCandidateSession]


@dataclass(slots=True)
class _PreparedState:
    session: AshbyCandidateSession
    plan: FormPlanV1
    permit: FinalSubmitPermit
    identity: AshbyApplicationIdentity
    fields: tuple[FormFieldV1, ...]
    attachment: AshbyAttachmentProof
    request_contract: AshbyFinalRequestContract
    pre_action_html: str
    pre_action_digest: str
    commit_claimed: bool = False
    released: bool = False


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
                "pointer-events:none",
                "content-visibility:hidden",
            )
        ):
            return False
        raw_classes = current.get("class") or ()
        class_names: tuple[str, ...]
        if isinstance(raw_classes, str):
            class_names = (raw_classes,)
        else:
            class_names = tuple(str(value) for value in raw_classes)
        if {value.casefold() for value in class_names}.intersection(
            {"hidden", "d-none", "ashby-hidden"}
        ):
            return False
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return True


def _rendered_wrapper(wrapper: Tag) -> bool:
    """Require React's explicit rendered marker to agree with static visibility."""

    marker = str(wrapper.get("data-ashby-rendered", "true")).strip().casefold()
    if marker not in {"true", "false"}:
        raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    visible = _visible(wrapper)
    if (marker == "true") != visible:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return visible


def _bounded_int(raw: object, *, minimum: int = 0) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if value < minimum:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _bounded_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if not math.isfinite(value):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _control_type(wrapper: Tag, control: Tag) -> FieldType:
    declared = str(wrapper.get("data-control-kind", "")).strip().casefold()
    if declared:
        try:
            field_type = FieldType(declared)
        except ValueError as exc:
            raise AshbyAdapterBlockedError(ReasonCode.UNSUPPORTED_CONTROL) from exc
        if field_type in {FieldType.CONSENT, FieldType.ATTESTATION}:
            if control.name != "input" or str(control.get("type", "")).casefold() != "checkbox":
                raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
            return field_type
    if control.name == "textarea":
        return FieldType.TEXTAREA
    if control.name == "select":
        return FieldType.MULTI_SELECT if control.has_attr("multiple") else FieldType.SELECT
    raw = str(control.get("type", "text")).strip().casefold()
    supported = {
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
    }
    inferred_field_type = supported.get(raw)
    if inferred_field_type is None:
        raise AshbyAdapterBlockedError(ReasonCode.UNSUPPORTED_CONTROL)
    return inferred_field_type


def _field_options(
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
    options: list[FormOptionV1] = []
    for position, node in enumerate(nodes):
        value = str(node.get("value", "")).strip()
        if not value:
            continue
        label = (
            node.get_text(" ", strip=True)
            if node.name == "option"
            else str(node.get("data-option-label", "")).strip()
        )
        options.append(
            FormOptionV1(
                option_id=str(node.get("data-option-id", f"option-{position}")).strip(),
                value=value,
                label=label or value,
                disabled=node.has_attr("disabled"),
            )
        )
    return tuple(options)


def _sensitive_category(wrapper: Tag, field_type: FieldType) -> SensitiveCategory | None:
    declared = str(wrapper.get("data-sensitive-category", "")).strip().casefold()
    if declared:
        try:
            return SensitiveCategory(declared)
        except ValueError as exc:
            raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT) from exc
    return {
        FieldType.CONSENT: SensitiveCategory.CONSENT,
        FieldType.ATTESTATION: SensitiveCategory.ATTESTATION,
    }.get(field_type)


def _wrapper_controls(wrapper: Tag, field_type: FieldType) -> tuple[Tag, ...]:
    controls = tuple(
        control
        for control in wrapper.select("input, textarea, select")
        if isinstance(control, Tag)
        and str(control.get("type", "")).strip().casefold()
        not in {"hidden", "submit", "button", "reset"}
    )
    expected = (
        tuple(
            control
            for control in controls
            if control.name == "input"
            and str(control.get("type", "")).strip().casefold() == "radio"
        )
        if field_type is FieldType.RADIO
        else controls
    )
    if not expected or (field_type is not FieldType.RADIO and len(expected) != 1):
        raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    names = {str(control.get("name", "")).strip() for control in expected}
    if (
        len(names) != 1
        or "" in names
        or any(_CONTROL_NAME_RE.fullmatch(name) is None for name in names)
        or any(control.has_attr("disabled") for control in expected)
    ):
        raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    return expected


def observe_ashby_v1_fields(html: str) -> tuple[FormFieldV1, ...]:
    """Observe only currently rendered React controls in deterministic DOM order."""

    if len((html or "").encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    soup = BeautifulSoup(html or "", "html.parser")
    forms = [
        form
        for form in soup.select(ASHBY_FORM_SELECTOR)
        if isinstance(form, Tag) and _visible(form)
    ]
    if len(forms) != 1:
        raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    wrappers = [
        wrapper
        for wrapper in forms[0].select(ASHBY_FIELD_SELECTOR)
        if isinstance(wrapper, Tag) and _rendered_wrapper(wrapper)
    ]
    if not wrappers or len(wrappers) > _MAX_FIELD_COUNT:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)

    fields: list[FormFieldV1] = []
    seen_ids: set[str] = set()
    for position, wrapper in enumerate(wrappers):
        field_id = str(wrapper.get("data-field-id", "")).strip()
        if _FIELD_ID_RE.fullmatch(field_id) is None or field_id in seen_ids:
            raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        candidate_controls = [
            control
            for control in wrapper.select("input, textarea, select")
            if isinstance(control, Tag)
            and str(control.get("type", "")).strip().casefold()
            not in {"hidden", "submit", "button", "reset"}
        ]
        if not candidate_controls:
            raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        field_type = _control_type(wrapper, candidate_controls[0])
        controls = _wrapper_controls(wrapper, field_type)
        label_node = wrapper.select_one("label, legend, [data-ashby-field-label]")
        label = label_node.get_text(" ", strip=True) if isinstance(label_node, Tag) else ""
        if not label:
            raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        control = controls[0]
        accepted = tuple(
            entry.strip() for entry in str(control.get("accept", "")).split(",") if entry.strip()
        )[:32]
        pattern = str(control.get("pattern", "")).strip() or None
        if pattern is not None and (
            len(pattern) > 128 or any(ord(character) < 32 for character in pattern)
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        fields.append(
            FormFieldV1(
                field_id=field_id,
                canonical_name=str(wrapper.get("data-canonical-name", "")).strip() or None,
                label=label,
                field_type=field_type,
                required=(
                    any(control.has_attr("required") for control in controls)
                    or str(wrapper.get("aria-required", "")).strip().casefold() == "true"
                ),
                position=position,
                options=_field_options(wrapper, control, field_type),
                constraints=FormFieldConstraintsV1(
                    min_length=_bounded_int(control.get("minlength")),
                    max_length=_bounded_int(control.get("maxlength")),
                    min_value=_bounded_float(control.get("min")),
                    max_value=_bounded_float(control.get("max")),
                    pattern=pattern,
                    accepted_file_types=accepted,
                    max_file_bytes=_bounded_int(
                        wrapper.get("data-max-file-bytes"),
                        minimum=1,
                    ),
                    multiple=control.has_attr("multiple"),
                ),
                sensitive_category=_sensitive_category(wrapper, field_type),
            )
        )
        seen_ids.add(field_id)
    return tuple(fields)


def _control_manifest(
    form: Tag,
    fields: tuple[FormFieldV1, ...],
) -> tuple[
    tuple[tuple[str, FieldType, tuple[str, ...]], ...],
    tuple[str, ...],
]:
    manifest: list[tuple[str, FieldType, tuple[str, ...]]] = []
    reviewed_names: set[str] = set()
    for field in fields:
        wrappers = [
            wrapper
            for wrapper in form.select(f'{ASHBY_FIELD_SELECTOR}[data-field-id="{field.field_id}"]')
            if isinstance(wrapper, Tag) and _rendered_wrapper(wrapper)
        ]
        if len(wrappers) != 1:
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        controls = _wrapper_controls(wrappers[0], field.field_type)
        names = tuple(dict.fromkeys(str(control.get("name", "")).strip() for control in controls))
        if any(name in reviewed_names for name in names):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        reviewed_names.update(names)
        manifest.append((field.field_id, field.field_type, names))

    system_names: list[str] = []
    for control in form.select('input[type="hidden"][name]'):
        if not isinstance(control, Tag):
            continue
        name = str(control.get("name", "")).strip()
        if (
            _CONTROL_NAME_RE.fullmatch(name) is None
            or not control.has_attr("data-ashby-system-field")
            or name in reviewed_names
            or name in system_names
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        system_names.append(name)
    all_named_controls = {
        str(control.get("name", "")).strip()
        for control in form.select("input[name], textarea[name], select[name]")
        if isinstance(control, Tag)
        and not control.has_attr("disabled")
        and str(control.get("type", "")).strip().casefold() not in {"submit", "button", "reset"}
    }
    if all_named_controls != reviewed_names.union(system_names):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return tuple(manifest), tuple(system_names)


def _final_control_is_actionable(control: Tag) -> bool:
    if (
        control.name != "button"
        or not _visible(control)
        or control.has_attr("disabled")
        or str(control.get("aria-disabled", "")).strip().casefold() == "true"
        or control.has_attr("inert")
    ):
        return False
    current: Tag | None = control
    while current is not None:
        if (
            current.has_attr("inert")
            or str(current.get("aria-disabled", "")).strip().casefold() == "true"
            or (current.name == "fieldset" and current.has_attr("disabled"))
        ):
            return False
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return True


def ashby_v1_final_request_contract(
    html: str,
    url: str,
    expected_identity: AshbyApplicationIdentity,
    fields: tuple[FormFieldV1, ...] | None = None,
) -> AshbyFinalRequestContract | None:
    """Bind one exact multipart main-frame candidate form request."""

    if len((html or "").encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        return None
    try:
        parsed = parse_ashby_candidate_url(url, expected_identity=expected_identity)
        observed = fields if fields is not None else observe_ashby_v1_fields(html)
    except (AshbyIdentityError, AshbyAdapterBlockedError, ValueError):
        return None
    soup = BeautifulSoup(html or "", "html.parser")
    forms = [
        form
        for form in soup.select(ASHBY_FORM_SELECTOR)
        if isinstance(form, Tag) and _visible(form)
    ]
    confirmations = [
        node
        for node in soup.select(ASHBY_CONFIRMATION_SELECTOR)
        if isinstance(node, Tag) and _visible(node)
    ]
    if len(forms) != 1 or confirmations:
        return None
    form = forms[0]
    controls = [
        control
        for control in form.select("button[data-ashby-submit-application]")
        if isinstance(control, Tag) and _final_control_is_actionable(control)
    ]
    if len(controls) != 1:
        return None
    submit = controls[0]
    if str(submit.get("type", "")).strip().casefold() != "submit" or any(
        submit.has_attr(attribute)
        for attribute in (
            "form",
            "formaction",
            "formmethod",
            "formenctype",
            "formtarget",
            "formnovalidate",
        )
    ):
        return None
    if str(form.get("method", "")).strip().casefold() != "post":
        return None
    if str(form.get("enctype", "")).strip().casefold() != "multipart/form-data":
        return None
    if str(form.get("target", "_self")).strip() not in {"", "_self"}:
        return None
    target = urljoin(url, str(form.get("action", "")).strip())
    try:
        if (
            target != canonical_ashby_application_url(url)
            or parse_ashby_candidate_url(target, expected_identity=expected_identity).identity
            != parsed.identity
        ):
            return None
        manifest, system_names = _control_manifest(form, observed)
    except (AshbyIdentityError, AshbyAdapterBlockedError, ValueError):
        return None
    submit_name = str(submit.get("name", "")).strip()
    submit_value = str(submit.get("value", "")).strip()
    reviewed_control_names = {name for _, _, control_names in manifest for name in control_names}
    if (
        _CONTROL_NAME_RE.fullmatch(submit_name) is None
        or not submit_value
        or len(submit_value) > 256
        or submit_name in reviewed_control_names
        or submit_name in system_names
    ):
        return None
    payload = {
        "identity": {
            "board": expected_identity.board_token,
            "posting": expected_identity.posting_id,
        },
        "target_url": target,
        "method": "POST",
        "enctype": "multipart/form-data",
        "field_controls": [
            {
                "field_id": field_id,
                "field_type": field_type.value,
                "names": names,
            }
            for field_id, field_type, names in manifest
        ],
        "system_controls": system_names,
        "submit_control": (submit_name, submit_value),
    }
    return AshbyFinalRequestContract(
        identity=expected_identity,
        target_url=target,
        method="POST",
        enctype="multipart/form-data",
        field_controls=manifest,
        system_controls=system_names,
        submit_control=(submit_name, submit_value),
        digest=_canonical_digest(payload),
    )


def ashby_v1_form_fingerprint(
    fields: tuple[FormFieldV1, ...],
    final_action_binding: str | None,
) -> str:
    """Hash exact ordered fields plus the exact final candidate request."""

    if (
        not fields
        or len(fields) > _MAX_FIELD_COUNT
        or final_action_binding is None
        or re.fullmatch(r"[0-9a-f]{64}", final_action_binding) is None
    ):
        raise ValueError("ASHBY_FORM_FINGERPRINT_INVALID")
    return _canonical_digest(
        {
            "adapter_version": ASHBY_V1_ADAPTER_VERSION,
            "selector_version": ASHBY_V1_SELECTOR_VERSION,
            "fields": [field.model_dump(mode="json") for field in fields],
            "final_action_binding": final_action_binding,
        }
    )


def _visible_confirmation_reference(soup: BeautifulSoup) -> str | None:
    try:
        references = [
            str(node.get("data-submission-id", "")).strip()
            for node in soup.select(ASHBY_CONFIRMATION_SELECTOR)
            if isinstance(node, Tag) and _visible(node)
        ]
    except Exception:
        return None
    if len(references) != 1 or _EVIDENCE_REFERENCE_RE.fullmatch(references[0]) is None:
        return None
    return references[0]


def assess_ashby_v1_snapshot(html: str, url: str = "") -> AshbyPageAssessment:
    """Classify one sanitized candidate snapshot without generic success text."""

    if len((html or "").encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        return AshbyPageAssessment(AshbyPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT)
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.stripped_strings).casefold()
    low_url = (url or "").casefold()
    if soup.select_one("[data-ashby-challenge], .g-recaptcha, .h-captcha") is not None or any(
        marker in text
        for marker in ("verify you are human", "security challenge", "complete the captcha")
    ):
        return AshbyPageAssessment(AshbyPageState.CHALLENGE, ReasonCode.CHALLENGE_DETECTED)
    if soup.select_one("[data-ashby-mfa], input[autocomplete='one-time-code']") is not None:
        return AshbyPageAssessment(AshbyPageState.MFA, ReasonCode.MFA_REQUIRED)
    if soup.select_one("[data-ashby-login], input[type='password']") is not None or any(
        marker in low_url for marker in ("/login", "/signin", "/sign-in")
    ):
        return AshbyPageAssessment(AshbyPageState.LOGIN, ReasonCode.SESSION_EXPIRED)
    if soup.select_one("[data-ashby-job-closed]") is not None:
        return AshbyPageAssessment(AshbyPageState.CLOSED, ReasonCode.JOB_CLOSED)
    if soup.select_one("[data-ashby-already-applied]") is not None:
        return AshbyPageAssessment(AshbyPageState.ALREADY_APPLIED, ReasonCode.ALREADY_APPLIED)
    if _visible_confirmation_reference(soup) is not None:
        return AshbyPageAssessment(AshbyPageState.CONFIRMATION)
    if soup.select_one(ASHBY_FORM_SELECTOR) is not None:
        return AshbyPageAssessment(AshbyPageState.FORM)
    if soup.select_one("[data-ashby-job-posting]") is not None:
        return AshbyPageAssessment(AshbyPageState.JOB)
    return AshbyPageAssessment(AshbyPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT)


def ashby_v1_validation_reason(html: str) -> ReasonCode | None:
    """Block visible validation failures and incomplete upload state."""

    soup = BeautifulSoup(html or "", "html.parser")
    if any(
        _visible(node)
        for node in soup.select("[data-ashby-validation-error], [aria-invalid='true']")
        if isinstance(node, Tag)
    ):
        return ReasonCode.REQUIRED_FIELD_UNKNOWN
    uploads = [
        node
        for node in soup.select("[data-ashby-upload-state]")
        if isinstance(node, Tag) and _visible(node)
    ]
    if any(
        str(node.get("data-ashby-upload-state", "")).strip().casefold() not in {"complete"}
        for node in uploads
    ):
        return ReasonCode.ATTACHMENT_UNVERIFIED
    return None


def ashby_v1_answer_bindings(
    fields: tuple[FormFieldV1, ...],
    decisions: tuple[AnswerDecisionV1, ...],
    *,
    selected_cv_hash: str,
) -> tuple[AshbyAnswerBinding, ...]:
    """Require every reviewed field exactly once and hash type-aware values."""

    decisions_by_id = {decision.field_id: decision for decision in decisions}
    if (
        len(decisions_by_id) != len(decisions)
        or len({field.field_id for field in fields}) != len(fields)
        or set(decisions_by_id) != {field.field_id for field in fields}
    ):
        raise ValueError("ASHBY_ANSWER_BINDING_INVALID")
    bindings: list[AshbyAnswerBinding] = []
    for field in fields:
        decision = decisions_by_id[field.field_id]
        if decision.disposition is not AnswerDisposition.RESOLVED or decision.value is None:
            raise ValueError("ASHBY_ANSWER_BINDING_INVALID")
        value = decision.value
        if field.field_type is FieldType.FILE:
            if value != VERIFIED_ATTACHMENT_SENTINEL:
                raise ValueError("ASHBY_ANSWER_BINDING_INVALID")
            material = f"file:{selected_cv_hash}"
        elif field.field_type is FieldType.MULTI_SELECT:
            if not isinstance(value, tuple):
                raise ValueError("ASHBY_ANSWER_BINDING_INVALID")
            material = "multi:" + json.dumps(
                sorted(re.sub(r"\r\n|\r|\n", "\r\n", item) for item in value),
                ensure_ascii=True,
                separators=(",", ":"),
            )
        elif field.field_type in {
            FieldType.CHECKBOX,
            FieldType.CONSENT,
            FieldType.ATTESTATION,
        }:
            if type(value) is not bool:
                raise ValueError("ASHBY_ANSWER_BINDING_INVALID")
            material = f"bool:{int(value)}"
        elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("ASHBY_ANSWER_BINDING_INVALID")
        else:
            normalized_value = (
                re.sub(r"\r\n|\r|\n", "\r\n", value) if isinstance(value, str) else value
            )
            material = f"value:{normalized_value}"
        bindings.append(
            AshbyAnswerBinding(
                field_id=field.field_id,
                field_type=field.field_type,
                value_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(bindings)


def _read_verified_resume_bytes(path: str, expected_sha256: str) -> bytes:
    with Path(path).open("rb") as handle:
        payload = handle.read(_MAX_RESUME_BYTES + 1)
    if (
        not payload
        or len(payload) > _MAX_RESUME_BYTES
        or not compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256)
    ):
        raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    return payload


def _resume_control(fields: tuple[FormFieldV1, ...]) -> tuple[FormFieldV1, str]:
    file_fields = [field for field in fields if field.field_type is FieldType.FILE]
    if len(file_fields) != 1:
        raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    field = file_fields[0]
    # The transport resolves this reviewed field ID to one exact name again.
    return field, field.field_id


def _descriptor() -> AdapterDescriptor:
    descriptor = adapter_for_platform("ashby")
    if (
        descriptor is None
        or descriptor.adapter_version != ASHBY_V1_ADAPTER_VERSION
        or descriptor.selector_version != ASHBY_V1_SELECTOR_VERSION
        or descriptor.execution_contract_version != TWO_PHASE_EXECUTION_CONTRACT_VERSION
    ):
        raise RuntimeError("ASHBY_V1_DESCRIPTOR_MISMATCH")
    return descriptor


class AshbyBrowserV1:
    """Two-phase Ashby adapter with no undocumented transport assumptions."""

    def __init__(
        self,
        *,
        browser_factory: AshbyBrowserFactory,
        answer_policy: AnswerPolicyV1 | None = None,
        descriptor: AdapterDescriptor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.descriptor = descriptor or _descriptor()
        self._browser_factory = browser_factory
        self._answer_policy = answer_policy or AnswerPolicyV1()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._prepared: dict[str, _PreparedState] = {}
        self._pending_preflight: dict[str, AshbyCandidateSession] = {}
        self._cleanup_preflight_id: ContextVar[str | None] = ContextVar(
            f"ashby-v1-cleanup-{id(self)}",
            default=None,
        )

    def can_inspect(self, job: JobData) -> bool:
        url = job.apply_url or job.source_url
        try:
            parse_ashby_candidate_url(url)
        except AshbyIdentityError:
            return False
        descriptor = adapter_for_url(url)
        return bool(
            descriptor is not None
            and descriptor.platform == self.descriptor.platform
            and descriptor.adapter_version == self.descriptor.adapter_version
            and descriptor.selector_version == self.descriptor.selector_version
        )

    @staticmethod
    def _require_snapshot_identity(
        snapshot: AshbyBrowserSnapshot,
        expected: AshbyApplicationIdentity,
    ) -> None:
        try:
            parse_ashby_candidate_url(snapshot.url, expected_identity=expected)
        except AshbyIdentityError as exc:
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc

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
        """Build a reviewed plan while React conditionals settle; never submit."""

        if (
            not self.can_inspect(job)
            or not resume_path
            or not selected_cv_id
            or application.cv_sha256 is None
            or application.profile_version is None
            or re.fullmatch(r"[0-9a-f]{64}", application.cv_sha256) is None
        ):
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            resume_bytes = _read_verified_resume_bytes(resume_path, application.cv_sha256)
            profile = UserProfile.model_validate(dict(user_profile))
        except OSError as exc:
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        except (TypeError, ValueError) as exc:
            raise AshbyAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN) from exc

        url = job.apply_url or job.source_url
        identity = parse_ashby_candidate_url(url).identity
        planner = answer_policy or self._answer_policy
        session = self._browser_factory(url)
        try:
            await session.navigate(url)
            snapshot = await session.snapshot()
            self._require_snapshot_identity(snapshot, identity)
            assessment = assess_ashby_v1_snapshot(snapshot.html, snapshot.url)
            if assessment.state is AshbyPageState.JOB:
                await session.open_application_form()
                snapshot = await session.snapshot()
                self._require_snapshot_identity(snapshot, identity)
                assessment = assess_ashby_v1_snapshot(snapshot.html, snapshot.url)
            if assessment.state is not AshbyPageState.FORM:
                raise AshbyAdapterBlockedError(assessment.reason_code or ReasonCode.SELECTOR_DRIFT)

            fields: tuple[FormFieldV1, ...] = ()
            decisions: tuple[AnswerDecisionV1, ...] = ()
            filled_ids: set[str] = set()
            attachment: AshbyAttachmentProof | None = None
            audit_identity: tuple[str, str, str, str] | None = None
            blockers: tuple[ReasonCode, ...] = ()
            request_contract: AshbyFinalRequestContract | None = None
            locale = snapshot.locale

            for _pass in range(_MAX_REACT_PASSES):
                validation = ashby_v1_validation_reason(snapshot.html)
                if validation is not None and validation is not ReasonCode.ATTACHMENT_UNVERIFIED:
                    raise AshbyAdapterBlockedError(validation)
                observed = observe_ashby_v1_fields(snapshot.html)
                previous_by_id = {field.field_id: field for field in fields}
                observed_by_id = {field.field_id: field for field in observed}
                if any(
                    field_id not in observed_by_id or observed_by_id[field_id] != prior
                    for field_id, prior in previous_by_id.items()
                ):
                    raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                new_fields = tuple(
                    field for field in observed if field.field_id not in previous_by_id
                )
                if not new_fields:
                    fields = observed
                    request_contract = ashby_v1_final_request_contract(
                        snapshot.html,
                        snapshot.url,
                        identity,
                        fields,
                    )
                    if request_contract is None:
                        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    break

                # Re-numbering is deterministic after newly rendered conditional controls.
                fields = tuple(
                    field.model_copy(update={"position": index})
                    for index, field in enumerate(observed)
                )
                file_fields = tuple(
                    field for field in new_fields if field.field_type is FieldType.FILE
                )
                if file_fields:
                    if len(file_fields) != 1 or attachment is not None:
                        raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                    file_field = file_fields[0]
                    form = BeautifulSoup(snapshot.html, "html.parser").select_one(
                        f'{ASHBY_FIELD_SELECTOR}[data-field-id="{file_field.field_id}"]'
                    )
                    if not isinstance(form, Tag):
                        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    controls = _wrapper_controls(form, file_field.field_type)
                    control_name = str(controls[0].get("name", "")).strip()
                    attachment = await session.ensure_resume_attachment(
                        field_id=file_field.field_id,
                        control_name=control_name,
                        resume_bytes=resume_bytes,
                        cv_id=selected_cv_id,
                        expected_sha256=application.cv_sha256,
                    )
                    if not attachment.matches(
                        field_id=file_field.field_id,
                        control_name=control_name,
                        cv_id=selected_cv_id,
                        cv_sha256=application.cv_sha256,
                    ):
                        raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

                provisional_fingerprint = _canonical_digest(
                    {
                        "adapter": self.descriptor.adapter_version,
                        "selector": self.descriptor.selector_version,
                        "fields": [field.model_dump(mode="json") for field in fields],
                    }
                )
                context = AnswerPolicyContext(
                    profile=profile,
                    profile_version=application.profile_version,
                    selected_cv_id=selected_cv_id,
                    selected_cv_hash=application.cv_sha256,
                    attached_cv_id=attachment.cv_id if attachment else None,
                    attached_cv_hash=attachment.cv_sha256 if attachment else None,
                    attachment_verified=bool(attachment and attachment.upload_complete),
                    adapter_name=self.descriptor.platform,
                    adapter_version=self.descriptor.adapter_version,
                    selector_version=self.descriptor.selector_version,
                    form_fingerprint=provisional_fingerprint,
                    locale=snapshot.locale,
                )
                policy = await planner.plan_fields(new_fields, context)
                if {decision.field_id for decision in policy.decisions} != {
                    field.field_id for field in new_fields
                }:
                    raise AshbyAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
                candidate_audit = (
                    policy.prompt_version,
                    policy.model_provider,
                    policy.model_name,
                    policy.model_digest,
                )
                if any(value is not None for value in candidate_audit):
                    if not all(isinstance(value, str) for value in candidate_audit):
                        raise AshbyAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
                    normalized_audit = tuple(str(value) for value in candidate_audit)
                    if audit_identity is not None and normalized_audit != audit_identity:
                        raise AshbyAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
                    audit_identity = normalized_audit  # type: ignore[assignment]
                decisions = (*decisions, *policy.decisions)
                blockers = tuple(dict.fromkeys((*blockers, *policy.blockers)))
                if blockers:
                    break
                if filled_ids.intersection(decision.field_id for decision in policy.decisions):
                    raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                await session.fill_once(policy.decisions)
                filled_ids.update(decision.field_id for decision in policy.decisions)
                await session.settle_react()
                snapshot = await session.snapshot()
                self._require_snapshot_identity(snapshot, identity)
                locale = snapshot.locale
                assessment = assess_ashby_v1_snapshot(snapshot.html, snapshot.url)
                if assessment.state is not AshbyPageState.FORM:
                    raise AshbyAdapterBlockedError(
                        assessment.reason_code or ReasonCode.FORM_CHANGED
                    )
            else:
                raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

            if request_contract is None:
                blockers = tuple(dict.fromkeys((*blockers, ReasonCode.FORM_PLAN_INCOMPLETE)))
                request_binding = _canonical_digest(
                    {"partial_fields": [field.field_id for field in fields]}
                )
            else:
                request_binding = request_contract.digest
            if attachment is None:
                blockers = tuple(dict.fromkeys((*blockers, ReasonCode.ATTACHMENT_UNVERIFIED)))
            attachment_verified = bool(
                attachment
                and attachment.upload_complete
                and compare_digest(attachment.cv_sha256, application.cv_sha256)
                and attachment.cv_id == selected_cv_id
            )
            fingerprint = ashby_v1_form_fingerprint(fields, request_binding)
            now = self._clock()
            context_version = (
                context.policy_version if "context" in locals() else "answer-policy-v1"
            )
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
                attached_cv_id=attachment.cv_id if attachment else selected_cv_id,
                attached_cv_hash=(attachment.cv_sha256 if attachment else application.cv_sha256),
                attachment_verified=attachment_verified,
                profile_version=application.profile_version,
                session_verified_at=now,
                created_at=now,
                expires_at=now + timedelta(minutes=30),
                fields=fields,
                decisions=decisions,
                blockers=blockers,
                locale=locale,
                answer_policy_version=context_version,
                llm_prompt_version=audit_identity[0] if audit_identity else None,
                llm_model_provider=audit_identity[1] if audit_identity else None,
                llm_model_name=audit_identity[2] if audit_identity else None,
                llm_model_digest=audit_identity[3] if audit_identity else None,
            )
        finally:
            await session.close()

    async def preflight(
        self,
        *,
        plan: FormPlanV1,
        permit: FinalSubmitPermit,
        context: AdapterPreflightContext | None = None,
    ) -> PreflightOutcome:
        """Reconstruct the exact reviewed form; final execution remains gated."""

        now = self._clock()
        try:
            qualified = (
                self.descriptor.allows_final_execution
                and self.descriptor.qualifies_form_fingerprint(plan.form_fingerprint)
                and plan.adapter_name == self.descriptor.platform
                and plan.adapter_version == self.descriptor.adapter_version
                and plan.selector_version == self.descriptor.selector_version
                and plan.ready_for_permit_at(now)
                and not permit.is_expired(now)
                and permit.binds(plan)
            )
        except ValueError:
            qualified = False
        if not qualified:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        if (
            context is None
            or context.selected_cv_id != plan.selected_cv_id
            or not compare_digest(context.selected_cv_hash, plan.selected_cv_hash)
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            resume_bytes = _read_verified_resume_bytes(
                context.resume_path,
                plan.selected_cv_hash,
            )
            parsed = parse_ashby_candidate_url(context.normalized_job_url)
            session = self._browser_factory(context.normalized_job_url)
        except (OSError, ValueError, AshbyIdentityError, AshbyAdapterBlockedError):
            return NeedsReviewOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        cleanup_id = hashlib.sha256(token_bytes(32)).hexdigest()
        self._pending_preflight[cleanup_id] = session
        self._cleanup_preflight_id.set(cleanup_id)
        try:
            await session.navigate(context.normalized_job_url)
            snapshot = await session.snapshot()
            self._require_snapshot_identity(snapshot, parsed.identity)
            assessment = assess_ashby_v1_snapshot(snapshot.html, snapshot.url)
            if assessment.state is AshbyPageState.JOB:
                await session.open_application_form()
                snapshot = await session.snapshot()
                self._require_snapshot_identity(snapshot, parsed.identity)
                assessment = assess_ashby_v1_snapshot(snapshot.html, snapshot.url)
            if assessment.state is AshbyPageState.ALREADY_APPLIED:
                return AlreadyAppliedOutcome()
            if assessment.state is not AshbyPageState.FORM:
                return _typed_pre_request_block(assessment.reason_code or ReasonCode.FORM_CHANGED)
            observed = observe_ashby_v1_fields(snapshot.html)
            if observed != plan.fields:
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            decisions_by_id = {decision.field_id: decision for decision in plan.decisions}
            if (
                len(decisions_by_id) != len(plan.decisions)
                or set(decisions_by_id) != {field.field_id for field in observed}
                or any(
                    decision.disposition is not AnswerDisposition.RESOLVED
                    for decision in plan.decisions
                )
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN)
            file_field, _ = _resume_control(observed)
            soup = BeautifulSoup(snapshot.html, "html.parser")
            wrapper = soup.select_one(
                f'{ASHBY_FIELD_SELECTOR}[data-field-id="{file_field.field_id}"]'
            )
            if not isinstance(wrapper, Tag):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            control_name = str(
                _wrapper_controls(wrapper, FieldType.FILE)[0].get("name", "")
            ).strip()
            attachment = await session.ensure_resume_attachment(
                field_id=file_field.field_id,
                control_name=control_name,
                resume_bytes=resume_bytes,
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            if not attachment.matches(
                field_id=file_field.field_id,
                control_name=control_name,
                cv_id=plan.selected_cv_id,
                cv_sha256=plan.selected_cv_hash,
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            await session.fill_once(plan.decisions)
            await session.settle_react()
            snapshot = await session.snapshot()
            self._require_snapshot_identity(snapshot, parsed.identity)
            validation_reason = ashby_v1_validation_reason(snapshot.html)
            if validation_reason is not None:
                return NeedsReviewOutcome(reason_code=validation_reason)
            observed_after_fill = observe_ashby_v1_fields(snapshot.html)
            request_contract = ashby_v1_final_request_contract(
                snapshot.html,
                snapshot.url,
                parsed.identity,
                observed_after_fill,
            )
            verified = await session.verify_resume_attachment(
                field_id=file_field.field_id,
                control_name=control_name,
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            if (
                observed_after_fill != plan.fields
                or request_contract is None
                or not compare_digest(
                    ashby_v1_form_fingerprint(
                        observed_after_fill,
                        request_contract.digest,
                    ),
                    plan.form_fingerprint,
                )
                or not verified.matches(
                    field_id=file_field.field_id,
                    control_name=control_name,
                    cv_id=plan.selected_cv_id,
                    cv_sha256=plan.selected_cv_hash,
                )
                or verified.receipt_sha256 != attachment.receipt_sha256
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
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
                identity=parsed.identity,
                fields=observed_after_fill,
                attachment=verified,
                request_contract=request_contract,
                pre_action_html=snapshot.html,
                pre_action_digest=hashlib.sha256(snapshot.html.encode("utf-8")).hexdigest(),
            )
            self._pending_preflight.pop(cleanup_id, None)
            return action
        except AshbyAdapterBlockedError as exc:
            return _typed_pre_request_block(exc.reason_code)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)

    async def commit(
        self,
        *,
        action: PreparedFinalActionV1,
        permit: FinalSubmitPermit,
    ) -> CommitOutcome:
        """Release one prepared candidate request and require fresh exact evidence."""

        if not self.descriptor.allows_final_execution:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        state = self._prepared.get(action.action_nonce)
        now = self._clock()
        if state is None:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        if state.released or state.commit_claimed:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_REPLAYED)
        try:
            bound = (
                action.binds(state.plan, permit, at=now)
                and permit == state.permit
                and state.plan.ready_for_permit_at(now)
            )
        except ValueError:
            bound = False
        if not bound:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)
        # Claim the one prepared action before the first browser await. Two
        # concurrent callers can then never cross the external boundary.
        state.commit_claimed = True

        try:
            file_field, _ = _resume_control(state.fields)
            control_name = next(
                names[0]
                for field_id, field_type, names in state.request_contract.field_controls
                if field_id == file_field.field_id and field_type is FieldType.FILE
            )
        except AshbyAdapterBlockedError as exc:
            return _typed_pre_request_block(exc.reason_code)
        except (IndexError, StopIteration, TypeError):
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)
        try:
            attachment = await state.session.verify_resume_attachment(
                field_id=file_field.field_id,
                control_name=control_name,
                cv_id=state.plan.selected_cv_id,
                expected_sha256=state.plan.selected_cv_hash,
            )
            snapshot = await state.session.snapshot()
        except AshbyAdapterBlockedError as exc:
            return _typed_pre_request_block(exc.reason_code)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)
        if (
            not attachment.matches(
                field_id=file_field.field_id,
                control_name=control_name,
                cv_id=state.plan.selected_cv_id,
                cv_sha256=state.plan.selected_cv_hash,
            )
            or attachment.receipt_sha256 != state.attachment.receipt_sha256
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            self._require_snapshot_identity(snapshot, state.identity)
            observed = observe_ashby_v1_fields(snapshot.html)
            request_contract = ashby_v1_final_request_contract(
                snapshot.html,
                snapshot.url,
                state.identity,
                observed,
            )
        except AshbyAdapterBlockedError as exc:
            return _typed_pre_request_block(exc.reason_code)
        if (
            request_contract is None
            or observed != state.fields
            or not compare_digest(request_contract.digest, state.request_contract.digest)
            or not compare_digest(
                hashlib.sha256(snapshot.html.encode("utf-8")).hexdigest(),
                state.pre_action_digest,
            )
            or not compare_digest(
                ashby_v1_form_fingerprint(observed, request_contract.digest),
                state.plan.form_fingerprint,
            )
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
        try:
            expectation = AshbyFinalCommitExpectation(
                identity=state.identity,
                form_fingerprint=state.plan.form_fingerprint,
                observed_fields=state.fields,
                answer_bindings=ashby_v1_answer_bindings(
                    state.fields,
                    state.plan.decisions,
                    selected_cv_hash=state.plan.selected_cv_hash,
                ),
                selected_cv_id=state.plan.selected_cv_id,
                selected_cv_hash=state.plan.selected_cv_hash,
                attachment_receipt_sha256=attachment.receipt_sha256 or "",
                pre_action_digest=state.pre_action_digest,
                request_contract=state.request_contract,
            )
        except ValueError:
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)

        final_action_at = self._clock()
        try:
            if not action.binds(state.plan, permit, at=final_action_at):
                return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)
        except ValueError:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)
        state.released = True
        try:
            receipt = await state.session.commit_final_action(expectation)
        except AshbyAdapterBlockedError as exc:
            if exc.reason_code in _ASHBY_REVIEW_REASONS:
                return NeedsReviewOutcome(reason_code=exc.reason_code)
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        except AshbyFinalActionAmbiguousError:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if not compare_digest(
            receipt.request_contract_digest,
            state.request_contract.digest,
        ):
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        # The exact request may have left. Every later error is indeterminate.
        try:
            post_action = await state.session.snapshot()
            self._require_snapshot_identity(post_action, state.identity)
            post_soup = BeautifulSoup(post_action.html, "html.parser")
            reference = _visible_confirmation_reference(post_soup)
            stable_reference = await state.session.confirmation_reference()
            assessment = assess_ashby_v1_snapshot(post_action.html, post_action.url)
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if assessment.state is AshbyPageState.ALREADY_APPLIED:
            return AlreadyAppliedOutcome()
        if assessment.state is AshbyPageState.CHALLENGE:
            return UnknownOutcome(reason_code=ReasonCode.CHALLENGE_DETECTED)
        if assessment.state is AshbyPageState.LOGIN:
            return UnknownOutcome(reason_code=ReasonCode.SESSION_EXPIRED)
        if (
            assessment.state is not AshbyPageState.CONFIRMATION
            or reference is None
            or stable_reference is None
            or not compare_digest(reference, stable_reference)
        ):
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        try:
            redacted_reference = hashlib.sha256(reference.encode("utf-8")).hexdigest()
            evidence_expectation = SubmissionEvidenceExpectation(
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
                        rule_id="ashby-v1:visible-confirmation",
                        channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                        visible_selector=ASHBY_CONFIRMATION_SELECTOR,
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
                rule_id="ashby-v1:visible-confirmation",
                channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                evidence_reference=redacted_reference,
                observed_at=self._clock(),
                observed_after_final_action=True,
                was_present_before_action=bool(
                    BeautifulSoup(state.pre_action_html, "html.parser").select(
                        ASHBY_CONFIRMATION_SELECTOR
                    )
                ),
                visible_selector=ASHBY_CONFIRMATION_SELECTOR,
                computed_visible=reference is not None,
            )
            confirmation = verify_submission_evidence(
                evidence_expectation,
                observation,
                pre_action_html=state.pre_action_html,
                post_action_html=post_action.html,
            )
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if not confirmation.confirmed or confirmation.evidence is None:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        return ConfirmedSubmittedOutcome(evidence=confirmation.evidence)

    async def cleanup_prepared_action(
        self,
        *,
        action: PreparedFinalActionV1 | None,
    ) -> None:
        sessions: list[AshbyCandidateSession] = []
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


def register_ashby_browser_v1(
    registry: SubmitterRegistry,
    *,
    browser_factory: AshbyBrowserFactory,
    answer_policy: AnswerPolicyV1 | None = None,
    descriptor: AdapterDescriptor | None = None,
) -> AshbyBrowserV1:
    """Register fixture inventory without authorizing arbitrary employer URLs."""

    adapter = AshbyBrowserV1(
        browser_factory=browser_factory,
        answer_policy=answer_policy,
        descriptor=descriptor,
    )
    registry.register_two_phase(adapter)
    return adapter
