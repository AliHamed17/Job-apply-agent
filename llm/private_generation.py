"""Single fail-closed boundary for private application LLM features."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.config import get_settings
from llm.client import LLMClient
from llm.contracts import (
    DataClassification,
    GenerationPurpose,
    LLMReasonCode,
    TModel,
    TypedGeneration,
    TypedGenerationError,
)


def bounded_private_generation_reason(error: Exception) -> str:
    """Return a stable log-safe reason without provider or prompt text."""

    if isinstance(error, TypedGenerationError):
        return error.reason_code.value
    return "LLM_UNEXPECTED_FAILURE"


def require_private_candidate_context(context: str) -> None:
    """Abstain when policy filtering leaves no candidate evidence."""

    if not context.strip():
        raise TypedGenerationError(
            LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED,
            "no policy-safe candidate context is available",
        )


async def generate_private_application_typed(
    *,
    client: LLMClient,
    response_model: type[TModel],
    prompt: str,
    purpose: GenerationPurpose,
    prompt_version: str,
    system: str,
    max_tokens: int = 2000,
) -> TypedGeneration[TModel]:
    """Generate schema-valid private content using a local model only.

    The explicit identity check happens before dispatching to ``generate_typed``.
    It keeps the boundary effective even if a future client overrides that
    method, while ``generate_typed`` independently enforces the same policy.
    """

    if not client.model_identity.local:
        raise TypedGenerationError(
            LLMReasonCode.MODEL_NOT_LOCAL,
            "private application generation requires a local model",
        )
    settings = getattr(client, "settings", None) or get_settings()
    deadline = datetime.now(UTC) + timedelta(seconds=settings.llm_generation_max_horizon_seconds)
    return await client.generate_typed(
        response_model=response_model,
        prompt=prompt,
        purpose=purpose,
        prompt_version=prompt_version,
        deadline=deadline,
        data_classification=DataClassification.PRIVATE_APPLICATION,
        system=system,
        max_tokens=max_tokens,
        temperature=0.1,
    )
