"""Pluggable LLM clients with a fail-closed typed-generation boundary."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any, Never

import httpx  # noqa: F401  # Compatibility patch point for existing Ollama tests.
import structlog
from pydantic import ValidationError

from core.config import get_settings
from llm.contracts import (
    DataClassification,
    GenerationPurpose,
    LLMReasonCode,
    ModelIdentity,
    TModel,
    TypedGeneration,
    TypedGenerationError,
    normalize_deadline,
    normalize_prompt_version,
)
from llm.execution_guard import assert_llm_generation_allowed
from llm.ollama_runtime import OllamaRuntime

logger = structlog.get_logger(__name__)

TYPED_SCHEMA_INSTRUCTION_PREFIX = "\nReturn one JSON object matching this JSON Schema exactly:\n"
TYPED_FORMAT_RETRY_CORRECTION = (
    "\nThe previous response failed schema validation. Correct the format only; do not add facts."
)
TYPED_REQUEST_RETRY_MARGIN_CHARS = len(TYPED_SCHEMA_INSTRUCTION_PREFIX) + len(
    TYPED_FORMAT_RETRY_CORRECTION
)

_PRIVATE_APPLICATION_PURPOSES = frozenset(
    {
        GenerationPurpose.CV_ROUTING,
        GenerationPurpose.FORM_RESOLUTION,
        GenerationPurpose.COVER_LETTER,
        GenerationPurpose.PROFILE_EXTRACTION,
        GenerationPurpose.OUTREACH,
        GenerationPurpose.CULTURE_FIT,
        GenerationPurpose.INTERVIEW_PREP,
        GenerationPurpose.FOLLOWUP,
        GenerationPurpose.SALARY,
        GenerationPurpose.INTERVIEW_SIMULATION,
    }
)


def _reject_json_constant(value: str) -> Never:
    del value
    raise ValueError("non-finite JSON constant is prohibited")


def _require_request_within_prompt_bound(
    *,
    settings: object,
    prompt: str,
    system: str,
    schema_text: str,
    correction: str = "",
) -> None:
    """Reject a complete provider request that exceeds the configured bound."""

    limit = getattr(settings, "llm_max_prompt_chars", None)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or len(prompt) + len(system) + len(schema_text) + len(correction) > limit
    ):
        raise TypedGenerationError(
            LLMReasonCode.PROMPT_TOO_LARGE,
            "typed generation input exceeds the configured bound",
        )


def _ollama_transport_schema(value: object) -> object:
    """Drop only grammar-unsupported size/regex hints from Ollama's schema.

    The complete Pydantic schema is still used for the input budget and every
    provider result is validated against the original response model. This
    transport-only projection avoids Ollama grammar initialization failures;
    it does not weaken the typed eligibility boundary.
    """

    if isinstance(value, dict):
        return {
            key: _ollama_transport_schema(item)
            for key, item in value.items()
            if key not in {"pattern", "minLength", "maxLength"}
        }
    if isinstance(value, list):
        return [_ollama_transport_schema(item) for item in value]
    return value


class LLMClient(ABC):
    """Abstract LLM client interface."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        """Generate a text completion."""
        ...

    @abstractmethod
    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        """Generate a JSON-structured response."""
        ...

    @property
    def model_identity(self) -> ModelIdentity:
        """Return a bounded identity without exposing credentials or endpoints."""

        return ModelIdentity(
            provider=self.__class__.__name__.removesuffix("Client").lower(),
            model=str(getattr(self, "model", "unknown"))[:128],
            local=False,
        )

    async def _generate_schema_payload(
        self,
        *,
        prompt: str,
        system: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        deadline: datetime,
        attempt: int,
    ) -> dict[str, Any]:
        """Compatibility implementation for providers without native JSON schema."""

        del temperature
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TypedGenerationError(
                LLMReasonCode.DEADLINE_EXCEEDED,
                "generation deadline elapsed before provider request",
            )
        schema_text = json.dumps(schema, separators=(",", ":"), ensure_ascii=True)
        correction = TYPED_SCHEMA_INSTRUCTION_PREFIX + schema_text
        if attempt == 2:
            correction += TYPED_FORMAT_RETRY_CORRECTION
        _require_request_within_prompt_bound(
            settings=getattr(self, "settings", None) or get_settings(),
            prompt=prompt,
            system=system,
            schema_text="",
            correction=correction,
        )
        try:
            result = await asyncio.wait_for(
                self.generate_json(
                    prompt=prompt + correction,
                    system=system,
                    max_tokens=max_tokens,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            raise TypedGenerationError(
                LLMReasonCode.DEADLINE_EXCEEDED,
                "generation exceeded its bounded deadline",
            ) from None
        if not isinstance(result, dict):
            raise ValueError("provider output was not an object")
        return result

    async def generate_typed(
        self,
        *,
        response_model: type[TModel],
        prompt: str,
        purpose: GenerationPurpose | str,
        prompt_version: str,
        deadline: datetime,
        data_classification: DataClassification | str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> TypedGeneration[TModel]:
        """Generate one schema-valid value with one formatting retry at most."""

        assert_llm_generation_allowed()
        settings = getattr(self, "settings", None) or get_settings()
        try:
            bounded_purpose = GenerationPurpose(purpose)
            classification = DataClassification(data_classification)
        except ValueError:
            raise TypedGenerationError(
                LLMReasonCode.CONFIGURATION_INVALID,
                "generation purpose or data classification is not supported",
            ) from None
        if (
            bounded_purpose in _PRIVATE_APPLICATION_PURPOSES
            and classification is not DataClassification.PRIVATE_APPLICATION
        ):
            raise TypedGenerationError(
                LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED,
                "private application purposes require private_application classification",
            )
        version = normalize_prompt_version(prompt_version)
        bounded_deadline = normalize_deadline(deadline)
        now = datetime.now(UTC)
        if bounded_deadline <= now:
            raise TypedGenerationError(
                LLMReasonCode.DEADLINE_EXCEEDED,
                "generation deadline has elapsed",
            )
        maximum_deadline = now + timedelta(seconds=settings.llm_generation_max_horizon_seconds)
        if bounded_deadline > maximum_deadline:
            raise TypedGenerationError(
                LLMReasonCode.CONFIGURATION_INVALID,
                "generation deadline exceeds the configured maximum horizon",
            )
        if classification is DataClassification.SENSITIVE_FACT:
            raise TypedGenerationError(
                LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED,
                "sensitive factual data cannot be sent to an LLM",
            )
        identity = self.model_identity
        if classification is not DataClassification.PUBLIC and not identity.local:
            raise TypedGenerationError(
                LLMReasonCode.MODEL_NOT_LOCAL,
                "non-public generation requires a local model",
            )

        schema = response_model.model_json_schema()
        schema_text = json.dumps(schema, separators=(",", ":"), ensure_ascii=True)
        _require_request_within_prompt_bound(
            settings=settings,
            prompt=prompt,
            system=system,
            schema_text=schema_text,
            correction=(TYPED_SCHEMA_INSTRUCTION_PREFIX + TYPED_FORMAT_RETRY_CORRECTION),
        )
        if not 1 <= max_tokens <= 16_384 or not 0 <= temperature <= 2:
            raise TypedGenerationError(
                LLMReasonCode.CONFIGURATION_INVALID,
                "generation options are outside bounded limits",
            )

        for attempt in (1, 2):
            try:
                payload = await self._generate_schema_payload(
                    prompt=prompt,
                    system=system,
                    schema=schema,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    deadline=bounded_deadline,
                    attempt=attempt,
                )
                serialized_payload = json.dumps(
                    payload,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                if len(serialized_payload) > settings.llm_max_prompt_chars:
                    raise ValueError("provider output exceeds the configured bound")
                value = response_model.model_validate(payload)
                # Ollama learns the exact digest during the required tags check.
                identity = self.model_identity
                if identity.provider == "ollama" and identity.digest is None:
                    raise TypedGenerationError(
                        LLMReasonCode.MODEL_NOT_READY,
                        "Ollama generation did not bind an exact model digest",
                    )
                return TypedGeneration(
                    value=value,
                    model_identity=identity,
                    purpose=bounded_purpose,
                    prompt_version=version,
                    data_classification=classification,
                    attempts=attempt,
                )
            except TypedGenerationError:
                raise
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
                if attempt == 2:
                    raise TypedGenerationError(
                        LLMReasonCode.OUTPUT_INVALID,
                        "provider output failed schema validation",
                    ) from None
        raise AssertionError("typed generation retry loop exhausted")


class OpenAIClient(LLMClient):
    """OpenAI API client (GPT-4o, etc.)."""

    def __init__(self):
        settings = get_settings()
        self.settings = settings
        try:
            import openai

            self.client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        except ImportError:
            raise ImportError("Install openai: pip install openai")
        self.model = settings.llm_model or "gpt-4o"

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider="openai", model=self.model, local=False)

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        assert_llm_generation_allowed()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        assert_llm_generation_allowed()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append(
            {
                "role": "user",
                "content": prompt + "\n\nRespond with valid JSON only.",
            }
        )

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content or "{}")


class AnthropicClient(LLMClient):
    """Anthropic Claude API client."""

    def __init__(self):
        settings = get_settings()
        self.settings = settings
        try:
            import anthropic

            self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        except ImportError:
            raise ImportError("Install anthropic: pip install anthropic")
        self.model = settings.llm_model or "claude-sonnet-4-20250514"

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider="anthropic", model=self.model, local=False)

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        assert_llm_generation_allowed()
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "You are a helpful assistant.",
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        assert_llm_generation_allowed()
        result = await self.generate(
            prompt=prompt + "\n\nRespond with valid JSON only. No markdown, no code blocks.",
            system=system,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        # Strip potential markdown wrapping
        result = result.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            if lines[-1].strip() == "```":
                result = "\n".join(lines[1:-1])
            else:
                result = "\n".join(lines[1:])
        return json.loads(result)


class OllamaClient(LLMClient):
    """Local Ollama client (no API key — runs against a local Ollama server)."""

    def __init__(self):
        settings = get_settings()
        self.settings = settings
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.llm_model or "qwen2.5:7b"
        self.runtime = OllamaRuntime(settings)

    @property
    def model_identity(self) -> ModelIdentity:
        return self.runtime.identity

    async def _chat(
        self,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        assert_llm_generation_allowed()
        deadline = datetime.now(UTC) + timedelta(
            seconds=self.runtime.settings.ollama_request_timeout_seconds
        )
        return await self.runtime.chat(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format="json" if json_mode else None,
            deadline=deadline,
        )

    async def _generate_schema_payload(
        self,
        *,
        prompt: str,
        system: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        deadline: datetime,
        attempt: int,
    ) -> dict[str, Any]:
        transport_schema = _ollama_transport_schema(schema)
        transport_schema_text = json.dumps(
            transport_schema,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        correction = ""
        if attempt == 2:
            correction = TYPED_FORMAT_RETRY_CORRECTION
        _require_request_within_prompt_bound(
            settings=self.settings,
            prompt=prompt,
            system=system,
            schema_text=transport_schema_text,
            correction=correction,
        )
        content = await self.runtime.chat(
            prompt=prompt + correction,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=transport_schema,
            deadline=deadline,
        )
        if len(content) > self.settings.llm_max_prompt_chars:
            raise ValueError("provider output exceeds the configured bound")
        payload = json.loads(
            content or "{}",
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError("provider output was not an object")
        return payload

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        assert_llm_generation_allowed()
        return await self._chat(prompt, system, max_tokens, temperature, json_mode=False)

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        assert_llm_generation_allowed()
        content = await self._chat(prompt, system, max_tokens, temperature=0.1, json_mode=True)
        return json.loads(content or "{}")


class MockClient(LLMClient):
    """Mock client for testing."""

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        assert_llm_generation_allowed()
        return "This is a mock response generated by the system. [PLACEHOLDER: Add details here]"

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        assert_llm_generation_allowed()
        return {"question_1": "mock answer 1", "question_2": "mock answer 2"}

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider="mock", model="deterministic-test", local=True)


def get_llm_client() -> LLMClient:
    """Factory — returns the LLM client configured via LLM_PROVIDER env var."""
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        return AnthropicClient()
    elif settings.llm_provider == "openai":
        return OpenAIClient()
    elif settings.llm_provider == "ollama":
        return OllamaClient()
    elif settings.llm_provider == "mock":
        return MockClient()
    else:
        raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
