"""Deterministic, evidence-bounded form answer planning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from profile.models import (
    CV_ARTIFACT_FACT_KEYS,
    SelectedCVFactCatalog,
    UserProfile,
    canonical_fact_key,
    is_sensitive_fact_key,
)
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.sensitive_policy import (
    contains_prompt_injection,
    contains_sensitive_text,
    normalize_policy_text,
)
from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    AnswerValue,
    FieldType,
    FormFieldV1,
    ReasonCode,
    field_canonical_label_compatible,
    field_has_reviewed_canonical_name,
    field_has_scoped_sensitive_semantics,
    field_is_reviewed_cv_attachment,
    field_requires_operator_review,
    is_canonical_locale,
    observed_form_fields_are_bounded,
)
from llm.contracts import FORM_RESOLUTION_PROMPT_VERSION

ANSWER_POLICY_VERSION = "answer-policy-v1"
_LLM_FIELD_SEMANTIC_PATTERNS: dict[str, tuple[str, ...]] = {
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
}
_LLM_SYNTHESIS_FIELD_PATTERNS = (
    "relevant technical experience",
    "provide one relevant technical experience",
    "ניסיון טכני רלוונטי",
    "נא לציין ניסיון טכני רלוונטי אחד",
)
_LLM_SYNTHESIS_EVIDENCE_KEYS = frozenset(_LLM_FIELD_SEMANTIC_PATTERNS)
_LLM_SYNTHESIS_ALLOWED_EVIDENCE_KEYS = frozenset(
    {*_LLM_SYNTHESIS_EVIDENCE_KEYS, "relevant_experience", "technical_summary"}
)


@dataclass(frozen=True, slots=True)
class AnswerPolicyContext:
    """Exact private context that bounds one form-planning decision."""

    profile: UserProfile
    profile_version: int
    selected_cv_id: str
    selected_cv_hash: str
    adapter_name: str
    adapter_version: str
    selector_version: str
    form_fingerprint: str
    locale: str = "en"
    policy_version: str = ANSWER_POLICY_VERSION
    attached_cv_id: str | None = None
    attached_cv_hash: str | None = None
    attachment_verified: bool = False
    selected_cv_fact_catalog: SelectedCVFactCatalog | None = None

    def __post_init__(self) -> None:
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise ValueError("profile_version must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.selected_cv_hash):
            raise ValueError("selected_cv_hash must be a lowercase SHA-256 digest")
        if not re.fullmatch(r"[0-9a-f]{64}", self.form_fingerprint):
            raise ValueError("form_fingerprint must be a lowercase SHA-256 digest")
        if not is_canonical_locale(self.locale):
            raise ValueError("locale must be a canonical BCP-47-style token")
        if type(self.attachment_verified) is not bool:
            raise ValueError("attachment_verified must be boolean")
        if self.attached_cv_id is not None and (
            not self.attached_cv_id.strip()
            or self.attached_cv_id != self.attached_cv_id.strip()
            or len(self.attached_cv_id) > 255
        ):
            raise ValueError("attached_cv_id must be canonical and bounded")
        if self.attached_cv_hash is not None and not re.fullmatch(
            r"[0-9a-f]{64}",
            self.attached_cv_hash,
        ):
            raise ValueError("attached_cv_hash must be a lowercase SHA-256 digest")
        if (
            self.selected_cv_fact_catalog is not None
            and self.selected_cv_fact_catalog.pdf_sha256 != self.selected_cv_hash
        ):
            raise ValueError("selected CV fact catalog must match selected_cv_hash")
        bounded = {
            "selected_cv_id": (self.selected_cv_id, 255),
            "adapter_name": (self.adapter_name, 64),
            "adapter_version": (self.adapter_version, 32),
            "selector_version": (self.selector_version, 64),
            "locale": (self.locale, 32),
            "policy_version": (self.policy_version, 64),
        }
        if any(
            not value.strip() or value != value.strip() or len(value) > limit
            for value, limit in bounded.values()
        ):
            raise ValueError("answer-policy context strings must be canonical and bounded")

    @property
    def selected_cv_fact_catalog_digest(self) -> str | None:
        catalog = self.selected_cv_fact_catalog
        return catalog.catalog_sha256 if catalog is not None else None

    def selected_cv_facts(self) -> dict[str, str]:
        """Return one conflict-free, exact-artifact-bound private fact view."""

        profile_facts = self.profile.evidence.facts_for_cv(self.selected_cv_hash)
        catalog_facts = (
            self.selected_cv_fact_catalog.as_dict()
            if self.selected_cv_fact_catalog is not None
            else {}
        )
        merged: dict[str, str] = {}
        conflicted: set[str] = set()
        for source in (profile_facts, catalog_facts):
            for raw_key, raw_value in source.items():
                key = canonical_fact_key(raw_key)
                value = str(raw_value).strip()
                if (
                    key not in CV_ARTIFACT_FACT_KEYS
                    or is_sensitive_fact_key(key)
                    or not value
                    or contains_sensitive_text(value)
                    or contains_prompt_injection(value)
                ):
                    continue
                if key in merged and merged[key] != value:
                    conflicted.add(key)
                    continue
                merged[key] = value
        for key in conflicted:
            merged.pop(key, None)
        return merged


@dataclass(frozen=True, slots=True)
class AnswerPolicyResult:
    decisions: tuple[AnswerDecisionV1, ...]
    blockers: tuple[ReasonCode, ...]
    prompt_version: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    model_digest: str | None = None


class FormPlanningBlockedError(ValueError):
    """Bounded fail-closed error raised before untrusted form resolution."""

    def __init__(self, reason_code: ReasonCode) -> None:
        super().__init__(reason_code.value)
        self.reason_code = reason_code


class _TypedLLMClient(Protocol):
    async def generate_typed(self, **kwargs): ...


class LLMFieldAnswerV1(BaseModel):
    """One bounded, schema-validated non-sensitive answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: AnswerValue
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_refs must be unique")
        if any(not item.strip() or len(item) > 255 for item in value):
            raise ValueError("evidence_refs must be nonblank and bounded")
        return value


