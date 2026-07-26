"""Application generation — uses LLM to produce tailored application materials."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from profile.models import CVArtifact, UserProfile
from typing import Annotated, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from core.config import get_settings
from core.sensitive_policy import contains_prompt_injection, contains_sensitive_text
from jobs.models import JobData
from llm.claim_evidence import (
    ClaimEvidenceRefV1,
    DraftClaimV1,
    EvidenceItemV1,
    bind_generated_claims,
    build_evidence_catalog,
    material_sentences,
    non_sensitive_cv_excerpt,
    validate_claim_evidence,
)
from llm.client import (
    TYPED_REQUEST_RETRY_MARGIN_CHARS,
    LLMClient,
    get_llm_client,
)
from llm.contracts import (
    MATERIAL_PROMPT_VERSION,
    DataClassification,
    GenerationPurpose,
    LLMReasonCode,
    ModelIdentity,
    TypedGenerationError,
    is_qualified_material_identity,
)
from llm.prompts import (
    COVER_LETTER_PROMPT,
    MATERIAL_PACKAGE_PROMPT,
    QA_ANSWERS_PROMPT,
    RECRUITER_MESSAGE_PROMPT,
    SYSTEM_PROMPT,
    build_salary_guidance,
    build_system_prompt,
)

logger = structlog.get_logger(__name__)

_FEEDBACK_BAD_MAX_CHARS = 600
_FEEDBACK_GOOD_MAX_CHARS = 600
_FEEDBACK_NOTE_MAX_CHARS = 160
_FEEDBACK_RESERVED_BAD_CHARS = 160
_FEEDBACK_RESERVED_GOOD_CHARS = 160
_FEEDBACK_RESERVED_NOTE_CHARS = 80

MaterialBlocker = Literal[
    "MATERIAL_CV_ARTIFACT_REQUIRED",
    "MATERIAL_COMPOSITION_INVALID",
    "MATERIAL_PROFILE_VERSION_REQUIRED",
    "MATERIAL_EVIDENCE_EMPTY",
    "MATERIAL_GENERATION_FAILED",
    "MATERIAL_MODEL_NOT_QUALIFIED",
    "RELEVANT_EXPERIENCE_UNSUPPORTED",
    "LLM_CIRCUIT_OPEN",
    "LLM_CONFIGURATION_INVALID",
    "LLM_LOCAL_MODEL_REQUIRED",
    "LLM_MODEL_MISSING",
    "LLM_POLICY_BLOCKED",
    "LLM_SCHEMA_INVALID",
    "LLM_TIMEOUT",
    "LLM_UNAVAILABLE",
    "SENSITIVE_FIELD_LLM_PROHIBITED",
    "SENSITIVE_CLAIM_PROHIBITED",
    "UNSUPPORTED_CLAIM",
    "UNFILLED_PLACEHOLDER",
    "UNTRUSTED_INPUT_BLOCKED",
]
MaterialEvidenceSourceV1 = Literal["cv", "user_confirmed"]
MaterialOpeningV1 = Literal["interest_role", "interest_opportunity"]
MaterialClosingV1 = Literal[
    "welcome_contribute",
    "learn_more",
    "thank_consideration",
]
MaterialAnswerFramingV1 = Literal[
    "interest_role",
    "interest_opportunity",
    "welcome_contribute",
    "learn_more",
]
_MATERIAL_FRAMING: dict[
    MaterialOpeningV1 | MaterialClosingV1 | MaterialAnswerFramingV1,
    str,
] = {
    "interest_role": "I am excited about this role.",
    "interest_opportunity": "I am interested in this opportunity.",
    "welcome_contribute": "I would welcome the opportunity to contribute.",
    "learn_more": "I look forward to learning more.",
    "thank_consideration": "Thank you for your consideration.",
}
_DETERMINISTIC_QA_SOURCE_REFS = frozenset(
    {
        "salary_expectations",
        "notice_period",
        "availability_date",
        "preferred_start",
    }
)
_SENSITIVE_QA_KEYS = frozenset(
    {
        "work_authorization",
        "visa",
        "visa_sponsorship",
        "sponsorship",
        "nationality",
        "citizenship",
        "clearance",
        "security_clearance",
        "certification",
        "licensing",
        "legal_status",
        "immigration_status",
        "right_to_work",
        "work_permit",
        "demographics",
        "consent",
        "attestation",
    }
)
_TYPED_ERROR_BLOCKERS: dict[LLMReasonCode, MaterialBlocker] = {
    LLMReasonCode.CONFIGURATION_INVALID: "LLM_CONFIGURATION_INVALID",
    LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED: "LLM_POLICY_BLOCKED",
    LLMReasonCode.PROMPT_TOO_LARGE: "LLM_CONFIGURATION_INVALID",
    LLMReasonCode.DEADLINE_EXCEEDED: "LLM_TIMEOUT",
    LLMReasonCode.CONCURRENCY_LIMIT: "LLM_UNAVAILABLE",
    LLMReasonCode.CIRCUIT_OPEN: "LLM_CIRCUIT_OPEN",
    LLMReasonCode.PROVIDER_UNAVAILABLE: "LLM_UNAVAILABLE",
    LLMReasonCode.MODEL_NOT_READY: "LLM_MODEL_MISSING",
    LLMReasonCode.MODEL_NOT_LOCAL: "LLM_LOCAL_MODEL_REQUIRED",
    LLMReasonCode.OUTPUT_INVALID: "LLM_SCHEMA_INVALID",
}


class MaterialQAAnswersV1(BaseModel):
    """Deterministically rendered non-sensitive application answers."""

    model_config = ConfigDict(extra="forbid")

    why_this_company: str = Field(max_length=1000)
    why_this_role: str = Field(max_length=1000)
    salary_expectations: str = Field(max_length=500)
    notice_period: str = Field(max_length=500)
    relevant_experience: str = Field(min_length=1, max_length=2000)


class MaterialDraftV1(BaseModel):
    """Private deterministic render; this model never crosses the LLM boundary."""

    model_config = ConfigDict(extra="forbid")

    cover_letter: str = Field(min_length=1, max_length=4000)
    recruiter_message: str = Field(min_length=1, max_length=1000)
    qa_answers: MaterialQAAnswersV1


class MaterialEvidenceSelectionV1(BaseModel):
    """A short model-selected ordinal bound to the displayed evidence kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_ordinal: int = Field(ge=1, le=99)
    source_kind: MaterialEvidenceSourceV1


