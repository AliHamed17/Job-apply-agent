import pytest
from unittest.mock import AsyncMock

from profile.cv_content_cache import clear_cv_text_cache, get_cv_text_by_id
from profile.cv_routing import (
    CVRoutingConfig,
    RoutingDecision,
    RoutingJob,
    validate_cv_alignment,
)


def test_cv_content_cache():
    clear_cv_text_cache()
    # Test getting text by ID using real config
    text = get_cv_text_by_id("ai-engineer")
    assert "AI Engineer" in text or "Ali Hamed" in text
    assert len(text) > 500


@pytest.mark.asyncio
async def test_validate_cv_alignment_suggests_realignment():
    clear_cv_text_cache()
    config = CVRoutingConfig.model_validate({
        "cvs": [
            {"id": "software-engineer", "file": "Ali_Hamed_CV_Software_Engineer.pdf", "title_terms": ["software"]},
            {"id": "ai-engineer", "file": "Ali_Hamed_CV_AI_Engineer.pdf", "title_terms": ["ai"]},
        ]
    })

    job = RoutingJob(title="Senior LLM Agent Engineer", description="Building RAG and AI Agents")
    decision = RoutingDecision(
        selected_cv_id="software-engineer",
        selected_file="Ali_Hamed_CV_Software_Engineer.pdf",
        confidence=0.8,
        matched_evidence=["title:software"],
    )

    mock_llm = AsyncMock()
    mock_llm.generate_json.return_value = {
        "is_good_match": False,
        "alignment_score": 0.4,
        "reasoning": "Job specifically requires LLM agent expertise",
        "suggested_cv_id": "ai-engineer",
    }

    updated = await validate_cv_alignment(job, decision, config, client=mock_llm)
    assert updated.selected_cv_id == "ai-engineer"
    assert updated.selected_file == "Ali_Hamed_CV_AI_Engineer.pdf"
    assert "llm_suggested_realign:ai-engineer" in updated.matched_evidence
    assert updated.alignment_score == 0.4