def option_set_hash(field: FormFieldV1) -> str:
    """Hash the exact observed option contract for reusable-answer scoping."""
    payload = [
        {
            "option_id": option.option_id,
            "value": option.value,
            "label": option.label,
            "disabled": option.disabled,
        }
        for option in field.options
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_field(field: FormFieldV1) -> str:
    return canonical_fact_key(field.canonical_name or "")


def _allowed_llm_evidence_keys(field: FormFieldV1) -> frozenset[str]:
    """Map only reviewed, non-sensitive label semantics to evidence keys."""

    canonical = _canonical_field(field)
    if canonical:
        if canonical in {"relevant_experience", "technical_summary"}:
            return frozenset({canonical, *_LLM_SYNTHESIS_ALLOWED_EVIDENCE_KEYS})
        return frozenset({canonical})
    normalized_label = normalize_policy_text(field.label)
    normalized_synthesis_labels = {
        normalize_policy_text(label) for label in _LLM_SYNTHESIS_FIELD_PATTERNS
    }
    if normalized_label in normalized_synthesis_labels:
        return _LLM_SYNTHESIS_ALLOWED_EVIDENCE_KEYS
    synthesis_matches = {
        key
        for key, phrases in _LLM_FIELD_SEMANTIC_PATTERNS.items()
        if normalized_label
        in {
            candidate
            for phrase in phrases
            for normalized_phrase in (normalize_policy_text(phrase),)
            for candidate in (
                f"relevant technical experience: {normalized_phrase}",
                f"provide one relevant technical experience about {normalized_phrase}",
                f"ניסיון טכני רלוונטי: {normalized_phrase}",
                f"נא לציין ניסיון טכני רלוונטי לגבי {normalized_phrase}",
            )
        }
    }
    if len(synthesis_matches) == 1:
        return frozenset(synthesis_matches)
    matches: set[str] = set()
    for key, phrases in _LLM_FIELD_SEMANTIC_PATTERNS.items():
        approved_labels = {
            candidate
            for phrase in phrases
            for normalized_phrase in (normalize_policy_text(phrase),)
            for candidate in (
                normalized_phrase,
                f"provide the candidate's {normalized_phrase}",
                f"נא לציין את {normalized_phrase}",
            )
        }
        if normalized_label in approved_labels:
            matches.add(key)
    return frozenset(matches) if len(matches) == 1 else frozenset()


def _direct_reviewed_semantic_key(field: FormFieldV1) -> str:
    """Return one exact label-to-fact mapping that needs no model synthesis."""

    if field.canonical_name:
        return ""
    normalized_label = normalize_policy_text(field.label)
    matches = {
        key
        for key, phrases in _LLM_FIELD_SEMANTIC_PATTERNS.items()
        if normalized_label
        in {
            candidate
            for phrase in phrases
            for normalized_phrase in (normalize_policy_text(phrase),)
            for candidate in (
                normalized_phrase,
                f"provide the candidate's {normalized_phrase}",
                f"נא לציין את {normalized_phrase}",
            )
        }
    }
    return next(iter(matches)) if len(matches) == 1 else ""


def _is_sensitive(field: FormFieldV1) -> bool:
    return field_requires_operator_review(field)


def _evidence_value_is_safe(field: FormFieldV1, raw: object, *, confirmed: bool) -> bool:
    """Prevent benign evidence keys from laundering protected or hostile values."""

    if isinstance(raw, (list, tuple)):
        rendered = " ".join(str(value) for value in raw)
    else:
        rendered = str(raw or "")
    if contains_prompt_injection(rendered):
        return False
    if not contains_sensitive_text(rendered):
        return True
    return confirmed and _is_sensitive(field)


def _identity_value(field: FormFieldV1, profile: UserProfile) -> object | None:
    canonical = _canonical_field(field)
    normalized_name = " ".join(profile.personal.name.split())
    name_parts = normalized_name.split(" ") if normalized_name else []
    unambiguous_given_name = name_parts[0] if len(name_parts) == 2 else None
    unambiguous_family_name = name_parts[1] if len(name_parts) == 2 else None
    values: dict[str, object] = {
        "email": profile.personal.email,
        "phone": profile.personal.phone,
        "full_name": normalized_name,
        "name": normalized_name,
        "first_name": unambiguous_given_name,
        "given_name": unambiguous_given_name,
        "last_name": unambiguous_family_name,
        "surname": unambiguous_family_name,
        "family_name": unambiguous_family_name,
        "location": profile.personal.location,
        "city": profile.personal.location.split(",", 1)[0].strip(),
        "linkedin": profile.links.linkedin,
        "linkedin_url": profile.links.linkedin,
        "github": profile.links.github,
        "github_url": profile.links.github,
        "portfolio": profile.links.portfolio,
        "portfolio_url": profile.links.portfolio,
    }
    value = values.get(canonical)
    if isinstance(value, str):
        return value.strip() or None
    return value


def _verified_attachment_decision(
    field: FormFieldV1,
    context: AnswerPolicyContext,
) -> AnswerDecisionV1 | None:
    if (
        not field_is_reviewed_cv_attachment(field)
        or context.attachment_verified is not True
        or context.attached_cv_id != context.selected_cv_id
        or context.attached_cv_hash != context.selected_cv_hash
    ):
        return None
    return AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
        value=VERIFIED_ATTACHMENT_SENTINEL,
        confidence=1.0,
        evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
    )


