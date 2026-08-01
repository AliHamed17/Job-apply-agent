"""Fixture-oriented, two-phase Greenhouse candidate-browser adapter.

This module contains no Playwright dependency and performs no network request.
The private runner supplies a narrow :class:`GreenhouseCandidateSession` that
owns one ephemeral candidate page.  Inspection may fill reversible controls
and upload the routed CV, but it never performs the final external action.

The checked-in platform descriptor is upgraded by the release integration
layer.  Until that descriptor is fixture-qualified under this exact adapter
and selector identity, the default constructor fails closed.  Even after
fixture qualification, an empty live form scope makes commit unreachable from
the production registry.
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
from urllib.parse import parse_qs, urlsplit
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
    field_is_reviewed_cv_attachment,
    field_requires_operator_review,
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
from submitters.greenhouse_identity import (
    GreenhouseApplicationIdentity,
    GreenhouseIdentityError,
    parse_greenhouse_candidate_url,
)
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    AdapterDescriptor,
    adapter_for_platform,
    adapter_for_url,
)

GREENHOUSE_V1_ADAPTER_VERSION = "1.0.0"
GREENHOUSE_V1_SELECTOR_VERSION = "greenhouse-candidate-v9"
GREENHOUSE_V1_NATIVE_TRANSPORT = "native-multipart-form-post-v1"
GREENHOUSE_CONFIRMATION_SELECTOR = '[data-qa="application-confirmation"], #application_confirmation'
_MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_FIELD_COUNT = 200
_MAX_STABILIZATION_ROUNDS = 8
_MAX_RESUME_BYTES = 20 * 1024 * 1024
_FIELD_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,500}$")
_FORM_SELECTORS = (
    "form#application_form",
    'form[data-qa="application-form"]',
    "form[data-greenhouse-application]",
)
_FIELD_WRAPPER_SELECTOR = (
    "[data-gh-field][data-field-id], "
    '[data-qa="application-field"][data-field-id], '
    ".field[data-field-id], "
    "fieldset[data-field-id]"
)
_FINAL_ACTION_SELECTORS = (
    'button#submit_app[type="submit"]',
    'button[data-qa="submit-application"][type="submit"]',
    'form[data-greenhouse-application] button[type="submit"]',
)


class GreenhouseVariant(StrEnum):
    """Bounded Greenhouse candidate-form variants qualified by fixtures."""

    HOSTED = "hosted"
    EMBEDDED = "embedded"
    JOB_ID = "job_id"


class GreenhousePageState(StrEnum):
    """Candidate-flow states recognized by the v1 selector contract."""

    JOB = "job"
    FORM = "form"
    REVIEW = "review"
    LOGIN = "login"
    MFA = "mfa"
    CHALLENGE = "challenge"
    CLOSED = "closed"
    ALREADY_APPLIED = "already_applied"
    CONFIRMATION = "confirmation"
    SELECTOR_DRIFT = "selector_drift"


@dataclass(frozen=True, slots=True)
class GreenhousePageAssessment:
    state: GreenhousePageState
    reason_code: ReasonCode | None = None


@dataclass(frozen=True, slots=True, repr=False)
class GreenhouseBrowserSnapshot:
    """Ephemeral candidate DOM; callers must never persist this object."""

    html: str
    url: str = ""
    locale: str = "en"

    def __post_init__(self) -> None:
        if len(self.html.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("GREENHOUSE_SNAPSHOT_TOO_LARGE")


@dataclass(frozen=True, slots=True, repr=False)
class GreenhouseAttachmentProof:
    """Session-local proof of a fresh, browser-observed CV upload."""

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


@dataclass(frozen=True, slots=True, repr=False)
class GreenhouseReviewedAnswerBinding:
    """One value-exact reviewed decision without its private raw answer."""

    field_id: str
    field_type: FieldType
    value_sha256: str
    successful_entry_count: int

    def __post_init__(self) -> None:
        if (
            not _FIELD_ID.fullmatch(self.field_id)
            or not isinstance(self.field_type, FieldType)
            or re.fullmatch(r"[0-9a-f]{64}", self.value_sha256) is None
            or self.successful_entry_count < 0
            or self.successful_entry_count > 32
            or (
                self.field_type
                not in {
                    FieldType.CHECKBOX,
                    FieldType.CONSENT,
                    FieldType.ATTESTATION,
                    FieldType.MULTI_SELECT,
                }
                and self.successful_entry_count == 0
            )
        ):
            raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class GreenhouseAnswerBinding:
    """A reviewed answer bound to one exact successful-control name."""

    reviewed: GreenhouseReviewedAnswerBinding
    control_name_sha256: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.control_name_sha256) is None:
            raise ValueError("GREENHOUSE_ANSWER_BINDING_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class GreenhouseSubmitterBinding:
    """Redacted name/value binding for the retained final submitter."""

    control_name_sha256: str
    value_sha256: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.control_name_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.value_sha256) is None
        ):
            raise ValueError("GREENHOUSE_SUBMITTER_BINDING_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class GreenhousePayloadBinding:
    """Transport-derived redacted binding for one reviewed native payload."""

    payload_commitment: str
    answer_bindings: tuple[GreenhouseAnswerBinding, ...]
    resume_control_name_sha256: str
    submitter_binding: GreenhouseSubmitterBinding | None

    def __post_init__(self) -> None:
        file_bindings = tuple(
            binding
            for binding in self.answer_bindings
            if binding.reviewed.field_type is FieldType.FILE
        )
        field_ids = tuple(binding.reviewed.field_id for binding in self.answer_bindings)
        names = tuple(binding.control_name_sha256 for binding in self.answer_bindings)
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.payload_commitment) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.resume_control_name_sha256) is None
            or not self.answer_bindings
            or len(file_bindings) != 1
            or file_bindings[0].control_name_sha256 != self.resume_control_name_sha256
            or len(field_ids) != len(set(field_ids))
            or len(names) != len(set(names))
            or (
                self.submitter_binding is not None
                and self.submitter_binding.control_name_sha256 in set(names)
            )
        ):
            raise ValueError("GREENHOUSE_PAYLOAD_BINDING_INVALID")


def _greenhouse_answer_material(
    field: FormFieldV1,
    value: object,
    *,
    selected_cv_hash: str,
) -> tuple[str, int]:
    """Return private canonical material and its successful-entry count."""

    if field.field_type in {
        FieldType.CHECKBOX,
        FieldType.CONSENT,
        FieldType.ATTESTATION,
    }:
        if type(value) is not bool:
            raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
        return json.dumps(["b", value], separators=(",", ":")), 1 if value else 0
    if field.field_type is FieldType.MULTI_SELECT:
        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
        normalized_values = sorted(
            item.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n") for item in value
        )
        return (
            json.dumps(["m", normalized_values], ensure_ascii=False, separators=(",", ":")),
            len(normalized_values),
        )
    if field.field_type is FieldType.FILE:
        if (
            value != VERIFIED_ATTACHMENT_SENTINEL
            or not field_is_reviewed_cv_attachment(field)
            or re.fullmatch(r"[0-9a-f]{64}", selected_cv_hash) is None
        ):
            raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
        return json.dumps(["f", selected_cv_hash], separators=(",", ":")), 1
    if (
        field.field_type
        not in {
            FieldType.TEXT,
            FieldType.TEXTAREA,
            FieldType.SELECT,
            FieldType.RADIO,
            FieldType.DATE,
            FieldType.NUMBER,
            FieldType.EMAIL,
            FieldType.PHONE,
            FieldType.URL,
        }
        or isinstance(value, bool)
        or not isinstance(value, (str, int, float))
    ):
        raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
    normalized_scalar = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return json.dumps(["s", normalized_scalar], ensure_ascii=False, separators=(",", ":")), 1


def _greenhouse_reviewed_blank_material(field: FormFieldV1) -> tuple[str, int]:
    """Bind a reviewed optional abstention to its exact empty control state."""

    if field.required or field_requires_operator_review(field):
        raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
    if field.field_type in {
        FieldType.TEXT,
        FieldType.TEXTAREA,
        FieldType.SELECT,
        FieldType.DATE,
        FieldType.NUMBER,
        FieldType.EMAIL,
        FieldType.PHONE,
        FieldType.URL,
    }:
        return json.dumps(["s", ""], separators=(",", ":")), 1
    if field.field_type is FieldType.MULTI_SELECT:
        return json.dumps(["m", []], separators=(",", ":")), 0
    if field.field_type is FieldType.CHECKBOX:
        return json.dumps(["b", False], separators=(",", ":")), 0
    raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")


def greenhouse_v1_reviewed_answer_bindings(
    fields: tuple[FormFieldV1, ...],
    decisions: tuple[AnswerDecisionV1, ...],
    *,
    selected_cv_hash: str,
) -> tuple[GreenhouseReviewedAnswerBinding, ...]:
    """Bind every exact reviewed Greenhouse field without retaining raw answers."""

    by_id = {decision.field_id: decision for decision in decisions}
    field_ids = tuple(field.field_id for field in fields)
    if (
        not fields
        or len(by_id) != len(decisions)
        or len(set(field_ids)) != len(field_ids)
        or set(by_id) != set(field_ids)
        or re.fullmatch(r"[0-9a-f]{64}", selected_cv_hash) is None
    ):
        raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
    bindings: list[GreenhouseReviewedAnswerBinding] = []
    file_count = 0
    for field in fields:
        decision = by_id[field.field_id]
        if decision.disposition is AnswerDisposition.RESOLVED and decision.value is not None:
            material, entry_count = _greenhouse_answer_material(
                field,
                decision.value,
                selected_cv_hash=selected_cv_hash,
            )
        elif decision.disposition is AnswerDisposition.ABSTAINED:
            material, entry_count = _greenhouse_reviewed_blank_material(field)
        else:
            raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
        if field.field_type is FieldType.FILE:
            file_count += 1
        bindings.append(
            GreenhouseReviewedAnswerBinding(
                field_id=field.field_id,
                field_type=field.field_type,
                value_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
                successful_entry_count=entry_count,
            )
        )
    if file_count != 1:
        raise ValueError("GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID")
    return tuple(bindings)


@dataclass(frozen=True, slots=True, repr=False)
class GreenhouseAtomicCommitExpectation:
    """Exact, private commit capability passed to one candidate session."""

    expected_hostname: str
    expected_identity: GreenhouseApplicationIdentity
    fields: tuple[FormFieldV1, ...]
    variant: GreenhouseVariant
    form_fingerprint: str
    action_binding: str
    dom_commitment: str
    resolved_action_url: str
    native_transport: str
    payload_commitment: str
    answer_bindings: tuple[GreenhouseAnswerBinding, ...]
    resume_control_name_sha256: str
    submitter_binding: GreenhouseSubmitterBinding | None
    cv_id: str
    cv_sha256: str
    cv_receipt_sha256: str

    def __post_init__(self) -> None:
        digests = (
            self.form_fingerprint,
            self.action_binding,
            self.dom_commitment,
            self.payload_commitment,
            self.resume_control_name_sha256,
            self.cv_sha256,
            self.cv_receipt_sha256,
        )
        answer_shape = tuple(
            (binding.reviewed.field_id, binding.reviewed.field_type)
            for binding in self.answer_bindings
        )
        try:
            GreenhousePayloadBinding(
                payload_commitment=self.payload_commitment,
                answer_bindings=self.answer_bindings,
                resume_control_name_sha256=self.resume_control_name_sha256,
                submitter_binding=self.submitter_binding,
            )
        except ValueError as exc:
            raise ValueError("GREENHOUSE_ATOMIC_COMMIT_EXPECTATION_INVALID") from exc
        if (
            not self.expected_hostname
            or not self.fields
            or not self.cv_id
            or not self.resolved_action_url
            or self.native_transport != GREENHOUSE_V1_NATIVE_TRANSPORT
            or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests)
            or answer_shape != tuple((field.field_id, field.field_type) for field in self.fields)
        ):
            raise ValueError("GREENHOUSE_ATOMIC_COMMIT_EXPECTATION_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class GreenhouseAtomicCommitObservation:
    """Bounded result returned by the single-use browser commit primitive."""

    expected_hostname: str
    expected_identity: GreenhouseApplicationIdentity
    fields: tuple[FormFieldV1, ...]
    variant: GreenhouseVariant
    form_fingerprint: str
    action_binding: str
    dom_commitment: str
    resolved_action_url: str
    native_transport: str
    payload_commitment: str
    answer_bindings: tuple[GreenhouseAnswerBinding, ...]
    resume_control_name_sha256: str
    submitter_binding: GreenhouseSubmitterBinding | None
    cv_id: str
    cv_sha256: str
    cv_receipt_sha256: str
    final_action_invoked: bool
    request_may_have_left: bool
    outbound_request_sha256: str | None = None
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        digests = (
            self.form_fingerprint,
            self.action_binding,
            self.dom_commitment,
            self.payload_commitment,
            self.resume_control_name_sha256,
            self.cv_sha256,
            self.cv_receipt_sha256,
        )
        answer_shape = tuple(
            (binding.reviewed.field_id, binding.reviewed.field_type)
            for binding in self.answer_bindings
        )
        try:
            GreenhousePayloadBinding(
                payload_commitment=self.payload_commitment,
                answer_bindings=self.answer_bindings,
                resume_control_name_sha256=self.resume_control_name_sha256,
                submitter_binding=self.submitter_binding,
            )
        except ValueError as exc:
            raise ValueError("GREENHOUSE_ATOMIC_COMMIT_OBSERVATION_INVALID") from exc
        if (
            not self.expected_hostname
            or not self.fields
            or not self.cv_id
            or not self.resolved_action_url
            or self.native_transport != GREENHOUSE_V1_NATIVE_TRANSPORT
            or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests)
            or answer_shape != tuple((field.field_id, field.field_type) for field in self.fields)
            or (
                self.outbound_request_sha256 is not None
                and re.fullmatch(r"[0-9a-f]{64}", self.outbound_request_sha256) is None
            )
            or (self.request_may_have_left and not self.final_action_invoked)
            or (self.request_may_have_left and self.outbound_request_sha256 is None)
            or (not self.request_may_have_left and self.outbound_request_sha256 is not None)
            or (not self.request_may_have_left and self.reason_code is None)
        ):
            raise ValueError("GREENHOUSE_ATOMIC_COMMIT_OBSERVATION_INVALID")

    def binds(self, expectation: GreenhouseAtomicCommitExpectation) -> bool:
        """Require every revalidated browser commitment to match preflight."""

        return (
            self.expected_hostname == expectation.expected_hostname
            and self.expected_identity == expectation.expected_identity
            and self.fields == expectation.fields
            and self.variant is expectation.variant
            and self.form_fingerprint == expectation.form_fingerprint
            and self.action_binding == expectation.action_binding
            and self.dom_commitment == expectation.dom_commitment
            and self.resolved_action_url == expectation.resolved_action_url
            and self.native_transport == expectation.native_transport
            and self.payload_commitment == expectation.payload_commitment
            and self.answer_bindings == expectation.answer_bindings
            and self.resume_control_name_sha256 == expectation.resume_control_name_sha256
            and self.submitter_binding == expectation.submitter_binding
            and self.cv_id == expectation.cv_id
            and self.cv_sha256 == expectation.cv_sha256
            and self.cv_receipt_sha256 == expectation.cv_receipt_sha256
        )


_ATOMIC_PRE_REQUEST_REVIEW_REASONS = frozenset(
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
_ATOMIC_PRE_REQUEST_FAILURE_REASONS = frozenset(
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
_ATOMIC_UNKNOWN_REASONS = frozenset(
    {
        ReasonCode.FINAL_ACTION_UNCONFIRMED,
        ReasonCode.STALE_INDETERMINATE,
        ReasonCode.SESSION_EXPIRED,
        ReasonCode.CHALLENGE_DETECTED,
        ReasonCode.NETWORK_ERROR,
        ReasonCode.INTERNAL_ERROR,
        ReasonCode.EVIDENCE_INVALID,
    }
)


def _atomic_observation_outcome(
    observation: object,
    expectation: GreenhouseAtomicCommitExpectation,
) -> CommitOutcome | None:
    """Map every atomic stage/reason combination to a valid typed outcome.

    ``None`` means the exact outbound request may have left and post-action
    evidence verification must continue. Once the intrinsic final action was
    invoked, lack of a gate-observed request remains retry-unsafe and unknown.
    """

    if not isinstance(observation, GreenhouseAtomicCommitObservation):
        return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
    try:
        bindings_match = observation.binds(expectation)
    except Exception:
        return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

    if observation.request_may_have_left:
        if not bindings_match:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if observation.reason_code is None:
            return None
        unknown_reason_code = (
            observation.reason_code
            if observation.reason_code in _ATOMIC_UNKNOWN_REASONS
            else ReasonCode.FINAL_ACTION_UNCONFIRMED
        )
        return UnknownOutcome(reason_code=unknown_reason_code)

    if observation.final_action_invoked:
        return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
    pre_request_reason = observation.reason_code
    if pre_request_reason is None:
        return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
    if pre_request_reason in _ATOMIC_PRE_REQUEST_REVIEW_REASONS:
        return NeedsReviewOutcome(reason_code=pre_request_reason)
    if pre_request_reason in _ATOMIC_PRE_REQUEST_FAILURE_REASONS:
        return FailedBeforeCommitOutcome(reason_code=pre_request_reason)
    return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)


class GreenhouseCandidateSession(Protocol):
    """One public Greenhouse candidate page owned by a private runner."""

    async def navigate(self, url: str) -> None:
        """Navigate to the reviewed canonical Greenhouse candidate URL."""

    async def open_candidate_form(self) -> None:
        """Open the public form using only a reversible Apply control."""

    async def snapshot(self) -> GreenhouseBrowserSnapshot:
        """Return an ephemeral DOM observation."""

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> GreenhouseAttachmentProof:
        """Upload immutable bytes and require a fresh completion receipt."""

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> GreenhouseAttachmentProof:
        """Recheck the exact session-local receipt immediately before commit."""

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        """Fill only resolved decisions for the currently observed form."""

    async def settle_reversible_form(self) -> None:
        """Wait for bounded conditional controls without clicking submit."""

    async def final_action_ready(self) -> bool:
        """Return whether exactly one visible, enabled submit control exists."""

    async def observed_form_action_binding(self) -> str | None:
        """Return a redacted binding for the structurally observed form action."""

    async def final_action_binding(self) -> str | None:
        """Return a redacted binding for the exact validated form action."""

    async def final_action_url(self) -> str | None:
        """Return the exact private candidate POST target for this session."""

    async def commit_dom_commitment(self) -> str | None:
        """Hash the exact live candidate form without returning its content."""

    async def commit_payload_binding(
        self,
        *,
        reviewed_answers: tuple[GreenhouseReviewedAnswerBinding, ...],
        expected_cv_sha256: str,
    ) -> GreenhousePayloadBinding | None:
        """Bind reviewed answers to exact controls and the native payload."""

    async def atomic_commit(
        self,
        expectation: GreenhouseAtomicCommitExpectation,
    ) -> GreenhouseAtomicCommitObservation:
        """Revalidate every binding and cross the outbound POST boundary once."""

    async def confirmation_reference(self) -> str | None:
        """Return a redacted digest of one stable, visible confirmation node."""

    async def close(self) -> None:
        """Release the page and browser on the owning event loop."""


GreenhouseBrowserFactory = Callable[[str], GreenhouseCandidateSession]


class GreenhouseAdapterBlockedError(RuntimeError):
    """Fail-closed adapter error containing only a bounded reason code."""

    def __init__(self, reason_code: ReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


@dataclass(slots=True)
class _PreparedState:
    session: GreenhouseCandidateSession
    plan: FormPlanV1
    permit: FinalSubmitPermit
    pre_action_html: str
    expected_hostname: str
    expected_identity: GreenhouseApplicationIdentity
    final_action_binding: str
    atomic_expectation: GreenhouseAtomicCommitExpectation
    commit_claimed: bool = False
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


def _visible_matches(soup: BeautifulSoup, selector: str) -> tuple[Tag, ...]:
    try:
        return tuple(
            item for item in soup.select(selector) if isinstance(item, Tag) and _visible(item)
        )
    except Exception:
        return ()


def _final_control_is_actionable(element: Tag, form: Tag) -> bool:
    """Fail closed on static states that make the exact final control inert."""

    if (
        element.name != "button"
        or element.find_parent("form") is not form
        or element.has_attr("form")
        or element.has_attr("disabled")
        or not _visible(element)
    ):
        return False
    current: Tag | None = element
    while current is not None:
        if (
            current.has_attr("inert")
            or str(current.get("aria-disabled", "")).strip().casefold() == "true"
            or (current.name == "fieldset" and current.has_attr("disabled"))
        ):
            return False
        style = re.sub(r"\s+", "", str(current.get("style", "")).casefold())
        if any(
            marker in style
            for marker in (
                "pointer-events:none",
                "content-visibility:hidden",
                "visibility:collapse",
            )
        ):
            return False
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return True


def greenhouse_v1_dom_commitment(html: str) -> str | None:
    """Return a digest of exactly one visible candidate form."""

    soup = BeautifulSoup(html or "", "html.parser")
    forms = tuple(
        item
        for item in soup.select(", ".join(_FORM_SELECTORS))
        if isinstance(item, Tag) and _visible(item)
    )
    if len(forms) != 1:
        return None
    return hashlib.sha256(str(forms[0]).encode("utf-8")).hexdigest()


def greenhouse_visible_confirmation_digest(html: str) -> str | None:
    """Hash canonical markup only when one visible ATS confirmation exists."""

    soup = BeautifulSoup(html or "", "html.parser")
    matches = _visible_matches(soup, GREENHOUSE_CONFIRMATION_SELECTOR)
    if len(matches) != 1:
        return None
    return hashlib.sha256(str(matches[0]).encode("utf-8")).hexdigest()


def _has_one_visible_confirmation(soup: BeautifulSoup) -> bool:
    return greenhouse_visible_confirmation_digest(str(soup)) is not None


def greenhouse_public_hostname(
    url: str,
    *,
    expected_hostname: str | None = None,
) -> str:
    """Compatibility wrapper over the shared candidate identity contract."""

    try:
        candidate = parse_greenhouse_candidate_url(
            url,
            expected_hostname=expected_hostname,
        )
    except GreenhouseIdentityError as exc:
        raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
    return candidate.hostname


def _require_snapshot_application_binding(
    snapshot: GreenhouseBrowserSnapshot,
    *,
    expected_hostname: str,
    expected_identity: GreenhouseApplicationIdentity,
) -> None:
    try:
        parse_greenhouse_candidate_url(
            snapshot.url,
            expected_hostname=expected_hostname,
            expected_identity=expected_identity,
        )
    except GreenhouseIdentityError as exc:
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc


def detect_greenhouse_variant(html: str, url: str = "") -> GreenhouseVariant:
    """Classify one official Greenhouse form without trusting an external host."""

    low_url = (url or "").casefold()
    parsed = urlsplit(url or "")
    query = {key.casefold(): values for key, values in parse_qs(parsed.query).items()}
    soup = BeautifulSoup(html or "", "html.parser")
    explicit = soup.select_one("[data-greenhouse-variant]")
    explicit_value = (
        str(explicit.get("data-greenhouse-variant", "")).strip().casefold()
        if explicit is not None
        else ""
    )
    if explicit_value in {variant.value for variant in GreenhouseVariant}:
        return GreenhouseVariant(explicit_value)
    if "/embed/job_app" in low_url or soup.select_one("[data-greenhouse-embedded]") is not None:
        return GreenhouseVariant.EMBEDDED
    if "gh_jid" in query or soup.select_one("[data-greenhouse-job-id]") is not None:
        return GreenhouseVariant.JOB_ID
    return GreenhouseVariant.HOSTED


def assess_greenhouse_v1_snapshot(
    html: str,
    url: str = "",
) -> GreenhousePageAssessment:
    """Classify a sanitized Greenhouse snapshot using exact selector markers."""

    if len((html or "").encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        return GreenhousePageAssessment(
            GreenhousePageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        )
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.stripped_strings).casefold()
    low_url = (url or "").casefold()

    challenge_selectors = (
        '[data-qa="captcha"]',
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
        return GreenhousePageAssessment(
            GreenhousePageState.CHALLENGE,
            ReasonCode.CHALLENGE_DETECTED,
        )
    if (
        soup.select_one('[data-qa="mfa-challenge"]') is not None
        or soup.select_one('input[autocomplete="one-time-code"]') is not None
    ):
        return GreenhousePageAssessment(GreenhousePageState.MFA, ReasonCode.MFA_REQUIRED)
    if (
        soup.select_one('[data-qa="sign-in"]') is not None
        or soup.select_one('input[type="password"]') is not None
        or any(marker in low_url for marker in ("/login", "/signin", "/sign-in"))
    ):
        return GreenhousePageAssessment(
            GreenhousePageState.LOGIN,
            ReasonCode.SESSION_EXPIRED,
        )
    if soup.select_one('[data-qa="job-closed"], #job_not_found') is not None or any(
        marker in text
        for marker in (
            "this job is no longer available",
            "this position has been filled",
            "the job posting has expired",
        )
    ):
        return GreenhousePageAssessment(
            GreenhousePageState.CLOSED,
            ReasonCode.JOB_CLOSED,
        )
    if soup.select_one('[data-qa="already-applied"]') is not None or any(
        marker in text
        for marker in (
            "you have already applied",
            "application already received",
        )
    ):
        return GreenhousePageAssessment(
            GreenhousePageState.ALREADY_APPLIED,
            ReasonCode.ALREADY_APPLIED,
        )
    if _has_one_visible_confirmation(soup):
        return GreenhousePageAssessment(GreenhousePageState.CONFIRMATION)

    form = soup.select_one(", ".join(_FORM_SELECTORS))
    if form is not None:
        if _visible_matches(
            soup,
            '[data-qa="resume-upload-pending"], [data-qa="resume-upload-error"]',
        ):
            return GreenhousePageAssessment(
                GreenhousePageState.FORM,
                ReasonCode.ATTACHMENT_UNVERIFIED,
            )
        if _visible_matches(
            soup,
            '[data-qa="validation-error"], [role="alert"].field-error',
        ):
            return GreenhousePageAssessment(
                GreenhousePageState.FORM,
                ReasonCode.REQUIRED_FIELD_UNKNOWN,
            )
        submit = tuple(
            item
            for item in soup.select(", ".join(_FINAL_ACTION_SELECTORS))
            if isinstance(item, Tag) and _final_control_is_actionable(item, form)
        )
        wrappers = _visible_matches(soup, _FIELD_WRAPPER_SELECTOR)
        if len(submit) == 1 and wrappers:
            return GreenhousePageAssessment(GreenhousePageState.REVIEW)
        if wrappers:
            return GreenhousePageAssessment(GreenhousePageState.FORM)
    if (
        len(
            _visible_matches(
                soup,
                '[data-qa="apply-button"], a[href*="/embed/job_app"], a[href*="/apply"]',
            )
        )
        == 1
    ):
        return GreenhousePageAssessment(GreenhousePageState.JOB)
    return GreenhousePageAssessment(
        GreenhousePageState.SELECTOR_DRIFT,
        ReasonCode.SELECTOR_DRIFT,
    )


def _bounded_int(raw: object, *, minimum: int = 0) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if value < minimum:
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _bounded_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED) from None
    if not math.isfinite(value):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _bounded_pattern(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or len(value) > 128 or any(ord(character) < 32 for character in value):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return value


def _label(wrapper: Tag, control: Tag) -> str:
    node = wrapper.select_one("legend, [data-qa='field-label'], label")
    if node is not None:
        value = node.get_text(" ", strip=True)
        if value:
            return value
    aria = str(control.get("aria-label", "")).strip()
    return aria


def _control_type(wrapper: Tag, control: Tag, label: str) -> FieldType:
    if control.name == "textarea":
        return FieldType.TEXTAREA
    if control.name == "select":
        return FieldType.MULTI_SELECT if control.has_attr("multiple") else FieldType.SELECT
    raw = str(control.get("type", "text")).casefold()
    if raw == "checkbox":
        consent_hint = " ".join(
            (
                str(wrapper.get("data-field-category", "")),
                str(wrapper.get("data-section", "")),
                label,
            )
        ).casefold()
        if any(marker in consent_hint for marker in ("consent", "privacy", "gdpr")):
            return FieldType.CONSENT
        if any(marker in consent_hint for marker in ("attest", "certify", "acknowledge")):
            return FieldType.ATTESTATION
        checkbox_count = len(wrapper.select('input[type="checkbox"]'))
        return FieldType.MULTI_SELECT if checkbox_count > 1 else FieldType.CHECKBOX
    return {
        "date": FieldType.DATE,
        "email": FieldType.EMAIL,
        "file": FieldType.FILE,
        "number": FieldType.NUMBER,
        "radio": FieldType.RADIO,
        "tel": FieldType.PHONE,
        "url": FieldType.URL,
    }.get(raw, FieldType.TEXT)


def _option_label(wrapper: Tag, node: Tag, value: str) -> str:
    if node.name == "option":
        return node.get_text(" ", strip=True) or value
    node_id = str(node.get("id", "")).strip()
    if node_id:
        label = wrapper.select_one(f'label[for="{node_id}"]')
        if label is not None:
            visible = label.get_text(" ", strip=True)
            if visible:
                return visible
    return str(node.get("data-option-label", value)).strip() or value


def _field_options(
    wrapper: Tag,
    control: Tag,
    field_type: FieldType,
) -> tuple[FormOptionV1, ...]:
    if field_type in {FieldType.SELECT, FieldType.MULTI_SELECT} and control.name == "select":
        nodes = [item for item in control.find_all("option") if isinstance(item, Tag)]
    elif field_type is FieldType.RADIO:
        nodes = [
            item for item in wrapper.select('input[type="radio"][value]') if isinstance(item, Tag)
        ]
    elif field_type is FieldType.MULTI_SELECT:
        nodes = [
            item
            for item in wrapper.select('input[type="checkbox"][value]')
            if isinstance(item, Tag)
        ]
    else:
        return ()

    options: list[FormOptionV1] = []
    for index, node in enumerate(nodes):
        value = str(node.get("value", "")).strip()
        if not value:
            continue
        options.append(
            FormOptionV1(
                option_id=str(
                    node.get("data-option-id", node.get("id", f"option-{index}"))
                ).strip(),
                value=value,
                label=_option_label(wrapper, node, value),
                disabled=node.has_attr("disabled"),
            )
        )
    return tuple(options)


def _canonical_name(wrapper: Tag, field_id: str) -> str | None:
    explicit = str(wrapper.get("data-canonical-name", "")).strip()
    if explicit:
        return explicit
    aliases = {
        "first_name": "first_name",
        "last_name": "last_name",
        "email": "email",
        "phone": "phone",
        "resume": "resume",
        "resume_upload": "resume_upload",
        "cover_letter": "cover_letter",
        "linkedin": "linkedin_url",
        "linkedin_url": "linkedin_url",
        "website": "portfolio_url",
        "portfolio": "portfolio_url",
    }
    return aliases.get(field_id.casefold())


def _sensitive_category(
    wrapper: Tag,
    field_type: FieldType,
    label: str,
) -> SensitiveCategory | None:
    if field_type is FieldType.CONSENT:
        return SensitiveCategory.CONSENT
    if field_type is FieldType.ATTESTATION:
        return SensitiveCategory.ATTESTATION
    context = " ".join(
        (
            str(wrapper.get("data-field-category", "")),
            str(wrapper.get("data-section", "")),
            label,
        )
    ).casefold()
    if any(
        marker in context
        for marker in (
            "demographic",
            "diversity",
            "eeoc",
            "ethnicity",
            "gender",
            "race",
            "veteran",
            "disability",
        )
    ):
        return SensitiveCategory.DEMOGRAPHIC
    return None


def observe_greenhouse_v1_fields(html: str) -> tuple[FormFieldV1, ...]:
    """Extract the exact ordered visible Greenhouse form contract."""

    if len((html or "").encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    soup = BeautifulSoup(html or "", "html.parser")
    forms = [item for item in soup.select(", ".join(_FORM_SELECTORS)) if _visible(item)]
    if len(forms) != 1:
        raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    wrappers = [
        item
        for item in forms[0].select(_FIELD_WRAPPER_SELECTOR)
        if isinstance(item, Tag) and _visible(item)
    ]
    if not wrappers or len(wrappers) > _MAX_FIELD_COUNT:
        raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

    fields: list[FormFieldV1] = []
    seen_ids: set[str] = set()
    for position, wrapper in enumerate(wrappers):
        field_id = str(wrapper.get("data-field-id", "")).strip()
        if not _FIELD_ID.fullmatch(field_id) or field_id in seen_ids:
            raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        controls = [
            item
            for item in wrapper.select("input:not([type='hidden']), textarea, select")
            if isinstance(item, Tag) and _visible(item)
        ]
        if not controls:
            raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        control = controls[0]
        label = _label(wrapper, control)
        if not label:
            raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        field_type = _control_type(wrapper, control, label)
        if field_type is FieldType.RADIO:
            names = {
                str(item.get("name", "")).strip()
                for item in controls
                if str(item.get("type", "")).casefold() == "radio"
            }
            if len(names) != 1 or "" in names:
                raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        elif field_type is FieldType.MULTI_SELECT and control.name != "select":
            names = {
                str(item.get("name", "")).strip()
                for item in controls
                if str(item.get("type", "")).casefold() == "checkbox"
            }
            if len(names) != 1 or "" in names:
                raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        elif len(controls) != 1:
            raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

        accepted = tuple(
            value.strip() for value in str(control.get("accept", "")).split(",") if value.strip()
        )[:32]
        required = (
            control.has_attr("required")
            or str(wrapper.get("aria-required", "")).casefold() == "true"
            or str(wrapper.get("data-required", "")).casefold() == "true"
        )
        fields.append(
            FormFieldV1(
                field_id=field_id,
                canonical_name=_canonical_name(wrapper, field_id),
                label=label,
                field_type=field_type,
                required=required,
                position=position,
                options=_field_options(wrapper, control, field_type),
                constraints=FormFieldConstraintsV1(
                    min_length=_bounded_int(control.get("minlength")),
                    max_length=_bounded_int(control.get("maxlength")),
                    min_value=_bounded_float(control.get("min")),
                    max_value=_bounded_float(control.get("max")),
                    pattern=_bounded_pattern(control.get("pattern")),
                    accepted_file_types=accepted,
                    multiple=(
                        control.has_attr("multiple")
                        or (field_type is FieldType.MULTI_SELECT and control.name != "select")
                    ),
                ),
                sensitive_category=_sensitive_category(wrapper, field_type, label),
            )
        )
        seen_ids.add(field_id)
    return tuple(fields)


def greenhouse_v1_form_fingerprint(
    fields: tuple[FormFieldV1, ...],
    variant: GreenhouseVariant,
    form_action_binding: str,
) -> str:
    """Hash the variant, controls, and exact redacted submission target."""

    if (
        not fields
        or len(fields) > _MAX_FIELD_COUNT
        or re.fullmatch(r"[0-9a-f]{64}", form_action_binding) is None
    ):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    payload = {
        "adapter_version": GREENHOUSE_V1_ADAPTER_VERSION,
        "selector_version": GREENHOUSE_V1_SELECTOR_VERSION,
        "variant": variant.value,
        "form_action_binding": form_action_binding,
        "fields": [field.model_dump(mode="json") for field in fields],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_verified_resume_bytes(path: str, expected_sha256: str) -> bytes:
    with Path(path).open("rb") as handle:
        payload = handle.read(_MAX_RESUME_BYTES + 1)
    if (
        not payload
        or len(payload) > _MAX_RESUME_BYTES
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    return payload


def _descriptor() -> AdapterDescriptor:
    descriptor = adapter_for_platform("greenhouse")
    if (
        descriptor is None
        or descriptor.adapter_version != GREENHOUSE_V1_ADAPTER_VERSION
        or descriptor.selector_version != GREENHOUSE_V1_SELECTOR_VERSION
        or descriptor.execution_contract_version != TWO_PHASE_EXECUTION_CONTRACT_VERSION
        or descriptor.transport != "browser"
    ):
        raise RuntimeError("GREENHOUSE_V1_DESCRIPTOR_MISMATCH")
    return descriptor


class GreenhouseBrowserV1:
    """Two-phase Greenhouse adapter with a single-click ambiguity boundary."""

    def __init__(
        self,
        *,
        browser_factory: GreenhouseBrowserFactory,
        answer_policy: AnswerPolicyV1 | None = None,
        descriptor: AdapterDescriptor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.descriptor = descriptor or _descriptor()
        self._browser_factory = browser_factory
        self._answer_policy = answer_policy or AnswerPolicyV1()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._prepared: dict[str, _PreparedState] = {}
        self._pending_preflight: dict[str, GreenhouseCandidateSession] = {}
        self._cleanup_preflight_id: ContextVar[str | None] = ContextVar(
            f"greenhouse-v1-cleanup-{id(self)}",
            default=None,
        )

    def can_inspect(self, job: JobData) -> bool:
        url = job.apply_url or job.source_url
        try:
            greenhouse_public_hostname(url)
        except GreenhouseAdapterBlockedError:
            return False
        candidate = adapter_for_url(url)
        return bool(
            candidate is not None
            and candidate.platform == "greenhouse"
            and self.descriptor.platform == "greenhouse"
            and self.descriptor.adapter_version == GREENHOUSE_V1_ADAPTER_VERSION
            and self.descriptor.selector_version == GREENHOUSE_V1_SELECTOR_VERSION
        )

    @staticmethod
    def _inspection_reason(assessment: GreenhousePageAssessment) -> ReasonCode | None:
        return assessment.reason_code or (
            ReasonCode.ALREADY_APPLIED
            if assessment.state is GreenhousePageState.CONFIRMATION
            else None
        )

    @staticmethod
    def _model_audit(
        policy: Any,
        prior: tuple[str, str, str, str] | None,
    ) -> tuple[str, str, str, str] | None:
        candidate = (
            policy.prompt_version,
            policy.model_provider,
            policy.model_name,
            policy.model_digest,
        )
        if not any(value is not None for value in candidate):
            return prior
        if not all(isinstance(value, str) for value in candidate):
            raise GreenhouseAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
        bounded = tuple(str(value) for value in candidate)
        if prior is not None and bounded != prior:
            raise GreenhouseAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
        return bounded  # type: ignore[return-value]

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
        """Observe and stabilize a public candidate form without submitting."""

        if (
            not self.can_inspect(job)
            or not resume_path
            or not selected_cv_id
            or application.cv_sha256 is None
            or application.profile_version is None
            or re.fullmatch(r"[0-9a-f]{64}", application.cv_sha256) is None
        ):
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            resume_bytes = _read_verified_resume_bytes(resume_path, application.cv_sha256)
        except OSError as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        try:
            profile = UserProfile.model_validate(dict(user_profile))
        except (TypeError, ValueError) as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN) from exc

        job_url = job.apply_url or job.source_url
        try:
            requested_candidate = parse_greenhouse_candidate_url(job_url)
        except GreenhouseIdentityError as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        planner = answer_policy or self._answer_policy
        session = self._browser_factory(job_url)
        try:
            await session.navigate(job_url)
            snapshot = await session.snapshot()
            _require_snapshot_application_binding(
                snapshot,
                expected_hostname=requested_candidate.hostname,
                expected_identity=requested_candidate.identity,
            )
            assessment = assess_greenhouse_v1_snapshot(snapshot.html, snapshot.url)
            if assessment.state is GreenhousePageState.JOB:
                await session.open_candidate_form()
                snapshot = await session.snapshot()
                _require_snapshot_application_binding(
                    snapshot,
                    expected_hostname=requested_candidate.hostname,
                    expected_identity=requested_candidate.identity,
                )
                assessment = assess_greenhouse_v1_snapshot(snapshot.html, snapshot.url)

            final_fields: tuple[FormFieldV1, ...] = ()
            final_decisions: tuple[AnswerDecisionV1, ...] = ()
            proof: GreenhouseAttachmentProof | None = None
            blockers: tuple[ReasonCode, ...] = ()
            audit_identity: tuple[str, str, str, str] | None = None
            context_policy_version = "answer-policy-v1"
            final_form_action_binding: str | None = None
            variant = detect_greenhouse_variant(snapshot.html, snapshot.url)
            complete = False

            for _round in range(_MAX_STABILIZATION_ROUNDS):
                reason = self._inspection_reason(assessment)
                if reason is not None:
                    raise GreenhouseAdapterBlockedError(reason)
                if assessment.state not in {
                    GreenhousePageState.FORM,
                    GreenhousePageState.REVIEW,
                }:
                    raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

                fields = observe_greenhouse_v1_fields(snapshot.html)
                candidate_variant = detect_greenhouse_variant(snapshot.html, snapshot.url)
                if final_fields and candidate_variant is not variant:
                    raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
                variant = candidate_variant
                form_action_binding = await session.observed_form_action_binding()
                if form_action_binding is None:
                    raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

                resume_fields = [
                    field
                    for field in fields
                    if field.field_type is FieldType.FILE
                    and field.canonical_name in {"resume", "resume_upload", "cv", "cv_upload"}
                ]
                if len(resume_fields) != 1:
                    raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                proof = await session.ensure_resume_attachment(
                    resume_bytes=resume_bytes,
                    cv_id=selected_cv_id,
                    expected_sha256=application.cv_sha256,
                )
                if not proof.matches(
                    cv_id=selected_cv_id,
                    cv_sha256=application.cv_sha256,
                ):
                    raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

                fingerprint = greenhouse_v1_form_fingerprint(
                    fields,
                    variant,
                    form_action_binding,
                )
                context = AnswerPolicyContext(
                    profile=profile,
                    profile_version=application.profile_version,
                    selected_cv_id=selected_cv_id,
                    selected_cv_hash=application.cv_sha256,
                    attached_cv_id=proof.cv_id,
                    attached_cv_hash=proof.cv_sha256,
                    attachment_verified=True,
                    adapter_name=self.descriptor.platform,
                    adapter_version=self.descriptor.adapter_version,
                    selector_version=self.descriptor.selector_version,
                    form_fingerprint=fingerprint,
                    locale=snapshot.locale,
                )
                context_policy_version = context.policy_version
                policy = await planner.plan_fields(fields, context)
                if {decision.field_id for decision in policy.decisions} != {
                    field.field_id for field in fields
                }:
                    raise GreenhouseAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
                audit_identity = self._model_audit(policy, audit_identity)
                final_fields = fields
                final_decisions = policy.decisions
                final_form_action_binding = form_action_binding
                blockers = tuple(dict.fromkeys(policy.blockers))
                if blockers:
                    break

                await session.fill(
                    tuple(
                        decision
                        for decision in policy.decisions
                        if decision.disposition is AnswerDisposition.RESOLVED
                    )
                )
                await session.settle_reversible_form()
                next_snapshot = await session.snapshot()
                _require_snapshot_application_binding(
                    next_snapshot,
                    expected_hostname=requested_candidate.hostname,
                    expected_identity=requested_candidate.identity,
                )
                next_assessment = assess_greenhouse_v1_snapshot(
                    next_snapshot.html,
                    next_snapshot.url,
                )
                if next_assessment.state in {
                    GreenhousePageState.FORM,
                    GreenhousePageState.REVIEW,
                }:
                    next_fields = observe_greenhouse_v1_fields(next_snapshot.html)
                    next_variant = detect_greenhouse_variant(
                        next_snapshot.html,
                        next_snapshot.url,
                    )
                    next_action_binding = await session.observed_form_action_binding()
                    validated_action_binding = (
                        await session.final_action_binding()
                        if await session.final_action_ready()
                        else None
                    )
                    if (
                        next_fields == fields
                        and next_variant is variant
                        and next_action_binding == form_action_binding
                        and validated_action_binding == form_action_binding
                    ):
                        snapshot = next_snapshot
                        assessment = next_assessment
                        final_form_action_binding = form_action_binding
                        complete = True
                        break
                snapshot = next_snapshot
                assessment = next_assessment
            else:
                raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)

            if not final_fields or proof is None or final_form_action_binding is None:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            proof = await session.verify_resume_attachment(
                cv_id=selected_cv_id,
                expected_sha256=application.cv_sha256,
            )
            attachment_verified = proof.matches(
                cv_id=selected_cv_id,
                cv_sha256=application.cv_sha256,
            )
            if not attachment_verified:
                blockers = tuple(dict.fromkeys((*blockers, ReasonCode.ATTACHMENT_UNVERIFIED)))
            if not complete:
                blockers = tuple(dict.fromkeys((*blockers, ReasonCode.FORM_PLAN_INCOMPLETE)))
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise GreenhouseAdapterBlockedError(ReasonCode.INTERNAL_ERROR)
            return FormPlanV1(
                plan_id=uuid4(),
                application_id=application_id,
                application_revision=application_revision,
                adapter_name=self.descriptor.platform,
                adapter_version=self.descriptor.adapter_version,
                selector_version=self.descriptor.selector_version,
                form_fingerprint=greenhouse_v1_form_fingerprint(
                    final_fields,
                    variant,
                    final_form_action_binding,
                ),
                selected_cv_id=selected_cv_id,
                selected_cv_hash=application.cv_sha256,
                attached_cv_id=proof.cv_id,
                attached_cv_hash=proof.cv_sha256,
                attachment_verified=attachment_verified,
                profile_version=application.profile_version,
                session_verified_at=now,
                created_at=now,
                expires_at=now + timedelta(minutes=30),
                fields=final_fields,
                decisions=final_decisions,
                blockers=blockers,
                locale=snapshot.locale,
                answer_policy_version=context_policy_version,
                llm_prompt_version=audit_identity[0] if audit_identity is not None else None,
                llm_model_provider=audit_identity[1] if audit_identity is not None else None,
                llm_model_name=audit_identity[2] if audit_identity is not None else None,
                llm_model_digest=audit_identity[3] if audit_identity is not None else None,
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
        """Replay an exact reviewed form and stop before the final click."""

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
            requested_candidate = parse_greenhouse_candidate_url(
                context.normalized_job_url,
            )
            resume_bytes = _read_verified_resume_bytes(
                context.resume_path,
                plan.selected_cv_hash,
            )
        except (OSError, GreenhouseIdentityError):
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
            _require_snapshot_application_binding(
                snapshot,
                expected_hostname=requested_candidate.hostname,
                expected_identity=requested_candidate.identity,
            )
            assessment = assess_greenhouse_v1_snapshot(snapshot.html, snapshot.url)
            if assessment.state is GreenhousePageState.JOB:
                await session.open_candidate_form()
                snapshot = await session.snapshot()
                _require_snapshot_application_binding(
                    snapshot,
                    expected_hostname=requested_candidate.hostname,
                    expected_identity=requested_candidate.identity,
                )
                assessment = assess_greenhouse_v1_snapshot(snapshot.html, snapshot.url)
            if assessment.state is GreenhousePageState.ALREADY_APPLIED:
                return AlreadyAppliedOutcome()
            if assessment.reason_code is not None:
                return NeedsReviewOutcome(reason_code=assessment.reason_code)

            proof: GreenhouseAttachmentProof | None = None
            decisions = {decision.field_id: decision for decision in plan.decisions}
            previous_fields: tuple[FormFieldV1, ...] | None = None
            variant: GreenhouseVariant | None = None

            for _round in range(_MAX_STABILIZATION_ROUNDS):
                if assessment.state is GreenhousePageState.ALREADY_APPLIED:
                    return AlreadyAppliedOutcome()
                if assessment.reason_code is not None:
                    return NeedsReviewOutcome(reason_code=assessment.reason_code)
                if assessment.state not in {
                    GreenhousePageState.FORM,
                    GreenhousePageState.REVIEW,
                }:
                    return NeedsReviewOutcome(reason_code=ReasonCode.SELECTOR_DRIFT)
                fields = observe_greenhouse_v1_fields(snapshot.html)
                current_variant = detect_greenhouse_variant(snapshot.html, snapshot.url)
                if variant is not None and current_variant is not variant:
                    return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
                variant = current_variant
                form_action_binding = await session.observed_form_action_binding()
                if form_action_binding is None:
                    return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
                if (
                    fields == plan.fields
                    and greenhouse_v1_form_fingerprint(
                        fields,
                        current_variant,
                        form_action_binding,
                    )
                    != plan.form_fingerprint
                ):
                    return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
                expected_by_id = {field.field_id: field for field in plan.fields}
                if any(expected_by_id.get(field.field_id) != field for field in fields):
                    return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)

                resume_fields = [
                    field
                    for field in fields
                    if field.field_type is FieldType.FILE
                    and field.canonical_name in {"resume", "resume_upload", "cv", "cv_upload"}
                ]
                if len(resume_fields) != 1:
                    return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
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

                step_decisions = tuple(
                    decisions[field.field_id]
                    for field in fields
                    if field.field_id in decisions
                    and decisions[field.field_id].disposition is AnswerDisposition.RESOLVED
                )
                required_ids = {field.field_id for field in fields if field.required}
                if not required_ids.issubset({decision.field_id for decision in step_decisions}):
                    return NeedsReviewOutcome(reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN)
                await session.fill(step_decisions)
                await session.settle_reversible_form()
                next_snapshot = await session.snapshot()
                _require_snapshot_application_binding(
                    next_snapshot,
                    expected_hostname=requested_candidate.hostname,
                    expected_identity=requested_candidate.identity,
                )
                next_assessment = assess_greenhouse_v1_snapshot(
                    next_snapshot.html,
                    next_snapshot.url,
                )
                if next_assessment.state in {
                    GreenhousePageState.FORM,
                    GreenhousePageState.REVIEW,
                }:
                    next_fields = observe_greenhouse_v1_fields(next_snapshot.html)
                    next_variant = detect_greenhouse_variant(
                        next_snapshot.html,
                        next_snapshot.url,
                    )
                    next_action_binding = await session.observed_form_action_binding()
                    if (
                        previous_fields == next_fields == fields == plan.fields
                        and next_variant is current_variant
                        and next_action_binding is not None
                        and next_action_binding == form_action_binding
                        and greenhouse_v1_form_fingerprint(
                            next_fields,
                            next_variant,
                            next_action_binding,
                        )
                        == plan.form_fingerprint
                        and await session.final_action_ready()
                    ):
                        snapshot = next_snapshot
                        assessment = next_assessment
                        break
                previous_fields = fields
                snapshot = next_snapshot
                assessment = next_assessment
            else:
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)

            if variant is None or assessment.state not in {
                GreenhousePageState.FORM,
                GreenhousePageState.REVIEW,
            }:
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            if proof is None:
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            proof = await session.verify_resume_attachment(
                cv_id=plan.selected_cv_id,
                expected_sha256=plan.selected_cv_hash,
            )
            if not proof.matches(cv_id=plan.selected_cv_id, cv_sha256=plan.selected_cv_hash):
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            if not await session.final_action_ready():
                return NeedsReviewOutcome(reason_code=ReasonCode.SELECTOR_DRIFT)
            final_action_binding = await session.final_action_binding()
            final_action_url = await session.final_action_url()
            dom_commitment = await session.commit_dom_commitment()
            try:
                reviewed_answers = greenhouse_v1_reviewed_answer_bindings(
                    plan.fields,
                    plan.decisions,
                    selected_cv_hash=plan.selected_cv_hash,
                )
            except ValueError:
                return NeedsReviewOutcome(reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN)
            payload_binding = await session.commit_payload_binding(
                reviewed_answers=reviewed_answers,
                expected_cv_sha256=plan.selected_cv_hash,
            )
            if (
                final_action_binding is None
                or final_action_url is None
                or dom_commitment is None
                or payload_binding is None
                or proof.receipt_sha256 is None
                or final_action_binding != await session.observed_form_action_binding()
                or greenhouse_v1_form_fingerprint(
                    plan.fields,
                    variant,
                    final_action_binding,
                )
                != plan.form_fingerprint
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
            atomic_expectation = GreenhouseAtomicCommitExpectation(
                expected_hostname=requested_candidate.hostname,
                expected_identity=requested_candidate.identity,
                fields=plan.fields,
                variant=variant,
                form_fingerprint=plan.form_fingerprint,
                action_binding=final_action_binding,
                dom_commitment=dom_commitment,
                resolved_action_url=final_action_url,
                native_transport=GREENHOUSE_V1_NATIVE_TRANSPORT,
                payload_commitment=payload_binding.payload_commitment,
                answer_bindings=payload_binding.answer_bindings,
                resume_control_name_sha256=payload_binding.resume_control_name_sha256,
                submitter_binding=payload_binding.submitter_binding,
                cv_id=plan.selected_cv_id,
                cv_sha256=plan.selected_cv_hash,
                cv_receipt_sha256=proof.receipt_sha256,
            )
            self._prepared[action_nonce] = _PreparedState(
                session=session,
                plan=plan,
                permit=permit,
                pre_action_html=snapshot.html,
                expected_hostname=requested_candidate.hostname,
                expected_identity=requested_candidate.identity,
                final_action_binding=final_action_binding,
                atomic_expectation=atomic_expectation,
            )
            self._pending_preflight.pop(cleanup_id, None)
            return action
        except GreenhouseAdapterBlockedError as exc:
            return NeedsReviewOutcome(reason_code=exc.reason_code)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)

    async def commit(
        self,
        *,
        action: PreparedFinalActionV1,
        permit: FinalSubmitPermit,
    ) -> CommitOutcome:
        """Click once and require fresh, ATS-specific visible evidence."""

        if not self.descriptor.allows_final_execution:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        state = self._prepared.get(action.action_nonce)
        now = self._clock()
        if state is None:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)
        if state.clicked or state.commit_claimed:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_REPLAYED)
        try:
            binding_valid = action.binds(state.plan, permit, at=now) and permit == state.permit
        except ValueError:
            binding_valid = False
        if not binding_valid:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)
        # The in-memory prepared action is one-use even when two coroutine
        # callers race before the first browser await. This is not the external
        # ambiguity marker; ``clicked`` remains false until all checks pass.
        state.commit_claimed = True

        # Preflight may be separated from the irreversible action by runner
        # scheduling or operator latency. Re-observe every external binding
        # immediately before crossing the ambiguity boundary. Nothing below
        # this block may click or mark the action as clicked.
        try:
            precommit = await state.session.snapshot()
            _require_snapshot_application_binding(
                precommit,
                expected_hostname=state.expected_hostname,
                expected_identity=state.expected_identity,
            )
            precommit_soup = BeautifulSoup(precommit.html, "html.parser")
            if _visible_matches(precommit_soup, GREENHOUSE_CONFIRMATION_SELECTOR):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            assessment = assess_greenhouse_v1_snapshot(
                precommit.html,
                precommit.url,
            )
            if assessment.reason_code is not None:
                return NeedsReviewOutcome(reason_code=assessment.reason_code)
            if assessment.state not in {
                GreenhousePageState.FORM,
                GreenhousePageState.REVIEW,
            }:
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            fields = observe_greenhouse_v1_fields(precommit.html)
            variant = detect_greenhouse_variant(precommit.html, precommit.url)
            observed_action_binding = await state.session.observed_form_action_binding()
            if (
                observed_action_binding is None
                or fields != state.plan.fields
                or greenhouse_v1_form_fingerprint(
                    fields,
                    variant,
                    observed_action_binding,
                )
                != state.plan.form_fingerprint
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            proof = await state.session.verify_resume_attachment(
                cv_id=state.plan.selected_cv_id,
                expected_sha256=state.plan.selected_cv_hash,
            )
            if (
                not proof.matches(
                    cv_id=state.plan.selected_cv_id,
                    cv_sha256=state.plan.selected_cv_hash,
                )
                or proof.receipt_sha256 != state.atomic_expectation.cv_receipt_sha256
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
            if not await state.session.final_action_ready():
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            final_action_binding = await state.session.final_action_binding()
            final_action_url = await state.session.final_action_url()
            dom_commitment = await state.session.commit_dom_commitment()
            if (
                final_action_binding is None
                or final_action_url is None
                or dom_commitment is None
                or final_action_binding != observed_action_binding
                or final_action_binding != state.final_action_binding
                or final_action_url != state.atomic_expectation.resolved_action_url
                or dom_commitment != state.atomic_expectation.dom_commitment
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
        except GreenhouseAdapterBlockedError as exc:
            return NeedsReviewOutcome(reason_code=exc.reason_code)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY)

        final_action_at = self._clock()
        try:
            still_bound = (
                action.binds(
                    state.plan,
                    permit,
                    at=final_action_at,
                )
                and permit == state.permit
            )
        except ValueError:
            still_bound = False
        if not still_bound:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)
        # Evidence comparison must use the immediate pre-click DOM rather than
        # the older preflight snapshot.
        state.pre_action_html = precommit.html
        # From this point onward, an unexpected transport error is ambiguous.
        # The session's typed observation is the only authority that may prove
        # an outbound request was blocked before leaving.
        state.clicked = True
        try:
            atomic_observation = await state.session.atomic_commit(
                state.atomic_expectation,
            )
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        atomic_outcome = _atomic_observation_outcome(
            atomic_observation,
            state.atomic_expectation,
        )
        if atomic_outcome is not None:
            return atomic_outcome

        try:
            post_action = await state.session.snapshot()
            _require_snapshot_application_binding(
                post_action,
                expected_hostname=state.expected_hostname,
                expected_identity=state.expected_identity,
            )
            employer_reference = await state.session.confirmation_reference()
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        assessment = assess_greenhouse_v1_snapshot(post_action.html, post_action.url)
        if assessment.state is GreenhousePageState.ALREADY_APPLIED:
            return AlreadyAppliedOutcome()
        if assessment.state is GreenhousePageState.CHALLENGE:
            return UnknownOutcome(reason_code=ReasonCode.CHALLENGE_DETECTED)
        if assessment.state is GreenhousePageState.LOGIN:
            return UnknownOutcome(reason_code=ReasonCode.SESSION_EXPIRED)
        snapshot_confirmation_digest = greenhouse_visible_confirmation_digest(
            post_action.html,
        )
        if (
            assessment.state is not GreenhousePageState.CONFIRMATION
            or not employer_reference
            or snapshot_confirmation_digest is None
            or employer_reference != snapshot_confirmation_digest
        ):
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        pre_soup = BeautifulSoup(state.pre_action_html, "html.parser")
        post_soup = BeautifulSoup(post_action.html, "html.parser")
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
                    rule_id="greenhouse-v1:visible-confirmation",
                    channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                    visible_selector=GREENHOUSE_CONFIRMATION_SELECTOR,
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
            rule_id="greenhouse-v1:visible-confirmation",
            channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
            evidence_reference=employer_reference,
            observed_at=self._clock(),
            observed_after_final_action=True,
            was_present_before_action=bool(pre_soup.select(GREENHOUSE_CONFIRMATION_SELECTOR)),
            visible_selector=GREENHOUSE_CONFIRMATION_SELECTOR,
            computed_visible=_has_one_visible_confirmation(post_soup),
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
        """Close each prepared or failed-preflight session exactly once."""

        sessions: list[GreenhouseCandidateSession] = []
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


def register_greenhouse_browser_v1(
    registry: SubmitterRegistry,
    *,
    browser_factory: GreenhouseBrowserFactory,
    answer_policy: AnswerPolicyV1 | None = None,
    descriptor: AdapterDescriptor | None = None,
) -> GreenhouseBrowserV1:
    """Register the fixture adapter without authorizing final execution."""

    adapter = GreenhouseBrowserV1(
        browser_factory=browser_factory,
        answer_policy=answer_policy,
        descriptor=descriptor,
    )
    registry.register_two_phase(adapter)
    return adapter
