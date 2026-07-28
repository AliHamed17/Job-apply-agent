"""Immutable domain contracts for evidence-verified submission attempts.

These types deliberately contain no ORM or browser behavior.  They are the
boundary between form inspection, operator review, the irreversible commit,
and evidence reconciliation.  Free-text external errors and raw page content
do not belong in this module.
"""

from __future__ import annotations

import json
import math
import re
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, TypeAlias
from urllib.parse import urlsplit
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

from core.sensitive_policy import (
    canonical_sensitive_key,
    contains_prompt_injection,
    contains_sensitive_text,
    is_sensitive_fact_key,
    normalize_policy_text,
)
from llm.contracts import (
    FORM_RESOLUTION_PROMPT_VERSION,
    QUALIFIED_LOCAL_LLM_MODEL,
    QUALIFIED_LOCAL_LLM_PROVIDER,
)
from llm.qualification_registry import is_qualified_local_model_identity

_MAX_FORM_PLAN_LIFETIME = timedelta(minutes=30)
_MAX_ANSWER_TEXT_LENGTH = 2_000
_MAX_FORM_FIELDS = 200
_MAX_FORM_DISCLOSURES = 32
_MAX_FORM_OPTIONS = 200
_MAX_ACCEPTED_FILE_TYPES = 32
_MAX_FORM_BLOCKERS = 32
_MAX_EVIDENCE_REFS = 16
_MAX_OBSERVED_FORM_BYTES = 256 * 1024
_MAX_FORM_PLAN_BYTES = 384 * 1024
_MAX_SAFE_PATTERN_CHARS = 128
_MAX_SAFE_PATTERN_REPEAT = 64
_MAX_SAFE_PATTERN_STATES = 256
_SAFE_PATTERN_LITERAL_CHARS = frozenset(string.ascii_letters + string.digits + " _:/@.,%-")
_SAFE_PATTERN_ESCAPED_LITERALS = frozenset(r"\.+-*?{}[]()|^$")
_ASCII_DIGITS = frozenset(string.digits)
_ASCII_WORD = frozenset(string.ascii_letters + string.digits + "_")
_ASCII_SPACE = frozenset(" \t\r\n\f\v")
_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL_VALUE_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_PHONE_VALUE_RE = re.compile(r"^\+?[0-9][0-9 ().-]{5,30}[0-9]$")
_CANONICAL_LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?$")

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
VERIFIED_ATTACHMENT_SENTINEL = "verified_attachment"
VERIFIED_ATTACHMENT_EVIDENCE_REF = "form-plan:verified-attachment"


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
    FORM_PLAN_INCOMPLETE = "FORM_PLAN_INCOMPLETE"
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
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_MODEL_MISSING = "LLM_MODEL_MISSING"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_CIRCUIT_OPEN = "LLM_CIRCUIT_OPEN"
    LLM_SCHEMA_INVALID = "LLM_SCHEMA_INVALID"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    ANSWER_POLICY_CHANGED = "ANSWER_POLICY_CHANGED"


# Public compatibility name used by API and persistence consumers.
SubmissionReasonCode = ReasonCode


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


class DisclosureKind(StrEnum):
    """Bounded candidate-facing notices observed separately from controls."""

    PRIVACY_POLICY = "privacy_policy"
    NO_PRIVACY_POLICY_NOTICE = "no_privacy_policy_notice"
    AI_DISCLOSURE = "ai_disclosure"
    IMPRINT = "imprint"
    DIVERSITY = "diversity"
    INFORMATION = "information"


class DisclosureSource(StrEnum):
    """How a disclosure was presented without persisting its raw target URL."""

    INLINE = "inline"
    LINK = "link"
    SYNTHETIC = "synthetic"


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
    VERIFIED_ATTACHMENT = "verified_attachment"
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
    accepted_file_types: tuple[BoundedText, ...] = Field(
        default=(),
        max_length=_MAX_ACCEPTED_FILE_TYPES,
    )
    max_file_bytes: int | None = Field(default=None, gt=0)
    multiple: bool = False

    @field_validator("min_value", "max_value")
    @classmethod
    def values_are_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("numeric form constraints must be finite")
        return value

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