def _coerce_answer(field: FormFieldV1, raw: object) -> AnswerValue | None:
    """Coerce trusted textual evidence to the exact observed control type."""
    if raw is None:
        return None
    if field.field_type == FieldType.NUMBER:
        if isinstance(raw, bool):
            return None
        if not isinstance(raw, (str, int, float)):
            return None
        try:
            numeric = float(raw)
        except (TypeError, ValueError):
            return None
        return int(numeric) if numeric.is_integer() else numeric
    if field.field_type in {FieldType.CHECKBOX, FieldType.CONSENT, FieldType.ATTESTATION}:
        if type(raw) is bool:
            return raw
        normalized = str(raw).strip().casefold()
        if normalized in {"yes", "true", "1", "checked", "agree", "accepted"}:
            return True
        if normalized in {"no", "false", "0", "unchecked", "disagree", "declined"}:
            return False
        return None
    if field.field_type in {FieldType.SELECT, FieldType.RADIO}:
        normalized = normalize_policy_text(str(raw))
        matches = {
            option.value
            for option in field.options
            if not option.disabled
            and normalized
            in {
                normalize_policy_text(option.value),
                normalize_policy_text(option.label),
            }
        }
        return next(iter(matches)) if len(matches) == 1 else None
    if field.field_type == FieldType.MULTI_SELECT:
        values = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        selected: list[str] = []
        for raw_value in values:
            normalized = normalize_policy_text(str(raw_value))
            matches = {
                option.value
                for option in field.options
                if not option.disabled
                and normalized
                in {
                    normalize_policy_text(option.value),
                    normalize_policy_text(option.label),
                }
            }
            if len(matches) != 1:
                return None
            selected.append(next(iter(matches)))
        return tuple(selected) if selected and len(selected) == len(set(selected)) else None
    if field.field_type == FieldType.UNKNOWN:
        return None
    value = str(raw).strip()
    return value or None


