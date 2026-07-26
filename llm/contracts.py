"""Provider-neutral contracts for bounded, schema-validated LLM generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PROMPT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")

QUALIFIED_LOCAL_LLM_PROVIDER = "ollama"
QUALIFIED_LOCAL_LLM_MODEL = "qwen2.5:7b"
FORM_RESOLUTION_PROMPT_VERSION: Final[Literal["form-resolution-v1"]] = "form-resolution-v1"
MATERIAL_PROMPT_VERSION: Final[Literal["application-materials-v1"]] = "application-materials-v1"


class DataClassification(StrEnum):
    """Bounded classes accepted by the typed-generation boundary."""

    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE_APPLICATION = "private_application"
    SENSITIVE_FACT = "sensitive_fact"


class GenerationPurpose(StrEnum):
    """Known generation purposes used for policy, metrics, and audit."""

    CV_ROUTING = "cv_routing"
    FORM_RESOLUTION = "form_resolution"
    COVER_LETTER = "cover_letter"
    PROFILE_EXTRACTION = "profile_extraction"
    OUTREACH = "outreach"
    CULTURE_FIT = "culture_fit"
    INTERVIEW_PREP = "interview_prep"
    FOLLOWUP = "followup"
    SALARY = "salary"
    INTERVIEW_SIMULATION = "interview_simulation"
    QUALITY_EVALUATION = "quality_evaluation"
    TEST = "test"


class LLMReasonCode(StrEnum):
    """Stable, bounded failure reasons. Provider text must never become a reason."""

    CONFIGURATION_INVALID = "LLM_CONFIGURATION_INVALID"
    DATA_CLASSIFICATION_PROHIBITED = "LLM_DATA_CLASSIFICATION_PROHIBITED"
    PROMPT_TOO_LARGE = "LLM_PROMPT_TOO_LARGE"
    DEADLINE_EXCEEDED = "LLM_DEADLINE_EXCEEDED"
    CONCURRENCY_LIMIT = "LLM_CONCURRENCY_LIMIT"
    CIRCUIT_OPEN = "LLM_CIRCUIT_OPEN"
    PROVIDER_UNAVAILABLE = "LLM_PROVIDER_UNAVAILABLE"
    MODEL_NOT_READY = "LLM_MODEL_NOT_READY"
    MODEL_NOT_LOCAL = "LLM_MODEL_NOT_LOCAL"
    OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    STAGE_PROHIBITED = "LLM_STAGE_PROHIBITED"


class ModelIdentity(BaseModel):
    """Bounded identity of the model that actually produced an output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    local: bool
    digest: str | None = None

    @field_validator("provider")
    @classmethod
    def _provider_is_token(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _TOKEN_RE.fullmatch(normalized):
            raise ValueError("provider must be a bounded token")
        return normalized

    @field_validator("model")
    @classmethod
    def _model_is_bounded(cls, value: str) -> str:
        normalized = value.strip()
        if any(character in normalized for character in ("\r", "\n", "\x00")):
            raise ValueError("model identity contains an invalid character")
        return normalized

    @field_validator("digest")
    @classmethod
    def _digest_is_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("model digest must be a canonical sha256 digest")
        return value


def is_qualified_material_identity(
    *,
    provider: object,
    model: object,
    local: object,
    digest: object,
    prompt_version: object,
) -> bool:
    """Return whether material provenance exactly matches the qualified artifact."""

    from llm.qualification_registry import is_qualified_local_model_identity

    return bool(
        is_qualified_local_model_identity(
            provider=provider,
            model=model,
            local=local,
            digest=digest,
        )
        and prompt_version == MATERIAL_PROMPT_VERSION
    )


TModel = TypeVar("TModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TypedGeneration(Generic[TModel]):
    """Validated output plus the minimum provenance needed for audit."""

    value: TModel
    model_identity: ModelIdentity
    purpose: GenerationPurpose
    prompt_version: str
    data_classification: DataClassification
    attempts: int


class TypedGenerationError(RuntimeError):
    """Safe typed-generation failure that never contains prompt/provider text."""

    def __init__(self, reason_code: LLMReasonCode, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def normalize_prompt_version(value: str) -> str:
    """Validate a bounded, log-safe prompt version."""

    normalized = value.strip()
    if not _PROMPT_VERSION_RE.fullmatch(normalized):
        raise TypedGenerationError(
            LLMReasonCode.CONFIGURATION_INVALID,
            "prompt_version must be a bounded version token",
        )
    return normalized


def normalize_deadline(value: datetime) -> datetime:
    """Require an aware deadline and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise TypedGenerationError(
            LLMReasonCode.CONFIGURATION_INVALID,
            "deadline must be timezone-aware",
        )
    return value.astimezone(UTC)
