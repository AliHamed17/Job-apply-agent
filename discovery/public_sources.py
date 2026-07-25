"""Challenge-independent public job discovery providers."""

from __future__ import annotations

import re

import httpx
import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)

REMOTIVE_JOBS_URL = "https://remotive.com/api/remote-jobs"
_SPACE_RE = re.compile(r"\s+")


def _plain_text(value: str) -> str:
    return _SPACE_RE.sub(" ", BeautifulSoup(value or "", "html.parser").get_text(" ")).strip()


def _matches_profile(title: str, description: str, profile) -> bool:
    haystack = f"{title} {description}".casefold()
    roles = [role.casefold().strip() for role in profile.preferences.roles if role.strip()]
    keywords = [
        keyword.casefold().strip()
        for keyword in profile.preferences.keywords
        if len(keyword.strip()) >= 2
    ]
    if any(role in haystack for role in roles):
        return True
    keyword_hits = sum(1 for keyword in set(keywords) if keyword in haystack)
    return keyword_hits >= 2


def parse_remotive_jobs(payload: dict, profile, max_jobs: int) -> list[JobData]:
    """Convert and locally filter the documented Remotive API response."""
    results: list[JobData] = []
    for row in payload.get("jobs", []):
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        description = _plain_text(str(row.get("description") or ""))
        if not title or not url or not _matches_profile(title, description, profile):
            continue
        tags = [str(tag).strip() for tag in row.get("tags", []) if str(tag).strip()]
        results.append(
            JobData(
                title=title,
                company=str(row.get("company_name") or "").strip(),
                location=str(row.get("candidate_required_location") or "Remote").strip(),
                employment_type=str(row.get("job_type") or "").strip(),
                description=description,
                apply_url=url,
                source_url=url,
                date_posted=str(row.get("publication_date") or "").strip(),
                keywords=tags,
            )
        )
        if len(results) >= max_jobs:
            break
    return results


async def fetch_remotive_jobs(profile, settings, client: httpx.AsyncClient | None = None):
    """Fetch one public feed request; callers enforce the six-hour cadence."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=settings.public_discovery_timeout_s)
    try:
        response = await client.get(
            REMOTIVE_JOBS_URL,
            headers={"User-Agent": "JobApplyAgent/0.1 (+local personal job search)"},
        )
        response.raise_for_status()
        jobs = parse_remotive_jobs(
            response.json(),
            profile,
            max(1, settings.public_discovery_max_jobs),
        )
        logger.info("public_discovery_fetched", source="remotive", matched=len(jobs))
        return jobs
    finally:
        if owns_client:
            await client.aclose()
