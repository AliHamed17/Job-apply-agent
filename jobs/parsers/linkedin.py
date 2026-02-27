"""LinkedIn job page parser (linkedin.com/jobs/view/...).

LinkedIn pages require JavaScript rendering (browser fetch).
Supports both the legacy "topcard" layout and the newer "unified" layout.
"""

from __future__ import annotations

import re

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)

# Job ID from URL: /jobs/view/1234567890 or /jobs/collections/.../1234567890
_JOB_ID_RE = re.compile(r"/jobs/(?:view|collections/[^/]+)/(\d+)", re.IGNORECASE)


def parse_linkedin(html: str, source_url: str) -> list[JobData]:
    """Parse a LinkedIn job detail page."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    title = _extract_title(soup)
    if not title:
        logger.debug("linkedin_no_title", url=source_url)
        return []

    company = _extract_company(soup)
    location = _extract_location(soup)
    employment_type = _extract_employment_type(soup)
    description = _extract_description(soup)
    apply_url = _extract_apply_url(soup, source_url)

    job = JobData(
        title=title,
        company=company,
        location=location,
        employment_type=employment_type,
        seniority=_detect_seniority(title),
        description=description,
        apply_url=apply_url,
        source_url=source_url,
    )

    logger.info("linkedin_parsed", url=source_url, title=title, company=company)
    return [job]


# ── Field extractors ──────────────────────────────────────────────────────────

def _extract_title(soup: BeautifulSoup) -> str:
    # Newer unified layout
    for sel in (
        "h1.jobs-unified-top-card__job-title",
        "h1[class*='top-card__title']",
        "h1[class*='topcard__title']",
        # Legacy layout
        "h1.top-card-layout__title",
        "h1.topcard__title",
        # Generic fallback
        "h1",
    ):
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t:
                return t
    return ""


def _extract_company(soup: BeautifulSoup) -> str:
    for sel in (
        "a.jobs-unified-top-card__company-name",
        "a[class*='topcard__org-name']",
        "a.topcard__org-name-link",
        ".jobs-unified-top-card__company-name",
        ".topcard__org-name-link",
        ".top-card-layout__company",
        # meta fallback
        'meta[property="og:site_name"]',
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get("content", "") if el.name == "meta" else el.get_text(strip=True)
            if text:
                return text
    return ""


def _extract_location(soup: BeautifulSoup) -> str:
    for sel in (
        ".jobs-unified-top-card__bullet",
        "span[class*='topcard__flavor--bullet']",
        ".topcard__flavor--bullet",
        ".jobs-unified-top-card__workplace-type",
        ".topcard__flavor",
        "[class*='job-criteria__text']",
    ):
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t:
                return t
    return ""


def _extract_employment_type(soup: BeautifulSoup) -> str:
    # LinkedIn shows employment type in job criteria list
    criteria_items = soup.select(".job-criteria__item, .jobs-unified-top-card__job-insight")
    for item in criteria_items:
        text = item.get_text(separator=" ", strip=True).lower()
        if "full-time" in text or "full time" in text:
            return "full-time"
        if "part-time" in text or "part time" in text:
            return "part-time"
        if "contract" in text:
            return "contract"
        if "intern" in text:
            return "internship"
    # Fallback: scan page text
    page_text = soup.get_text(separator=" ", strip=True).lower()
    if "full-time" in page_text or "full time" in page_text:
        return "full-time"
    return ""


def _extract_description(soup: BeautifulSoup) -> str:
    for sel in (
        "div#job-details",
        ".jobs-description__content",
        ".description__text",
        ".show-more-less-html__markup",
        ".jobs-box__html-content",
        "article",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 100:
                return text
    return ""


def _extract_apply_url(soup: BeautifulSoup, source_url: str) -> str:
    # Look for Easy Apply button or external apply link
    for sel in (
        'a[href*="apply"]',
        'button[aria-label*="Apply"]',
        '.jobs-apply-button',
    ):
        el = soup.select_one(sel)
        if el and el.name == "a":
            href = el.get("href", "")
            if href and href.startswith("http"):
                return href
    return source_url


def _detect_seniority(title: str) -> str:
    t = title.lower()
    for kw, level in {
        "intern": "intern", "junior": "junior", "entry": "entry",
        "senior": "senior", "sr.": "senior", "lead": "lead",
        "staff": "senior", "principal": "lead", "manager": "manager",
        "director": "director",
    }.items():
        if kw in t:
            return level
    return ""
