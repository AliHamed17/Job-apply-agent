"""Cloud vision is an explicit, fail-closed exception to local inference."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import Settings
from llm import vision
from llm.contracts import LLMReasonCode, TypedGenerationError
from llm.execution_guard import prohibit_llm_generation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "analyzer", "constructor_name"),
    (
        ("openai", vision.analyze_screenshot_openai, "AsyncOpenAI"),
        ("anthropic", vision.analyze_screenshot_anthropic, "AsyncAnthropic"),
    ),
)
@pytest.mark.parametrize(
    ("app_env", "cloud_enabled", "no_cloud"),
    (
        ("development", False, False),
        ("test", True, True),
        ("production", True, False),
    ),
)
async def test_disabled_cloud_vision_never_constructs_a_cloud_client(
    monkeypatch,
    provider: str,
    analyzer,
    constructor_name: str,
    app_env: str,
    cloud_enabled: bool,
    no_cloud: bool,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env=app_env,
        llm_provider=provider,
        cloud_vision_enabled=cloud_enabled,
        ollama_no_cloud=no_cloud,
    )
    monkeypatch.setattr(vision, "get_settings", lambda: settings)
    constructor = MagicMock(side_effect=AssertionError("cloud client constructed"))
    monkeypatch.setitem(
        sys.modules,
        provider,
        SimpleNamespace(**{constructor_name: constructor}),
    )

    assert await analyzer(b"private-pixels", "https://example.test/job?token=secret") == {}
    constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    (
        Settings(
            _env_file=None,
            llm_provider="ollama",
            cloud_vision_enabled=False,
            ollama_no_cloud=True,
        ),
        Settings(
            _env_file=None,
            llm_provider="openai",
            cloud_vision_enabled=True,
            ollama_no_cloud=True,
        ),
        Settings(
            _env_file=None,
            llm_provider="mock",
            cloud_vision_enabled=True,
            ollama_no_cloud=False,
        ),
    ),
)
async def test_no_cloud_paths_return_before_screenshot_capture(
    monkeypatch,
    settings: Settings,
) -> None:
    screenshot = AsyncMock(side_effect=AssertionError("screenshot captured"))
    openai_analyzer = AsyncMock(side_effect=AssertionError("OpenAI called"))
    anthropic_analyzer = AsyncMock(side_effect=AssertionError("Anthropic called"))
    monkeypatch.setattr(vision, "get_settings", lambda: settings)
    monkeypatch.setattr(vision, "screenshot_url", screenshot)
    monkeypatch.setattr(vision, "analyze_screenshot_openai", openai_analyzer)
    monkeypatch.setattr(vision, "analyze_screenshot_anthropic", anthropic_analyzer)

    assert await vision.extract_job_via_vision("https://example.test/private") == {}
    screenshot.assert_not_awaited()
    openai_analyzer.assert_not_awaited()
    anthropic_analyzer.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_action_guard_blocks_opted_in_vision_before_client_construction(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        llm_provider="openai",
        cloud_vision_enabled=True,
        ollama_no_cloud=False,
    )
    monkeypatch.setattr(vision, "get_settings", lambda: settings)
    constructor = MagicMock(side_effect=AssertionError("cloud client constructed"))
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(AsyncOpenAI=constructor),
    )

    with prohibit_llm_generation():
        with pytest.raises(TypedGenerationError) as exc_info:
            await vision.analyze_screenshot_openai(
                b"private-pixels",
                "https://example.test/job",
            )

    assert exc_info.value.reason_code is LLMReasonCode.STAGE_PROHIBITED
    constructor.assert_not_called()


def test_malformed_vision_output_is_not_logged(monkeypatch) -> None:
    warning = MagicMock()
    monkeypatch.setattr(vision.logger, "warning", warning)
    private_output = "secret-model-output"

    assert vision._parse_vision_json(private_output) == {}

    assert private_output not in repr(warning.call_args)
    assert warning.call_args.kwargs == {}