class FormDisclosureV1(_FrozenDomainModel):
    """One ordered, bounded disclosure included in operator review.

    ``summary`` is public candidate-form text, never an answer or candidate
    value. ``reference_sha256`` binds an inline element, public link target, or
    deterministic no-policy sentinel without persisting a raw URL.
    """

    disclosure_id: BoundedText
    kind: DisclosureKind
    source: DisclosureSource
    position: int = Field(ge=0)
    summary: LongBoundedText
    content_sha256: Sha256Digest
    reference_sha256: Sha256Digest
    acknowledgement_field_id: BoundedText | None = None

    @model_validator(mode="after")
    def validate_disclosure(self) -> FormDisclosureV1:
        expected = sha256(self.summary.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected:
            raise ValueError("disclosure content digest must bind the bounded summary")
        if self.kind is DisclosureKind.NO_PRIVACY_POLICY_NOTICE:
            if (
                self.source is not DisclosureSource.SYNTHETIC
                or self.acknowledgement_field_id is not None
            ):
                raise ValueError("no-policy notice must be synthetic and non-interactive")
        elif self.source is DisclosureSource.SYNTHETIC:
            raise ValueError("only the no-policy notice may be synthetic")
        return self


class FormFieldV1(_FrozenDomainModel):
    """A single observed form field with exact options and constraints."""

    field_id: BoundedText
    canonical_name: BoundedText | None = None
    label: LongBoundedText
    field_type: FieldType
    required: bool
    position: int = Field(ge=0)
    options: tuple[FormOptionV1, ...] = Field(default=(), max_length=_MAX_FORM_OPTIONS)
    constraints: FormFieldConstraintsV1 = Field(default_factory=FormFieldConstraintsV1)
    sensitive_category: SensitiveCategory | None = None

    @model_validator(mode="after")
    def validate_options(self) -> FormFieldV1:
        option_values = [option.value for option in self.options]
        if len(option_values) != len(set(option_values)):
            raise ValueError("form option values must be unique within a field")
        normalized_ids = [
            normalize_policy_text(option.option_id)
            for option in self.options
            if option.option_id is not None
        ]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("form option IDs must be unique within a field")
        alias_owners: dict[str, set[int]] = {}
        for index, option in enumerate(self.options):
            for raw_alias in (option.value, option.label):
                normalized_alias = normalize_policy_text(raw_alias)
                if not normalized_alias:
                    raise ValueError("form option aliases must contain visible text")
                alias_owners.setdefault(normalized_alias, set()).add(index)
        if any(len(owners) > 1 for owners in alias_owners.values()):
            raise ValueError("normalized option values and labels must identify exactly one option")
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


_SENSITIVE_FIELD_MARKERS = (
    "authorization",
    "authorised to work",
    "authorized to work",
    "eligible to work",
    "eligibility to work",
    "employment eligibility",
    "legally eligible",
    "legally entitled",
    "entitled to work",
    "entitled to employment",
    "take employment",
    "lawfully work",
    "lawfully accept employment",
    "legal right to work",
    "legal right to be employed",
    "legal ability to work",
    "work right",
    "employment right",
    "unrestricted work",
    "ability to work legally",
    "allowed to work",
    "permitted to work",
    "permission to work",
    "employment permission",
    "permission to perform this job",
    "permit for employment",
    "employment permit",
    "work permit",
    "sponsorship",
    "employer support",
    "immigration support",
    "visa support",
    "visa",
    "nationality",
    "country of origin",
    "country of citizenship",
    "native country",
    "citizenship",
    "citizen",
    "security clearance",
    "clearance",
    "license",
    "licence",
    "licensed",
    "licensing",
    "certification",
    "certified",
    "certify",
    "certificate number",
    "demographic",
    "gender",
    "pronoun",
    "non-binary",
    "race",
    "ethnicity",
    "disability",
    "veteran",
    "marital",
    "religion",
    "date of birth",
    "sexual orientation",
    "military status",
    "military service",
    "national service",
    "army service",
    "itar",
    "export control",
    "protected person",
    "security vetting",
    "background check",
    "background clearance",
    "bar admission",
    "bar membership",
    "admitted to the bar",
    "professional registration",
    "right of abode",
    "permit to work",
    "sexual identity",
    "indigenous status",
    "aboriginal status",
    "first nations status",
    "protected veteran",
    "conflict of interest",
    "non-compete",
    "restrictive covenant",
    "consent",
    "attestation",
    "attest",
    "מורשה לעבוד",
    "מורשית לעבוד",
    "זכאי לעבוד",
    "זכאית לעבוד",
    "רשאי לעבוד",
    "רשאית לעבוד",
    "מותר לך לעבוד",
    "לעבוד כחוק",
    "זכות לעבוד",
    "יכולת חוקית לעבוד",
    "מניעה חוקית",
    "אישור עבודה",
    "היתר עבודה",
    "היתר העסקה",
    "אשרת עבודה",
    "תמיכת המעסיק",
    "חסות",
    "ויזה",
    "לאום",
    "ארץ מוצא",
    "ארץ המוצא",
    "מדינת מוצא",
    "אזרחות",
    "אזרח",
    "סיווג ביטחוני",
    "סיווג בטחוני",
    "סיווג",
    "רישיון",
    "רשיון",
    "הסמכה",
    "מוסמך",
    "תעודה",
    "דמוגרפ",
    "מגדר",
    "כינוי גוף",
    "כינויי גוף",
    "כינויי הגו",
    "מין",
    "גזע",
    "אתניות",
    "מוגבלות",
    "נכות",
    "מצב משפחתי",
    "דת",
    "גיל",
    "תאריך לידה",
    "נטייה מינית",
    "שירות צבאי",
    "ותיק צבאי",
    "הסכמה",
    "מסכים",
    "הצהרה",
    "מאשר",
)

_PROMPT_INJECTION_MARKERS = (
    "ignore previous instruction",
    "ignore all previous",
    "disregard previous instruction",
    "disregard all previous",
    "override previous instruction",
    "reveal the system prompt",
    "reveal your prompt",
    "system prompt",
    "developer message",
    "jailbreak",
    "act as an ai",
    "act as a system",
    "do not follow your instruction",
    "התעלם מההוראות",
    "התעלמי מההוראות",
    "הוראות קודמות",
    "חשוף את הנחיות המערכת",
    "חשפי את הנחיות המערכת",
)


_CANONICAL_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "email": (
        "email",
        "email address",
        "preferred email",
        "application email",
        "contact email",
        "כתובת דואר אלקטרוני",
        "דואר אלקטרוני מועדף",
        "דואל להגשת מועמדות",
        "דואל ליצירת קשר",
    ),
    "phone": (
        "phone",
        "phone number",
        "mobile",
        "mobile number",
        "telephone",
        "preferred phone",
        "application phone",
        "contact telephone",
        "מספר טלפון",
        "טלפון מועדף",
        "טלפון להגשת מועמדות",
        "טלפון ליצירת קשר",
    ),
    "full_name": (
        "full name",
        "legal display name",
        "candidate name",
        "name for correspondence",
        "שם מלא",
        "שם מלא לתצוגה",
        "שם המועמד",
        "שם להתכתבות",
    ),
    "name": ("name", "full name", "candidate name", "שם", "שם מלא", "שם המועמד"),
    "first_name": (
        "first name",
        "given name",
        "forename",
        "candidate first name",
        "applicant given name",
        "שם פרטי",
        "השם הפרטי",
        "שם פרטי של המועמד",
        "שם פרטי של מגיש המועמדות",
    ),
    "given_name": (
        "first name",
        "given name",
        "forename",
        "candidate first name",
        "applicant given name",
        "שם פרטי",
    ),
    "last_name": (
        "last name",
        "surname",
        "family name",
        "candidate last name",
        "applicant family name",
        "שם משפחה",
        "שם המשפחה",
        "שם משפחה של המועמד",
        "שם משפחה של מגיש המועמדות",
    ),
    "surname": ("last name", "surname", "family name", "שם משפחה"),
    "family_name": ("last name", "surname", "family name", "שם משפחה"),
    "location": ("location", "current location", "address", "מיקום", "כתובת"),
    "city": (
        "city",
        "current city",
        "home city",
        "city of residence",
        "location city",
        "עיר",
        "עיר נוכחית",
        "עיר מגורים",
        "עיר המגורים",
        "עיר לפי מיקום",
    ),
    "linkedin": (
        "linkedin",
        "linkedin profile",
        "linkedin url",
        "profile link",
        "professional profile",
        "candidate profile url",
        "public profile",
        "קישור לפרופיל",
        "פרופיל מקצועי",
        "כתובת פרופיל המועמד",
        "פרופיל ציבורי",
    ),
    "linkedin_url": ("linkedin", "linkedin profile", "linkedin url", "קישור ללינקדאין"),
    "github": ("github", "github profile", "github url", "קישור לגיטהאב"),
    "github_url": ("github", "github profile", "github url", "קישור לגיטהאב"),
    "portfolio": ("portfolio", "portfolio link", "portfolio url", "תיק עבודות"),
    "portfolio_url": ("portfolio", "portfolio link", "portfolio url", "תיק עבודות"),
    "resume": (
        "resume",
        "resume upload",
        "upload resume",
        "upload your resume",
        "cv",
        "curriculum vitae",
        "קורות חיים",
        "העלאת קורות חיים",
    ),
    "resume_upload": (
        "resume",
        "resume upload",
        "upload resume",
        "upload your resume",
        "cv",
        "curriculum vitae",
        "קורות חיים",
        "העלאת קורות חיים",
    ),
    "cv": (
        "cv",
        "cv upload",
        "upload cv",
        "upload your cv",
        "resume",
        "curriculum vitae",
        "קורות חיים",
        "העלאת קורות חיים",
    ),
    "cv_upload": (
        "cv",
        "cv upload",
        "upload cv",
        "upload your cv",
        "resume",
        "curriculum vitae",
        "קורות חיים",
        "העלאת קורות חיים",
    ),
    "primary_language": ("primary programming language", "שפת תכנות עיקרית"),
    "backend_framework": ("backend framework", "מסגרת פיתוח צד שרת"),
    "database_skill": ("database technology", "טכנולוגיית מסדי נתונים"),
    "cloud_platform": ("cloud platform", "פלטפורמת ענן"),
    "container_platform": ("container platform", "פלטפורמת קונטיינרים"),
    "iac_tool": ("infrastructure as code tool", "כלי תשתית כקוד"),
    "data_tool": ("distributed data tool", "כלי נתונים מבוזר"),
    "ml_framework": ("machine learning framework", "מסגרת מודלים"),
    "frontend_language": ("frontend language", "שפת צד לקוח"),
    "frontend_framework": ("frontend framework", "מסגרת פיתוח צד לקוח"),
    "test_framework": ("testing framework", "מסגרת בדיקות"),
    "automation_tool": ("browser automation tool", "כלי אוטומציית דפדפן"),
    "operating_system": ("operating system", "מערכת הפעלה"),
    "embedded_language": ("embedded programming language", "שפת תכנות משובצת"),
    "realtime_system": ("real time operating system", "מערכת הפעלה בזמן אמת"),
    "analytics_tool": ("analytics visualization tool", "כלי המחשה אנליטי"),
    "pipeline_tool": ("data pipeline tool", "כלי צינור נתונים"),
    "api_style": ("api design style", "סגנון תכנון ממשק"),
    "version_control": ("version control system", "מערכת ניהול גרסאות"),
    "highest_degree": ("highest academic degree", "תואר אקדמי גבוה ביותר"),
    "team_leadership": ("team leadership", "have you led a team of engineers"),
    "work_authorization": (
        "work authorization",
        "work authorisation",
        "authorized to work",
        "authorised to work",
        "אישור עבודה",
        "מורשה לעבוד",
    ),
    "work_authorisation": (
        "work authorization",
        "work authorisation",
        "authorized to work",
        "authorised to work",
        "אישור עבודה",
        "מורשה לעבוד",
    ),
    "work_permit": ("work permit", "employment permit", "אישור עבודה כהיתר", "היתר עבודה"),
    "permit_to_work": ("permit to work", "work permit", "היתר עבודה"),
    "right_to_work": (
        "right to work",
        "authorized to work",
        "authorised to work",
        "זכות לעבוד",
        "מורשה לעבוד",
    ),
    "right_of_abode": ("right of abode", "זכות מגורים"),
    "visa_sponsorship": ("visa sponsorship", "חסות לויזה"),
    "sponsorship": ("sponsorship", "employment sponsorship", "חסות", "חסות תעסוקתית"),
    "nationality": (
        "nationality",
        "what is your nationality",
        "מהו הלאום שלך",
        "מהי הלאומיות שלך",
        "לאום",
    ),
    "citizenship": (
        "citizenship",
        "what is your citizenship",
        "מהי האזרחות שלך",
        "אזרחות",
    ),
    "security_clearance": ("security clearance", "סיווג ביטחוני", "סיווג בטחוני"),
    "clearance": ("clearance", "required clearance", "סיווג נדרש", "סיווג"),
    "security_vetting": ("security vetting", "בדיקה ביטחונית"),
    "background_clearance": ("background clearance", "background check"),
    "itar_status": ("itar status", "itar eligibility"),
    "export_control_status": ("export control status", "export control eligibility"),
    "protected_person_status": ("protected person status", "us protected person status"),
    "license": ("license", "professional license", "רישיון", "רישיון מקצועי"),
    "licensing": ("licensing", "licensing status", "מצב רישיון"),
    "bar_admission": ("bar admission", "admitted to the bar"),
    "bar_membership": ("bar membership", "membership in the bar"),
    "professional_registration": ("professional registration", "רישום מקצועי"),
    "certification": ("certification", "professional certification", "הסמכה מקצועית"),
    "certification_status": ("certification status", "הסמכה"),
    "gender": ("gender", "מגדר"),
    "race": ("race", "גזע"),
    "ethnicity": ("ethnicity", "אתניות"),
    "disability": ("disability", "מוגבלות"),
    "veteran_status": ("veteran status", "military service", "שירות צבאי"),
    "marital_status": ("marital status", "מצב משפחתי"),
    "religion": ("religion", "דת"),
    "age": ("age", "גיל"),
    "sexual_orientation": ("sexual orientation", "נטייה מינית"),
    "sexual_identity": ("sexual identity", "זהות מינית"),
    "indigenous_status": ("indigenous status", "מעמד ילידי"),
    "national_service": ("national service", "שירות לאומי"),
    "army_service": ("army service", "שירות צבאי"),
    "consent": ("consent", "privacy consent", "הסכמה"),
    "attestation": ("attestation", "applicant declaration", "הצהרת המועמד"),
}

