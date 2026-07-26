from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from unittest.mock import patch

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.config import Settings
from core.operations import llm_readiness
from llm.client import LLMClient, OllamaClient
from llm.contracts import (
    DataClassification,
    GenerationPurpose,
    LLMReasonCode,
    ModelIdentity,
    TypedGenerationError,
)
from llm.execution_guard import (
    assert_llm_generation_allowed,
    prohibit_llm_generation,
)
from llm.ollama_runtime import (
    _CIRCUIT_FAILURE_SCRIPT,
    _CIRCUIT_SUCCESS_SCRIPT,
    OllamaRuntime,
    _InferenceLease,
    is_allowed_local_ollama_url,
    is_cloud_model,
)
from llm.qualification_registry import load_qualified_local_model

_DIGEST = load_qualified_local_model().digest.removeprefix("sha256:")
_OLLAMA_VERSION = "0.31.1"


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: Literal["supported"]
    confidence: float = Field(ge=0, le=1)


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class _HTTP:
    def __init__(
        self,
        *,
        outputs: list[str] | None = None,
        tags: object | None = None,
        server_version: object = _OLLAMA_VERSION,
    ) -> None:
        self.outputs = list(outputs or ())
        self.tags = tags if tags is not None else _local_tags()
        self.server_version = server_version
        self.posts: list[dict[str, Any]] = []

    def async_client(self, **_kwargs: object) -> _AsyncContext:
        return _AsyncContext(self)

    def sync_client(self, **_kwargs: object) -> _SyncContext:
        return _SyncContext(self)

    async def get(self, url: str) -> _Response:
        if url.endswith("/api/version"):
            return _Response({"version": self.server_version})
        return _Response(self.tags)

    async def post(self, _url: str, *, json: dict[str, Any]) -> _Response:
        self.posts.append(json)
        return _Response({"message": {"content": self.outputs.pop(0)}})


class _AsyncContext:
    def __init__(self, transport: _HTTP) -> None:
        self.transport = transport

    async def __aenter__(self) -> _HTTP:
        return self.transport

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SyncContext:
    def __init__(self, transport: _HTTP) -> None:
        self.transport = transport

    def __enter__(self) -> _SyncContext:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        if url.endswith("/api/version"):
            return _Response({"version": self.transport.server_version})
        return _Response(self.transport.tags)


def _local_tags(**overrides: object) -> dict[str, object]:
    model: dict[str, object] = {
        "name": "qwen2.5:7b",
        "model": "qwen2.5:7b",
        "digest": f"sha256:{_DIGEST}",
        "size": 4_700_000_000,
        "details": {"format": "gguf"},
    }
    model.update(overrides)
    return {"models": [model]}


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_provider": "ollama",
        "llm_model": "qwen2.5:7b",
        "ollama_base_url": "http://127.0.0.1:11434",
        "tasks_always_eager": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _deadline(seconds: float = 10) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _ollama_client(settings: Settings) -> OllamaClient:
    with patch("llm.client.get_settings", return_value=settings):
        return OllamaClient()


def _runtime(settings: Settings) -> OllamaRuntime:
    return OllamaRuntime(settings)


@pytest.fixture(autouse=True)
def _current_qualification_report(monkeypatch):
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda **_kwargs: True,
    )


def test_local_ollama_is_the_default_without_environment(monkeypatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_NO_CLOUD", raising=False)
    settings = Settings(_env_file=None)

    assert settings.llm_provider == "ollama"
    assert settings.llm_model == "qwen2.5:7b"
    assert settings.ollama_no_cloud is True


@pytest.mark.asyncio
async def test_final_stage_guard_blocks_every_public_mock_generation_entrypoint() -> None:
    from llm.client import MockClient

    client = MockClient()
    with prohibit_llm_generation():
        with pytest.raises(TypedGenerationError) as text_error:
            await client.generate("private prompt")
        with pytest.raises(TypedGenerationError) as json_error:
            await client.generate_json("private prompt")
        with pytest.raises(TypedGenerationError) as typed_error:
            await client.generate_typed(
                response_model=_Answer,
                prompt="private prompt",
                purpose="test",
                prompt_version="v1",
                deadline=_deadline(),
                data_classification="internal",
            )

    assert {
        text_error.value.reason_code,
        json_error.value.reason_code,
        typed_error.value.reason_code,
    } == {LLMReasonCode.STAGE_PROHIBITED}
    assert "private prompt" not in str(text_error.value)
    assert_llm_generation_allowed()


@pytest.mark.asyncio
async def test_final_stage_guard_is_nested_and_context_local() -> None:
    async def child_created_inside_guard() -> LLMReasonCode:
        await asyncio.sleep(0)
        with pytest.raises(TypedGenerationError) as exc_info:
            assert_llm_generation_allowed()
        return exc_info.value.reason_code

    with prohibit_llm_generation():
        with prohibit_llm_generation():
            inherited = asyncio.create_task(child_created_inside_guard())
        with pytest.raises(TypedGenerationError) as still_blocked:
            assert_llm_generation_allowed()

    assert still_blocked.value.reason_code is LLMReasonCode.STAGE_PROHIBITED
    assert await inherited is LLMReasonCode.STAGE_PROHIBITED
    assert_llm_generation_allowed()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://127.99.1.2:11434",
        "http://[::1]:11434",
        "http://host.docker.internal:11434",
    ],
)
def test_only_explicit_local_ollama_hosts_are_allowed(url: str) -> None:
    assert is_allowed_local_ollama_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://ollama.example.test",
        "http://192.168.1.10:11434",
        "http://localhost:11434/api",
        "http://user:secret@localhost:11434",
        "file:///tmp/ollama.sock",
    ],
)
def test_nonlocal_or_ambiguous_ollama_endpoints_are_rejected(url: str) -> None:
    assert not is_allowed_local_ollama_url(url)


