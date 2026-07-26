from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.config import Settings
from llm.client import (
    TYPED_FORMAT_RETRY_CORRECTION,
    TYPED_REQUEST_RETRY_MARGIN_CHARS,
    TYPED_SCHEMA_INSTRUCTION_PREFIX,
    LLMClient,
    OllamaClient,
    _ollama_transport_schema,
)
from llm.contracts import LLMReasonCode, ModelIdentity, TypedGenerationError
from llm.ollama_runtime import OllamaRuntime, estimate_context_tokens
from llm.qualification_registry import load_qualified_local_model

_QUALIFIED_DIGEST = load_qualified_local_model().digest.removeprefix("sha256:")
_OLLAMA_VERSION = "0.31.1"


@pytest.fixture(autouse=True)
def _current_qualification_report(monkeypatch):
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda **_kwargs: True,
    )


class _BoundedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["supported"]
    reference: str = Field(pattern=r"^ev_[0-9a-f]{24}$", min_length=27, max_length=27)


class _RetryClient(LLMClient):
    def __init__(self, settings: Settings, outputs: list[dict[str, Any]]) -> None:
        self.settings = settings
        self.outputs = list(outputs)
        self.calls = 0

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider="fixture", model="bounded", local=True)

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        del prompt, system, max_tokens, temperature
        raise AssertionError("typed test must use JSON generation")

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        del prompt, system, max_tokens
        self.calls += 1
        return self.outputs.pop(0)


class _Response:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {"message": {"content": "{}"}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class _Transport:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> _Transport:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, _url: str, *, json: dict[str, Any]) -> _Response:
        self.posts.append(json)
        return _Response()


class _ModelSwapTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.readiness_calls = 0

    async def get(self, url: str) -> _Response:
        if url.endswith("/api/version"):
            return _Response({"version": _OLLAMA_VERSION})
        self.readiness_calls += 1
        digest = _QUALIFIED_DIGEST if self.readiness_calls == 1 else "b" * 64
        return _Response(
            {
                "models": [
                    {
                        "name": "qwen2.5:7b",
                        "digest": digest,
                        "size": 4_700_000_000,
                        "details": {"format": "gguf"},
                    }
                ]
            }
        )

    async def post(self, _url: str, *, json: dict[str, Any]) -> _Response:
        self.posts.append(json)
        return _Response(
            {"message": {"content": '{"answer":"supported","reference":"ev_' + ("a" * 24) + '"}'}}
        )


class _StableRetryTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.readiness_calls = 0

    async def get(self, url: str) -> _Response:
        if url.endswith("/api/version"):
            return _Response({"version": _OLLAMA_VERSION})
        self.readiness_calls += 1
        return _Response(
            {
                "models": [
                    {
                        "name": "qwen2.5:7b",
                        "digest": _QUALIFIED_DIGEST,
                        "size": 4_700_000_000,
                        "details": {"format": "gguf"},
                    }
                ]
            }
        )

    async def post(self, _url: str, *, json: dict[str, Any]) -> _Response:
        self.posts.append(json)
        content = (
            '{"answer":"wrong","reference":"bad"}'
            if len(self.posts) == 1
            else '{"answer":"supported","reference":"ev_' + ("a" * 24) + '"}'
        )
        return _Response({"message": {"content": content}})


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_provider": "ollama",
        "llm_model": "qwen2.5:7b",
        "ollama_base_url": "http://127.0.0.1:11434",
        "ollama_no_cloud": True,
        "tasks_always_eager": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=10)


def _exact_retry_prompt(settings: Settings) -> str:
    schema_text = json.dumps(
        _BoundedAnswer.model_json_schema(),
        separators=(",", ":"),
        ensure_ascii=True,
    )
    prompt_chars = (
        settings.llm_max_prompt_chars - len(schema_text) - TYPED_REQUEST_RETRY_MARGIN_CHARS
    )
    assert prompt_chars > 0
    return "p" * prompt_chars


def test_ollama_context_window_has_bounded_safe_default() -> None:
    assert _settings().ollama_num_ctx == 16_384
    with pytest.raises(ValidationError):
        _settings(ollama_num_ctx=8_191)
    with pytest.raises(ValidationError):
        _settings(ollama_num_ctx=32_769)


def test_default_request_deadline_and_lease_are_bounded_and_compatible() -> None:
    settings = _settings()

    assert settings.ollama_request_timeout_seconds == 120.0
    assert settings.llm_generation_max_horizon_seconds == 120.0
    assert settings.ollama_lease_ttl_seconds == 130
    OllamaRuntime(settings).validate_local_configuration()


def test_context_estimate_includes_utf8_schema_and_output_budget() -> None:
    estimate = estimate_context_tokens(
        prompt="א" * 100,
        system="system",
        response_format=_BoundedAnswer.model_json_schema(),
        max_tokens=300,
    )

    assert estimate > 556


@pytest.mark.asyncio
async def test_context_contract_fails_before_transport_instead_of_truncating() -> None:
    runtime = OllamaRuntime(
        _settings(
            ollama_num_ctx=8_192,
            llm_model="qwen2.5:context-preflight",
        )
    )

    with pytest.raises(TypedGenerationError) as exc_info:
        await runtime.chat(
            prompt="x" * 20_000,
            system="",
            max_tokens=4_000,
            temperature=0,
            response_format=_BoundedAnswer.model_json_schema(),
            deadline=_deadline(),
            require_ready_model=False,
        )

    assert exc_info.value.reason_code is LLMReasonCode.CONFIGURATION_INVALID


