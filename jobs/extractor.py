"""Job extractor — orchestrates parsers to extract JobData from HTML."""

from __future__ import annotations

import structlog

from jobs.models import JobData
from jobs.parsers.greenhouse import parse_greenhouse
from jobs.parsers.html_heuristic import parse_html_heuristic
from jobs.parsers.jsonld import parse_jsonld
from jobs.parsers.lever import parse_lever

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


def extract_jobs(html: str, url: str) -> ExtractionResult:
    """Extract job postings from an HTML page.

    Strategy (tried in order):
    1. JSON-LD structured data (Schema.org JobPosting)
    2. Greenhouse-specific parser (boards.greenhouse.io)
    3. Lever-specific parser (jobs.lever.co)
    4. Generic HTML heuristic fallback

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

    # 4) Generic HTML heuristic
    heuristic_jobs = parse_html_heuristic(html, url)
    if heuristic_jobs:
        logger.info("extracted_via_heuristic", url=url, count=len(heuristic_jobs))
        page_type = "single_job" if len(heuristic_jobs) == 1 else "listing"
        return ExtractionResult(
            jobs=heuristic_jobs, page_type=page_type, parser_used="html_heuristic"
        )

    logger.info("no_jobs_found", url=url)
    return ExtractionResult(page_type="no_jobs")
