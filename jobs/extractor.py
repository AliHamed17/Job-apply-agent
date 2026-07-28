"""Job extractor — orchestrates parsers to extract JobData from HTML."""

from __future__ import annotations

from urllib.parse import urlsplit

import structlog

from jobs.models import JobData
from jobs.parsers.comeet import parse_comeet
from jobs.parsers.greenhouse import (
    CandidateReference,
    greenhouse_identity,
    greenhouse_page_references,
    parse_greenhouse,
)
from jobs.parsers.html_heuristic import parse_html_heuristic
from jobs.parsers.israeli_boards import is_israeli_board, parse_israeli_board
from jobs.parsers.jsonld import parse_jsonld
from jobs.parsers.lever import parse_lever
from jobs.parsers.linkedin import parse_linkedin
from jobs.parsers.workday import is_workday_url, parse_workday
from submitters.lever_identity import is_lever_public_url

logger = structlog.get_logger(__name__)


def _has_parseable_http_source(url: str) -> bool:
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        _ = parts.port
    except (TypeError, ValueError):
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(hostname)


def _greenhouse_bound_jsonld_jobs(
    jobs: list[JobData],
    source_url: str,
    trusted_references: tuple[CandidateReference, ...],
) -> list[JobData]:
    """Keep JSON-LD only when it exactly covers trusted Greenhouse targets.

    Candidate, embedded, and board-listing markup is parsed first only to
    establish authoritative application identities. Structured metadata may
    enrich those targets, but it must provide one explicit matching record for
    every trusted target. Partial, duplicate, or conflicting coverage falls
    back to the canonical ATS parser.
    """

    if not trusted_references:
        return jobs

    trusted_by_identity: dict[object, str] = {}
    for candidate, candidate_url in trusted_references:
        existing = trusted_by_identity.get(candidate.identity)
        if existing is not None and existing != candidate_url:
            return []
        trusted_by_identity[candidate.identity] = candidate_url

    matching_by_identity: dict[object, JobData] = {}
    for job in jobs:
        candidate = greenhouse_identity(job.apply_url)
        if candidate is None or candidate.identity not in trusted_by_identity:
            continue
        if candidate.identity in matching_by_identity:
            return []
        matching_by_identity[candidate.identity] = job

    if set(matching_by_identity) != set(trusted_by_identity):
        return []
    return [
        matching_by_identity[identity].model_copy(
            update={
                "apply_url": candidate_url,
                "source_url": source_url.strip(),
            }
        )
        for identity, candidate_url in trusted_by_identity.items()
    ]


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
    if not html or not html.strip() or not _has_parseable_http_source(url):
        return ExtractionResult(page_type="no_jobs")

    url_lower = url.lower()

    # Establish trusted Greenhouse application identities before generic
    # structured metadata gets a chance to choose a target.
    greenhouse_references = greenhouse_page_references(html, url)
    greenhouse_jobs: list[JobData] = []
    if greenhouse_references:
        greenhouse_jobs = parse_greenhouse(html, url)

    # 1) JSON-LD — high-fidelity metadata, bound to ATS identity when present.
    jsonld_jobs = _greenhouse_bound_jsonld_jobs(
        parse_jsonld(
            html,
            url,
            require_explicit_url=bool(greenhouse_references),
        ),
        url,
        greenhouse_references,
    )
    if jsonld_jobs:
        logger.info("extracted_via_jsonld", url=url, count=len(jsonld_jobs))
        page_type = "single_job" if len(jsonld_jobs) == 1 else "listing"
        return ExtractionResult(jobs=jsonld_jobs, page_type=page_type, parser_used="jsonld")

    # 2) Greenhouse. Candidate and embedded URLs use the same canonical
    # identity contract as execution routing; external listing links alone
    # cannot select this parser.
    if greenhouse_jobs:
        logger.info("extracted_via_greenhouse", url=url, count=len(greenhouse_jobs))
        page_type = "single_job" if len(greenhouse_jobs) == 1 else "listing"
        return ExtractionResult(
            jobs=greenhouse_jobs,
            page_type=page_type,
            parser_used="greenhouse",
        )
    if greenhouse_references:
        # A trusted Greenhouse target was present but neither structured data
        # nor canonical markup produced an unambiguous job. Generic parsing
        # must not replace its target identity.
        return ExtractionResult(page_type="no_jobs", parser_used="greenhouse")

    # 3) Lever
    if is_lever_public_url(url):
        lever_jobs = parse_lever(html, url)
        if lever_jobs:
            logger.info("extracted_via_lever", url=url, count=len(lever_jobs))
            page_type = "single_job" if len(lever_jobs) == 1 else "listing"
            return ExtractionResult(jobs=lever_jobs, page_type=page_type, parser_used="lever")

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
            return ExtractionResult(jobs=comeet_jobs, page_type=page_type, parser_used="comeet")

    # 6) Workday
    if is_workday_url(url):
        workday_jobs = parse_workday(html, url)
        if workday_jobs:
            logger.info("extracted_via_workday", url=url, count=len(workday_jobs))
            page_type = "single_job" if len(workday_jobs) == 1 else "listing"
            return ExtractionResult(jobs=workday_jobs, page_type=page_type, parser_used="workday")

    # 7) Israeli boards (Drushim / AllJobs / JobMaster) — Hebrew, RTL.
    # Ahead of the generic heuristic, which reads their label/value markup as
    # body text and loses the description/requirements split.
    if is_israeli_board(url):
        il_jobs = parse_israeli_board(html, url)
        if il_jobs:
            logger.info("extracted_via_israeli_board", url=url, count=len(il_jobs))
            page_type = "single_job" if len(il_jobs) == 1 else "listing"
            return ExtractionResult(jobs=il_jobs, page_type=page_type, parser_used="israeli_board")

    # 8) Generic HTML heuristic
    heuristic_jobs = parse_html_heuristic(html, url)
    if heuristic_jobs:
        logger.info("extracted_via_heuristic", url=url, count=len(heuristic_jobs))
        page_type = "single_job" if len(heuristic_jobs) == 1 else "listing"
        return ExtractionResult(
            jobs=heuristic_jobs, page_type=page_type, parser_used="html_heuristic"
        )

    logger.info("no_jobs_found", url=url)
    return ExtractionResult(page_type="no_jobs")