_SEMANTIC_LABEL_WRAPPERS = (
    "{alias}",
    "your {alias}",
    "candidate {alias}",
    "enter {alias}",
    "please enter {alias}",
    "provide {alias}",
    "please provide {alias}",
    "select {alias}",
    "please select {alias}",
    "choose {alias}",
    "what is your {alias}",
    "what is the {alias}",
    "do you have {alias}",
    "do you hold {alias}",
    "are you {alias}",
    "have you {alias}",
    "required {alias}",
)


def _normalize_field_semantics(value: str) -> str:
    normalized = normalize_policy_text(value)
    return re.sub(r"[^\w\u0590-\u05ff]+", " ", normalized).strip()


def is_canonical_locale(value: str) -> bool:
    """Accept only a bounded canonical language/script/region token."""

    return bool(
        isinstance(value, str) and len(value) <= 32 and _CANONICAL_LOCALE_RE.fullmatch(value)
    )


def field_canonical_label_compatible(field: FormFieldV1) -> bool:
    """Require an exact reviewed semantic match before trusting an ATS canonical key."""

    if not field.canonical_name:
        return True
    canonical = canonical_sensitive_key(field.canonical_name)
    if not canonical:
        return False
    label = _normalize_field_semantics(field.label)
    canonical_phrase = _normalize_field_semantics(canonical.replace("_", " "))
    aliases = _CANONICAL_LABEL_ALIASES.get(canonical, (canonical_phrase,))
    approved = {
        _normalize_field_semantics(wrapper.format(alias=_normalize_field_semantics(alias)))
        for alias in aliases
        for wrapper in _SEMANTIC_LABEL_WRAPPERS
    }
    return label in approved