def _resolved(
    field: FormFieldV1,
    raw_value: object,
    *,
    provenance: AnswerProvenance,
    evidence_ref: str,
    confidence: float = 1.0,
) -> AnswerDecisionV1 | None:
    value = _coerce_answer(field, raw_value)
    if value is None:
        return None
    return AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=provenance,
        value=value,
        confidence=confidence,
        evidence_refs=(evidence_ref,),
    )


def _abstain(field: FormFieldV1, reason: ReasonCode) -> AnswerDecisionV1:
    disposition = (
        AnswerDisposition.OPERATOR_REQUIRED
        if field.required or _is_sensitive(field)
        else AnswerDisposition.ABSTAINED
    )
    return AnswerDecisionV1(
        field_id=field.field_id,
        disposition=disposition,
        provenance=AnswerProvenance.ABSTAINED,
        reason_code=reason,
    )


def _llm_reason(exc: Exception) -> ReasonCode:
    reason = str(getattr(exc, "reason_code", ""))
    if reason in {"LLM_MODEL_NOT_READY", "LLM_MODEL_NOT_LOCAL"}:
        return ReasonCode.LLM_MODEL_MISSING
    if reason in {"LLM_DEADLINE_EXCEEDED"}:
        return ReasonCode.LLM_TIMEOUT
    if reason == "LLM_CIRCUIT_OPEN":
        return ReasonCode.LLM_CIRCUIT_OPEN
    if reason == "LLM_OUTPUT_INVALID":
        return ReasonCode.LLM_SCHEMA_INVALID
    return ReasonCode.LLM_UNAVAILABLE


def _llm_answer_supported(
    field: FormFieldV1,
    value: AnswerValue,
    evidence_refs: tuple[str, ...],
    evidence_catalog: dict[str, str],
) -> bool:
    """Deterministically prove cited evidence supports the proposed answer."""
    allowed_evidence_keys = _allowed_llm_evidence_keys(field)
    if not allowed_evidence_keys:
        return False

    def cited_canonical(ref: str) -> str:
        if ref.startswith("profile:user_confirmed:"):
            return canonical_fact_key(ref.removeprefix("profile:user_confirmed:"))
        cv_prefix = f"cv:{ref.split(':', 2)[1]}:" if ref.startswith("cv:") else ""
        if cv_prefix and ref.startswith(cv_prefix):
            return canonical_fact_key(ref.removeprefix(cv_prefix))
        return ""

    # A matching value is not semantic support: generic values such as "yes",
    # dates, and numbers can occur under unrelated evidence keys.  Require the
    # field and every cited fact to share the exact canonical meaning.
    if any(cited_canonical(ref) not in allowed_evidence_keys for ref in evidence_refs):
        return False
    cited = {ref: evidence_catalog[ref] for ref in evidence_refs}
    exact_controls = {
        FieldType.SELECT,
        FieldType.RADIO,
        FieldType.MULTI_SELECT,
        FieldType.CHECKBOX,
        FieldType.NUMBER,
        FieldType.DATE,
        FieldType.EMAIL,
        FieldType.PHONE,
        FieldType.URL,
    }
    if field.field_type in exact_controls:
        return any(_coerce_answer(field, text) == value for text in cited.values())
    if field.field_type not in {FieldType.TEXT, FieldType.TEXTAREA}:
        return False

    from llm.claim_evidence import (
        ClaimEvidenceQuoteV1,
        DraftClaimV1,
        make_evidence_item,
        validate_claim_evidence,
    )

    items = []
    item_by_ref = {}
    for ref, text in cited.items():
        source_kind: Literal["cv", "user_confirmed"] = (
            "cv" if ref.startswith("cv:") else "user_confirmed"
        )
        # The content-addressed evidence item bounds source_ref to 200 chars.
        source_ref = hashlib.sha256(ref.encode("utf-8")).hexdigest()
        item = make_evidence_item(source_kind, source_ref, text)
        items.append(item)
        item_by_ref[ref] = item
    claim_text = str(value)
    claim = DraftClaimV1(
        claim_id=f"claim_form_{hashlib.sha256(field.field_id.encode()).hexdigest()[:16]}",
        claim_text=claim_text,
        evidence_quotes=tuple(
            ClaimEvidenceQuoteV1(
                evidence_id=item_by_ref[ref].evidence_id,
                quote=item_by_ref[ref].text,
            )
            for ref in evidence_refs
        ),
    )
    return validate_claim_evidence((claim_text,), (claim,), tuple(items)).eligible


