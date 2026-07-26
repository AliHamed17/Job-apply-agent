"""Israeli board posting parsers (Drushim, AllJobs, JobMaster, Jobs IL).

Thin wrappers over jobs/parsers/israeli_boards.py, which is the single real
implementation and what jobs/extractor.py dispatches to. These names are kept
because other modules and tests import them.

The previous version fabricated data whenever a selector missed:

    title    = ... else "Software Engineer"
    company  = ... else "Drushim Employer"
    location = ... else "Israel"

so an unreadable page still produced a confident-looking JobData for a job
that does not exist — which would then be scored, generated for, and applied
to. It also assigned the whole page text to *both* description and
requirements, which skews CV routing, since route_cv matches skills against
the requirements text.

These now return ``None`` when a page cannot be read. Callers must handle
that rather than receive an invented posting.
"""

from __future__ import annotations

import structlog

from jobs.models import JobData
from jobs.parsers.israeli_boards import parse_israeli_board

logger = structlog.get_logger(__name__)


def parse_drushim_job(html_content: str, source_url: str) -> JobData | None:
    """Parse a drushim.co.il posting, or None if it cannot be read."""
    return _parse_one(html_content, source_url, board="drushim")


def parse_jobs_il_job(html_content: str, source_url: str) -> JobData | None:
    """Parse a jobs.co.il / jobmaster.co.il posting, or None."""
    return _parse_one(html_content, source_url, board="jobs_il")


def _parse_one(html_content: str, source_url: str, board: str) -> JobData | None:
    jobs = parse_israeli_board(html_content, source_url)
    if not jobs:
        logger.info("israeli_posting_unparseable", board=board, url=source_url)
        return None
    return jobs[0]