def field_has_reviewed_canonical_name(field: FormFieldV1) -> bool:
    """Whether the canonical fact name belongs to the reviewed semantic registry."""

    return bool(
        field.canonical_name
        and canonical_sensitive_key(field.canonical_name) in _CANONICAL_LABEL_ALIASES
    )


def field_is_reviewed_cv_attachment(field: FormFieldV1) -> bool:
    """Recognize only reviewed resume/CV upload controls."""

    if field.field_type != FieldType.FILE:
        return False
    canonical = canonical_sensitive_key(field.canonical_name or "")
    if canonical:
        return canonical in {"resume", "resume_upload", "cv", "cv_upload"} and (
            field_canonical_label_compatible(field)
        )
    label = _normalize_field_semantics(field.label)
    approved = {
        _normalize_field_semantics(wrapper.format(alias=_normalize_field_semantics(alias)))
        for canonical_name in ("resume", "cv")
        for alias in _CANONICAL_LABEL_ALIASES[canonical_name]
        for wrapper in _SEMANTIC_LABEL_WRAPPERS
    }
    return label in approved


def _canonical_sensitive_category(canonical_name: str) -> SensitiveCategory | None:
    key = canonical_sensitive_key(canonical_name)
    if not key or not is_sensitive_fact_key(key):
        return None
    if "consent" in key or key in {"privacy", "terms_accepted"}:
        return SensitiveCategory.CONSENT
    if "attest" in key or "declaration" in key:
        return SensitiveCategory.ATTESTATION
    if any(marker in key for marker in ("sponsor", "visa")):
        return SensitiveCategory.SPONSORSHIP
    if any(marker in key for marker in ("nationality", "national_origin", "country_of_origin")):
        return SensitiveCategory.NATIONALITY
    if "citizen" in key or "country_of_citizenship" in key:
        return SensitiveCategory.CITIZENSHIP
    if any(
        marker in key
        for marker in (
            "clearance",
            "security_vetting",
            "background_check",
            "itar",
            "export_control",
            "protected_person",
        )
    ):
        return SensitiveCategory.CLEARANCE
    if any(
        marker in key
        for marker in (
            "license",
            "licensing",
            "bar_admission",
            "bar_membership",
            "professional_registration",
        )
    ):
        return SensitiveCategory.LICENSING
    if "certif" in key or "credential" in key:
        return SensitiveCategory.CERTIFICATION
    if any(
        marker in key
        for marker in (
            "authorization",
            "authorisation",
            "right_to_work",
            "right_of_abode",
            "work_permit",
            "permit_to_work",
            "employment_eligibility",
            "eligible_to_work",
        )
    ):
        return SensitiveCategory.AUTHORIZATION
    if any(
        marker in key
        for marker in (
            "age",
            "birth",
            "demographic",
            "disab",
            "ethnic",
            "gender",
            "indigenous",
            "marital",
            "military",
            "national_service",
            "army_service",
            "pronoun",
            "race",
            "religion",
            "sexual",
            "veteran",
        )
    ):
        return SensitiveCategory.DEMOGRAPHIC
    return None