def _evidence_bound_llm_value(
    field: FormFieldV1,
    typed: LLMFieldAnswerV1,
    evidence_catalog: dict[str, str],
) -> AnswerValue | None:
    """Render prose from one exact cited fact instead of trusting a paraphrase."""

    if field.field_type not in {FieldType.TEXT, FieldType.TEXTAREA}:
        return _coerce_answer(field, typed.value)
    if len(typed.evidence_refs) != 1:
        return None
    return _coerce_answer(field, evidence_catalog[typed.evidence_refs[0]])


class AnswerPolicyV1:
    """Resolve each field by fixed provenance precedence; never mutate caches."""

    def __init__(self, *, llm_client: _TypedLLMClient | None = None, db=None):
        self.llm_client = llm_client
        self.db = db

    def _operator_approved(
        self,
        field: FormFieldV1,
        context: AnswerPolicyContext,
    ) -> AnswerDecisionV1 | None:
        if self.db is None or not field.canonical_name:
            return None
        from db.models import OperatorApprovedAnswer

        row = (
            self.db.query(OperatorApprovedAnswer)
            .filter(
                OperatorApprovedAnswer.canonical_field == _canonical_field(field),
                OperatorApprovedAnswer.field_type == field.field_type.value,
                OperatorApprovedAnswer.option_set_hash == option_set_hash(field),
                OperatorApprovedAnswer.locale == context.locale,
                OperatorApprovedAnswer.profile_version == context.profile_version,
                OperatorApprovedAnswer.selected_cv_id == context.selected_cv_id,
                OperatorApprovedAnswer.selected_cv_hash == context.selected_cv_hash,
                OperatorApprovedAnswer.adapter_name == context.adapter_name,
                OperatorApprovedAnswer.adapter_version == context.adapter_version,
                OperatorApprovedAnswer.selector_version == context.selector_version,
                OperatorApprovedAnswer.form_fingerprint == context.form_fingerprint,
                OperatorApprovedAnswer.policy_version == context.policy_version,
                OperatorApprovedAnswer.revoked_at.is_(None),
            )
            .order_by(OperatorApprovedAnswer.approved_at.desc(), OperatorApprovedAnswer.id.desc())
            .first()
        )
        if row is None:
            return None
        try:
            raw_value = json.loads(row.answer_json)
        except (TypeError, ValueError):
            return None
        if not _evidence_value_is_safe(field, raw_value, confirmed=True):
            return None
        return _resolved(
            field,
            raw_value,
            provenance=AnswerProvenance.OPERATOR_APPROVED_REUSABLE,
            evidence_ref=f"operator-approved-answer:{row.id}",
        )

    async def _local_llm(
        self,
        field: FormFieldV1,
        context: AnswerPolicyContext,
    ) -> tuple[AnswerDecisionV1 | None, object | None, ReasonCode | None]:
        if self.llm_client is None:
            return None, None, None
        model_identity = getattr(self.llm_client, "model_identity", None)
        if model_identity is not None and getattr(model_identity, "local", None) is not True:
            return None, None, ReasonCode.LLM_UNAVAILABLE
        from llm.claim_evidence import non_sensitive_cv_excerpt

        allowed_evidence_keys = _allowed_llm_evidence_keys(field)
        if not allowed_evidence_keys:
            return None, None, ReasonCode.UNSUPPORTED_CLAIM
        evidence_catalog: dict[str, str] = {}
        for key, value in sorted(context.profile.evidence.llm_safe_confirmed_facts().items()):
            if key not in allowed_evidence_keys:
                continue
            safe_value = non_sensitive_cv_excerpt(value, max_chars=500)
            if safe_value:
                evidence_catalog[f"profile:user_confirmed:{key}"] = safe_value
        for key, value in sorted(context.selected_cv_facts().items()):
            if key not in allowed_evidence_keys or is_sensitive_fact_key(key):
                continue
            safe_value = non_sensitive_cv_excerpt(value, max_chars=500)
            if safe_value:
                evidence_catalog[f"cv:{context.selected_cv_hash}:{key}"] = safe_value
        # Keep the local prompt and allowed citation surface bounded.
        evidence_catalog = dict(list(evidence_catalog.items())[:40])
        if not evidence_catalog:
            return None, None, ReasonCode.UNSUPPORTED_CLAIM
        prompt = json.dumps(
            {
                "field": field.model_dump(mode="json"),
                "locale": context.locale,
                "evidence_catalog": evidence_catalog,
                "instruction": (
                    "Answer only this non-sensitive application field. "
                    "Every answer must cite exact evidence_refs from evidence_catalog. "
                    "For text and textarea controls, copy exactly one cited catalog "
                    "value without paraphrasing or combining facts. "
                    "Do not infer identity, legal, demographic, consent, or attestation facts."
                ),
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )
        try:
            generated = await self.llm_client.generate_typed(
                response_model=LLMFieldAnswerV1,
                prompt=prompt,
                purpose="form_resolution",
                prompt_version=FORM_RESOLUTION_PROMPT_VERSION,
                deadline=datetime.now(UTC) + timedelta(seconds=30),
                data_classification="private_application",
                system="Return only schema-valid JSON. Abstain from unsupported factual claims.",
                max_tokens=300,
                temperature=0.0,
            )
        except Exception as exc:
            return None, None, _llm_reason(exc)
        payload = getattr(generated, "value", generated)
        try:
            typed = (
                payload
                if isinstance(payload, LLMFieldAnswerV1)
                else LLMFieldAnswerV1.model_validate(payload)
            )
        except (TypeError, ValueError):
            return None, generated, ReasonCode.LLM_SCHEMA_INVALID
        if not typed.evidence_refs or not set(typed.evidence_refs).issubset(evidence_catalog):
            return None, generated, ReasonCode.UNSUPPORTED_CLAIM
        answer_value = _evidence_bound_llm_value(field, typed, evidence_catalog)
        if answer_value is None or not _llm_answer_supported(
            field,
            answer_value,
            typed.evidence_refs,
            evidence_catalog,
        ):
            return None, generated, ReasonCode.UNSUPPORTED_CLAIM
        decision = AnswerDecisionV1(
            field_id=field.field_id,
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.LOCAL_LLM,
            value=answer_value,
            confidence=typed.confidence,
            evidence_refs=typed.evidence_refs,
        )
        return decision, generated, None if decision is not None else ReasonCode.LLM_SCHEMA_INVALID

    async def plan_fields(
        self,
        fields: tuple[FormFieldV1, ...],
        context: AnswerPolicyContext,
    ) -> AnswerPolicyResult:
        if not observed_form_fields_are_bounded(fields):
            raise FormPlanningBlockedError(ReasonCode.FORM_CHANGED)
        decisions: list[AnswerDecisionV1] = []
        blockers: set[ReasonCode] = set()
        model_audit: tuple[str, str, str] | None = None

        for field in fields:
            canonical = _canonical_field(field)
            evidence_canonical = canonical or _direct_reviewed_semantic_key(field)
            compatible_semantics = field_canonical_label_compatible(field)
            file_control = field.field_type == FieldType.FILE
            semantic_reason = (
                ReasonCode.UNSUPPORTED_CLAIM
                if canonical
                and not compatible_semantics
                and not field_has_reviewed_canonical_name(field)
                else None
            )
            ambiguous_sensitive_identity = _is_sensitive(
                field
            ) and not field_has_scoped_sensitive_semantics(field)
            automatic_resolution_allowed = (
                compatible_semantics and not ambiguous_sensitive_identity and not file_control
            )
            decision = _verified_attachment_decision(field, context)
            if decision is None:
                decision = _resolved(
                    field,
                    (
                        _identity_value(field, context.profile)
                        if automatic_resolution_allowed
                        else None
                    ),
                    provenance=AnswerProvenance.DETERMINISTIC_IDENTITY,
                    evidence_ref=f"profile:identity:{canonical}",
                )
            if decision is None and evidence_canonical and automatic_resolution_allowed:
                confirmed = context.profile.evidence.confirmed_fact(evidence_canonical)
                if confirmed is not None and _evidence_value_is_safe(
                    field,
                    confirmed,
                    confirmed=True,
                ):
                    decision = _resolved(
                        field,
                        confirmed,
                        provenance=AnswerProvenance.USER_CONFIRMED,
                        evidence_ref=f"profile:user_confirmed:{evidence_canonical}",
                    )
            if decision is None and automatic_resolution_allowed:
                decision = self._operator_approved(field, context)
            if (
                decision is None
                and evidence_canonical
                and automatic_resolution_allowed
                and not _is_sensitive(field)
            ):
                cv_value = context.selected_cv_facts().get(evidence_canonical)
                if cv_value is not None and _evidence_value_is_safe(
                    field,
                    cv_value,
                    confirmed=False,
                ):
                    decision = _resolved(
                        field,
                        cv_value,
                        provenance=AnswerProvenance.CV_EVIDENCE,
                        evidence_ref=f"cv:{context.selected_cv_hash}:{evidence_canonical}",
                        confidence=0.95,
                    )
            llm_reason = (
                ReasonCode.ATTACHMENT_UNVERIFIED
                if file_control and field_is_reviewed_cv_attachment(field)
                else semantic_reason
            )
            generated = None
            if decision is None and automatic_resolution_allowed and not _is_sensitive(field):
                decision, generated, llm_reason = await self._local_llm(field, context)
                identity = getattr(generated, "model_identity", None)
                candidate_audit = None
                if (
                    identity is not None
                    and getattr(identity, "local", None) is True
                    and isinstance(getattr(identity, "provider", None), str)
                    and isinstance(getattr(identity, "model", None), str)
                    and isinstance(getattr(identity, "digest", None), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", str(identity.digest))
                ):
                    candidate_audit = (
                        str(identity.provider),
                        str(identity.model),
                        str(identity.digest),
                    )
                if decision is not None:
                    if candidate_audit is None:
                        decision = None
                        llm_reason = ReasonCode.LLM_MODEL_MISSING
                        blockers.add(ReasonCode.LLM_MODEL_MISSING)
                    elif model_audit is None:
                        model_audit = candidate_audit
                    elif model_audit != candidate_audit:
                        # Never combine answers from two model artifacts in one
                        # immutable review plan. Keep the first identity,
                        # discard the changed-model answer, and block the plan.
                        decision = None
                        llm_reason = ReasonCode.LLM_UNAVAILABLE
                        blockers.add(ReasonCode.LLM_UNAVAILABLE)
            if decision is None:
                decision = _abstain(
                    field,
                    llm_reason or ReasonCode.REQUIRED_FIELD_UNKNOWN,
                )
            if decision.disposition != AnswerDisposition.RESOLVED and field.required:
                blockers.add(ReasonCode.REQUIRED_FIELD_UNKNOWN)
                decision_reason = decision.reason_code
                if (
                    decision_reason is not None
                    and decision_reason != ReasonCode.REQUIRED_FIELD_UNKNOWN
                ):
                    blockers.add(decision_reason)
            decisions.append(decision)

        return AnswerPolicyResult(
            decisions=tuple(decisions),
            blockers=tuple(sorted(blockers, key=str)),
            prompt_version=(FORM_RESOLUTION_PROMPT_VERSION if model_audit else None),
            model_provider=(model_audit[0] if model_audit else None),
            model_name=(model_audit[1] if model_audit else None),
            model_digest=(model_audit[2] if model_audit else None),
        )
