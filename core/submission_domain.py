"""Immutable domain contracts for evidence-verified submission attempts.

These types deliberately contain no ORM or browser behavior.  They are the
boundary between form inspection, operator review, the irreversible commit,
and evidence reconciliation.  Free-text external errors and raw page content
do not belong in this module.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

_MAX_FORM_PLAN_LIFETIME = timedelta(minutes=30)
_MAX_ANSWER_TEXT_LENGTH = 2_000
_SAFE_FORM_PATTERN = re.compile(r"^[A-Za-z0-9\\\[\]\{\}\^\$\.\+\*\?\-,_:/ @]+$")

BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
EvidenceReference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
LongBoundedText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AttemptStage(StrEnum):
    """Operational progression of one submission attempt."""

    QUEUED = "queued"
    INSPECTING = "inspecting"
    PREPARING = "preparing"
    READY = "ready"
    COMMITTING = "committing"
    VERIFYING = "verifying"
    FINISHED = "finished"


class AttemptOutcome(StrEnum):
    """Terminal business outcome, kept separate from operational stage."""

    CONFIRMED_SUBMITTED = "confirmed_submitted"
    ALREADY_APPLIED = "already_applied"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    FAILED_BEFORE_COMMIT = "failed_before_commit"
    DRAFT_ONLY = "draft_only"
    OPERATOR_CONFIRMED = "operator_confirmed"
    LEGACY_UNVERIFIED = "legacy_unverified"


class ReasonCode(StrEnum):
    """Bounded, stable reason codes safe for storage and metrics labels."""

    RUNTIME_NOT_READY = "RUNTIME_NOT_READY"
    BUILD_MISMATCH = "BUILD_MISMATCH"
    ADAPTER_NOT_QUALIFIED = "ADAPTER_NOT_QUALIFIED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    CHALLENGE_DETECTED = "CHALLENGE_DETECTED"
    FORM_CHANGED = "FORM_CHANGED"
    REQUIRED_FIELD_UNKNOWN = "REQUIRED_FIELD_UNKNOWN"
    ATTACHMENT_UNVERIFIED = "ATTACHMENT_UNVERIFIED"
    FINAL_ACTION_UNCONFIRMED = "FINAL_ACTION_UNCONFIRMED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    JOB_CLOSED = "JOB_CLOSED"
    SELECTOR_DRIFT = "SELECTOR_DRIFT"
    STALE_INDETERMINATE = "STALE_INDETERMINATE"
    DRY_RUN_DISCARDED = "DRY_RUN_DISCARDED"
    DRAFT_ONLY = "DRAFT_ONLY"
    PERMIT_MISSING = "PERMIT_MISSING"
    PERMIT_EXPIRED = "PERMIT_EXPIRED"
    PERMIT_REPLAYED = "PERMIT_REPLAYED"
    PERMIT_BINDING_MISMATCH = "PERMIT_BINDING_MISMATCH"
    COMMAND_EXPIRED = "COMMAND_EXPIRED"
    COMMAND_REPLAYED = "COMMAND_REPLAYED"
    GOVERNOR_DENIED = "GOVERNOR_DENIED"
    OPERATOR_CANCELLED = "OPERATOR_CANCELLED"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    UNSUPPORTED_CONTROL = "UNSUPPORTED_CONTROL"
    NETWORK_ERROR = "NETWORK_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PROFILE_VERSION_NOT_FOUND = "PROFILE_VERSION_NOT_FOUND"
    PROFILE_SNAPSHOT_INVALID = "PROFILE_SNAPSHOT_INVALID"


class FieldType(StrEnum):
    """Supported observed browser control types."""

    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    DATE = "date"
    NUMBER = "number"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    FILE = "file"
    CONSENT = "consent"
    ATTESTATION = "attestation"
    UNKNOWN = "unknown"


class SensitiveCategory(StrEnum):
    """Questions whose factual answers cannot be synthesized by an LLM."""

    AUTHORIZATION = "authorization"
    SPONSORSHIP = "sponsorship"
    NATIONALITY = "nationality"
    CITIZENSHIP = "citizenship"
    CLEARANCE = "clearance"
    LICENSING = "licensing"
    CERTIFICATION = "certification"
    DEMOGRAPHIC = "demographic"
    CONSENT = "consent"
    ATTESTATION = "attestation"


class AnswerDisposition(StrEnum):
    RESOLVED = "resolved"
    ABSTAINED = "abstained"
    OPERATOR_REQUIRED = "operator_required"


class AnswerProvenance(StrEnum):
    """Ordered, auditable answer sources from the form-resolution policy."""

    DETERMINISTIC_IDENTITY = "deterministic_identity"
    USER_CONFIRMED = "user_confirmed"
    OPERATOR_APPROVED_REUSABLE = "operator_approved_reusable"
    CV_EVIDENCE = "cv_evidence"
    LOCAL_LLM = "local_llm"
    ABSTAINED = "abstained"


class EvidenceType(StrEnum):
    """Employer-side evidence types that can be independently verified."""

    EMPLOYER_APPLICATION_ID = "employer_application_id"
    API_RECEIPT = "api_receipt"
    CANDIDATE_PORTAL_RECORD = "candidate_portal_record"
    VISIBLE_POST_CLICK_CONFIRMATION = "visible_post_click_confirmation"


class _FrozenDomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FormOptionV1(_FrozenDomainModel):
    """One exact option observed in a form control."""

    option_id: BoundedText | None = None
    value: BoundedText
    label: LongBoundedText
    disabled: bool = False


class FormFieldConstraintsV1(_FrozenDomainModel):
    """Structured browser constraints; no arbitrary mutable dictionaries."""

    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    min_value: float | None = None
    max_value: float | None = None
    pattern: BoundedText | None = None
    accepted_file_types: tuple[BoundedText, ...] = ()
    max_file_bytes: int | None = Field(default=None, gt=0)
    multiple: bool = False

    @model_validator(mode="after")
    def validate_ranges(self) -> FormFieldConstraintsV1:
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot exceed max_length")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value cannot exceed max_value")
        return self


class FormFieldV1(_FrozenDomainModel):
    """A single observed form field with exact options and constraints."""

    field_id: BoundedText
    canonical_name: BoundedText | None = None
    label: LongBoundedText
    field_type: FieldType
    required: bool
    position: int = Field(ge=0)
    options: tuple[FormOptionV1, ...] = ()
    constraints: FormFieldConstraintsV1 = Field(default_factory=FormFieldConstraintsV1)
    sensitive_category: SensitiveCategory | None = None

    @model_validator(mode="after")
    def validate_options(self) -> FormFieldV1:
        option_values = [option.value for option in self.options]
        if len(option_values) != len(set(option_values)):
            raise ValueError("form option values must be unique within a field")
        if self.field_type in {FieldType.SELECT, FieldType.MULTI_SELECT, FieldType.RADIO}:
            if not self.options:
                raise ValueError(f"{self.field_type.value} fields must expose exact options")
        elif self.options:
            raise ValueError(f"{self.field_type.value} fields cannot contain options")
        control_sensitive_category = {
            FieldType.CONSENT: SensitiveCategory.CONSENT,
            FieldType.ATTESTATION: SensitiveCategory.ATTESTATION,
        }
        required_category = control_sensitive_category.get(self.field_type)
        if required_category is not None and self.sensitive_category != required_category:
            raise ValueError(
                f"{self.field_type.value} fields require the matching sensitive category"
            )
        if (
            self.sensitive_category
            in {
                SensitiveCategory.CONSENT,
                SensitiveCategory.ATTESTATION,
            }
            and required_category != self.sensitive_category
        ):
            raise ValueError(
                "consent and attestation sensitivity must match the observed control type"
            )
        return self


AnswerValue: TypeAlias = str | bool | int | float | tuple[str, ...]

_BOOLEAN_FIELD_TYPES = frozenset(
    {
        FieldType.CHECKBOX,
        FieldType.CONSENT,
        FieldType.ATTESTATION,
    }
)
_TEXT_FIELD_TYPES = frozenset(
    {
        FieldType.TEXT,
        FieldType.TEXTAREA,
        FieldType.DATE,
        FieldType.EMAIL,
        FieldType.PHONE,
        FieldType.URL,
        FieldType.FILE,
    }
)


def _matches_bounded_form_pattern(pattern: str, value: str) -> bool:
    """Evaluate only a deliberately small, non-nesting HTML-pattern subset."""

    if len(pattern) > 128 or _SAFE_FORM_PATTERN.fullmatch(pattern) is None:
        return False
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        return False


def _validate_resolved_field_value(field: FormFieldV1, value: AnswerValue) -> None:
    """Reject cross-type values before any adapter can interpret truthiness."""

    if field.field_type in _BOOLEAN_FIELD_TYPES:
        if type(value) is not bool:
            raise ValueError(f"{field.field_type.value} answers must be boolean")
        return

    if field.field_type == FieldType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("number answers must be finite numeric values")
        try:
            numeric_value = float(value)
        except OverflowError as exc:
            raise ValueError("number answers must be finite numeric values") from exc
        if not math.isfinite(numeric_value):
            raise ValueError("number answers must be finite numeric values")
        constraints = field.constraints
        if constraints.min_value is not None and numeric_value < constraints.min_value:
            raise ValueError("number answer is below the observed minimum")
        if constraints.max_value is not None and numeric_value > constraints.max_value:
            raise ValueError("number answer exceeds the observed maximum")
        return

    if field.field_type in _TEXT_FIELD_TYPES:
        if not isinstance(value, str):
            raise ValueError(f"{field.field_type.value} answers must be strings")
        constraints = field.constraints
        length = len(value)
        if length > _MAX_ANSWER_TEXT_LENGTH:
            raise ValueError("string answer exceeds the bounded domain maximum")
        if constraints.min_length is not None and length < constraints.min_length:
            raise ValueError("string answer is shorter than the observed minimum")
        if constraints.max_length is not None and length > constraints.max_length:
            raise ValueError("string answer exceeds the observed maximum")
        if constraints.pattern is not None and not _matches_bounded_form_pattern(
            constraints.pattern,
            value,
        ):
            raise ValueError("string answer does not match a safe observed pattern")
        return

    if field.field_type in {FieldType.SELECT, FieldType.RADIO}:
        if not isinstance(value, str):
            raise ValueError(f"{field.field_type.value} answers must be strings")
        return

    if field.field_type == FieldType.MULTI_SELECT:
        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise ValueError("multi-select answers must be a tuple of strings")
        return

    raise ValueError("unknown controls cannot contain resolved answers")


class AnswerDecisionV1(_FrozenDomainModel):
    """An auditable answer or explicit abstention for one observed field."""

    field_id: BoundedText
    disposition: AnswerDisposition
    provenance: AnswerProvenance
    value: AnswerValue | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_refs: tuple[BoundedText, ...] = ()
    reason_code: ReasonCode | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> AnswerDecisionV1:
        if self.disposition == AnswerDisposition.RESOLVED:
            if self.value is None:
                raise ValueError("resolved answers require a value")
            if isinstance(self.value, str) and not self.value.strip():
                raise ValueError("resolved string answers cannot be blank")
            if isinstance(self.value, tuple) and (
                not self.value or any(not item.strip() for item in self.value)
            ):
                raise ValueError("resolved multi-value answers cannot be empty or blank")
            if self.provenance == AnswerProvenance.ABSTAINED:
                raise ValueError("resolved answers cannot have abstained provenance")
            if self.reason_code is not None:
                raise ValueError("resolved answers cannot carry a blocker reason")
        else:
            if self.value is not None:
                raise ValueError("abstained/operator-required decisions cannot contain an answer")
            if self.provenance != AnswerProvenance.ABSTAINED:
                raise ValueError("non-resolved decisions require abstained provenance")
            if self.reason_code is None:
                raise ValueError("non-resolved decisions require a bounded reason code")
        return self


class FormPlanV1(_FrozenDomainModel):
    """Immutable reviewed snapshot that expires after at most 30 minutes."""

    plan_id: UUID
    application_id: PositiveInt
    application_revision: PositiveInt
    adapter_name: BoundedText
    adapter_version: BoundedText
    selector_version: BoundedText
    form_fingerprint: Sha256Digest
    selected_cv_id: BoundedText
    selected_cv_hash: Sha256Digest
    attached_cv_id: BoundedText
    attached_cv_hash: Sha256Digest
    attachment_verified: bool
    profile_version: PositiveInt
    session_verified_at: AwareDatetime
    created_at: AwareDatetime
    expires_at: AwareDatetime
    fields: tuple[FormFieldV1, ...]
    decisions: tuple[AnswerDecisionV1, ...]
    blockers: tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def validate_plan(self) -> FormPlanV1:
        if self.expires_at <= self.created_at:
            raise ValueError("form plan expiry must be after creation")
        if self.expires_at - self.created_at > _MAX_FORM_PLAN_LIFETIME:
            raise ValueError("form plan lifetime cannot exceed 30 minutes")

        field_by_id = {field.field_id: field for field in self.fields}
        if len(field_by_id) != len(self.fields):
            raise ValueError("form field IDs must be unique")
        decision_by_id = {decision.field_id: decision for decision in self.decisions}
        if len(decision_by_id) != len(self.decisions):
            raise ValueError("answer decision field IDs must be unique")
        unknown_field_ids = set(decision_by_id).difference(field_by_id)
        if unknown_field_ids:
            raise ValueError("answer decisions must reference observed form fields")
        if len(set(self.blockers)) != len(self.blockers):
            raise ValueError("form plan blockers must be unique")

        for field_id, decision in decision_by_id.items():
            field = field_by_id[field_id]
            if (
                field.sensitive_category is not None
                and decision.disposition == AnswerDisposition.RESOLVED
                and decision.provenance
                not in {
                    AnswerProvenance.USER_CONFIRMED,
                    AnswerProvenance.OPERATOR_APPROVED_REUSABLE,
                }
            ):
                raise ValueError("sensitive answers require confirmed operator evidence")
            if (
                field.sensitive_category is not None
                and decision.disposition == AnswerDisposition.RESOLVED
                and not decision.evidence_refs
            ):
                raise ValueError("sensitive answers require at least one evidence reference")
            if decision.disposition == AnswerDisposition.RESOLVED:
                assert decision.value is not None
                _validate_resolved_field_value(field, decision.value)
            if decision.disposition == AnswerDisposition.RESOLVED and field.field_type in {
                FieldType.SELECT,
                FieldType.RADIO,
            }:
                allowed_values = {option.value for option in field.options if not option.disabled}
                if decision.value not in allowed_values:
                    raise ValueError("resolved option does not match an enabled observed option")
            if (
                decision.disposition == AnswerDisposition.RESOLVED
                and field.field_type == FieldType.MULTI_SELECT
            ):
                if not isinstance(decision.value, tuple):
                    raise ValueError("multi-select answers must be a tuple")
                allowed_values = {option.value for option in field.options if not option.disabled}
                if not set(decision.value).issubset(allowed_values):
                    raise ValueError("resolved options do not match enabled observed options")

        unresolved_required = {
            field.field_id
            for field in self.fields
            if field.required
            and (
                field.field_id not in decision_by_id
                or decision_by_id[field.field_id].disposition != AnswerDisposition.RESOLVED
            )
        }
        if unresolved_required and ReasonCode.REQUIRED_FIELD_UNKNOWN not in self.blockers:
            raise ValueError("unresolved required fields require REQUIRED_FIELD_UNKNOWN")
        return self

    def is_expired(self, at: datetime) -> bool:
        """Return true at the expiry boundary; callers must pass an aware time."""

        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("expiry checks require a timezone-aware datetime")
        return at >= self.expires_at

    @property
    def ready_for_permit(self) -> bool:
        """Require reviewed answers, the exact CV attachment, and a live session."""

        return (
            not self.blockers
            and self.attachment_verified
            and self.attached_cv_id == self.selected_cv_id
            and self.attached_cv_hash == self.selected_cv_hash
            and self.created_at <= self.session_verified_at <= self.expires_at
        )

    def ready_for_permit_at(self, at: datetime) -> bool:
        """Require that all review evidence already exists at admission time."""

        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("permit-readiness checks require a timezone-aware datetime")
        return (
            self.ready_for_permit
            and self.created_at <= at < self.expires_at
            and self.session_verified_at <= at
        )


class FinalSubmitPermit(_FrozenDomainModel):
    """One-use capability bound to the exact reviewed external action."""

    attempt_id: PositiveInt
    job_url_hash: Sha256Digest
    application_revision: PositiveInt
    adapter_name: BoundedText
    adapter_version: BoundedText
    selector_version: BoundedText
    form_fingerprint: Sha256Digest
    cv_hash: Sha256Digest
    expires_at: AwareDatetime
    nonce: BoundedText

    def is_expired(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("expiry checks require a timezone-aware datetime")
        return at >= self.expires_at

    def binds(self, plan: FormPlanV1) -> bool:
        """Check every permit field derivable from a reviewed form plan."""

        return (
            plan.ready_for_permit
            and self.application_revision == plan.application_revision
            and self.adapter_name == plan.adapter_name
            and self.adapter_version == plan.adapter_version
            and self.selector_version == plan.selector_version
            and self.form_fingerprint == plan.form_fingerprint
            and self.cv_hash == plan.selected_cv_hash
            and self.expires_at <= plan.expires_at
        )


class PreparedFinalActionV1(_FrozenDomainModel):
    """Ephemeral handle produced after reversible browser preflight.

    It contains no answers or page content. The adapter may use its opaque
    nonce to locate in-memory browser state, but ``commit`` receives no form
    plan and is therefore limited to the already-prepared irreversible action.
    """

    kind: Literal["final_action_ready"] = "final_action_ready"
    attempt_id: PositiveInt
    adapter_name: BoundedText
    adapter_version: BoundedText
    selector_version: BoundedText
    form_fingerprint: Sha256Digest
    attached_cv_hash: Sha256Digest
    prepared_at: AwareDatetime
    expires_at: AwareDatetime
    action_nonce: Sha256Digest

    @model_validator(mode="after")
    def validate_lifetime(self) -> PreparedFinalActionV1:
        if self.expires_at <= self.prepared_at:
            raise ValueError("final-action handle expiry must be after preflight")
        if self.expires_at - self.prepared_at > timedelta(minutes=5):
            raise ValueError("final-action handle lifetime cannot exceed 5 minutes")
        return self

    def binds(
        self,
        plan: FormPlanV1,
        permit: FinalSubmitPermit,
        *,
        at: datetime,
    ) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("final-action binding checks require an aware datetime")
        return (
            at < self.expires_at
            and self.attempt_id == permit.attempt_id
            and self.adapter_name == plan.adapter_name == permit.adapter_name
            and self.adapter_version == plan.adapter_version == permit.adapter_version
            and self.selector_version == plan.selector_version == permit.selector_version
            and self.form_fingerprint == plan.form_fingerprint == permit.form_fingerprint
            and self.attached_cv_hash == plan.attached_cv_hash == permit.cv_hash
            and self.prepared_at <= at
            and self.expires_at <= permit.expires_at
            and permit.binds(plan)
        )


class SubmissionEvidence(_FrozenDomainModel):
    """Redacted employer-side evidence bound to an attempt, form, and CV."""

    attempt_id: PositiveInt
    evidence_type: EvidenceType
    employer_application_id: EvidenceReference | None = None
    api_receipt_id: EvidenceReference | None = None
    candidate_portal_reference: EvidenceReference | None = None
    form_fingerprint: Sha256Digest
    attached_cv_hash: Sha256Digest
    observed_at: AwareDatetime
    digest: Sha256Digest

    @model_validator(mode="after")
    def require_typed_reference(self) -> SubmissionEvidence:
        references = {
            EvidenceType.EMPLOYER_APPLICATION_ID: self.employer_application_id,
            EvidenceType.API_RECEIPT: self.api_receipt_id,
            EvidenceType.CANDIDATE_PORTAL_RECORD: self.candidate_portal_reference,
        }
        populated = [kind for kind, value in references.items() if value is not None]
        if self.evidence_type == EvidenceType.VISIBLE_POST_CLICK_CONFIRMATION:
            if populated:
                raise ValueError("visible confirmation evidence cannot include a typed reference")
        elif references[self.evidence_type] is None or populated != [self.evidence_type]:
            raise ValueError(
                f"{self.evidence_type.value} evidence requires its typed reference "
                "exactly and forbids other references"
            )
        return self


class ConfirmedSubmittedOutcome(_FrozenDomainModel):
    kind: Literal[AttemptOutcome.CONFIRMED_SUBMITTED] = AttemptOutcome.CONFIRMED_SUBMITTED
    evidence: SubmissionEvidence


class AlreadyAppliedOutcome(_FrozenDomainModel):
    kind: Literal[AttemptOutcome.ALREADY_APPLIED] = AttemptOutcome.ALREADY_APPLIED
    reason_code: Literal[ReasonCode.ALREADY_APPLIED] = ReasonCode.ALREADY_APPLIED
    evidence: SubmissionEvidence | None = None


_NEEDS_REVIEW_REASONS = frozenset(
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
_UNKNOWN_REASONS = frozenset(
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
_FAILED_BEFORE_COMMIT_REASONS = frozenset(
    {
        ReasonCode.RUNTIME_NOT_READY,
        ReasonCode.BUILD_MISMATCH,
        ReasonCode.ADAPTER_NOT_QUALIFIED,
        ReasonCode.SESSION_EXPIRED,
        ReasonCode.FORM_CHANGED,
        ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ReasonCode.ATTACHMENT_UNVERIFIED,
        ReasonCode.JOB_CLOSED,
        ReasonCode.SELECTOR_DRIFT,
        ReasonCode.PERMIT_MISSING,
        ReasonCode.PERMIT_EXPIRED,
        ReasonCode.PERMIT_REPLAYED,
        ReasonCode.PERMIT_BINDING_MISMATCH,
        ReasonCode.COMMAND_EXPIRED,
        ReasonCode.COMMAND_REPLAYED,
        ReasonCode.GOVERNOR_DENIED,
        ReasonCode.OPERATOR_CANCELLED,
        ReasonCode.UNSUPPORTED_CONTROL,
        ReasonCode.NETWORK_ERROR,
        ReasonCode.INTERNAL_ERROR,
    }
)


class NeedsReviewOutcome(_FrozenDomainModel):
    kind: Literal[AttemptOutcome.NEEDS_REVIEW] = AttemptOutcome.NEEDS_REVIEW
    reason_code: ReasonCode
    blocked_field_ids: tuple[BoundedText, ...] = ()

    @field_validator("reason_code")
    @classmethod
    def validate_reason(cls, value: ReasonCode) -> ReasonCode:
        if value not in _NEEDS_REVIEW_REASONS:
            raise ValueError("reason code is not valid for a needs-review outcome")
        return value


class UnknownOutcome(_FrozenDomainModel):
    kind: Literal[AttemptOutcome.UNKNOWN] = AttemptOutcome.UNKNOWN
    reason_code: ReasonCode

    @field_validator("reason_code")
    @classmethod
    def validate_reason(cls, value: ReasonCode) -> ReasonCode:
        if value not in _UNKNOWN_REASONS:
            raise ValueError("reason code is not valid for an unknown outcome")
        return value


class FailedBeforeCommitOutcome(_FrozenDomainModel):
    kind: Literal[AttemptOutcome.FAILED_BEFORE_COMMIT] = AttemptOutcome.FAILED_BEFORE_COMMIT
    reason_code: ReasonCode

    @field_validator("reason_code")
    @classmethod
    def validate_reason(cls, value: ReasonCode) -> ReasonCode:
        if value not in _FAILED_BEFORE_COMMIT_REASONS:
            raise ValueError("reason code is not valid for a failed-before-commit outcome")
        return value


class DraftOnlyOutcome(_FrozenDomainModel):
    kind: Literal[AttemptOutcome.DRAFT_ONLY] = AttemptOutcome.DRAFT_ONLY
    reason_code: Literal[ReasonCode.DRY_RUN_DISCARDED, ReasonCode.DRAFT_ONLY] = (
        ReasonCode.DRY_RUN_DISCARDED
    )


CommitOutcome: TypeAlias = Annotated[
    ConfirmedSubmittedOutcome
    | AlreadyAppliedOutcome
    | NeedsReviewOutcome
    | UnknownOutcome
    | FailedBeforeCommitOutcome
    | DraftOnlyOutcome,
    Field(discriminator="kind"),
]

COMMIT_OUTCOME_ADAPTER = TypeAdapter(CommitOutcome)

PreflightOutcome: TypeAlias = Annotated[
    PreparedFinalActionV1
    | AlreadyAppliedOutcome
    | NeedsReviewOutcome
    | FailedBeforeCommitOutcome
    | DraftOnlyOutcome,
    Field(discriminator="kind"),
]

PREFLIGHT_OUTCOME_ADAPTER = TypeAdapter(PreflightOutcome)


def parse_commit_outcome(value: object) -> CommitOutcome:
    """Validate untrusted adapter output against the discriminated contract."""

    return COMMIT_OUTCOME_ADAPTER.validate_python(value)


def parse_preflight_outcome(value: object) -> PreflightOutcome:
    """Reject confirmed/unknown results before the irreversible boundary."""

    return PREFLIGHT_OUTCOME_ADAPTER.validate_python(value)