def field_has_scoped_sensitive_semantics(field: FormFieldV1) -> bool:
    """Prove that canonical key, observed label, and sensitive category agree."""

    control_category = {
        FieldType.CONSENT: SensitiveCategory.CONSENT,
        FieldType.ATTESTATION: SensitiveCategory.ATTESTATION,
    }.get(field.field_type)
    if control_category is not None:
        return field.sensitive_category == control_category
    if not field.canonical_name or not field_canonical_label_compatible(field):
        return False
    expected = _canonical_sensitive_category(field.canonical_name)
    return expected is not None and field.sensitive_category in {None, expected}


def _field_policy_text(field: FormFieldV1) -> str:
    raw_parts = [field.canonical_name or "", field.label]
    raw_parts.extend(
        text
        for option in field.options
        for text in (option.option_id or "", option.value, option.label)
    )
    # Also preserve each option attribute as one contiguous bounded sequence.
    # Otherwise an attacker can split "ignore previous instructions" across
    # adjacent labels while benign value tokens interrupt the aggregate.
    raw_parts.extend(option.option_id or "" for option in field.options)
    raw_parts.extend(option.value for option in field.options)
    raw_parts.extend(option.label for option in field.options)
    raw_parts.extend(
        [
            field.constraints.pattern or "",
            *field.constraints.accepted_file_types,
        ]
    )
    raw = " ".join(raw_parts).casefold().replace("_", " ")
    # ATS labels often spell inclusive Hebrew suffixes as רשאי/ת, זכאי.ת,
    # or את/ה. Removing only these join punctuation characters yields their
    # normal lexical forms without making the match language-dependent.
    return re.sub(r"[/.\u00b7]", "", raw)


def _has_bounded_option_phrase(value: str, phrase: str) -> bool:
    return bool(
        re.search(
            rf"(?<![\w\u0590-\u05ff]){re.escape(phrase)}(?![\w\u0590-\u05ff])",
            value,
        )
    )


def _option_set_indicates_protected_class(field: FormFieldV1) -> bool:
    """Recognize EEO option-set signatures without global single-word matches."""

    option_texts = tuple(
        _normalize_field_semantics(" ".join((option.option_id or "", option.value, option.label)))
        for option in field.options
        if not option.disabled
    )

    def matches(markers: tuple[str, ...]) -> set[str]:
        return {
            marker
            for marker in markers
            if any(_has_bounded_option_phrase(text, marker) for text in option_texts)
        }

    race = matches(
        (
            "hispanic or latino",
            "black or african american",
            "native hawaiian",
            "pacific islander",
            "middle eastern or north african",
            "american indian",
            "alaska native",
            "two or more races",
            "white",
            "black",
            "asian",
        )
    )
    religion = matches(
        (
            "jewish",
            "christian",
            "muslim",
            "hindu",
            "atheist",
            "buddhist",
            "sikh",
            "no religion",
        )
    )
    gender = matches(
        (
            "man",
            "woman",
            "male",
            "female",
            "non binary",
            "transgender man",
            "transgender woman",
        )
    )
    return (
        (len(race) >= 2 and bool(race.difference({"white", "black"})))
        or len(religion) >= 2
        or len(gender) >= 2
    )