@pytest.mark.parametrize(
    "model",
    ["qwen:cloud", "gpt-oss:120b-cloud", "cloud-qwen:latest"],
)
def test_cloud_model_aliases_are_rejected(model: str) -> None:
    assert is_cloud_model(model)
    runtime = _runtime(_settings(llm_model=model))

    with pytest.raises(TypedGenerationError) as exc_info:
        runtime.validate_local_configuration()

    assert exc_info.value.reason_code is LLMReasonCode.MODEL_NOT_LOCAL


@pytest.mark.asyncio
async def test_exact_local_model_readiness_binds_digest() -> None:
    transport = _HTTP()
    runtime = _runtime(_settings())

    with patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client):
        readiness = await runtime.readiness()

    assert readiness.ok is True
    assert readiness.model_identity == ModelIdentity(
        provider="ollama",
        model="qwen2.5:7b",
        local=True,
        digest=f"sha256:{_DIGEST}",
    )
    assert readiness.as_check()["digest"] == f"sha256:{_DIGEST}"


@pytest.mark.asyncio
async def test_readiness_degrades_when_installed_digest_is_not_qualified() -> None:
    transport = _HTTP(tags=_local_tags(digest=f"sha256:{'f' * 64}"))
    runtime = _runtime(_settings())

    with patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client):
        readiness = await runtime.readiness()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert readiness.model_identity.digest == f"sha256:{'f' * 64}"


@pytest.mark.asyncio
async def test_normal_readiness_requires_current_passing_qualification_report() -> None:
    transport = _HTTP()
    runtime = OllamaRuntime(_settings())

    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            return_value=False,
        ),
    ):
        readiness = await runtime.readiness()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert readiness.model_identity.digest == f"sha256:{_DIGEST}"


@pytest.mark.asyncio
async def test_normal_readiness_accepts_exact_digest_with_current_passing_report() -> None:
    transport = _HTTP()
    runtime = OllamaRuntime(_settings())

    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            return_value=True,
        ),
    ):
        readiness = await runtime.readiness()

    assert readiness.ok is True
    assert readiness.model_identity.digest == f"sha256:{_DIGEST}"
    assert readiness.ollama_server_version == _OLLAMA_VERSION


@pytest.mark.asyncio
async def test_readiness_binds_live_server_version_and_inference_config() -> None:
    transport = _HTTP()
    runtime = OllamaRuntime(
        _settings(
            ollama_num_ctx=8_192,
            llm_max_prompt_chars=12_000,
        )
    )

    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            return_value=True,
        ) as currentness,
    ):
        readiness = await runtime.readiness()

    assert readiness.ok is True
    currentness.assert_called_once_with(
        ollama_server_version=_OLLAMA_VERSION,
        ollama_request_timeout_seconds=120.0,
        llm_generation_max_horizon_seconds=120.0,
        ollama_connect_timeout_seconds=3.0,
        ollama_lease_wait_seconds=10.0,
        ollama_lease_ttl_seconds=130,
        ollama_circuit_failure_threshold=3,
        ollama_circuit_reset_seconds=30.0,
        ollama_num_ctx=8_192,
        llm_max_prompt_chars=12_000,
        lease_mode="process_local",
        ollama_no_cloud=True,
    )


@pytest.mark.asyncio
async def test_readiness_rejects_changed_live_ollama_server_version() -> None:
    transport = _HTTP(server_version="0.31.2")
    runtime = OllamaRuntime(_settings())

    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            side_effect=lambda **kwargs: kwargs.get("ollama_server_version") == _OLLAMA_VERSION,
        ),
    ):
        readiness = await runtime.readiness()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert readiness.ollama_server_version == "0.31.2"
    assert readiness.as_check()["ollama_server_version"] == "0.31.2"


