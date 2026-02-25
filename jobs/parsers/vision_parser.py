"""Vision-based job parser — last-resort parser for obfuscated pages.

Called by the extractor when all HTML-based parsers have failed.
Requires the ``playwright`` optional dependency.

Per HANDOVER_PLAN.md Phase 10:
  "Use GPT-4o-vision or Claude 3.5 Sonnet to 'look' at job pages that are
   heavily obfuscated or use canvas-based layouts."
"""

from __future__ import annotations

import structlog

from jobs.models import JobData

logger = structlog.get_logger(__name__)


async def parse_via_vision(source_url: str) -> list[JobData]:
    """Extract a job posting by taking a screenshot and analysing it with a vision LLM.

    This is an async parser — the extractor must await it.
    Returns an empty list if vision extraction fails or finds no job data.
    """
    try:
        from llm.vision import extract_job_via_vision  # noqa: PLC0415
    except ImportError:
        logger.warning("vision_module_unavailable")
        return []

    data = await extract_job_via_vision(source_url)
    if not data or not data.get("title"):
        return []

    job = JobData(
        title=data.get("title", ""),
        company=data.get("company", ""),
        location=data.get("location", ""),
        employment_type=data.get("employment_type", ""),
        seniority=data.get("seniority", ""),
        description=data.get("description", ""),
        requirements=data.get("requirements", ""),
        apply_url=data.get("apply_url") or source_url,
        source_url=source_url,
        date_posted=data.get("date_posted", ""),
    )

    logger.info(
        "vision_parser_result",
        url=source_url,
        title=job.title,
        company=job.company,
    )
    return [job]
