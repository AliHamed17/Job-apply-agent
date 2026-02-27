"""Job extractor — orchestrates parsers to extract JobData from HTML."""

from __future__ import annotations

import structlog

from jobs.models import JobData
from jobs.parsers.comeet import parse_comeet
from jobs.parsers.greenhouse import parse_greenhouse
from jobs.parsers.html_heuristic import parse_html_heuristic
from jobs.parsers.jsonld import parse_jsonld
from jobs.parsers.lever import parse_lever
from jobs.parsers.linkedin import parse_linkedin
from jobs.parsers.workday import is_workday_url, parse_workday

logger = structlog.get_logger(__name__)


class ExtractionResult:
    """Container for extraction results."""

    def __init__(
        self,
        jobs: list[JobData] | None = None,
        page_type: str = "unknown",  # single_job, listing, no_jobs
        parser_used: str = "",
    ):
        self.jobs = jobs or []
        self.page_type = page_type
        self.parser_used = parser_used

    @property
    def has_jobs(self) -> bool:
        return len(self.jobs) > 0


async def extract_jobs_with_vision(url: str) -> ExtractionResult:
    """Async vision fallback — screenshot the page and let the LLM parse it.

    Only called when ``extract_jobs`` returns no results.  Requires
    the ``playwright`` optional dependency and a configured LLM with
    vision capability.  Falls back to an empty result if unavailable.
    """
    from jobs.parsers.vision_parser import parse_via_vision  # noqa: PLC0415

    vision_jobs = await parse_via_vision(url)
    if vision_jobs:
        logger.info("extracted_via_vision", url=url, count=len(vision_jobs))
        page_type = "single_job" if len(vision_jobs) == 1 else "listing"
        return ExtractionResult(jobs=vision_jobs, page_type=page_type, parser_used="vision")

    logger.info("vision_no_jobs", url=url)
    return ExtractionResult(page_type="no_jobs")


def extract_jobs(html: str, url: str) -> ExtractionResult:
    """Extract job postings from an HTML page.

    Strategy (tried in order):
    1. JSON-LD structured data (Schema.org JobPosting)
    2. Greenhouse-specific parser (boards.greenhouse.io)
    3. Lever-specific parser (jobs.lever.co)
    4. Workday-specific parser (myworkdayjobs.com / myworkday.com)
    5. Generic HTML heuristic fallback

    For obfuscated/canvas-heavy pages where all parsers fail, call the
    async ``extract_jobs_with_vision(url)`` as a last resort.

    Returns an ExtractionResult with parsed jobs and metadata.
    """
    if not html or not html.strip():
        return ExtractionResult(page_type="no_jobs")

    url_lower = url.lower()

    # 1) JSON-LD — most reliable
    jsonld_jobs = parse_jsonld(html, url)
    if jsonld_jobs:
        logger.info("extracted_via_jsonld", url=url, count=len(jsonld_jobs))
        page_type = "single_job" if len(jsonld_jobs) == 1 else "listing"
        return ExtractionResult(jobs=jsonld_jobs, page_type=page_type, parser_used="jsonld")

    # 2) Greenhouse
    if "greenhouse.io" in url_lower:
        gh_jobs = parse_greenhouse(html, url)
        if gh_jobs:
            logger.info("extracted_via_greenhouse", url=url, count=len(gh_jobs))
            page_type = "single_job" if len(gh_jobs) == 1 else "listing"
            return ExtractionResult(
                jobs=gh_jobs, page_type=page_type, parser_used="greenhouse"
            )

    # 3) Lever
    if "lever.co" in url_lower:
        lever_jobs = parse_lever(html, url)
        if lever_jobs:
            logger.info("extracted_via_lever", url=url, count=len(lever_jobs))
            page_type = "single_job" if len(lever_jobs) == 1 else "listing"
            return ExtractionResult(
                jobs=lever_jobs, page_type=page_type, parser_used="lever"
            )

    # 4) LinkedIn
    if "linkedin.com" in url_lower:
        li_jobs = parse_linkedin(html, url)
        if li_jobs:
            logger.info("extracted_via_linkedin", url=url, count=len(li_jobs))
            return ExtractionResult(jobs=li_jobs, page_type="single_job", parser_used="linkedin")

    # 5) Comeet
    if "comeet.com" in url_lower or "comeet.co" in url_lower:
        comeet_jobs = parse_comeet(html, url)
        if comeet_jobs:
            logger.info("extracted_via_comeet", url=url, count=len(comeet_jobs))
            page_type = "single_job" if len(comeet_jobs) == 1 else "listing"
            return ExtractionResult(
                jobs=comeet_jobs, page_type=page_type, parser_used="comeet"
            )

    # 6) Workday
    if is_workday_url(url):
        workday_jobs = parse_workday(html, url)
        if workday_jobs:
            logger.info("extracted_via_workday", url=url, count=len(workday_jobs))
            page_type = "single_job" if len(workday_jobs) == 1 else "listing"
            return ExtractionResult(
                jobs=workday_jobs, page_type=page_type, parser_used="workday"
            )

    # 7) Generic HTML heuristic
    heuristic_jobs = parse_html_heuristic(html, url)
    if heuristic_jobs:
        logger.info("extracted_via_heuristic", url=url, count=len(heuristic_jobs))
        page_type = "single_job" if len(heuristic_jobs) == 1 else "listing"
        return ExtractionResult(
            jobs=heuristic_jobs, page_type=page_type, parser_used="html_heuristic"
        )

    logger.info("no_jobs_found", url=url)
    return ExtractionResult(page_type="no_jobs")