@pytest.mark.parametrize(
    "server_version",
    [None, "", "latest", "0.31.1\nspoof", "../../0.31.1", {"version": "0.31.1"}],
)
@pytest.mark.asyncio
async def test_readiness_rejects_missing_or_malformed_server_version(
    server_version: object,
) -> None:
    transport = _HTTP(server_version=server_version)
    runtime = OllamaRuntime(_settings())

    with patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client):
        readiness = await runtime.readiness()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.PROVIDER_UNAVAILABLE
    assert readiness.ollama_server_version is None


@pytest.mark.asyncio
async def test_server_version_drift_during_generation_fails_closed() -> None:
    class _DriftingServer(_HTTP):
        def __init__(self) -> None:
            super().__init__(outputs=['{"answer":"supported","confidence":0.9}'])
            self.version_calls = 0

        async def get(self, url: str) -> _Response:
            if url.endswith("/api/version"):
                self.version_calls += 1
                version = _OLLAMA_VERSION if self.version_calls == 1 else "0.31.2"
                return _Response({"version": version})
            return _Response(self.tags)

    transport = _DriftingServer()
    client = _ollama_client(_settings(ollama_base_url="http://127.0.0.1:11439"))

    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            side_effect=lambda **kwargs: kwargs.get("ollama_server_version") == _OLLAMA_VERSION,
        ),
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        await client.generate_typed(
            response_model=_Answer,
            prompt="Resolve the non-sensitive fixture.",
            purpose=GenerationPurpose.FORM_RESOLUTION,
            prompt_version="form-v1",
            deadline=_deadline(),
            data_classification=DataClassification.PRIVATE_APPLICATION,
        )

    assert exc_info.value.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert transport.version_calls == 2
    assert len(transport.posts) == 1


def test_sync_readiness_requires_current_passing_qualification_report() -> None:
    transport = _HTTP()
    runtime = OllamaRuntime(_settings())

    with (
        patch("llm.ollama_runtime.httpx.Client", transport.sync_client),
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            return_value=False,
        ),
    ):
        readiness = runtime.readiness_sync()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert readiness.model_identity.digest == f"sha256:{_DIGEST}"


def test_sync_readiness_rejects_changed_live_ollama_server_version() -> None:
    transport = _HTTP(server_version="0.31.2")
    runtime = OllamaRuntime(_settings())

    with (
        patch("llm.ollama_runtime.httpx.Client", transport.sync_client),
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            side_effect=lambda **kwargs: kwargs.get("ollama_server_version") == _OLLAMA_VERSION,
        ),
    ):
        readiness = runtime.readiness_sync()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert readiness.ollama_server_version == "0.31.2"


def test_sync_readiness_rejects_installed_digest_mismatch() -> None:
    transport = _HTTP(tags=_local_tags(digest=f"sha256:{'f' * 64}"))
    runtime = OllamaRuntime(_settings())

    with patch("llm.ollama_runtime.httpx.Client", transport.sync_client):
        readiness = runtime.readiness_sync()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_READY
    assert readiness.model_identity.digest == f"sha256:{'f' * 64}"


@pytest.mark.parametrize(
    "overrides",
    [
        {"digest": None},
        {"digest": "f" * 63},
        {"size": 0},
        {"size": True},
        {"details": {"format": "safetensors"}},
        {"details": {}},
    ],
)
@pytest.mark.asyncio
async def test_name_only_or_unproven_model_is_not_ready(overrides: dict[str, object]) -> None:
    transport = _HTTP(tags=_local_tags(**overrides))
    runtime = _runtime(_settings())

    with patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client):
        readiness = await runtime.readiness()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_LOCAL
    assert readiness.model_identity.digest is None


@pytest.mark.asyncio
async def test_missing_exact_model_is_not_ready() -> None:
    transport = _HTTP(tags=_local_tags(name="qwen2.5:7b-latest"))
    runtime = _runtime(_settings())

    with patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client):
        readiness = await runtime.readiness()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.MODEL_NOT_READY