class MaterialCompositionPlanV1(BaseModel):
    """Typed selection plan containing no model-authored application prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cover_letter_opening: MaterialOpeningV1
    cover_letter_evidence: tuple[MaterialEvidenceSelectionV1, ...] = Field(
        min_length=1,
        max_length=6,
    )
    cover_letter_closing: MaterialClosingV1
    recruiter_opening: MaterialOpeningV1
    recruiter_evidence: tuple[MaterialEvidenceSelectionV1, ...] = Field(
        default=(),
        max_length=2,
    )
    recruiter_closing: MaterialClosingV1
    why_this_company: MaterialAnswerFramingV1
    why_this_role: MaterialAnswerFramingV1
    relevant_experience_evidence: tuple[MaterialEvidenceSelectionV1, ...] = Field(
        min_length=1,
        max_length=3,
    )


class QAAnswerV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    answer: str = Field(max_length=5000)


class MaterialPackageV1(BaseModel):
    """Immutable, evidence-audited application material bound to one CV/profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["material-package-v1"] = "material-package-v1"
    cv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_version: int = Field(ge=1)
    prompt_version: Literal["application-materials-v1"] = MATERIAL_PROMPT_VERSION
    model_identity: ModelIdentity
    cover_letter: str
    recruiter_message: str
    qa_answers: tuple[QAAnswerV1, ...]
    claim_evidence: tuple[ClaimEvidenceRefV1, ...]
    relevant_experience_claim_digests: tuple[
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")], ...
    ] = Field(default=(), max_length=20)
    placeholder_fields: tuple[str, ...] = ()
    eligibility_blockers: tuple[MaterialBlocker, ...] = ()

    @property
    def eligible(self) -> bool:
        supported_claim_digests = {
            claim.claim_digest for claim in self.claim_evidence if claim.supported
        }
        relevant_experience = self.qa_answer_dict().get("relevant_experience", "").strip()
        return (
            bool(relevant_experience)
            and bool(self.relevant_experience_claim_digests)
            and set(self.relevant_experience_claim_digests).issubset(supported_claim_digests)
            and not self.placeholder_fields
            and not self.eligibility_blockers
            and all(claim.supported for claim in self.claim_evidence)
            and is_qualified_material_identity(
                provider=self.model_identity.provider,
                model=self.model_identity.model,
                local=self.model_identity.local,
                digest=self.model_identity.digest,
                prompt_version=self.prompt_version,
            )
        )

    def qa_answer_dict(self) -> dict[str, str]:
        return {item.field_id: item.answer for item in self.qa_answers}


