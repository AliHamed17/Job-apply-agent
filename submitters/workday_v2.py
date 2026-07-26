"""Fixture-qualified, two-phase Workday candidate-browser adapter.

This module deliberately has no Playwright dependency and performs no network
requests by itself.  A private runner supplies a :class:`WorkdayCandidateSession`
that owns one browser page on one event loop.  The adapter observes, fills, and
verifies that page through a narrow protocol while keeping page HTML, answers,
CV paths, and cookies in memory only.

The checked-in descriptor is fixture-qualified with an empty live-canary
scope.  Consequently the production adapter can inspect forms but both the
registry and :meth:`WorkdayBrowserV2.commit` refuse the irreversible action.
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
from hmac import compare_digest
from pathlib import Path
from profile.models import UserProfile
from secrets import token_bytes
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
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

WORKDAY_V2_ADAPTER_VERSION = "2.0.3"
WORKDAY_V2_SELECTOR_VERSION = "workday-candidate-v2.4"
WORKDAY_CONFIRMATION_SELECTOR = 'main[data-automation-id="confirmationPage"][data-application-id]'
_MAX_FIXTURE_HTML_BYTES = 256 * 1024
_MAX_FIELD_COUNT = 200
_MAX_REVERSIBLE_STEPS = 12
_MAX_RESUME_BYTES = 20 * 1024 * 1024
_MAX_IDENTITY_FIELDS = 128
_WORKDAY_ACTION_SUFFIXES = frozenset({"apply", "application"})
_WORKDAY_CANDIDATE_SUFFIXES = ("myworkdayjobs.com", "myworkday.com")
_WORKDAY_JOB_KEYS = frozenset(
    {
        "job",
        "jobid",
        "jobposting",
        "jobpostingid",
        "jobreference",
        "jobrequisition",
        "jobrequisitionid",
        "requisition",
        "requisitionid",
        "requisitionreference",
    }
)
_WORKDAY_SITE_KEYS = frozenset(
    {
        "careersite",
        "careersiteid",
        "externalcareersite",
        "externalcareersiteid",
        "site",
        "siteid",
    }
)
_WORKDAY_RESERVED_HOST_LABELS = frozenset(
    {
        "api",
        "community",
        "developer",
        "localhost",
        "status",
        "support",
        "www",
    }
)


@dataclass(frozen=True, slots=True)
class WorkdayJobIdentity:
    """Exact tenant and requisition identity for one candidate flow."""

    hostname: str
    site: str
    requisition: str


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayFinalRequestContract:
    """Redacted exact target contract for one irreversible Workday request."""

    job_identity: WorkdayJobIdentity
    method: str
    digest: str

    def __post_init__(self) -> None:
        if self.method != "POST" or re.fullmatch(r"[0-9a-f]{64}", self.digest) is None:
            raise ValueError("WORKDAY_FINAL_REQUEST_CONTRACT_INVALID")


def _bound_final_request_digest(target_digest: str, payload_sha256: str) -> str:
    return hashlib.sha256(
        f"workday-final-request-v1|{target_digest}|{payload_sha256}".encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayBoundFinalRequestContract:
    """Exact target plus canonical FormData commitment, with no raw values."""

    target_contract: WorkdayFinalRequestContract
    payload_sha256: str
    digest: str

    def __post_init__(self) -> None:
        expected_digest = _bound_final_request_digest(
            self.target_contract.digest,
            self.payload_sha256,
        )
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256 or "") is None
            or re.fullmatch(r"[0-9a-f]{64}", self.digest or "") is None
            or not compare_digest(expected_digest, self.digest)
        ):
            raise ValueError("WORKDAY_BOUND_FINAL_REQUEST_CONTRACT_INVALID")

    @classmethod
    def bind(
        cls,
        target_contract: WorkdayFinalRequestContract,
        payload_sha256: str,
    ) -> WorkdayBoundFinalRequestContract:
        """Bind one redacted payload commitment to an already-reviewed target."""

        return cls(
            target_contract=target_contract,
            payload_sha256=payload_sha256,
            digest=_bound_final_request_digest(target_contract.digest, payload_sha256),
        )


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayAnswerBinding:
    """One redacted expected answer bound to a reviewed Workday field."""

    field_id: str
    field_type: FieldType
    value_sha256: str

    def __post_init__(self) -> None:
        if (
            not self.field_id.strip()
            or not isinstance(self.field_type, FieldType)
            or re.fullmatch(r"[0-9a-f]{64}", self.value_sha256) is None
        ):
            raise ValueError("WORKDAY_ANSWER_BINDING_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayFinalCommitExpectation:
    """Immutable truth bindings revalidated inside the browser transport."""

    job_identity: WorkdayJobIdentity
    pre_action_digest: str
    form_fingerprint: str
    final_action_binding: str
    observed_fields: tuple[FormFieldV1, ...]
    step_field_counts: tuple[int, ...]
    answer_bindings: tuple[WorkdayAnswerBinding, ...]
    selected_cv_id: str
    selected_cv_hash: str
    attachment_receipt_sha256: str
    request_contract: WorkdayFinalRequestContract

    def __post_init__(self) -> None:
        digests = (
            self.pre_action_digest,
            self.form_fingerprint,
            self.final_action_binding,
            self.selected_cv_hash,
            self.attachment_receipt_sha256,
            self.request_contract.digest,
        )
        try:
            form_matches = compare_digest(
                workday_v2_form_fingerprint(
                    self.observed_fields,
                    self.step_field_counts,
                    self.final_action_binding,
                ),
                self.form_fingerprint,
            )
        except (ValueError, WorkdayAdapterBlockedError):
            form_matches = False
        if (
            not self.selected_cv_id.strip()
            or any(re.fullmatch(r"[0-9a-f]{64}", value or "") is None for value in digests)
            or self.request_contract.job_identity != self.job_identity
            or not compare_digest(self.request_contract.digest, self.final_action_binding)
            or tuple((binding.field_id, binding.field_type) for binding in self.answer_bindings)
            != tuple((field.field_id, field.field_type) for field in self.observed_fields)
            or not form_matches
        ):
            raise ValueError("WORKDAY_FINAL_COMMIT_EXPECTATION_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayBoundFinalCommitExpectation:
    """Transport-local immutable expectation including the exact payload."""

    base: WorkdayFinalCommitExpectation
    request_contract: WorkdayBoundFinalRequestContract

    def __post_init__(self) -> None:
        if (
            self.request_contract.target_contract.job_identity != self.base.job_identity
            or not compare_digest(
                self.request_contract.target_contract.digest,
                self.base.request_contract.digest,
            )
        ):
            raise ValueError("WORKDAY_BOUND_FINAL_COMMIT_EXPECTATION_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class WorkdayFinalActionReceipt:
    """Redacted proof that exactly one validated request may have left."""

    target_digest: str
    payload_sha256: str
    request_digest: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.target_digest or "") is None
            or re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256 or "") is None
            or re.fullmatch(r"[0-9a-f]{64}", self.request_digest or "") is None
            or not compare_digest(
                _bound_final_request_digest(self.target_digest, self.payload_sha256),
                self.request_digest,
            )
        ):
            raise ValueError("WORKDAY_FINAL_ACTION_RECEIPT_INVALID")

    @classmethod
    def from_contract(
        cls,
        contract: WorkdayBoundFinalRequestContract,
    ) -> WorkdayFinalActionReceipt:
        return cls(
            target_digest=contract.target_contract.digest,
            payload_sha256=contract.payload_sha256,
            request_digest=contract.digest,
        )


class WorkdayFinalActionAmbiguousError(RuntimeError):
    """The final request may have left the browser; retry is unsafe."""


def _workday_answer_digest_material(
    field_type: FieldType,
    value: object,
    *,
    selected_cv_hash: str,
) -> str:
    if field_type in {
        FieldType.CHECKBOX,
        FieldType.CONSENT,
        FieldType.ATTESTATION,
    }:
        if type(value) is not bool:
            raise ValueError("WORKDAY_ANSWER_BINDING_INVALID")
        return "b:1" if value else "b:0"
    if field_type is FieldType.MULTI_SELECT:
        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise ValueError("WORKDAY_ANSWER_BINDING_INVALID")
        encoded = []
        for item in sorted(value):
            encoded.append(f"{len(item.encode('utf-8'))}:{item}")
        return f"m:{''.join(encoded)}"
    if field_type is FieldType.FILE:
        if (
            value != VERIFIED_ATTACHMENT_SENTINEL
            or re.fullmatch(r"[0-9a-f]{64}", selected_cv_hash or "") is None
        ):
            raise ValueError("WORKDAY_ANSWER_BINDING_INVALID")
        return f"f:{selected_cv_hash}"
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("WORKDAY_ANSWER_BINDING_INVALID")
    return f"s:{value}"


def workday_v2_answer_bindings(
    fields: tuple[FormFieldV1, ...],
    decisions: tuple[AnswerDecisionV1, ...],
    *,
    selected_cv_hash: str,
) -> tuple[WorkdayAnswerBinding, ...]:
    """Hash reviewed answers and bind file fields to exact selected-CV bytes."""

    by_id = {decision.field_id: decision for decision in decisions}
    field_ids = tuple(field.field_id for field in fields)
    if (
        len(by_id) != len(decisions)
        or len(set(field_ids)) != len(field_ids)
        or set(by_id) != set(field_ids)
    ):
        raise ValueError("WORKDAY_ANSWER_BINDING_INVALID")
    bindings = []
    for field in fields:
        decision = by_id[field.field_id]
        if decision.disposition is not AnswerDisposition.RESOLVED or decision.value is None:
            raise ValueError("WORKDAY_ANSWER_BINDING_INVALID")
        material = _workday_answer_digest_material(
            field.field_type,
            decision.value,
            selected_cv_hash=selected_cv_hash,
        )
        bindings.append(
            WorkdayAnswerBinding(
                field_id=field.field_id,
                field_type=field.field_type,
                value_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(bindings)


def _identity_key(raw: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(raw).casefold())


def _bounded_identity_value(raw: object) -> str | None:
    if not isinstance(raw, (str, int)):
        return None
    value = str(raw).strip()
    if not value or len(value) > 200 or any(ord(character) < 32 for character in value):
        return None
    return value


def _normalize_requisition_value(raw: object) -> str | None:
    value = _bounded_identity_value(raw)
    if value is None:
        return None
    if "_" in value:
        suffix = value.rsplit("_", 1)[-1].strip()
        if suffix:
            value = suffix
    return value.casefold()


def _normalize_site_value(raw: object) -> str | None:
    value = _bounded_identity_value(raw)
    return value.casefold() if value is not None else None


def _identity_pair_matches(
    key: object,
    value: object,
    expected: WorkdayJobIdentity,
) -> bool | None:
    normalized_key = _identity_key(key)
    if normalized_key in _WORKDAY_JOB_KEYS:
        return _normalize_requisition_value(value) == expected.requisition
    if normalized_key in _WORKDAY_SITE_KEYS:
        return _normalize_site_value(value) == expected.site
    return None


def _query_identity_matches(query: str, expected: WorkdayJobIdentity) -> bool:
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=_MAX_IDENTITY_FIELDS,
        )
    except ValueError:
        return False
    for key, value in pairs:
        match = _identity_pair_matches(key, value, expected)
        if match is False:
            return False
    return True


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
    candidate_suffix = next(
        (suffix for suffix in _WORKDAY_CANDIDATE_SUFFIXES if hostname.endswith(f".{suffix}")),
        None,
    )
    if candidate_suffix is None:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    tenant_labels = hostname[: -(len(candidate_suffix) + 1)].split(".")
    if not tenant_labels or any(
        not label
        or label in _WORKDAY_RESERVED_HOST_LABELS
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", label) is None
        for label in tenant_labels
    ):
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    descriptor = adapter_for_url(candidate)
    if descriptor is None or descriptor.platform != "workday":
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    return hostname


def workday_job_identity(
    url: str,
    *,
    expected_hostname: str | None = None,
) -> WorkdayJobIdentity:
    """Extract one bounded Workday job identity from an exact candidate URL."""

    hostname = workday_public_hostname(url, expected_hostname=expected_hostname)
    try:
        parsed = urlsplit((url or "").strip())
    except ValueError as exc:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
    segments: list[str] = []
    for raw_segment in parsed.path.split("/"):
        if not raw_segment:
            continue
        segment = unquote(raw_segment)
        if (
            not segment
            or segment in {".", ".."}
            or "/" in segment
            or "\\" in segment
            or any(ord(character) < 32 for character in segment)
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        segments.append(segment)
    job_positions = [index for index, segment in enumerate(segments) if segment.casefold() == "job"]
    if len(job_positions) != 1:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    job_position = job_positions[0]
    if job_position < 1:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    site = segments[job_position - 1].strip()
    if len(site) > 160 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]*", site) is None:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    tail = segments[job_position + 1 :]
    while tail and tail[-1].casefold() in _WORKDAY_ACTION_SUFFIXES:
        tail.pop()
    if not tail:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    requisition = tail[-1].strip()
    if "_" in requisition:
        suffix = requisition.rsplit("_", 1)[-1].strip()
        if suffix:
            requisition = suffix
    if len(requisition) > 160 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]*", requisition) is None:
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    identity = WorkdayJobIdentity(
        hostname=hostname,
        site=site.casefold(),
        requisition=requisition.casefold(),
    )
    if not _query_identity_matches(parsed.query, identity):
        raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
    return identity


def _canonical_final_target(url: str, expected_job: WorkdayJobIdentity) -> str | None:
    try:
        parsed = urlsplit((url or "").strip())
        observed = workday_job_identity(
            url,
            expected_hostname=expected_job.hostname,
        )
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=_MAX_IDENTITY_FIELDS,
        )
    except (ValueError, WorkdayAdapterBlockedError):
        return None
    if observed != expected_job:
        return None
    canonical_query = urlencode(sorted(pairs), doseq=True)
    return parsed._replace(
        scheme="https",
        netloc=expected_job.hostname,
        query=canonical_query,
        fragment="",
    ).geturl()


def workday_v2_request_contract(
    action_url: str,
    method: str,
    expected_job: WorkdayJobIdentity,
) -> WorkdayFinalRequestContract | None:
    """Build a redacted exact POST target bound to one site and requisition."""

    normalized_method = (method or "").strip().upper()
    if normalized_method != "POST":
        return None
    canonical_target = _canonical_final_target(action_url, expected_job)
    if canonical_target is None:
        return None
    material = (
        f"{normalized_method}|{canonical_target}|{expected_job.site}|{expected_job.requisition}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return WorkdayFinalRequestContract(
        job_identity=expected_job,
        method=normalized_method,
        digest=digest,
    )


def workday_v2_final_request_matches(
    contract: WorkdayBoundFinalRequestContract,
    *,
    method: str,
    url: str,
    payload_sha256: str,
) -> bool:
    """Validate one exact target and redacted canonical FormData commitment."""

    observed = workday_v2_request_contract(
        url,
        method,
        contract.target_contract.job_identity,
    )
    try:
        observed_bound = (
            WorkdayBoundFinalRequestContract.bind(observed, payload_sha256)
            if observed is not None
            else None
        )
    except ValueError:
        return False
    return bool(
        observed_bound is not None
        and compare_digest(
            observed_bound.target_contract.digest,
            contract.target_contract.digest,
        )
        and compare_digest(observed_bound.payload_sha256, contract.payload_sha256)
        and compare_digest(observed_bound.digest, contract.digest)
    )


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
            and compare_digest(self.cv_sha256, cv_sha256)
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

    async def commit_final_action(
        self,
        expectation: WorkdayFinalCommitExpectation,
    ) -> WorkdayFinalActionReceipt:
        """Atomically revalidate and release one exact irreversible request."""

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
    job_identity: WorkdayJobIdentity
    observed_fields: tuple[FormFieldV1, ...]
    step_field_counts: tuple[int, ...]
    attachment_proof: WorkdayAttachmentProof
    final_action_binding: str
    request_contract: WorkdayFinalRequestContract
    pre_action_html: str
    pre_action_digest: str
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


def _visible_confirmation_reference(soup: BeautifulSoup) -> str | None:
    try:
        matches = [
            str(element.get("data-application-id", "")).strip()
            for element in soup.select(WORKDAY_CONFIRMATION_SELECTOR)
            if _visible(element) and bool(str(element.get("data-application-id", "")).strip())
        ]
    except Exception:
        return None
    return matches[0] if len(matches) == 1 else None


def _has_one_visible_confirmation_reference(soup: BeautifulSoup) -> bool:
    return _visible_confirmation_reference(soup) is not None


def _final_control_is_actionable(element: Tag) -> bool:
    """Fail closed on static states that make the retained final control inert."""

    if element.name != "button" or element.has_attr("disabled") or not _visible(element):
        return False
    current: Tag | None = element
    while current is not None:
        if (
            current.has_attr("inert")
            or str(current.get("aria-disabled", "")).strip().casefold() == "true"
            or (current.name == "fieldset" and current.has_attr("disabled"))
        ):
            return False
        style = str(current.get("style", "")).replace(" ", "").casefold()
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


def _visible_review_contract(soup: BeautifulSoup) -> tuple[Tag, Tag] | None:
    try:
        reviews = [
            element
            for element in soup.select('[data-automation-id="reviewPage"]')
            if isinstance(element, Tag) and _visible(element)
        ]
        if len(reviews) != 1:
            return None
        submits = [
            element
            for element in reviews[0].select('button[data-automation-id="submitApplication"]')
            if isinstance(element, Tag) and _final_control_is_actionable(element)
        ]
    except Exception:
        return None
    return (reviews[0], submits[0]) if len(submits) == 1 else None


def workday_v2_final_action_contract(
    html: str,
    url: str,
    expected_job: WorkdayJobIdentity,
) -> WorkdayFinalRequestContract | None:
    """Return the exact explicit POST contract for the reviewed final control."""

    if len((html or "").encode("utf-8")) > _MAX_FIXTURE_HTML_BYTES:
        return None
    try:
        if workday_job_identity(url, expected_hostname=expected_job.hostname) != expected_job:
            return None
    except WorkdayAdapterBlockedError:
        return None
    soup = BeautifulSoup(html or "", "html.parser")
    contract = _visible_review_contract(soup)
    if contract is None or _has_one_visible_confirmation_reference(soup):
        return None
    review, submit = contract
    if submit.has_attr("form") or submit.has_attr("formaction") or submit.has_attr("formmethod"):
        return None
    button_type = str(submit.get("type", "submit")).strip().casefold()
    if button_type != "submit":
        return None
    form = review if review.name == "form" else submit.find_parent("form")
    if not isinstance(form, Tag):
        return None
    action = str(form.get("action", "")).strip()
    method = str(form.get("method", "")).strip()
    encoding = str(form.get("enctype", "")).strip().casefold()
    if not action or not method or encoding != "multipart/form-data":
        return None
    return workday_v2_request_contract(
        urljoin(url, action),
        method,
        expected_job,
    )


def workday_v2_final_action_binding(
    html: str,
    url: str,
    expected_job: WorkdayJobIdentity,
) -> str | None:
    """Return a redacted digest for the exact validated final-action target."""

    contract = workday_v2_final_action_contract(html, url, expected_job)
    return contract.digest if contract is not None else None


def workday_v2_final_action_ready(
    html: str,
    url: str,
    expected_job: WorkdayJobIdentity,
) -> bool:
    """Validate the exact visible review container and its bound action."""

    return workday_v2_final_action_binding(html, url, expected_job) is not None


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

    if _visible_review_contract(soup) is not None:
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
    field_type = supported.get(raw)
    if field_type is None:
        raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
    return field_type


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
    final_action_binding: str | None = None,
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
    if final_action_binding is not None:
        if re.fullmatch(r"[0-9a-f]{64}", final_action_binding) is None:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        payload["final_action_binding"] = final_action_binding
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
        or not compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256)
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
            workday_job_identity(url)
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
    def _require_snapshot_job(
        snapshot: WorkdayBrowserSnapshot,
        expected: WorkdayJobIdentity,
    ) -> None:
        try:
            observed = workday_job_identity(
                snapshot.url,
                expected_hostname=expected.hostname,
            )
        except WorkdayAdapterBlockedError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
        if observed != expected:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)

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
        private draft so later pages can be observed. It never performs final submit.
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
        expected_job = workday_job_identity(job_url)
        planner = answer_policy or self._answer_policy
        session = self._browser_factory(job_url)
        try:
            await session.navigate(job_url)
            snapshot = await session.snapshot()
            self._require_snapshot_job(snapshot, expected_job)
            assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            if assessment.state is WorkdayPageState.JOB:
                await session.open_candidate_form()
                snapshot = await session.snapshot()
                self._require_snapshot_job(snapshot, expected_job)
                assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            fields: tuple[FormFieldV1, ...] = ()
            decisions: tuple[AnswerDecisionV1, ...] = ()
            step_field_counts: tuple[int, ...] = ()
            proof: WorkdayAttachmentProof | None = None
            audit_identity: tuple[str, str, str, str] | None = None
            plan_blockers: tuple[ReasonCode, ...] = ()
            inspection_complete = False
            final_action_binding: str | None = None
            locale = snapshot.locale

            for _step in range(_MAX_REVERSIBLE_STEPS):
                reason = self._inspection_reason(assessment)
                if reason is not None:
                    raise WorkdayAdapterBlockedError(reason)
                if assessment.state is WorkdayPageState.REVIEW:
                    if not fields or proof is None:
                        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    final_request_contract = workday_v2_final_action_contract(
                        snapshot.html,
                        snapshot.url,
                        expected_job,
                    )
                    if final_request_contract is None:
                        raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    final_action_binding = final_request_contract.digest
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
                self._require_snapshot_job(snapshot, expected_job)
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
            if inspection_complete and final_action_binding is None:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
            fingerprint = workday_v2_form_fingerprint(
                fields,
                step_field_counts,
                final_action_binding,
            )
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
        """Fill and prepare the exact Submit control, but never perform it."""

        now = self._clock()
        if not self._preflight_binding_valid(plan, permit, now=now):
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)
        if (
            context is None
            or context.selected_cv_id != plan.selected_cv_id
            or not compare_digest(context.selected_cv_hash, plan.selected_cv_hash)
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            expected_job = workday_job_identity(context.normalized_job_url)
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
            self._require_snapshot_job(snapshot, expected_job)
            assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            if assessment.state is WorkdayPageState.JOB:
                await session.open_candidate_form()
                snapshot = await session.snapshot()
                self._require_snapshot_job(snapshot, expected_job)
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
                self._require_snapshot_job(snapshot, expected_job)
                assessment = assess_workday_v2_snapshot(snapshot.html, snapshot.url)
            else:
                return NeedsReviewOutcome(reason_code=ReasonCode.SELECTOR_DRIFT)

            final_request_contract = workday_v2_final_action_contract(
                snapshot.html,
                snapshot.url,
                expected_job,
            )
            if (
                assessment.state is not WorkdayPageState.REVIEW
                or final_request_contract is None
                or tuple(observed_fields) != plan.fields
            ):
                return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
            final_action_binding = final_request_contract.digest
            if not compare_digest(
                workday_v2_form_fingerprint(
                    observed_fields,
                    step_field_counts,
                    final_action_binding,
                ),
                plan.form_fingerprint,
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
                job_identity=expected_job,
                observed_fields=observed_fields,
                step_field_counts=step_field_counts,
                attachment_proof=proof,
                final_action_binding=final_action_binding,
                request_contract=final_request_contract,
                pre_action_html=pre_action.html,
                pre_action_digest=hashlib.sha256(pre_action.html.encode("utf-8")).hexdigest(),
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
        """Perform one atomic final action and require exact employer evidence."""

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

        try:
            attachment_proof = await state.session.verify_resume_attachment(
                cv_id=state.plan.selected_cv_id,
                expected_sha256=state.plan.selected_cv_hash,
            )
            pre_click = await state.session.snapshot()
        except WorkdayAdapterBlockedError as exc:
            return NeedsReviewOutcome(reason_code=exc.reason_code)
        except Exception:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.INTERNAL_ERROR)

        if (
            not attachment_proof.matches(
                cv_id=state.plan.selected_cv_id,
                cv_sha256=state.plan.selected_cv_hash,
            )
            or attachment_proof.cv_id != state.attachment_proof.cv_id
            or attachment_proof.upload_complete is not state.attachment_proof.upload_complete
            or not compare_digest(
                attachment_proof.cv_sha256,
                state.attachment_proof.cv_sha256,
            )
            or attachment_proof.receipt_sha256 is None
            or state.attachment_proof.receipt_sha256 is None
            or not compare_digest(
                attachment_proof.receipt_sha256,
                state.attachment_proof.receipt_sha256,
            )
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            self._require_snapshot_job(pre_click, state.job_identity)
        except WorkdayAdapterBlockedError:
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
        assessment = assess_workday_v2_snapshot(pre_click.html, pre_click.url)
        if assessment.state is not WorkdayPageState.REVIEW:
            return NeedsReviewOutcome(
                reason_code=assessment.reason_code or ReasonCode.FORM_CHANGED,
            )
        current_request_contract = workday_v2_final_action_contract(
            pre_click.html,
            pre_click.url,
            state.job_identity,
        )
        if (
            current_request_contract is None
            or not compare_digest(
                current_request_contract.digest,
                state.final_action_binding,
            )
            or not compare_digest(
                current_request_contract.digest,
                state.request_contract.digest,
            )
        ):
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
        current_action_binding = current_request_contract.digest
        current_digest = hashlib.sha256(pre_click.html.encode("utf-8")).hexdigest()
        if not compare_digest(current_digest, state.pre_action_digest):
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)
        try:
            exact_form_binding = state.observed_fields == state.plan.fields and compare_digest(
                workday_v2_form_fingerprint(
                    state.observed_fields,
                    state.step_field_counts,
                    current_action_binding,
                ),
                state.plan.form_fingerprint,
            )
        except WorkdayAdapterBlockedError:
            exact_form_binding = False
        if not exact_form_binding:
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)

        final_action_at = self._clock()
        try:
            binding_valid = (
                action.binds(state.plan, permit, at=final_action_at)
                and permit == state.permit
                and state.plan.ready_for_permit_at(final_action_at)
            )
        except ValueError:
            binding_valid = False
        if not binding_valid:
            return FailedBeforeCommitOutcome(reason_code=ReasonCode.PERMIT_BINDING_MISMATCH)

        try:
            commit_expectation = WorkdayFinalCommitExpectation(
                job_identity=state.job_identity,
                pre_action_digest=state.pre_action_digest,
                form_fingerprint=state.plan.form_fingerprint,
                final_action_binding=state.final_action_binding,
                observed_fields=state.observed_fields,
                step_field_counts=state.step_field_counts,
                answer_bindings=workday_v2_answer_bindings(
                    state.observed_fields,
                    state.plan.decisions,
                    selected_cv_hash=state.plan.selected_cv_hash,
                ),
                selected_cv_id=state.plan.selected_cv_id,
                selected_cv_hash=state.plan.selected_cv_hash,
                attachment_receipt_sha256=state.attachment_proof.receipt_sha256 or "",
                request_contract=state.request_contract,
            )
        except ValueError:
            return NeedsReviewOutcome(reason_code=ReasonCode.FORM_CHANGED)

        state.clicked = True
        try:
            action_receipt = await state.session.commit_final_action(commit_expectation)
        except WorkdayAdapterBlockedError as exc:
            return NeedsReviewOutcome(reason_code=exc.reason_code)
        except WorkdayFinalActionAmbiguousError:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if not compare_digest(
            action_receipt.target_digest,
            state.request_contract.digest,
        ):
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        # A valid receipt means the irreversible request may have left. Every
        # later error is therefore indeterminate and can never become retryable
        # review/failure state.
        try:
            post_action = await state.session.snapshot()
            post_soup = BeautifulSoup(post_action.html, "html.parser")
            snapshot_reference = _visible_confirmation_reference(post_soup)
            employer_reference = await state.session.confirmation_reference()
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        try:
            self._require_snapshot_job(post_action, state.job_identity)
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        try:
            assessment = assess_workday_v2_snapshot(post_action.html, post_action.url)
            references_match = bool(
                snapshot_reference is not None
                and employer_reference
                and compare_digest(
                    snapshot_reference.encode("utf-8"),
                    employer_reference.strip().encode("utf-8"),
                )
            )
        except Exception:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if assessment.state is WorkdayPageState.ALREADY_APPLIED:
            return AlreadyAppliedOutcome()
        if assessment.state is WorkdayPageState.CHALLENGE:
            return UnknownOutcome(reason_code=ReasonCode.CHALLENGE_DETECTED)
        if assessment.state is WorkdayPageState.LOGIN:
            return UnknownOutcome(reason_code=ReasonCode.SESSION_EXPIRED)
        if assessment.state is not WorkdayPageState.CONFIRMATION:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
        if not references_match or snapshot_reference is None:
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

        try:
            pre_soup = BeautifulSoup(state.pre_action_html, "html.parser")
            redacted_reference = hashlib.sha256(snapshot_reference.encode("utf-8")).hexdigest()
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
                    _has_one_visible_confirmation_reference(post_soup) and bool(snapshot_reference)
                ),
            )
            confirmation = verify_submission_evidence(
                expectation,
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
