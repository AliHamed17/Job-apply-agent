"""LLM-based CV selection — reads each candidate CV's actual PDF text and
asks the model to pick the best fit for a specific job.

The deterministic, keyword-based router (profile/cv_routing.py::route_cv)
is fast, free, and fully auditable, but it can only score what it has
signal for — a job posting with no scraped description gives its skill
matcher nothing to work with, so it falls back to a generic default CV
without ever having "read" anything. This module is the smarter, slower
fallback: when the deterministic router doesn't confidently match, it reads
each CV's real content (not just hand-tagged keywords) and lets the LLM
judge across all candidates at once.

This is distinct from validate_cv_alignment() in profile/cv_routing.py,
which re-checks a single already-selected CV against short tag summaries of
the alternatives, and runs on every confident selection too (cheap — one
CV's full text). This module runs only on low-confidence/fallback routing
and reads every candidate CV's full text before choosing — more thorough,
reserved for the case where the keyword matcher had nothing to go on.

Never invoked when the deterministic router already found a confident
match — that result is authoritative and free; there's no reason to spend
LLM tokens re-confirming it.
"""

from __future__ import annotations

from pathlib import Path
from profile.cv_content_cache import get_cv_text_by_id
from profile.cv_routing import CVRoutingConfig, RoutingDecision, RoutingJob

import structlog

from llm.client import LLMClient, get_llm_client

logger = structlog.get_logger(__name__)

# Per-CV excerpt length fed to the LLM — keeps the combined prompt bounded
# even across a dozen CVs. The opening of a resume (title line, summary,
# top skills) carries most of the matching signal.
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
    """Fetch each CV's extracted PDF text via the shared content cache.

    Reuses profile.cv_content_cache so a CV's PDF is parsed once per
    process, not re-parsed on every job that hits the fallback path.
    Missing/unreadable files come back as "" from the cache and are
    skipped here — one bad PDF shouldn't take down routing for every job.
    """
    excerpts: dict[str, str] = {}
    for cv in config.cvs:
        text = get_cv_text_by_id(cv.id, cv_routing_path=cv_routing_path, cv_directory=cv_directory)
        if text.strip():
            excerpts[cv.id] = text.strip()[:_EXCERPT_CHARS]
    return excerpts


async def select_cv_via_llm(
    job: RoutingJob,
    config: CVRoutingConfig,
    cv_excerpts: dict[str, str],
    client: LLMClient | None = None,
) -> RoutingDecision:
    """Ask the LLM to pick the best-fitting CV by reading each one's real text.

    Abstains (selected_cv_id=None) on any LLM/parsing failure, an empty
    excerpt set, or if the model names a CV id outside cv_excerpts — never
    fabricates a selection outside the known set.
    """
    if not cv_excerpts:
        return RoutingDecision(
            selected_cv_id=None, selected_file=None, confidence=0.0,
            matched_evidence=[], fallback_reason="no_cv_text_available",
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
            selected_cv_id=None, selected_file=None, confidence=0.0,
            matched_evidence=[], fallback_reason="llm_routing_error",
        )

    selected_id = result.get("selected_cv_id")
    reasoning = str(result.get("reasoning") or "")[:300]
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    if not selected_id or selected_id not in cv_excerpts:
        logger.info("llm_cv_routing_abstained", selected_id=selected_id, reasoning=reasoning)
        return RoutingDecision(
            selected_cv_id=None, selected_file=None, confidence=confidence,
            matched_evidence=[reasoning] if reasoning else [],
            fallback_reason="llm_abstained",
        )

    cv = next(c for c in config.cvs if c.id == selected_id)
    logger.info("llm_cv_routing_selected", cv_id=cv.id, confidence=confidence, reasoning=reasoning)
    return RoutingDecision(
        selected_cv_id=cv.id,
        selected_file=cv.file,
        confidence=confidence,
        matched_evidence=[f"llm:{reasoning}"] if reasoning else ["llm:selected"],
    )