@pytest.mark.asyncio
async def test_generation_deadline_bounds_model_readiness_probe() -> None:
    runtime = _runtime(
        _settings(
            ollama_connect_timeout_seconds=3,
            ollama_circuit_failure_threshold=3,
        )
    )

    class _SlowHTTP(_AsyncContext):
        async def get(self, _url: str) -> _Response:
            await asyncio.sleep(1)
            return _Response(_local_tags())

    class _SlowContext:
        async def __aenter__(self) -> _SlowHTTP:
            return _SlowHTTP(_HTTP())

        async def __aexit__(self, *_args: object) -> None:
            return None

    started = asyncio.get_running_loop().time()
    with patch(
        "llm.ollama_runtime.httpx.AsyncClient",
        side_effect=lambda **_kwargs: _SlowContext(),
    ):
        status = await runtime.readiness(
            deadline=_deadline(0.05),
            record_failure=True,
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert not status.ok
    assert status.reason_code in {
        LLMReasonCode.PROVIDER_UNAVAILABLE,
        LLMReasonCode.DEADLINE_EXCEEDED,
    }
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_repeated_readiness_outage_opens_circuit_before_chat_transport() -> None:
    runtime = _runtime(
        _settings(
            llm_model="qwen2.5:readiness-circuit",
            ollama_circuit_failure_threshold=1,
        )
    )
    calls = 0

    class _FailingHTTP(_AsyncContext):
        async def get(self, _url: str) -> _Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("private readiness detail")

    class _FailingContext:
        async def __aenter__(self) -> _FailingHTTP:
            return _FailingHTTP(_HTTP())

        async def __aexit__(self, *_args: object) -> None:
            return None

    with patch(
        "llm.ollama_runtime.httpx.AsyncClient",
        side_effect=lambda **_kwargs: _FailingContext(),
    ):
        with pytest.raises(TypedGenerationError) as first:
            await runtime.chat(
                prompt="private",
                system="",
                max_tokens=10,
                temperature=0,
                response_format=None,
                deadline=_deadline(),
                require_ready_model=True,
            )
        with pytest.raises(TypedGenerationError) as second:
            await runtime.chat(
                prompt="private",
                system="",
                max_tokens=10,
                temperature=0,
                response_format=None,
                deadline=_deadline(),
                require_ready_model=True,
            )

    assert first.value.reason_code is LLMReasonCode.PROVIDER_UNAVAILABLE
    assert second.value.reason_code is LLMReasonCode.CIRCUIT_OPEN
    assert calls == 1
    assert "private readiness detail" not in str(first.value)


@pytest.mark.asyncio
async def test_typed_generation_uses_schema_digest_and_one_formatting_retry() -> None:
    transport = _HTTP(
        outputs=[
            '{"answer":"wrong","confidence":0.5}',
            '{"answer":"supported","confidence":0.9}',
        ]
    )
    client = _ollama_client(_settings())

    with patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client):
        result = await client.generate_typed(
            response_model=_Answer,
            prompt="Resolve the non-sensitive fixture.",
            purpose=GenerationPurpose.FORM_RESOLUTION,
            prompt_version="form-v1",
            deadline=_deadline(),
            data_classification=DataClassification.PRIVATE_APPLICATION,
        )

    assert result.value == _Answer(answer="supported", confidence=0.9)
    assert result.attempts == 2
    assert result.model_identity.digest == f"sha256:{_DIGEST}"
    assert len(transport.posts) == 2
    assert transport.posts[0]["format"] == _Answer.model_json_schema()
    assert "failed schema validation" in transport.posts[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_second_malformed_typed_output_fails_closed_without_more_calls() -> None:
    transport = _HTTP(outputs=["not json", '{"answer":"still-wrong"}'])
    client = _ollama_client(_settings())

    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        await client.generate_typed(
            response_model=_Answer,
            prompt="Resolve fixture.",
            purpose="form_resolution",
            prompt_version="v1",
            deadline=_deadline(),
            data_classification="private_application",
        )

    assert exc_info.value.reason_code is LLMReasonCode.OUTPUT_INVALID
    assert len(transport.posts) == 2
    assert "not json" not in str(exc_info.value)
    assert exc_info.value.__suppress_context__ is True


class _CloudFixtureClient(LLMClient):
    settings = Settings(_env_file=None)

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider="fixture-cloud", model="fixture", local=False)

    async def generate(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("cloud generation must not run")

    async def generate_json(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("cloud generation must not run")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "purpose",
    (
        GenerationPurpose.CV_ROUTING,
        GenerationPurpose.FORM_RESOLUTION,
        GenerationPurpose.COVER_LETTER,
        GenerationPurpose.PROFILE_EXTRACTION,
    ),
)
async def test_private_application_purposes_reject_public_classification_before_provider(
    purpose: GenerationPurpose,
) -> None:
    client = _CloudFixtureClient()

    with pytest.raises(TypedGenerationError) as exc_info:
        await client.generate_typed(
            response_model=_Answer,
            prompt="private application content mislabeled as public",
            purpose=purpose,
            prompt_version="v1",
            deadline=_deadline(),
            data_classification=DataClassification.PUBLIC,
        )

    assert exc_info.value.reason_code is LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED


@pytest.mark.asyncio
async def test_private_sensitive_or_mislabeled_application_never_reaches_cloud() -> None:
    client = _CloudFixtureClient()

    with pytest.raises(TypedGenerationError) as private_error:
        await client.generate_typed(
            response_model=_Answer,
            prompt="private CV evidence",
            purpose="form_resolution",
            prompt_version="v1",
            deadline=_deadline(),
            data_classification="private_application",
        )
    with pytest.raises(TypedGenerationError) as sensitive_error:
        await client.generate_typed(
            response_model=_Answer,
            prompt="nationality",
            purpose="form_resolution",
            prompt_version="v1",
            deadline=_deadline(),
            data_classification="sensitive_fact",
        )
    with pytest.raises(TypedGenerationError) as mislabeled_public_error:
        await client.generate_typed(
            response_model=_Answer,
            prompt="private form answer mislabeled as public",
            purpose="form_resolution",
            prompt_version="v1",
            deadline=_deadline(),
            data_classification="public",
        )

    assert private_error.value.reason_code is LLMReasonCode.MODEL_NOT_LOCAL
    assert sensitive_error.value.reason_code is LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED
    assert mislabeled_public_error.value.reason_code is LLMReasonCode.DATA_CLASSIFICATION_PROHIBITED


def test_process_lock_survives_distinct_event_loops() -> None:
    settings = _settings()

    async def acquire_once() -> None:
        async with _InferenceLease(settings, _deadline()):
            await asyncio.sleep(0)

    asyncio.run(acquire_once())
    asyncio.run(acquire_once())


class _SharedCircuitRedis:
    def __init__(self) -> None:
        self.failures = 0
        self.open = False
        self.closed = 0
        self.keys: set[str] = set()

    async def exists(self, key: str) -> int:
        self.keys.add(key)
        return int(self.open)

    async def eval(self, script: str, _keys: int, *args: object) -> int:
        failures_key = str(args[0])
        open_key = str(args[1])
        self.keys.update({failures_key, open_key})
        if script == _CIRCUIT_FAILURE_SCRIPT:
            if self.open:
                return -1
            self.failures += 1
            threshold = int(args[2])
            if self.failures >= threshold:
                self.open = True
                self.failures = 0
                return 1
            return 0
        if script == _CIRCUIT_SUCCESS_SCRIPT:
            if self.open:
                return -1
            self.failures = 0
            return 1
        raise AssertionError("unexpected circuit script")

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_distributed_circuit_is_shared_and_opens_at_exact_threshold() -> None:
    settings = _settings(
        tasks_always_eager=False,
        ollama_circuit_failure_threshold=2,
    )
    first = _runtime(settings)
    second = _runtime(settings)
    shared = _SharedCircuitRedis()

    with patch("llm.ollama_runtime._circuit_redis_client", return_value=shared):
        assert await first._record_failure_async(_deadline())
        await second._ensure_circuit_closed_async(_deadline())
        assert await second._record_failure_async(_deadline())
        with pytest.raises(TypedGenerationError) as exc_info:
            await first._ensure_circuit_closed_async(_deadline())

    assert exc_info.value.reason_code is LLMReasonCode.CIRCUIT_OPEN
    assert shared.closed == 4
    assert all("127.0.0.1" not in key and "qwen" not in key for key in shared.keys)


@pytest.mark.asyncio
async def test_stale_distributed_success_cannot_reopen_circuit() -> None:
    settings = _settings(
        tasks_always_eager=False,
        ollama_circuit_failure_threshold=1,
    )
    runtime = _runtime(settings)
    shared = _SharedCircuitRedis()

    with patch("llm.ollama_runtime._circuit_redis_client", return_value=shared):
        assert await runtime._record_failure_async(_deadline())
        assert not await runtime._record_success_async(_deadline())
        with pytest.raises(TypedGenerationError) as exc_info:
            await runtime._ensure_circuit_closed_async(_deadline())

    assert exc_info.value.reason_code is LLMReasonCode.CIRCUIT_OPEN


@pytest.mark.asyncio
async def test_distributed_circuit_outage_prevents_ollama_request() -> None:
    settings = _settings(tasks_always_eager=False)
    runtime = _runtime(settings)

    class _UnavailableCircuit:
        async def exists(self, _key: str) -> int:
            raise ConnectionError

        async def aclose(self) -> None:
            return None

    with (
        patch(
            "llm.ollama_runtime._circuit_redis_client",
            return_value=_UnavailableCircuit(),
        ),
        patch("llm.ollama_runtime.httpx.AsyncClient") as ollama_client,
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        await runtime.chat(
            prompt="private fixture",
            system="",
            max_tokens=10,
            temperature=0,
            response_format=None,
            deadline=_deadline(),
            require_ready_model=False,
        )

    assert exc_info.value.reason_code is LLMReasonCode.PROVIDER_UNAVAILABLE
    ollama_client.assert_not_called()


@pytest.mark.asyncio
async def test_waiting_caller_rechecks_circuit_after_lease_acquisition() -> None:
    settings = _settings(
        tasks_always_eager=False,
        ollama_circuit_failure_threshold=1,
        ollama_lease_wait_seconds=2,
    )
    first = _runtime(settings)
    second = _runtime(settings)
    circuit = _SharedCircuitRedis()
    second_checked_early = asyncio.Event()
    allow_first_failure = asyncio.Event()
    circuit_checks = 0
    request_count = 0

    original_exists = circuit.exists

    async def observed_exists(key: str) -> int:
        nonlocal circuit_checks
        circuit_checks += 1
        if circuit_checks >= 3:
            second_checked_early.set()
        return await original_exists(key)

    circuit.exists = observed_exists  # type: ignore[method-assign]

    class _Lease:
        async def set(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def eval(self, *_args: object) -> int:
            return 1

        async def aclose(self) -> None:
            return None

    class _FailingHTTP:
        async def post(self, _url: str, *, json: dict[str, Any]) -> _Response:
            del json
            nonlocal request_count
            request_count += 1
            await allow_first_failure.wait()
            raise httpx.ConnectError("bounded synthetic failure")

    class _FailingContext:
        async def __aenter__(self) -> _FailingHTTP:
            return _FailingHTTP()

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def invoke(runtime: OllamaRuntime) -> LLMReasonCode:
        with pytest.raises(TypedGenerationError) as exc_info:
            await runtime.chat(
                prompt="private fixture",
                system="",
                max_tokens=10,
                temperature=0,
                response_format=None,
                deadline=_deadline(3),
                require_ready_model=False,
            )
        return exc_info.value.reason_code

    with (
        patch("llm.ollama_runtime._circuit_redis_client", return_value=circuit),
        patch("llm.ollama_runtime._redis_client", side_effect=lambda *_args: _Lease()),
        patch(
            "llm.ollama_runtime.httpx.AsyncClient",
            side_effect=lambda **_kwargs: _FailingContext(),
        ),
    ):
        first_call = asyncio.create_task(invoke(first))
        while request_count == 0:
            await asyncio.sleep(0.01)
        second_call = asyncio.create_task(invoke(second))
        await asyncio.wait_for(second_checked_early.wait(), timeout=1)
        allow_first_failure.set()
        first_reason, second_reason = await asyncio.gather(first_call, second_call)

    assert first_reason is LLMReasonCode.PROVIDER_UNAVAILABLE
    assert second_reason is LLMReasonCode.CIRCUIT_OPEN
    assert request_count == 1


@pytest.mark.asyncio
async def test_distributed_mode_uses_owned_redis_lease() -> None:
    settings = _settings(tasks_always_eager=False)

    class _RedisLease:
        def __init__(self) -> None:
            self.set_call: tuple[tuple[object, ...], dict[str, object]] | None = None
            self.eval_call: tuple[object, ...] | None = None
            self.closed = False

        async def set(self, *args: object, **kwargs: object) -> bool:
            self.set_call = (args, kwargs)
            return True

        async def eval(self, *args: object) -> int:
            self.eval_call = args
            return 1

        async def aclose(self) -> None:
            self.closed = True

    redis_lease = _RedisLease()
    with patch("llm.ollama_runtime._redis_client", return_value=redis_lease):
        async with _InferenceLease(settings, _deadline()):
            pass

    assert redis_lease.set_call is not None
    _, set_kwargs = redis_lease.set_call
    assert set_kwargs == {
        "nx": True,
        "px": settings.ollama_lease_ttl_seconds * 1000,
    }
    assert redis_lease.eval_call is not None
    assert redis_lease.eval_call[1:3] == (1, "job-agent:llm:ollama:inference")
    assert redis_lease.closed is True


@pytest.mark.asyncio
async def test_distributed_success_state_outage_discards_model_response() -> None:
    settings = _settings(tasks_always_eager=False)
    runtime = _runtime(settings)
    transport = _HTTP(outputs=["must-not-be-returned"])

    class _Circuit:
        async def exists(self, _key: str) -> int:
            return 0

        async def eval(self, script: str, *_args: object) -> int:
            if script == _CIRCUIT_SUCCESS_SCRIPT:
                raise ConnectionError
            return 0

        async def aclose(self) -> None:
            return None

    class _Lease:
        async def set(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def eval(self, *_args: object) -> int:
            return 1

        async def aclose(self) -> None:
            return None

    with (
        patch("llm.ollama_runtime._circuit_redis_client", return_value=_Circuit()),
        patch("llm.ollama_runtime._redis_client", return_value=_Lease()),
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        await runtime.chat(
            prompt="private fixture",
            system="",
            max_tokens=10,
            temperature=0,
            response_format=None,
            deadline=_deadline(),
            require_ready_model=False,
        )

    assert exc_info.value.reason_code is LLMReasonCode.PROVIDER_UNAVAILABLE
    assert len(transport.posts) == 1


def test_distributed_sync_readiness_outage_never_contacts_ollama() -> None:
    settings = _settings(tasks_always_eager=False)
    runtime = _runtime(settings)

    class _UnavailableCircuit:
        def exists(self, _key: str) -> int:
            raise ConnectionError

        def close(self) -> None:
            return None

    with (
        patch(
            "llm.ollama_runtime._circuit_redis_client_sync",
            return_value=_UnavailableCircuit(),
        ),
        patch("llm.ollama_runtime.httpx.Client") as ollama_client,
    ):
        readiness = runtime.readiness_sync()

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.PROVIDER_UNAVAILABLE
    ollama_client.assert_not_called()


@pytest.mark.asyncio
async def test_distributed_lease_acquisition_is_bounded_by_generation_deadline() -> None:
    settings = _settings(tasks_always_eager=False, ollama_lease_wait_seconds=2)

    class _SlowRedisLease:
        def __init__(self) -> None:
            self.closed = False

        async def set(self, *_args: object, **_kwargs: object) -> bool:
            await asyncio.sleep(0.2)
            return True

        async def eval(self, *_args: object) -> int:
            return 1

        async def aclose(self) -> None:
            self.closed = True

    redis_lease = _SlowRedisLease()
    with (
        patch("llm.ollama_runtime._redis_client", return_value=redis_lease),
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        async with _InferenceLease(settings, _deadline(0.03)):
            raise AssertionError("expired lease unexpectedly entered")

    assert exc_info.value.reason_code is LLMReasonCode.DEADLINE_EXCEEDED
    assert redis_lease.closed is True
    # The failed distributed acquire must not leak the process-level lock.
    async with _InferenceLease(_settings(), _deadline()):
        pass


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_leak_process_lock() -> None:
    settings = _settings(ollama_lease_wait_seconds=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with _InferenceLease(settings, _deadline()):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with _InferenceLease(settings, _deadline()):
            raise AssertionError("cancelled waiter unexpectedly acquired")

    holding = asyncio.create_task(holder())
    await entered.wait()
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    waiting.cancel()
    with suppress(asyncio.CancelledError):
        await waiting
    release.set()
    await holding

    async with _InferenceLease(settings, _deadline()):
        pass


@pytest.mark.asyncio
async def test_cancelled_holder_releases_process_lock() -> None:
    settings = _settings(ollama_lease_wait_seconds=1)
    entered = asyncio.Event()

    async def holder() -> None:
        async with _InferenceLease(settings, _deadline()):
            entered.set()
            await asyncio.Event().wait()

    holding = asyncio.create_task(holder())
    await entered.wait()
    holding.cancel()
    with suppress(asyncio.CancelledError):
        await holding

    async with _InferenceLease(settings, _deadline()):
        pass


def test_settings_reject_lease_ttl_shorter_than_generation_horizon() -> None:
    with pytest.raises(ValidationError):
        _settings(
            ollama_request_timeout_seconds=60,
            llm_generation_max_horizon_seconds=120,
            ollama_lease_ttl_seconds=65,
        )


def test_runtime_rejects_constructed_lease_ttl_shorter_than_generation_horizon() -> None:
    valid = _settings()
    unsafe_values = valid.model_dump()
    unsafe_values["ollama_request_timeout_seconds"] = 60
    unsafe_values["llm_generation_max_horizon_seconds"] = 120
    unsafe_values["ollama_lease_ttl_seconds"] = 65
    runtime = _runtime(Settings.model_construct(**unsafe_values))

    with pytest.raises(TypedGenerationError) as exc_info:
        runtime.validate_local_configuration()

    assert exc_info.value.reason_code is LLMReasonCode.CONFIGURATION_INVALID


@pytest.mark.asyncio
async def test_request_timeout_caps_slow_drip_below_longer_caller_horizon() -> None:
    runtime = _runtime(
        _settings(
            llm_model="qwen2.5:request-timeout",
            ollama_request_timeout_seconds=1,
            llm_generation_max_horizon_seconds=5,
            ollama_lease_ttl_seconds=10,
        )
    )
    real_async_client = httpx.AsyncClient

    class _SlowDripBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (
                b'{"message":{"content":"',
                b"still ",
                b"arriving ",
                b'slowly"}}',
            ):
                await asyncio.sleep(0.4)
                yield chunk

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, stream=_SlowDripBody())

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    started = asyncio.get_running_loop().time()
    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        await runtime.chat(
            prompt="private fixture",
            system="",
            max_tokens=10,
            temperature=0,
            response_format=None,
            deadline=_deadline(5),
            require_ready_model=False,
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert exc_info.value.reason_code is LLMReasonCode.DEADLINE_EXCEEDED
    assert 0.8 <= elapsed < 1.5


@pytest.mark.asyncio
async def test_readiness_currentness_check_cannot_overrun_deadline() -> None:
    transport = _HTTP()
    runtime = _runtime(_settings())

    def slow_currentness(*_args: object) -> bool:
        time.sleep(0.2)
        return True

    started = asyncio.get_running_loop().time()
    with (
        patch("llm.ollama_runtime.httpx.AsyncClient", transport.async_client),
        patch.object(runtime, "_identity_is_current", side_effect=slow_currentness),
    ):
        readiness = await runtime.readiness(deadline=_deadline(0.05), record_failure=True)
    elapsed = asyncio.get_running_loop().time() - started

    assert readiness.ok is False
    assert readiness.reason_code is LLMReasonCode.DEADLINE_EXCEEDED
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_absolute_deadline_stops_slow_drip_response_and_releases_resources() -> None:
    runtime = _runtime(
        _settings(
            llm_model="qwen2.5:slow-drip-deadline",
            ollama_circuit_failure_threshold=3,
        )
    )
    real_async_client = httpx.AsyncClient
    request_count = 0

    class _SlowDripBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            for chunk in (
                b'{"message":{"content":"',
                b"still ",
                b"arriving ",
                b'slowly"}}',
            ):
                await asyncio.sleep(0.03)
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    body = _SlowDripBody()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, request=request, stream=body)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    started = asyncio.get_running_loop().time()
    with (
        patch(
            "llm.ollama_runtime.httpx.AsyncClient",
            side_effect=client_factory,
        ),
        pytest.raises(TypedGenerationError) as exc_info,
    ):
        await runtime.chat(
            prompt="private fixture",
            system="",
            max_tokens=10,
            temperature=0,
            response_format=None,
            deadline=_deadline(0.05),
            require_ready_model=False,
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert exc_info.value.reason_code is LLMReasonCode.DEADLINE_EXCEEDED
    assert request_count == 1
    assert elapsed < 0.3
    assert body.closed is True
    assert runtime._circuit.failures == 1

    # The timed-out request must not leak the process-level inference lease.
    async with _InferenceLease(_settings(), _deadline()):
        pass


@pytest.mark.asyncio
async def test_circuit_breaker_is_bounded_and_fails_before_second_request() -> None:
    settings = _settings(
        llm_model="qwen2.5:circuit-test",
        ollama_circuit_failure_threshold=1,
    )
    runtime = _runtime(settings)
    request_count = 0

    class _FailingHTTP(_AsyncContext):
        async def post(self, _url: str, *, json: dict[str, Any]) -> _Response:
            del json
            nonlocal request_count
            request_count += 1
            raise httpx.ConnectError("private provider detail")

    class _FailingContext:
        async def __aenter__(self) -> _FailingHTTP:
            return _FailingHTTP(_HTTP())

        async def __aexit__(self, *_args: object) -> None:
            return None

    with patch(
        "llm.ollama_runtime.httpx.AsyncClient",
        side_effect=lambda **_kwargs: _FailingContext(),
    ):
        with pytest.raises(TypedGenerationError) as first:
            await runtime.chat(
                prompt="private",
                system="",
                max_tokens=10,
                temperature=0,
                response_format=None,
                deadline=_deadline(),
                require_ready_model=False,
            )
        with pytest.raises(TypedGenerationError) as second:
            await runtime.chat(
                prompt="private",
                system="",
                max_tokens=10,
                temperature=0,
                response_format=None,
                deadline=_deadline(),
                require_ready_model=False,
            )

    assert first.value.reason_code is LLMReasonCode.PROVIDER_UNAVAILABLE
    assert second.value.reason_code is LLMReasonCode.CIRCUIT_OPEN
    assert request_count == 1
    assert "private provider detail" not in str(first.value)
    assert first.value.__suppress_context__ is True


def test_sync_readiness_and_operations_expose_only_bounded_model_identity() -> None:
    transport = _HTTP()
    settings = _settings()

    with patch("llm.ollama_runtime.httpx.Client", transport.sync_client):
        check = llm_readiness(settings)

    assert check == {
        "ok": True,
        "provider": "ollama",
        "model": "qwen2.5:7b",
        "local": True,
        "digest": f"sha256:{_DIGEST}",
        "ollama_server_version": _OLLAMA_VERSION,
    }
    assert "url" not in check


@pytest.mark.asyncio
async def test_all_sanitized_malformed_and_injection_outputs_fail_closed() -> None:
    from scripts.evaluate_v4_quality import DEFAULT_FIXTURES, evaluate_quality

    result = (await evaluate_quality(DEFAULT_FIXTURES))["tasks"]["malformed_output"]

    assert result["confusion_counts"] == {
        "correctly_blocked": 30,
        "incorrectly_accepted_or_misclassified": 0,
        "typed_rejected": 12,
        "semantic_blocked": 18,
    }
    assert result["semantic_prompt_injection_cases"] == 18
    assert result["semantic_prompt_injections_blocked"] == 18
    assert result["eligible_for_preparation"] == 0
    assert result["reason_counts"] == {
        "LLM_OUTPUT_INVALID": 12,
        "REQUIRED_FIELD_UNKNOWN": 6,
        "UNTRUSTED_INPUT_BLOCKED": 6,
        "llm_input_rejected": 6,
    }
