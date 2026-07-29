"""OllamaClient — local LLM provider, no API key.

Verifies the request payload sent to Ollama's /api/chat endpoint and the
response parsing for both generate() and generate_json(), without hitting
a real Ollama server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import Settings
from llm.client import OllamaClient
from llm.qualification_registry import load_qualified_local_model

_QUALIFIED_DIGEST = load_qualified_local_model().digest


def _client(**overrides) -> OllamaClient:
    settings = Settings(
        _env_file=None,
        llm_provider="ollama",
        llm_model="qwen2.5:7b",
        ollama_base_url="http://localhost:11434",
        **overrides,
    )
    with patch("llm.client.get_settings", return_value=settings):
        return OllamaClient()


def _mock_json_response(payload: object):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _mock_response(content: str):
    return _mock_json_response({"message": {"role": "assistant", "content": content}})


def _mock_readiness_get() -> AsyncMock:
    async def get(url: str):
        if url.endswith("/api/version"):
            return _mock_json_response({"version": "0.31.1"})
        if url.endswith("/api/tags"):
            return _mock_json_response(
                {
                    "models": [
                        {
                            "name": "qwen2.5:7b",
                            "model": "qwen2.5:7b",
                            "digest": _QUALIFIED_DIGEST,
                            "size": 4_700_000_000,
                            "details": {"format": "gguf"},
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected Ollama readiness URL: {url}")

    return AsyncMock(side_effect=get)


@pytest.mark.asyncio
async def test_generate_posts_correct_payload_and_parses_content():
    client = _client()
    mock_post = AsyncMock(return_value=_mock_response("Hello from Qwen"))

    with (
        patch("llm.client.httpx.AsyncClient") as mock_ac,
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            return_value=True,
        ),
    ):
        transport = mock_ac.return_value.__aenter__.return_value
        transport.get = _mock_readiness_get()
        transport.post = mock_post
        result = await client.generate(
            prompt="Say hi", system="You are helpful.", max_tokens=50, temperature=0.7
        )

    assert result == "Hello from Qwen"
    url, kwargs = mock_post.call_args[0][0], mock_post.call_args.kwargs
    assert url == "http://localhost:11434/api/chat"
    payload = kwargs["json"]
    assert payload["model"] == "qwen2.5:7b"
    assert payload["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Say hi"},
    ]
    assert payload["options"] == {
        "temperature": 0.7,
        "num_predict": 50,
        "num_ctx": 16_384,
    }
    assert payload["stream"] is False
    assert "format" not in payload  # plain generate() must not force JSON mode


@pytest.mark.asyncio
async def test_generate_json_forces_json_format_and_parses_dict():
    client = _client()
    mock_post = AsyncMock(return_value=_mock_response('{"question_1": "5 years"}'))

    with (
        patch("llm.client.httpx.AsyncClient") as mock_ac,
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            return_value=True,
        ),
    ):
        transport = mock_ac.return_value.__aenter__.return_value
        transport.get = _mock_readiness_get()
        transport.post = mock_post
        result = await client.generate_json(prompt="Answer the question")

    assert result == {"question_1": "5 years"}
    payload = mock_post.call_args.kwargs["json"]
    assert payload["format"] == "json"
    assert payload["options"]["temperature"] == 0.1


@pytest.mark.asyncio
async def test_no_system_message_when_system_empty():
    client = _client()
    mock_post = AsyncMock(return_value=_mock_response("ok"))

    with (
        patch("llm.client.httpx.AsyncClient") as mock_ac,
        patch(
            "llm.qualification_registry.qualified_model_report_is_current",
            return_value=True,
        ),
    ):
        transport = mock_ac.return_value.__aenter__.return_value
        transport.get = _mock_readiness_get()
        transport.post = mock_post
        await client.generate(prompt="hi")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_factory_returns_ollama_client_for_ollama_provider():
    from llm.client import get_llm_client

    settings = Settings(_env_file=None, llm_provider="ollama", llm_model="qwen2.5:7b")
    with patch("llm.client.get_settings", return_value=settings):
        client = get_llm_client()
    assert isinstance(client, OllamaClient)