def _option_set_has_injection_signature(field: FormFieldV1) -> bool:
    """Detect hostile instruction tokens split across option properties."""

    aggregate = normalize_policy_text(
        " ".join(
            text
            for option in field.options
            for text in (option.option_id or "", option.value, option.label)
        )
    )
    tokens = set(re.findall(r"[a-z]+", aggregate))
    return bool(
        tokens.intersection({"ignore", "disregard", "override"})
        and tokens.intersection({"previous", "prior", "system", "developer"})
        and tokens.intersection({"instruction", "instructions", "prompt", "message"})
    )


def field_is_sensitive(field: FormFieldV1) -> bool:
    """Fail closed on typed, canonical, or label evidence of a sensitive field."""
    if field.sensitive_category is not None or field.field_type in {
        FieldType.CONSENT,
        FieldType.ATTESTATION,
    }:
        return True
    if field.canonical_name and is_sensitive_fact_key(field.canonical_name):
        return True
    if _option_set_indicates_protected_class(field):
        return True
    text = _field_policy_text(field)
    return (
        contains_sensitive_text(text)
        or any(marker in text for marker in _SENSITIVE_FIELD_MARKERS)
        or bool(re.search(r"\b(?:age|sex)\b", text))
    )


def field_requires_operator_review(field: FormFieldV1) -> bool:
    """Block sensitive or adversarial field instructions from automation."""

    if field_is_sensitive(field):
        return True
    if _option_set_has_injection_signature(field):
        return True
    text = _field_policy_text(field)
    return contains_prompt_injection(text) or any(
        marker in text for marker in _PROMPT_INJECTION_MARKERS
    )


def observed_form_fields_are_bounded(fields: tuple[FormFieldV1, ...]) -> bool:
    """Bound untrusted ATS form observations before any resolver or model call."""

    if len(fields) > _MAX_FORM_FIELDS:
        return False
    payload = [field.model_dump(mode="json") for field in fields]
    return (
        len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= _MAX_OBSERVED_FORM_BYTES
    )


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


@dataclass(frozen=True, slots=True)
class _SafePatternAtom:
    """One finite character predicate with an explicitly bounded repetition."""

    characters: frozenset[str]
    minimum: int = 1
    maximum: int = 1

    def matches(self, character: str) -> bool:
        return character in self.characters


def _escaped_pattern_characters(pattern: str, index: int) -> tuple[frozenset[str], int] | None:
    """Parse one supported escape without invoking the regular-expression engine."""

    if index + 1 >= len(pattern):
        return None
    escaped = pattern[index + 1]
    if escaped == "d":
        return _ASCII_DIGITS, index + 2
    if escaped == "w":
        return _ASCII_WORD, index + 2
    if escaped == "s":
        return _ASCII_SPACE, index + 2
    if escaped in _SAFE_PATTERN_ESCAPED_LITERALS:
        return frozenset({escaped}), index + 2
    return None


def _class_unit(
    pattern: str,
    index: int,
) -> tuple[frozenset[str], str | None, int] | None:
    """Parse one literal or supported escape inside an ASCII character class."""

    if index >= len(pattern) or pattern[index] == "]":
        return None
    if pattern[index] == "\\":
        escaped = _escaped_pattern_characters(pattern, index)
        if escaped is None:
            return None
        characters, next_index = escaped
        literal = next(iter(characters)) if len(characters) == 1 else None
        return characters, literal, next_index
    character = pattern[index]
    if character not in _SAFE_PATTERN_LITERAL_CHARS or character == "[":
        return None
    return frozenset({character}), character, index + 1


def _character_class(
    pattern: str,
    index: int,
) -> tuple[frozenset[str], int] | None:
    """Parse a positive finite ASCII class; negation and nested syntax fail closed."""

    cursor = index + 1
    if cursor >= len(pattern) or pattern[cursor] in {"^", "]"}:
        return None
    characters: set[str] = set()
    while cursor < len(pattern) and pattern[cursor] != "]":
        first = _class_unit(pattern, cursor)
        if first is None:
            return None
        first_chars, first_literal, cursor = first
        if (
            cursor < len(pattern)
            and pattern[cursor] == "-"
            and cursor + 1 < len(pattern)
            and pattern[cursor + 1] != "]"
        ):
            second = _class_unit(pattern, cursor + 1)
            if second is None:
                return None
            second_chars, second_literal, cursor = second
            if (
                first_literal is None
                or second_literal is None
                or ord(first_literal) > ord(second_literal)
            ):
                return None
            characters.update(
                chr(codepoint) for codepoint in range(ord(first_literal), ord(second_literal) + 1)
            )
        else:
            characters.update(first_chars)
    if cursor >= len(pattern) or pattern[cursor] != "]" or not characters:
        return None
    return frozenset(characters), cursor + 1


def _bounded_quantifier(pattern: str, index: int) -> tuple[int, int, int] | None:
    """Parse no quantifier, `?`, `{n}`, or finite `{n,m}`."""

    if index >= len(pattern):
        return 1, 1, index
    if pattern[index] == "?":
        return 0, 1, index + 1
    if pattern[index] != "{":
        if pattern[index] in {"*", "+"}:
            return None
        return 1, 1, index
    close = pattern.find("}", index + 1)
    if close < 0:
        return None
    body = pattern[index + 1 : close]
    match = re.fullmatch(r"(\d{1,2})(?:,(\d{1,2}))?", body)
    if match is None:
        return None
    minimum = int(match.group(1))
    maximum = int(match.group(2) or match.group(1))
    if maximum < 1 or minimum > maximum or maximum > _MAX_SAFE_PATTERN_REPEAT:
        return None
    return minimum, maximum, close + 1


