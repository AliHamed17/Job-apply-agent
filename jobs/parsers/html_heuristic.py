"""Generic HTML heuristic parser — fallback when no structured data exists."""

from __future__ import annotations

import re

import structlog
from bs4 import BeautifulSoup, Tag

from jobs.models import JobData

logger = structlog.get_logger(__name__)

# Common CSS selectors / patterns for job pages
_TITLE_SELECTORS = [
    "h1.job-title", "h1.posting-headline", "h1[data-qa='job-title']",
    ".job-title h1", ".posting-headline h1",
    "h1.app-title", "h1.position-title",
    "h1",  # fallback
]

_COMPANY_SELECTORS = [
    ".company-name", ".employer-name", "[data-qa='company-name']",
    ".hiring-company", ".job-company",
    'meta[property="og:site_name"]',
]

_LOCATION_SELECTORS = [
    ".job-location", ".location", "[data-qa='job-location']",
    ".posting-categories .sort-by-time",
]

_DESCRIPTION_SELECTORS = [
    ".job-description", ".posting-description", "[data-qa='job-description']",
    ".description", "#job-description", ".job-details",
    "article", ".content-body",
]

_APPLY_SELECTORS = [
    'a[href*="apply"]',
    'a.apply-button',
    'a[data-qa="apply-button"]',
    'button.apply',
]


def _find_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    """Try multiple CSS selectors and return the first match's text."""
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            # Handle meta tags
            if isinstance(el, Tag) and el.name == "meta":
                return el.get("content", "")
            text = el.get_text(strip=True)
            if text:
                return text
    return ""


def _find_apply_url(soup: BeautifulSoup, source_url: str) -> str:
    """Try to find an apply button/link."""
    for sel in _APPLY_SELECTORS:
        el = soup.select_one(sel)
        if el and isinstance(el, Tag):
            href = el.get("href", "")
            if href:
                return str(href)
    return source_url


def _extract_description(soup: BeautifulSoup) -> str:
    """Extract the job description text."""
    for sel in _DESCRIPTION_SELECTORS:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 100:  # likely a real description
                return text
    return ""


def _detect_seniority(title: str) -> str:
    """Simple seniority detection from title."""
    title_lower = title.lower()
    for kw, level in {
        "intern": "intern", "junior": "junior", "entry": "entry",
        "mid": "mid", "senior": "senior", "sr.": "senior",
        "lead": "lead", "principal": "lead", "staff": "senior",
        "manager": "manager", "director": "director",
    }.items():
        if kw in title_lower:
            return level
    return ""


def _detect_employment_type(text: str) -> str:
    """Detect employment type from page text."""
    text_lower = text.lower()
    if "full-time" in text_lower or "full time" in text_lower:
        return "full-time"
    if "part-time" in text_lower or "part time" in text_lower:
        return "part-time"
    if "contract" in text_lower:
        return "contract"
    if "internship" in text_lower or "intern" in text_lower:
        return "internship"
    return ""


def parse_html_heuristic(html: str, source_url: str) -> list[JobData]:
    """Parse a job page using HTML heuristics.

    Returns at most one JobData (best effort from a single page).
    Returns empty list if page doesn't look like a job posting.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    title = _find_text(soup, _TITLE_SELECTORS)
    if not title:
        logger.debug("no_title_found", url=source_url)
        return []

    # Reject if title doesn't look job-like
    page_text = soup.get_text(separator=" ", strip=True).lower()
    job_indicators = [
        "apply", "position", "role", "responsibilities",
        "requirements", "qualifications", "experience",
        "salary", "benefits", "we are looking",
    ]
    matches = sum(1 for ind in job_indicators if ind in page_text)
    if matches < 2:
        logger.debug("page_not_job_like", url=source_url, indicators_found=matches)
        return []

    company = _find_text(soup, _COMPANY_SELECTORS)
    location = _find_text(soup, _LOCATION_SELECTORS)
    description = _extract_description(soup)
    apply_url = _find_apply_url(soup, source_url)

    job = JobData(
        title=title,
        company=company,
        location=location,
        employment_type=_detect_employment_type(page_text),
        seniority=_detect_seniority(title),
        description=description,
        apply_url=apply_url,
        source_url=source_url,
    )

    if job.is_complete:
        logger.info("heuristic_parsed", url=source_url, title=title)
        return [job]

    return []
