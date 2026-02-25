"""Greenhouse-specific job page parser (boards.greenhouse.io)."""

from __future__ import annotations

import re

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)


def parse_greenhouse(html: str, source_url: str) -> list[JobData]:
    """Parse a Greenhouse job board page.

    Supports both:
    - Individual job pages (boards.greenhouse.io/company/jobs/12345)
    - Listing pages (boards.greenhouse.io/company)
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Check if it's a listing page
    job_links = soup.select("div.opening a")
    if len(job_links) > 1:
        # It's a listing page — extract individual job links
        jobs: list[JobData] = []
        for link in job_links:
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if title and href:
                location_el = link.find_next("span", class_="location")
                location = location_el.get_text(strip=True) if location_el else ""

                # Build full URL
                if href.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(source_url)
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"

                jobs.append(JobData(
                    title=title,
                    location=location,
                    apply_url=href,
                    source_url=source_url,
                    company=_extract_company_greenhouse(soup, source_url),
                ))
        if jobs:
            logger.info("greenhouse_listing_parsed", url=source_url, count=len(jobs))
            return jobs

    # Single job page
    title = ""
    title_el = soup.select_one("h1.app-title") or soup.select_one("h1")
    if title_el:
        title = title_el.get_text(strip=True)

    if not title:
        return []

    company = _extract_company_greenhouse(soup, source_url)

    location = ""
    loc_el = soup.select_one(".location")
    if loc_el:
        location = loc_el.get_text(strip=True)

    description = ""
    desc_el = soup.select_one("#content") or soup.select_one(".content")
    if desc_el:
        description = desc_el.get_text(separator="\n", strip=True)

    # Greenhouse apply URL pattern
    apply_url = source_url
    apply_el = soup.select_one('a[href*="/apply"]')
    if apply_el:
        href = apply_el.get("href", "")
        if href.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(source_url)
            apply_url = f"{parsed.scheme}://{parsed.netloc}{href}"
        elif href:
            apply_url = href

    job = JobData(
        title=title,
        company=company,
        location=location,
        seniority=_detect_seniority(title),
        description=description,
        apply_url=apply_url,
        source_url=source_url,
    )

    logger.info("greenhouse_parsed", url=source_url, title=title)
    return [job]


def _extract_company_greenhouse(soup: BeautifulSoup, url: str) -> str:
    """Extract company name from Greenhouse page."""
    # Try meta tag
    meta = soup.select_one('meta[property="og:title"]')
    if meta:
        content = meta.get("content", "")
        if " at " in content:
            return content.split(" at ")[-1].strip()

    # Try from URL: boards.greenhouse.io/COMPANY/...
    match = re.search(r"greenhouse\.io/([^/]+)", url)
    if match:
        return match.group(1).replace("-", " ").title()

    return ""


def _detect_seniority(title: str) -> str:
    """Detect seniority from title."""
    title_lower = title.lower()
    for kw, level in {
        "intern": "intern", "junior": "junior", "senior": "senior",
        "sr.": "senior", "lead": "lead", "staff": "senior",
        "principal": "lead", "manager": "manager", "director": "director",
    }.items():
        if kw in title_lower:
            return level
    return ""