def _parse_bounded_form_pattern(pattern: str) -> tuple[_SafePatternAtom, ...] | None:
    """Compile an untrusted pattern into a finite, non-regex matcher."""

    if not pattern or len(pattern) > _MAX_SAFE_PATTERN_CHARS:
        return None
    cursor = 0
    if pattern.startswith("^"):
        cursor = 1
    slash_count = 0
    for character in reversed(pattern[:-1]):
        if character != "\\":
            break
        slash_count += 1
    terminal_anchor = pattern.endswith("$") and slash_count % 2 == 0
    terminal = len(pattern) - 1 if terminal_anchor else len(pattern)
    if cursor > terminal:
        return None

    atoms: list[_SafePatternAtom] = []
    state_count = 0
    while cursor < terminal:
        character = pattern[cursor]
        if character == "[":
            parsed = _character_class(pattern[:terminal], cursor)
        elif character == "\\":
            parsed = _escaped_pattern_characters(pattern[:terminal], cursor)
        elif character in _SAFE_PATTERN_LITERAL_CHARS:
            parsed = frozenset({character}), cursor + 1
        else:
            return None
        if parsed is None:
            return None
        characters, cursor = parsed
        quantifier = _bounded_quantifier(pattern[:terminal], cursor)
        if quantifier is None:
            return None
        minimum, maximum, cursor = quantifier
        state_count += maximum
        if state_count > _MAX_SAFE_PATTERN_STATES:
            return None
        atoms.append(
            _SafePatternAtom(
                characters=characters,
                minimum=minimum,
                maximum=maximum,
            )
        )
    return tuple(atoms) if atoms else None


def _matches_bounded_form_pattern(pattern: str, value: str) -> bool:
    """Match a finite linear subset without executing employer-provided regex."""

    atoms = _parse_bounded_form_pattern(pattern)
    if atoms is None:
        return False
    maximum_length = sum(atom.maximum for atom in atoms)
    if len(value) > maximum_length:
        return False

    positions = {0}
    for atom in atoms:
        next_positions: set[int] = set()
        for position in positions:
            if atom.minimum == 0:
                next_positions.add(position)
            cursor = position
            for count in range(1, atom.maximum + 1):
                if cursor >= len(value) or not atom.matches(value[cursor]):
                    break
                cursor += 1
                if count >= atom.minimum:
                    next_positions.add(cursor)
        if not next_positions:
            return False
        positions = next_positions
    return len(value) in positions


