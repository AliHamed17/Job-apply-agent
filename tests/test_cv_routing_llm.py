"""LLM-based CV selection (profile/cv_routing_llm.py).

Covers the fallback path used when the deterministic keyword matcher
(profile/cv_routing.py::route_cv) can't confidently pick a CV — e.g. a job
posting with no scraped description. The LLM here actually reads each CV's
extracted PDF text rather than matching hand-tagged keywords.
"""

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


# ── load_cv_excerpts ──────────────────────────────────────────────────


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


def test_load_cv_excerpts_skips_unreadable_file(tmp_path):
    config = _config()

    def fake_get_text(cv_id, cv_routing_path=None, cv_directory=None):
        return "" if cv_id == "cv_a" else "b content"

    with patch("profile.cv_routing_llm.get_cv_text_by_id", side_effect=fake_get_text):
        excerpts = load_cv_excerpts(config, tmp_path)

    assert excerpts == {"cv_b": "b content"}


def test_load_cv_excerpts_skips_blank_text(tmp_path):
    config = _config()

    with patch("profile.cv_routing_llm.get_cv_text_by_id", side_effect=["   ", "real text"]):
        excerpts = load_cv_excerpts(config, tmp_path)

    assert excerpts == {"cv_b": "real text"}


# ── select_cv_via_llm ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_select_cv_via_llm_returns_selection():
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
async def test_select_cv_via_llm_abstains_on_unknown_id():
    client = MagicMock()
    client.generate_json = AsyncMock(return_value={"selected_cv_id": "cv_z", "confidence": 0.5})

    decision = await select_cv_via_llm(_job(), _config(), {"cv_a": "text"}, client=client)

    assert decision.selected_cv_id is None
    assert decision.fallback_reason == "llm_abstained"


@pytest.mark.asyncio
async def test_select_cv_via_llm_abstains_on_null_selection():
    client = MagicMock()
    client.generate_json = AsyncMock(return_value={"selected_cv_id": None, "confidence": 0.1})

    decision = await select_cv_via_llm(_job(), _config(), {"cv_a": "text"}, client=client)

    assert decision.selected_cv_id is None
    assert decision.fallback_reason == "llm_abstained"


@pytest.mark.asyncio
async def test_select_cv_via_llm_abstains_on_client_error():
    client = MagicMock()
    client.generate_json = AsyncMock(side_effect=RuntimeError("ollama unreachable"))

    decision = await select_cv_via_llm(_job(), _config(), {"cv_a": "text"}, client=client)

    assert decision.selected_cv_id is None
    assert decision.fallback_reason == "llm_routing_error"


@pytest.mark.asyncio
async def test_select_cv_via_llm_short_circuits_on_empty_excerpts():
    client = MagicMock()
    client.generate_json = AsyncMock()

    decision = await select_cv_via_llm(_job(), _config(), {}, client=client)

    assert decision.selected_cv_id is None
    assert decision.fallback_reason == "no_cv_text_available"
    client.generate_json.assert_not_called()
