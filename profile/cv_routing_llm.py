"""LLM-based CV selection for low-confidence deterministic routes."""

from __future__ import annotations

import math
from pathlib import Path
from profile.cv_content_cache import get_cv_text_by_id
from profile.cv_routing import CVRoutingConfig, RoutingDecision, RoutingJob

import structlog

from llm.client import LLMClient, get_llm_client

logger = structlog.get_logger(__name__)

_EXCERPT_CHARS = 1800
_PROMPT = """You are matching a candidate's resume variants to a job posting.
Read each resume excerpt below and pick the ONE that is the best fit for
this specific job. Consider the actual skills, experience, and seniority
described in each resume - not just keyword overlap.

JOB:
Title: {title}
Description: {description}

CANDIDATE RESUMES (id: excerpt):
{cv_block}

Respond with ONLY JSON:
{{"selected_cv_id": "<one of the ids above, or null if truly none fit>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence citing specific evidence from the chosen resume>"}}
"""


def load_cv_excerpts(
    config: CVRoutingConfig,
    cv_directory: str | Path,
    cv_routing_path: str | Path | None = None,
) -> dict[str, str]:
    """Fetch bounded extracted text for each readable configured CV."""
    excerpts: dict[str, str] = {}
    for cv in config.cvs:
        text = get_cv_text_by_id(
            cv.id,
            cv_routing_path=cv_routing_path,
            cv_directory=cv_directory,
        )
        if text.strip():
            excerpts[cv.id] = text.strip()[:_EXCERPT_CHARS]
    return excerpts


async def select_cv_via_llm(
    job: RoutingJob,
    config: CVRoutingConfig,
    cv_excerpts: dict[str, str],
    client: LLMClient | None = None,
) -> RoutingDecision:
    """Select a configured CV, or abstain with an auditable reason."""
    if not cv_excerpts:
        return RoutingDecision(
            selected_cv_id=None,
            selected_file=None,
            confidence=0.0,
            matched_evidence=[],
            fallback_reason="no_cv_text_available",
        )

    client = client or get_llm_client()
    cv_block = "\n\n".join(f"[{cv_id}]\n{text}" for cv_id, text in cv_excerpts.items())
    prompt = _PROMPT.format(
        title=job.title or "(no title)",
        description=(job.description or "(no description provided)")[:3000],
        cv_block=cv_block,
    )

    try:
        result = await client.generate_json(prompt=prompt, max_tokens=300)
    except Exception as exc:
        logger.warning("llm_cv_routing_failed", error=str(exc))
        return RoutingDecision(
            selected_cv_id=None,
            selected_file=None,
            confidence=0.0,
            matched_evidence=[],
            fallback_reason="llm_routing_error",
        )

    if not isinstance(result, dict):
        selected_id = None
        reasoning = ""
    else:
        selected_id = result.get("selected_cv_id")
        reasoning = str(result.get("reasoning") or "")[:300]

    if not isinstance(selected_id, str):
        selected_id = None
    configured = {cv.id: cv for cv in config.cvs}
    selected = configured.get(selected_id)
    if selected is None or selected_id not in cv_excerpts:
        logger.info("llm_cv_routing_abstained", selected_id=selected_id, reasoning=reasoning)
        return RoutingDecision(
            selected_cv_id=None,
            selected_file=None,
            confidence=0.0,
            matched_evidence=[f"llm:{reasoning}"] if reasoning else [],
            fallback_reason="llm_abstained",
        )

    try:
        confidence = float(result.get("confidence", 0.0)) if isinstance(result, dict) else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    else:
        confidence = max(0.0, min(1.0, confidence))
    fallback_reason = (
        None
        if confidence >= config.minimum_confidence
        else "llm_confidence_below_threshold"
    )

    logger.info(
        "llm_cv_routing_selected",
        cv_id=selected.id,
        confidence=confidence,
        reasoning=reasoning,
    )
    return RoutingDecision(
        selected_cv_id=selected.id,
        selected_file=selected.file,
        confidence=confidence,
        matched_evidence=[f"llm:{reasoning}"] if reasoning else ["llm:selected"],
        fallback_reason=fallback_reason,
    )
