"""Lever-specific job page parser (jobs.lever.co)."""

from __future__ import annotations

from urllib.parse import urlsplit

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData
from submitters.lever_identity import (
    LeverIdentityError,
    canonical_lever_apply_url,
    canonical_lever_listing_url,
    parse_lever_posting_identity,
)

logger = structlog.get_logger(__name__)


def parse_lever(html: str, source_url: str) -> list[JobData]:
    """Parse a Lever job board page.

    Supports both:
    - Individual job pages (jobs.lever.co/company/uuid)
    - Listing pages (jobs.lever.co/company)
    """
    try:
        source_identity = parse_lever_posting_identity(source_url)
        listing_url = None
    except LeverIdentityError:
        source_identity = None
        try:
            listing_url = canonical_lever_listing_url(source_url)
        except LeverIdentityError:
            return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Check if it's a listing page
    postings = soup.select(".posting")
    if len(postings) > 1:
        jobs: list[JobData] = []
        for posting in postings:
            title_el = posting.select_one(".posting-title h5") or posting.select_one("h5")
            title = title_el.get_text(strip=True) if title_el else ""

            link_el = posting.select_one("a.posting-title") or posting.select_one("a")
            href = str(link_el.get("href", "")).strip() if link_el else ""

            location = ""
            loc_el = posting.select_one(".posting-categories .sort-by-time") or posting.select_one(
                ".location"
            )
            if loc_el:
                location = loc_el.get_text(strip=True)

            worktype = ""
            wt_el = posting.select_one(".posting-categories .commitment")
            if wt_el:
                worktype = wt_el.get_text(strip=True).lower()

            try:
                posting_identity = parse_lever_posting_identity(href)
            except LeverIdentityError:
                posting_identity = None
            listing_site = listing_url.rsplit("/", 1)[-1] if listing_url else ""
            listing_host = (urlsplit(listing_url).hostname or "") if listing_url else ""
            if (
                title
                and posting_identity is not None
                and posting_identity.hostname == listing_host
                and posting_identity.site == listing_site
            ):
                jobs.append(
                    JobData(
                        title=title,
                        company=posting_identity.site.replace("-", " ").title(),
                        location=location,
                        employment_type=worktype,
                        apply_url=posting_identity.apply_url,
                        source_url=posting_identity.job_url,
                    )
                )

        if jobs:
            logger.info("lever_listing_parsed", url=source_url, count=len(jobs))
            return jobs

    # Single job page
    title = ""
    title_el = soup.select_one("h2.posting-headline") or soup.select_one(".posting-headline h2")
    if not title_el:
        title_el = soup.select_one("h2") or soup.select_one("h1")
    if title_el:
        title = title_el.get_text(strip=True)

    if not title:
        return []

    if source_identity is None:
        return []
    company = source_identity.site.replace("-", " ").title()

    location = ""
    loc_el = soup.select_one(".posting-categories .sort-by-time") or soup.select_one(".location")
    if loc_el:
        location = loc_el.get_text(strip=True)

    worktype = ""
    wt_el = soup.select_one(".posting-categories .commitment")
    if wt_el:
        worktype = wt_el.get_text(strip=True).lower()

    description = ""
    desc_sections = soup.select(".posting-page .section-wrapper")
    if desc_sections:
        parts = []
        for sec in desc_sections:
            parts.append(sec.get_text(separator="\n", strip=True))
        description = "\n\n".join(parts)
    else:
        desc_el = soup.select_one(".posting-page")
        if desc_el:
            description = desc_el.get_text(separator="\n", strip=True)

    # Lever apply URL
    apply_url = canonical_lever_apply_url(source_url)

    job = JobData(
        title=title,
        company=company,
        location=location,
        employment_type=worktype,
        seniority=_detect_seniority(title),
        description=description,
        apply_url=apply_url,
        source_url=source_identity.job_url,
    )

    logger.info("lever_parsed", url=source_url, title=title)
    return [job]


def _extract_company_lever(url: str) -> str:
    """Extract company name from Lever URL: jobs.lever.co/COMPANY/..."""
    try:
        return parse_lever_posting_identity(url).site.replace("-", " ").title()
    except LeverIdentityError:
        try:
            listing = canonical_lever_listing_url(url)
        except LeverIdentityError:
            return ""
        return listing.rsplit("/", 1)[-1].replace("-", " ").title()


def _detect_seniority(title: str) -> str:
    """Detect seniority from title."""
    title_lower = title.lower()
    for kw, level in {
        "intern": "intern",
        "junior": "junior",
        "senior": "senior",
        "sr.": "senior",
        "lead": "lead",
        "staff": "senior",
        "principal": "lead",
        "manager": "manager",
        "director": "director",
    }.items():
        if kw in title_lower:
            return level
    return ""