@dataclass
class GeneratedApplication:
    """Container for all generated application materials."""

    cover_letter: str = ""
    recruiter_message: str = ""
    qa_answers: dict[str, str] = field(default_factory=dict)
    has_placeholders: bool = False
    placeholder_fields: list[str] = field(default_factory=list)
    cv_sha256: str | None = None
    profile_version: int | None = None
    claim_evidence: list[ClaimEvidenceRefV1] = field(default_factory=list)
    eligibility_blockers: list[MaterialBlocker] = field(default_factory=list)
    material_package: MaterialPackageV1 | None = None

    @property
    def eligible(self) -> bool:
        package = self.material_package
        if package is None:
            return False
        return (
            package.eligible
            and not self.has_placeholders
            and not self.eligibility_blockers
            and self.cv_sha256 == package.cv_sha256
            and self.profile_version == package.profile_version
            and self.cover_letter == package.cover_letter
            and self.recruiter_message == package.recruiter_message
            and self.qa_answers == package.qa_answer_dict()
            and self.claim_evidence == list(package.claim_evidence)
            and self.placeholder_fields == list(package.placeholder_fields)
        )


def _normalized_material_sentence(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).rstrip(".!?").casefold()


def _relevant_experience_claim_digests(
    relevant_experience: str,
    draft_claims: tuple[DraftClaimV1, ...],
    validated_claims: tuple[ClaimEvidenceRefV1, ...],
) -> tuple[str, ...]:
    """Bind relevant experience to supported exact output sentences only."""

    relevant_sentences = {
        normalized
        for sentence in material_sentences((relevant_experience,))
        if (normalized := _normalized_material_sentence(sentence))
    }
    if not relevant_sentences:
        return ()
    digests: list[str] = []
    for draft_claim, validated in zip(
        draft_claims,
        validated_claims,
        strict=True,
    ):
        if (
            validated.supported
            and _normalized_material_sentence(draft_claim.claim_text) in relevant_sentences
        ):
            digests.append(validated.claim_digest)
    return tuple(dict.fromkeys(digests))


def _check_placeholders(text: str) -> list[str]:
    """Find [PLACEHOLDER: ...] markers in generated text."""

    return re.findall(r"\[PLACEHOLDER:\s*([^\]]+)\]", text)


