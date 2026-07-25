"""LLM-assisted CV selection for low-confidence deterministic routes.

The deterministic router remains the first decision-maker. This module is
only used when it cannot confidently select a CV, and it fails closed when
the provider, CV text, or returned ID is unusable.
"""

from __future__ import annotations

from pathlib import Path
from profile.cv_routing import CVRoutingConfig, RoutingDecision, RoutingJob
from profile.pdf_loader import extract_text_from_pdf

import structlog

from llm.prompts import CV_ROUTING_PROMPT

logger = structlog.get_logger(__name__)

MAX_CV_EXCERPT_CHARS = 1800


def load_cv_excerpts(
    config: CVRoutingConfig,
    cv_directory: str | Path,
) -> dict[str, str]:
    """Extract bounded text for every readable CV in the routing config."""
    directory = Path(cv_directory)
    excerpts: dict[str, str] = {}

    for cv in config.cvs:
        path = (directory / cv.file).resolve()
        try:
            text = extract_text_from_pdf(path).strip()
        except Exception as exc:
            logger.warning(
                "cv_routing_pdf_unreadable",
                cv_id=cv.id,
                path=str(path),
                error=str(exc),
            )
            continue

        if text:
            excerpts[cv.id] = text[:MAX_CV_EXCERPT_CHARS]

    return excerpts


def _build_prompt(
    job: RoutingJob,
    config: CVRoutingConfig,
    excerpts: dict[str, str],
) -> str:
    cv_sections = []
    for cv in config.cvs:
        excerpt = excerpts.get(cv.id)
        if not excerpt:
            continue
        cv_sections.append(
            "\n".join(
                [
                    f"### CV {cv.id} ({cv.file})",
                    f"Configured focus: {', '.join(cv.title_terms + cv.skills)}",
                    excerpt,
                ]
            )
        )

    return CV_ROUTING_PROMPT.format(
        job_title=job.title or "unspecified",
        seniority=job.seniority or "unspecified",
        job_description=(job.description or "")[:4000],
        cv_options="\n\n".join(cv_sections),
    )


async def select_cv_via_llm(
    job: RoutingJob,
    config: CVRoutingConfig,
    excerpts: dict[str, str],
    client=None,
) -> RoutingDecision:
    """Select one configured CV, or abstain with an auditable reason."""
    if not excerpts:
        return RoutingDecision(
            selected_cv_id=None,
            selected_file=None,
            confidence=0.0,
            matched_evidence=[],
            fallback_reason="no_cv_text_available",
        )

    from llm.client import get_llm_client

    llm = client or get_llm_client()
    try:
        result = await llm.generate_json(
            prompt=_build_prompt(job, config, excerpts),
            system=(
                "You are an expert technical recruiter. Select a CV using only "
                "the supplied job and CV text. Never invent candidate evidence."
            ),
            max_tokens=800,
        )
    except Exception as exc:
        logger.warning("cv_routing_llm_failed", error=str(exc))
        return RoutingDecision(
            selected_cv_id=None,
            selected_file=None,
            confidence=0.0,
            matched_evidence=[],
            fallback_reason="llm_routing_error",
        )

    selected_id = result.get("selected_cv_id") if isinstance(result, dict) else None
    if not isinstance(selected_id, str):
        selected_id = None
    configured = {cv.id: cv for cv in config.cvs}
    selected = configured.get(selected_id)

    # A valid config ID is not enough: the selected CV must also have supplied
    # text, otherwise the model did not have evidence for the choice.
    if selected is None or selected_id not in excerpts:
        return RoutingDecision(
            selected_cv_id=None,
            selected_file=None,
            confidence=0.0,
            matched_evidence=[],
            fallback_reason="llm_abstained",
        )

    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    reasoning = str(result.get("reasoning", "")).strip()
    fallback_reason = (
        None
        if confidence >= config.minimum_confidence
        else "llm_confidence_below_threshold"
    )

    return RoutingDecision(
        selected_cv_id=selected.id,
        selected_file=selected.file,
        confidence=confidence,
        matched_evidence=[f"llm:{reasoning}"] if reasoning else ["llm:selected"],
        fallback_reason=fallback_reason,
    )
