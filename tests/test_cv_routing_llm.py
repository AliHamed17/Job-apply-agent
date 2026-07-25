"""Tests for the evidence-bounded LLM CV routing fallback."""

from __future__ import annotations

from profile.cv_routing import CVDefinition, CVRoutingConfig, RoutingJob
from profile.cv_routing_llm import load_cv_excerpts, select_cv_via_llm
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _config() -> CVRoutingConfig:
    return CVRoutingConfig(
        cvs=[
            CVDefinition(id="cv_a", file="a.pdf"),
            CVDefinition(id="cv_b", file="b.pdf"),
        ],
        fallback_cv_id="cv_a",
    )


def _job() -> RoutingJob:
    return RoutingJob(title="AI Engineer", description="Build LLM pipelines")


def test_load_cv_excerpts_extracts_and_truncates(tmp_path):
    config = _config()
    long_text = "x" * 3000

    def fake_get_text(cv_id, cv_routing_path=None, cv_directory=None):
        return long_text if cv_id == "cv_a" else "short bio for b"

    with patch("profile.cv_routing_llm.get_cv_text_by_id", side_effect=fake_get_text):
        excerpts = load_cv_excerpts(config, tmp_path)

    assert set(excerpts) == {"cv_a", "cv_b"}
    assert len(excerpts["cv_a"]) == 1800
    assert excerpts["cv_b"] == "short bio for b"


def test_load_cv_excerpts_skips_unreadable_and_blank_files(tmp_path):
    config = _config()

    def fake_get_text(cv_id, cv_routing_path=None, cv_directory=None):
        return "" if cv_id == "cv_a" else "   "

    with patch("profile.cv_routing_llm.get_cv_text_by_id", side_effect=fake_get_text):
        excerpts = load_cv_excerpts(config, tmp_path)

    assert excerpts == {}


@pytest.mark.asyncio
async def test_select_cv_via_llm_returns_supported_selection():
    client = MagicMock()
    client.generate_json = AsyncMock(
        return_value={
            "selected_cv_id": "cv_b",
            "confidence": 0.87,
            "reasoning": "Strong LLM/RAG overlap",
        }
    )

    decision = await select_cv_via_llm(
        _job(), _config(), {"cv_a": "generic text", "cv_b": "LLM RAG pytorch"}, client=client
    )

    assert decision.selected_cv_id == "cv_b"
    assert decision.selected_file == "b.pdf"
    assert decision.confidence == 0.87
    assert decision.fallback_reason is None
    assert decision.matched_evidence == ["llm:Strong LLM/RAG overlap"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"selected_cv_id": "cv_z", "confidence": 0.5},
        {"selected_cv_id": None, "confidence": 0.1},
        {"selected_cv_id": ["cv_b"], "confidence": 0.9},
    ],
)
async def test_select_cv_via_llm_rejects_unsupported_selection(result):
    client = MagicMock()
    client.generate_json = AsyncMock(return_value=result)

    decision = await select_cv_via_llm(_job(), _config(), {"cv_a": "text"}, client=client)

    assert decision.selected_cv_id is None
    assert decision.fallback_reason == "llm_abstained"


@pytest.mark.asyncio
async def test_select_cv_via_llm_marks_low_confidence_for_review():
    client = MagicMock()
    client.generate_json = AsyncMock(
        return_value={
            "selected_cv_id": "cv_b",
            "confidence": 0.2,
            "reasoning": "Only weak overlap",
        }
    )

    decision = await select_cv_via_llm(
        _job(), _config(), {"cv_a": "generic text", "cv_b": "some text"}, client=client
    )

    assert decision.selected_cv_id == "cv_b"
    assert decision.fallback_reason == "llm_confidence_below_threshold"


@pytest.mark.asyncio
async def test_select_cv_via_llm_rejects_non_finite_confidence():
    client = MagicMock()
    client.generate_json = AsyncMock(
        return_value={
            "selected_cv_id": "cv_b",
            "confidence": "NaN",
            "reasoning": "Malformed provider confidence",
        }
    )

    decision = await select_cv_via_llm(
        _job(), _config(), {"cv_a": "generic text", "cv_b": "some text"}, client=client
    )

    assert decision.selected_cv_id == "cv_b"
    assert decision.confidence == 0.0
    assert decision.fallback_reason == "llm_confidence_below_threshold"


@pytest.mark.asyncio
async def test_select_cv_via_llm_abstains_on_provider_error_or_missing_text():
    client = MagicMock()
    client.generate_json = AsyncMock(side_effect=RuntimeError("ollama unreachable"))

    error_decision = await select_cv_via_llm(_job(), _config(), {"cv_a": "text"}, client=client)
    empty_decision = await select_cv_via_llm(_job(), _config(), {}, client=client)

    assert error_decision.fallback_reason == "llm_routing_error"
    assert empty_decision.fallback_reason == "no_cv_text_available"
    client.generate_json.assert_awaited_once()