def _load_few_shot_examples(limit: int = 5) -> list[dict]:
    """Load the most recent cover letter feedback pairs from the DB.

    Returns a list of dicts with keys "bad", "good", "note".
    Returns an empty list if the DB is unavailable or has no feedback.
    """
    try:
        from db.models import CoverLetterFeedback
        from db.session import get_session_factory

        db = get_session_factory()()
        try:
            rows = (
                db.query(CoverLetterFeedback)
                .order_by(CoverLetterFeedback.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "bad": str(r.original_text or "")[:1500],
                    "good": str(r.corrected_text or "")[:1500],
                    "note": str(r.feedback_note or "")[:300],
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception as exc:
        logger.warning("few_shot_load_failed", reason_code=type(exc).__name__)
        return []


def _require_local_material_client(client: LLMClient) -> None:
    if not client.model_identity.local:
        raise TypedGenerationError(
            LLMReasonCode.MODEL_NOT_LOCAL,
            "private application materials require a local model",
        )


async def _require_qualified_material_client(client: LLMClient) -> ModelIdentity:
    """Bind a fresh local client to one exact ready model before prompting."""

    _require_local_material_client(client)
    identity = client.model_identity
    if identity.provider != "ollama" or identity.model != "qwen2.5:7b":
        raise MaterialPackageBlockedError("MATERIAL_MODEL_NOT_QUALIFIED")
    if is_qualified_material_identity(
        provider=identity.provider,
        model=identity.model,
        local=identity.local,
        digest=identity.digest,
        prompt_version=MATERIAL_PROMPT_VERSION,
    ):
        return identity

    runtime = getattr(client, "runtime", None)
    readiness = getattr(runtime, "readiness", None)
    if not callable(readiness):
        raise MaterialPackageBlockedError("MATERIAL_MODEL_NOT_QUALIFIED")
    settings = getattr(client, "settings", None) or get_settings()
    deadline = datetime.now(UTC) + timedelta(
        seconds=min(
            15.0,
            settings.ollama_request_timeout_seconds,
            settings.llm_generation_max_horizon_seconds,
        )
    )
    ready = await readiness(
        deadline=deadline,
        record_failure=True,
    )
    if not ready.ok:
        raise TypedGenerationError(
            ready.reason_code or LLMReasonCode.MODEL_NOT_READY,
            "qualified local material model is not ready",
        )
    identity = ready.model_identity
    if client.model_identity != identity:
        raise MaterialPackageBlockedError("MATERIAL_MODEL_NOT_QUALIFIED")
    if not is_qualified_material_identity(
        provider=identity.provider,
        model=identity.model,
        local=identity.local,
        digest=identity.digest,
        prompt_version=MATERIAL_PROMPT_VERSION,
    ):
        raise MaterialPackageBlockedError("MATERIAL_MODEL_NOT_QUALIFIED")
    return identity


def material_input_has_prompt_injection(job: JobData, cv_text: str = "") -> bool:
    """Detect adversarial instructions before any material-model invocation."""

    return contains_prompt_injection(
        "\n".join(
            (
                job.title or "",
                job.company or "",
                job.location or "",
                job.description or "",
                cv_text or "",
            )
        )
    )


def _require_safe_material_input(job: JobData, cv_text: str = "") -> None:
    if material_input_has_prompt_injection(job, cv_text):
        raise TypedGenerationError(
            LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED,
            "untrusted application input contains prohibited instructions",
        )


async def generate_cover_letter(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
    few_shot_examples: list[dict] | None = None,
    cv_text: str | None = None,
) -> str:
    """Generate a tailored cover letter for a specific job.

    Args:
        few_shot_examples: Optional list of {"bad", "good", "note"} dicts from
                           the feedback DB. Injected into the system prompt to
                           steer the LLM toward the user's preferred style.
                           If None, examples are auto-loaded from the DB.
        cv_text: Optional text of the specifically aligned CV.
    """
    if client is None:
        client = get_llm_client()
    _require_local_material_client(client)
    _require_safe_material_input(job, cv_text or profile.resume.text)

    if few_shot_examples is None:
        few_shot_examples = _load_few_shot_examples()

    system = build_system_prompt(few_shot_examples) if few_shot_examples else SYSTEM_PROMPT

    from llm.language import detect_language

    lang = detect_language(f"{job.title} {job.description}")
    if lang == "he":
        system += (
            "\nCRITICAL LANGUAGE INSTRUCTION: The job posting is in Hebrew. "
            "Write the cover letter in professional, fluent Hebrew."
        )

    resume_content = non_sensitive_cv_excerpt(
        cv_text if cv_text and cv_text.strip() else profile.resume.text,
        max_chars=4000,
    )

    prompt = COVER_LETTER_PROMPT.format(
        job_title=job.title,
        company=job.company,
        location=job.location,
        description=job.description[:3000],  # truncate to fit context
        name=profile.personal.name,
        user_location=profile.personal.location,
        resume_text=resume_content,
        project_spotlights="Use only projects explicitly present in the resume above.",
        cover_letter_style=profile.cover_letter.style,
    )

    result = await client.generate(prompt=prompt, system=system)
    logger.info(
        "cover_letter_generated",
        length=len(result),
        few_shot_count=len(few_shot_examples),
    )
    return result


async def generate_recruiter_message(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
) -> str:
    """Generate a short recruiter outreach message."""
    if client is None:
        client = get_llm_client()
    _require_local_material_client(client)
    _require_safe_material_input(job)

    key_skills = ", ".join(profile.preferences.keywords[:10])

    prompt = RECRUITER_MESSAGE_PROMPT.format(
        job_title=job.title,
        company=job.company,
        name=profile.personal.name,
        key_skills=key_skills,
    )

    result = await client.generate(prompt=prompt, system=SYSTEM_PROMPT, max_tokens=500)
    logger.info("recruiter_message_generated", length=len(result))
    return result


async def generate_qa_answers(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
    cv_text: str | None = None,
) -> dict[str, str]:
    """Generate answers to common application questions."""
    if client is None:
        client = get_llm_client()
    _require_local_material_client(client)
    _require_safe_material_input(job, cv_text or profile.resume.text)

    salary = profile.preferences.salary
    resume_content = non_sensitive_cv_excerpt(
        cv_text if cv_text and cv_text.strip() else profile.resume.text,
        max_chars=4000,
    )

    prompt = QA_ANSWERS_PROMPT.format(
        job_title=job.title,
        company=job.company,
        name=profile.personal.name,
        user_location=profile.personal.location,
        resume_text=resume_content,
        salary_guidance=build_salary_guidance(salary.min, salary.max, salary.currency),
    )

    try:
        result = await client.generate_json(prompt=prompt, system=SYSTEM_PROMPT)
        safe_result = {
            str(key): str(value)
            for key, value in result.items()
            if str(key).strip().casefold() not in _SENSITIVE_QA_KEYS
        }
        logger.info("qa_answers_generated", keys=list(safe_result))
        return safe_result
    except Exception as exc:
        logger.error("qa_generation_failed", reason_code=type(exc).__name__)
        return {}


class MaterialPackageBlockedError(RuntimeError):
    """Stable fail-closed outcome without prompt/provider error text."""

    def __init__(self, reason_code: MaterialBlocker) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sanitize_feedback_examples(rows: list[dict]) -> list[dict[str, str]]:
    """Return bounded, non-sensitive examples safe for the local system prompt."""

    sanitized: list[dict[str, str]] = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        raw_bad = str(row.get("bad") or "")
        raw_good = str(row.get("good") or "")
        raw_note = str(row.get("note") or "")
        combined = "\n".join((raw_bad, raw_good, raw_note))
        if contains_prompt_injection(combined) or contains_sensitive_text(combined):
            continue
        bad = raw_bad.strip()[:_FEEDBACK_BAD_MAX_CHARS]
        good = raw_good.strip()[:_FEEDBACK_GOOD_MAX_CHARS]
        note = raw_note.strip()[:_FEEDBACK_NOTE_MAX_CHARS]
        if not bad or not good:
            continue
        sanitized.append({"bad": bad, "good": good, "note": note})
    return sanitized


def _material_prompt_for_catalog(
    job: JobData,
    profile: UserProfile,
    catalog: tuple[EvidenceItemV1, ...],
) -> str:
    return MATERIAL_PACKAGE_PROMPT.format(
        job_title=(job.title or "")[:300],
        company=(job.company or "")[:200],
        location=(job.location or "")[:200],
        description=(job.description or "")[:2000],
        cover_letter_style=profile.cover_letter.style[:400],
        evidence_catalog=_render_material_evidence_catalog(catalog),
    )


def _render_material_evidence_catalog(catalog: tuple[EvidenceItemV1, ...]) -> str:
    """Render short prompt-only ordinals without exposing persistent evidence IDs."""

    return "\n".join(
        f"{ordinal}. [{item.source_kind}] {item.text}"
        for ordinal, item in enumerate(catalog, start=1)
    )


def _is_narrative_evidence(item: EvidenceItemV1) -> bool:
    return not (
        item.source_kind == "user_confirmed" and item.source_ref in _DETERMINISTIC_QA_SOURCE_REFS
    )


def _compact_feedback_example(example: dict[str, str]) -> dict[str, str]:
    return {
        "bad": example["bad"][:_FEEDBACK_RESERVED_BAD_CHARS],
        "good": example["good"][:_FEEDBACK_RESERVED_GOOD_CHARS],
        "note": example["note"][:_FEEDBACK_RESERVED_NOTE_CHARS],
    }


def _build_bounded_material_context(
    job: JobData,
    profile: UserProfile,
    catalog: tuple[EvidenceItemV1, ...],
    feedback_rows: list[dict],
) -> tuple[str, str, tuple[EvidenceItemV1, ...], int]:
    """Fit schema, base instructions, evidence, then feedback under one bound."""

    settings = get_settings()
    schema_text = json.dumps(
        MaterialCompositionPlanV1.model_json_schema(),
        separators=(",", ":"),
        ensure_ascii=True,
    )
    budget = settings.llm_max_prompt_chars - len(schema_text) - TYPED_REQUEST_RETRY_MARGIN_CHARS
    sanitized_examples = _sanitize_feedback_examples(feedback_rows)
    reserved_example = (
        _compact_feedback_example(sanitized_examples[0]) if sanitized_examples else None
    )
    reserved_system = build_system_prompt([reserved_example]) if reserved_example else SYSTEM_PROMPT
    selected: list[EvidenceItemV1] = []
    for item in catalog:
        if len(selected) >= 99:
            break
        candidate_catalog = tuple((*selected, item))
        candidate_prompt = _material_prompt_for_catalog(
            job,
            profile,
            candidate_catalog,
        )
        if len(candidate_prompt) + len(reserved_system) <= budget:
            selected.append(item)

    if not selected:
        raise MaterialPackageBlockedError("LLM_CONFIGURATION_INVALID")

    prompt_catalog = tuple(selected)
    prompt = _material_prompt_for_catalog(job, profile, prompt_catalog)
    chosen_examples: list[dict[str, str]] = []
    system = SYSTEM_PROMPT
    for index, example in enumerate(sanitized_examples):
        candidate_system = build_system_prompt([*chosen_examples, example])
        if len(prompt) + len(candidate_system) <= budget:
            chosen_examples.append(example)
            system = candidate_system
        elif index == 0 and reserved_example is not None:
            candidate_system = build_system_prompt([reserved_example])
            if len(prompt) + len(candidate_system) <= budget:
                chosen_examples.append(reserved_example)
                system = candidate_system

    if len(prompt) + len(system) > budget:
        raise MaterialPackageBlockedError("LLM_CONFIGURATION_INVALID")
    return prompt, system, prompt_catalog, len(chosen_examples)


def _resolve_material_selections(
    selections: tuple[MaterialEvidenceSelectionV1, ...],
    catalog: tuple[EvidenceItemV1, ...],
) -> tuple[EvidenceItemV1, ...]:
    """Resolve exact catalog items or reject the entire model plan."""

    resolved: list[EvidenceItemV1] = []
    seen_ordinals: set[int] = set()
    for selection in selections:
        ordinal = selection.evidence_ordinal
        if ordinal in seen_ordinals or ordinal > len(catalog):
            raise MaterialPackageBlockedError("MATERIAL_COMPOSITION_INVALID")
        seen_ordinals.add(ordinal)
        item = catalog[ordinal - 1]
        if (
            item.source_kind != selection.source_kind
            or not _is_narrative_evidence(item)
            or contains_sensitive_text(item.text)
            or contains_prompt_injection(item.text)
        ):
            raise MaterialPackageBlockedError("MATERIAL_COMPOSITION_INVALID")
        resolved.append(item)
    return tuple(resolved)


def _confirmed_qa_answer(
    profile: UserProfile,
    catalog: tuple[EvidenceItemV1, ...],
    *,
    source_ref: Literal["salary_expectations", "notice_period"],
    fallback: str,
) -> tuple[str, tuple[EvidenceItemV1, ...]]:
    """Render exact confirmed evidence without exposing it to the model."""

    confirmed = profile.evidence.confirmed_fact(source_ref)
    if not confirmed:
        return fallback, ()
    evidence = tuple(
        item
        for item in catalog
        if item.source_kind == "user_confirmed" and item.source_ref == source_ref
    )
    rendered = " ".join(item.text for item in evidence)
    normalized_confirmed = re.sub(r"\s+", " ", confirmed).strip()
    if (
        not evidence
        or rendered != normalized_confirmed
        or len(rendered) > 500
        or contains_sensitive_text(rendered)
        or contains_prompt_injection(rendered)
    ):
        raise MaterialPackageBlockedError("MATERIAL_COMPOSITION_INVALID")
    return rendered, evidence


def _render_material_plan(
    plan: MaterialCompositionPlanV1,
    profile: UserProfile,
    *,
    prompt_catalog: tuple[EvidenceItemV1, ...],
    full_catalog: tuple[EvidenceItemV1, ...],
) -> tuple[MaterialDraftV1, tuple[EvidenceItemV1, ...]]:
    """Render only fixed framing and exact evidence selected by ordinal."""

    cover_evidence = _resolve_material_selections(
        plan.cover_letter_evidence,
        prompt_catalog,
    )
    recruiter_evidence = _resolve_material_selections(
        plan.recruiter_evidence,
        prompt_catalog,
    )
    relevant_evidence = _resolve_material_selections(
        plan.relevant_experience_evidence,
        prompt_catalog,
    )
    salary, salary_evidence = _confirmed_qa_answer(
        profile,
        full_catalog,
        source_ref="salary_expectations",
        fallback="Open to discussion.",
    )
    notice, notice_evidence = _confirmed_qa_answer(
        profile,
        full_catalog,
        source_ref="notice_period",
        fallback="Best discussed with the hiring team.",
    )

    cover_letter = "\n\n".join(
        (
            "Dear Hiring Team.",
            _MATERIAL_FRAMING[plan.cover_letter_opening],
            "\n".join(item.text for item in cover_evidence),
            _MATERIAL_FRAMING[plan.cover_letter_closing],
        )
    )
    recruiter_parts = [
        _MATERIAL_FRAMING[plan.recruiter_opening],
        *(item.text for item in recruiter_evidence),
        _MATERIAL_FRAMING[plan.recruiter_closing],
    ]
    relevant_experience = "\n".join(item.text for item in relevant_evidence)
    draft = MaterialDraftV1(
        cover_letter=cover_letter,
        recruiter_message=" ".join(recruiter_parts),
        qa_answers=MaterialQAAnswersV1(
            why_this_company=_MATERIAL_FRAMING[plan.why_this_company],
            why_this_role=_MATERIAL_FRAMING[plan.why_this_role],
            salary_expectations=salary,
            notice_period=notice,
            relevant_experience=relevant_experience,
        ),
    )
    audit_catalog = tuple(
        {
            item.evidence_id: item
            for item in (
                *prompt_catalog,
                *salary_evidence,
                *notice_evidence,
            )
        }.values()
    )
    return draft, audit_catalog


async def generate_material_package(
    job: JobData,
    profile: UserProfile,
    *,
    cv_artifact: CVArtifact,
    profile_version: int,
    client: LLMClient | None = None,
) -> MaterialPackageV1:
    """Generate and validate one CV/profile-bound material package."""

    if profile_version < 1:
        raise MaterialPackageBlockedError("MATERIAL_PROFILE_VERSION_REQUIRED")
    if material_input_has_prompt_injection(job, cv_artifact.extracted_text):
        raise MaterialPackageBlockedError("UNTRUSTED_INPUT_BLOCKED")
    if client is None:
        client = get_llm_client()
    qualified_identity = await _require_qualified_material_client(client)

    full_catalog = build_evidence_catalog(profile, cv_artifact)
    narrative_catalog = tuple(item for item in full_catalog if _is_narrative_evidence(item))
    if not narrative_catalog:
        raise MaterialPackageBlockedError("MATERIAL_EVIDENCE_EMPTY")

    prompt, system, prompt_catalog, feedback_count = _build_bounded_material_context(
        job,
        profile,
        narrative_catalog,
        _load_few_shot_examples(),
    )
    generated = await client.generate_typed(
        response_model=MaterialCompositionPlanV1,
        prompt=prompt,
        purpose=GenerationPurpose.COVER_LETTER,
        prompt_version=MATERIAL_PROMPT_VERSION,
        deadline=datetime.now(UTC)
        + timedelta(seconds=get_settings().ollama_request_timeout_seconds),
        data_classification=DataClassification.PRIVATE_APPLICATION,
        system=system,
        max_tokens=1600,
        temperature=0.1,
    )
    draft, audit_catalog = _render_material_plan(
        generated.value,
        profile,
        prompt_catalog=prompt_catalog,
        full_catalog=full_catalog,
    )
    qa_dict = draft.qa_answers.model_dump()
    all_text = [
        draft.cover_letter,
        draft.recruiter_message,
        *qa_dict.values(),
    ]
    bound_claims = bind_generated_claims(all_text, audit_catalog)
    validation = validate_claim_evidence(all_text, bound_claims, audit_catalog)
    relevant_experience_claim_digests = _relevant_experience_claim_digests(
        draft.qa_answers.relevant_experience,
        bound_claims,
        validation.claims,
    )
    placeholders = tuple(dict.fromkeys(_check_placeholders(" ".join(all_text))))
    blockers: set[MaterialBlocker] = set()
    if generated.model_identity != qualified_identity or not is_qualified_material_identity(
        provider=generated.model_identity.provider,
        model=generated.model_identity.model,
        local=generated.model_identity.local,
        digest=generated.model_identity.digest,
        prompt_version=MATERIAL_PROMPT_VERSION,
    ):
        blockers.add("MATERIAL_MODEL_NOT_QUALIFIED")
    if placeholders:
        blockers.add("UNFILLED_PLACEHOLDER")
    if "SENSITIVE_CLAIM_PROHIBITED" in validation.blockers:
        blockers.add("SENSITIVE_CLAIM_PROHIBITED")
    if any(reason != "SENSITIVE_CLAIM_PROHIBITED" for reason in validation.blockers):
        blockers.add("UNSUPPORTED_CLAIM")
    if any(key.casefold() in _SENSITIVE_QA_KEYS for key in qa_dict):
        # The schema excludes these fields, but this defense remains at the
        # eligibility boundary in case a caller supplies a constructed model.
        blockers.add("SENSITIVE_FIELD_LLM_PROHIBITED")
    if not relevant_experience_claim_digests:
        blockers.add("RELEVANT_EXPERIENCE_UNSUPPORTED")

    package = MaterialPackageV1(
        cv_sha256=cv_artifact.pdf_sha256,
        profile_version=profile_version,
        model_identity=generated.model_identity,
        cover_letter=draft.cover_letter,
        recruiter_message=draft.recruiter_message,
        qa_answers=tuple(
            QAAnswerV1(field_id=field_id, answer=answer) for field_id, answer in qa_dict.items()
        ),
        claim_evidence=validation.claims,
        relevant_experience_claim_digests=relevant_experience_claim_digests,
        placeholder_fields=placeholders,
        eligibility_blockers=tuple(sorted(blockers)),
    )
    logger.info(
        "material_package_generated",
        cv_digest_prefix=cv_artifact.pdf_sha256[:12],
        profile_version=profile_version,
        claim_count=len(package.claim_evidence),
        prompt_evidence_count=len(prompt_catalog),
        feedback_example_count=feedback_count,
        blocker_count=len(package.eligibility_blockers),
        eligible=package.eligible,
    )
    return package


def _blocked_application(reason_code: MaterialBlocker) -> GeneratedApplication:
    return GeneratedApplication(eligibility_blockers=[reason_code])


async def generate_full_application(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
    cv_text: str | None = None,
    *,
    cv_artifact: CVArtifact | None = None,
    profile_version: int | None = None,
) -> GeneratedApplication:
    """Return a typed, evidence-validated application draft.

    ``cv_text`` remains accepted so old call sites fail safely instead of
    raising a signature error.  Raw text cannot establish PDF identity, so it
    never substitutes for ``cv_artifact`` and no LLM call is made without the
    exact CV hash and profile revision.
    """

    del cv_text
    if cv_artifact is None:
        return _blocked_application("MATERIAL_CV_ARTIFACT_REQUIRED")
    if profile_version is None or profile_version < 1:
        return _blocked_application("MATERIAL_PROFILE_VERSION_REQUIRED")
    try:
        package = await generate_material_package(
            job,
            profile,
            cv_artifact=cv_artifact,
            profile_version=profile_version,
            client=client,
        )
    except MaterialPackageBlockedError as exc:
        logger.warning("material_package_blocked", reason_code=exc.reason_code)
        return _blocked_application(exc.reason_code)
    except TypedGenerationError as exc:
        logger.warning("material_package_generation_failed", reason_code=exc.reason_code.value)
        return _blocked_application(
            _TYPED_ERROR_BLOCKERS.get(exc.reason_code, "MATERIAL_GENERATION_FAILED")
        )
    except Exception as exc:
        logger.warning("material_package_generation_failed", reason_code=type(exc).__name__)
        return _blocked_application("MATERIAL_GENERATION_FAILED")

    return GeneratedApplication(
        cover_letter=package.cover_letter,
        recruiter_message=package.recruiter_message,
        qa_answers=package.qa_answer_dict(),
        has_placeholders=bool(package.placeholder_fields),
        placeholder_fields=list(package.placeholder_fields),
        cv_sha256=package.cv_sha256,
        profile_version=package.profile_version,
        claim_evidence=list(package.claim_evidence),
        eligibility_blockers=list(package.eligibility_blockers),
        material_package=package,
    )