@pytest.mark.asyncio
async def test_ollama_transport_sends_explicit_context_window() -> None:
    settings = _settings(ollama_num_ctx=16_384)
    runtime = OllamaRuntime(settings)
    transport = _Transport()

    with patch("llm.ollama_runtime.httpx.AsyncClient", return_value=transport):
        await runtime.chat(
            prompt="bounded",
            system="",
            max_tokens=10,
            temperature=0,
            response_format=None,
            deadline=_deadline(),
            require_ready_model=False,
        )

    assert transport.posts[0]["options"]["num_ctx"] == 16_384


def test_transport_schema_drops_only_unsupported_grammar_hints() -> None:
    full_schema = _BoundedAnswer.model_json_schema()
    transport_schema = _ollama_transport_schema(full_schema)
    rendered = json.dumps(transport_schema, sort_keys=True)

    assert '"pattern"' not in rendered
    assert '"minLength"' not in rendered
    assert '"maxLength"' not in rendered
    assert transport_schema["additionalProperties"] is False
    assert transport_schema["required"] == ["answer", "reference"]


@pytest.mark.asyncio
async def test_exact_shared_retry_margin_allows_two_complete_requests() -> None:
    settings = _settings(llm_max_prompt_chars=1_000)
    client = _RetryClient(
        settings,
        [
            {"answer": "wrong", "reference": "bad"},
            {"answer": "supported", "reference": "ev_" + ("a" * 24)},
        ],
    )

    result = await client.generate_typed(
        response_model=_BoundedAnswer,
        prompt=_exact_retry_prompt(settings),
        purpose="test",
        prompt_version="v1",
        deadline=_deadline(),
        data_classification="internal",
    )

    assert result.attempts == 2
    assert client.calls == 2
    assert TYPED_REQUEST_RETRY_MARGIN_CHARS == len(TYPED_SCHEMA_INSTRUCTION_PREFIX) + len(
        TYPED_FORMAT_RETRY_CORRECTION
    )


@pytest.mark.asyncio
async def test_one_character_over_shared_retry_bound_never_calls_provider() -> None:
    settings = _settings(llm_max_prompt_chars=1_000)
    client = _RetryClient(
        settings,
        [{"answer": "supported", "reference": "ev_" + ("a" * 24)}],
    )

    with pytest.raises(TypedGenerationError) as exc_info:
        await client.generate_typed(
            response_model=_BoundedAnswer,
            prompt=_exact_retry_prompt(settings) + "x",
            purpose="test",
            prompt_version="v1",
            deadline=_deadline(),
            data_classification="internal",
        )

    assert exc_info.value.reason_code is LLMReasonCode.PROMPT_TOO_LARGE
    assert client.calls == 0


@pytest.mark.asyncio
async def test_excessive_future_deadline_is_rejected_without_clamping_or_provider_call() -> None:
    settings = _settings(
        llm_generation_max_horizon_seconds=1,
        ollama_request_timeout_seconds=1,
    )
    client = _RetryClient(
        settings,
        [{"answer": "supported", "reference": "ev_" + ("a" * 24)}],
    )

    with pytest.raises(TypedGenerationError) as exc_info:
        await client.generate_typed(
            response_model=_BoundedAnswer,
            prompt="bounded",
            purpose="test",
            prompt_version="v1",
            deadline=datetime.now(UTC) + timedelta(seconds=2),
            data_classification="internal",
        )

    assert exc_info.value.reason_code is LLMReasonCode.CONFIGURATION_INVALID
    assert client.calls == 0


@pytest.mark.asyncio
async def test_model_artifact_swap_during_typed_attempt_fails_closed() -> None:
    settings = _settings(ollama_base_url="http://127.0.0.1:11435")
    with patch("llm.client.get_settings", return_value=settings):
        client = OllamaClient()
    transport = _ModelSwapTransport()

    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", return_value=transport),
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        await client.generate_typed(
            response_model=_BoundedAnswer,
            prompt="bounded synthetic request",
            purpose="test",
            prompt_version="v1",
            deadline=_deadline(),
            data_classification="internal",
        )

    assert exc_info.value.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert transport.readiness_calls == 2
    assert len(transport.posts) == 1


@pytest.mark.asyncio
async def test_every_formatting_attempt_rechecks_digest_before_and_after_inference() -> None:
    settings = _settings(ollama_base_url="http://127.0.0.1:11436")
    with patch("llm.client.get_settings", return_value=settings):
        client = OllamaClient()
    transport = _StableRetryTransport()

    with patch("llm.ollama_runtime.httpx.AsyncClient", return_value=transport):
        result = await client.generate_typed(
            response_model=_BoundedAnswer,
            prompt="bounded synthetic request",
            purpose="test",
            prompt_version="v1",
            deadline=_deadline(),
            data_classification="internal",
        )

    assert result.attempts == 2
    assert transport.readiness_calls == 4
    assert len(transport.posts) == 2