def _valid_semantic_text_value(field_type: FieldType, value: str) -> bool:
    """Validate browser-typed strings at the authoritative domain boundary."""

    if field_type == FieldType.DATE:
        if _DATE_VALUE_RE.fullmatch(value) is None:
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if field_type == FieldType.EMAIL:
        return _EMAIL_VALUE_RE.fullmatch(value) is not None
    if field_type == FieldType.PHONE:
        return (
            _PHONE_VALUE_RE.fullmatch(value) is not None
            and value.count("+") <= 1
            and value.count("(") == value.count(")")
            and 7 <= sum(character.isdigit() for character in value) <= 15
        )
    if field_type == FieldType.URL:
        if any(character.isspace() or ord(character) < 32 for character in value):
            return False
        try:
            parsed = urlsplit(value)
            return bool(
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
            )
        except (UnicodeError, ValueError):
            return False
    return True


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
        if not _valid_semantic_text_value(field.field_type, value):
            raise ValueError(f"{field.field_type.value} answer is not a valid canonical value")
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
    evidence_refs: tuple[BoundedText, ...] = Field(
        default=(),
        max_length=_MAX_EVIDENCE_REFS,
    )
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
    fields: tuple[FormFieldV1, ...] = Field(max_length=_MAX_FORM_FIELDS)
    disclosures: tuple[FormDisclosureV1, ...] = Field(
        default=(),
        max_length=_MAX_FORM_DISCLOSURES,
    )
    decisions: tuple[AnswerDecisionV1, ...] = Field(max_length=_MAX_FORM_FIELDS)
    blockers: tuple[ReasonCode, ...] = Field(
        default=(),
        max_length=_MAX_FORM_BLOCKERS,
    )
    locale: BoundedText = "en"
    answer_policy_version: BoundedText = "answer-policy-v1"
    llm_prompt_version: BoundedText | None = None
    llm_model_provider: BoundedText | None = None
    llm_model_name: BoundedText | None = None
    llm_model_digest: (
        Annotated[
            str,
            StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
        ]
        | None
    ) = None

    @field_validator("locale")
    @classmethod
    def locale_is_canonical(cls, value: str) -> str:
        if not is_canonical_locale(value):
            raise ValueError("locale must be a canonical BCP-47-style token")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> FormPlanV1:
        if self.expires_at <= self.created_at:
            raise ValueError("form plan expiry must be after creation")
        if self.expires_at - self.created_at > _MAX_FORM_PLAN_LIFETIME:
            raise ValueError("form plan lifetime cannot exceed 30 minutes")
        serialized_size = len(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if serialized_size > _MAX_FORM_PLAN_BYTES:
            raise ValueError("serialized form plan exceeds the bounded domain maximum")
        model_audit = (
            self.llm_prompt_version,
            self.llm_model_provider,
            self.llm_model_name,
            self.llm_model_digest,
        )
        if any(value is not None for value in model_audit) and not all(
            value is not None for value in model_audit
        ):
            raise ValueError("LLM form-plan audit identity must be all present or all absent")
        field_by_id = {field.field_id: field for field in self.fields}
        if len(field_by_id) != len(self.fields):
            raise ValueError("form field IDs must be unique")
        disclosure_by_id = {disclosure.disclosure_id: disclosure for disclosure in self.disclosures}
        if len(disclosure_by_id) != len(self.disclosures):
            raise ValueError("form disclosure IDs must be unique")
        disclosure_positions = [disclosure.position for disclosure in self.disclosures]
        if len(disclosure_positions) != len(set(disclosure_positions)):
            raise ValueError("form disclosure positions must be unique")
        disclosure_kinds = {disclosure.kind for disclosure in self.disclosures}
        if {
            DisclosureKind.PRIVACY_POLICY,
            DisclosureKind.NO_PRIVACY_POLICY_NOTICE,
        }.issubset(disclosure_kinds):
            raise ValueError("privacy policy and no-policy notice are mutually exclusive")
        for disclosure in self.disclosures:
            if disclosure.acknowledgement_field_id is None:
                continue
            acknowledgement = field_by_id.get(disclosure.acknowledgement_field_id)
            if acknowledgement is None or acknowledgement.field_type not in {
                FieldType.CONSENT,
                FieldType.ATTESTATION,
            }:
                raise ValueError(
                    "disclosure acknowledgement must reference an observed consent control"
                )
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
            sensitive_field = field_requires_operator_review(field)
            verified_attachment_decision = (
                decision.disposition == AnswerDisposition.RESOLVED
                and decision.provenance == AnswerProvenance.VERIFIED_ATTACHMENT
            )
            explicitly_confirmed = bool(decision.evidence_refs) and all(
                reference.startswith("operator_confirmation:")
                for reference in decision.evidence_refs
            )
            if verified_attachment_decision:
                if (
                    not field_is_reviewed_cv_attachment(field)
                    or decision.value != VERIFIED_ATTACHMENT_SENTINEL
                    or decision.evidence_refs != (VERIFIED_ATTACHMENT_EVIDENCE_REF,)
                    or self.attachment_verified is not True
                    or self.attached_cv_id != self.selected_cv_id
                    or self.attached_cv_hash != self.selected_cv_hash
                ):
                    raise ValueError(
                        "verified attachment answers require exact reviewed attachment metadata"
                    )
            elif (
                decision.disposition == AnswerDisposition.RESOLVED
                and field.field_type == FieldType.FILE
            ):
                raise ValueError("file controls accept only verified attachment provenance")
            if (
                decision.disposition == AnswerDisposition.RESOLVED
                and not field_canonical_label_compatible(field)
                and not (
                    decision.provenance == AnswerProvenance.USER_CONFIRMED and explicitly_confirmed
                )
            ):
                raise ValueError(
                    "automatic answers require compatible canonical and observed field semantics"
                )
            if (
                sensitive_field
                and decision.disposition == AnswerDisposition.RESOLVED
                and decision.provenance
                not in {
                    AnswerProvenance.USER_CONFIRMED,
                    AnswerProvenance.OPERATOR_APPROVED_REUSABLE,
                }
            ):
                raise ValueError("sensitive answers require confirmed operator evidence")
            if (
                sensitive_field
                and decision.disposition == AnswerDisposition.RESOLVED
                and not decision.evidence_refs
            ):
                raise ValueError("sensitive answers require at least one evidence reference")
            if (
                sensitive_field
                and decision.disposition == AnswerDisposition.RESOLVED
                and decision.provenance == AnswerProvenance.OPERATOR_APPROVED_REUSABLE
                and not field_has_scoped_sensitive_semantics(field)
            ):
                raise ValueError("reusable sensitive answers require scoped field semantics")
            if (
                sensitive_field
                and decision.disposition == AnswerDisposition.RESOLVED
                and decision.provenance == AnswerProvenance.USER_CONFIRMED
                and not explicitly_confirmed
            ):
                canonical = canonical_sensitive_key(field.canonical_name or "")
                expected_reference = f"profile:user_confirmed:{canonical}"
                if (
                    not field_has_scoped_sensitive_semantics(field)
                    or not canonical
                    or any(reference != expected_reference for reference in decision.evidence_refs)
                ):
                    raise ValueError(
                        "profile-confirmed sensitive answers require exact scoped evidence"
                    )
            if (
                decision.disposition == AnswerDisposition.RESOLVED
                and not verified_attachment_decision
            ):
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

        uses_local_llm = any(
            decision.provenance == AnswerProvenance.LOCAL_LLM for decision in self.decisions
        )
        has_model_audit = all(value is not None for value in model_audit)
        if uses_local_llm != has_model_audit:
            raise ValueError(
                "LLM form-plan audit identity must exist exactly when local LLM answers exist"
            )
        if uses_local_llm and (
            self.llm_prompt_version != FORM_RESOLUTION_PROMPT_VERSION
            or self.llm_model_provider != QUALIFIED_LOCAL_LLM_PROVIDER
            or self.llm_model_name != QUALIFIED_LOCAL_LLM_MODEL
            or not is_qualified_local_model_identity(
                provider=self.llm_model_provider,
                model=self.llm_model_name,
                local=True,
                digest=self.llm_model_digest,
            )
        ):
            raise ValueError("local LLM answers require the qualified prompt and model identity")

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
