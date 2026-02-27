"""Comeet job board parser (comeet.com / comeet.co).

Comeet is a React/Next.js app — static HTML is minimal.
We try three strategies in order:
  1. Extract from __NEXT_DATA__ JSON embedded in <script> tags
  2. Extract from common CSS selectors present in server-side-rendered HTML
  3. Fall back to URL-based extraction (company + title from path segments)
"""

from __future__ import annotations

import json
import re

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)

# URL patterns:
#   https://www.comeet.com/jobs/{company_uid}/{company-slug}/{pos_uid}/{pos-slug}
#   https://www.comeet.co/jobs/{company_uid}/{company-slug}/{pos_uid}/{pos-slug}
_URL_RE = re.compile(
    r"comeet\.co[m]?/jobs/([^/]+)/([^/]+)/([^/?#]+)(?:/([^/?#]*))?",
    re.IGNORECASE,
)


def parse_comeet(html: str, source_url: str) -> list[JobData]:
    """Parse a Comeet position page and return a list of JobData."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: __NEXT_DATA__ JSON
    job = _parse_next_data(soup, source_url)
    if job:
        logger.info("comeet_parsed_via_next_data", url=source_url)
        return [job]

    # Strategy 2: CSS selectors
    job = _parse_html_structure(soup, source_url)
    if job:
        logger.info("comeet_parsed_via_html", url=source_url)
        return [job]

    # Strategy 3: URL-based extraction
    job = _parse_from_url(source_url)
    if job:
        logger.info("comeet_parsed_via_url", url=source_url)
        return [job]

    return []


# ── Strategy 1: __NEXT_DATA__ ─────────────────────────────────────────────────

def _parse_next_data(soup: BeautifulSoup, source_url: str) -> JobData | None:
    """Extract from Next.js server-side state embedded in <script id='__NEXT_DATA__'>."""
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        # Also try any inline script containing __NEXT_DATA__
        for s in soup.find_all("script", type="application/json"):
            text = s.get_text()
            if "__NEXT_DATA__" in text or "position" in text.lower():
                script = s
                break
    if not script:
        return None

    try:
        data = json.loads(script.get_text())
    except Exception:
        return None

    # Drill into the nested structure — Comeet puts position under props.pageProps
    pos = (
        _dig(data, "props", "pageProps", "position")
        or _dig(data, "props", "pageProps", "data", "position")
        or _dig(data, "props", "pageProps", "job")
        or _dig(data, "pageProps", "position")
    )

    if not pos:
        return None

    title = pos.get("name") or pos.get("title") or pos.get("job_title") or ""
    if not title:
        return None

    company = (
        pos.get("company_name")
        or _dig(data, "props", "pageProps", "company", "name")
        or _dig(data, "props", "pageProps", "companyName")
        or _extract_company_from_url(source_url)
    )

    location = pos.get("location") or pos.get("city") or pos.get("office") or ""
    if isinstance(location, dict):
        location = location.get("city") or location.get("name") or ""

    employment_type = (
        pos.get("employment_type")
        or pos.get("job_type")
        or pos.get("type")
        or ""
    )
    description = pos.get("description") or pos.get("details") or pos.get("summary") or ""
    requirements = pos.get("requirements") or pos.get("qualifications") or ""

    apply_url = pos.get("apply_url") or pos.get("applicationUrl") or source_url

    return JobData(
        title=title,
        company=company,
        location=location,
        employment_type=employment_type,
        seniority=_detect_seniority(title),
        description=description,
        requirements=requirements,
        apply_url=apply_url,
        source_url=source_url,
    )


# ── Strategy 2: HTML structure ────────────────────────────────────────────────

def _parse_html_structure(soup: BeautifulSoup, source_url: str) -> JobData | None:
    """Try common Comeet CSS selectors."""
    # Title candidates
    title = ""
    for sel in (
        "h1.position-title",
        "h1[data-hook='position-title']",
        ".position-title h1",
        ".job-title",
        "h1",
    ):
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            if title:
                break

    if not title:
        return None

    # Company
    company = _extract_company_from_url(source_url)
    for sel in (".company-name", "[data-hook='company-name']", ".company"):
        el = soup.select_one(sel)
        if el:
            company = el.get_text(strip=True) or company
            break

    # Location
    location = ""
    for sel in (
        ".position-location",
        "[data-hook='position-location']",
        ".job-location",
        ".location",
    ):
        el = soup.select_one(sel)
        if el:
            location = el.get_text(strip=True)
            break

    # Employment type
    employment_type = ""
    for sel in (".position-type", ".job-type", ".employment-type", ".commitment"):
        el = soup.select_one(sel)
        if el:
            employment_type = el.get_text(strip=True)
            break

    # Description
    description = ""
    for sel in (".position-description", ".job-description", ".description-content", "article"):
        el = soup.select_one(sel)
        if el:
            description = el.get_text(separator="\n", strip=True)
            break

    return JobData(
        title=title,
        company=company,
        location=location,
        employment_type=employment_type,
        seniority=_detect_seniority(title),
        description=description,
        apply_url=source_url,
        source_url=source_url,
    )


# ── Strategy 3: URL-based ─────────────────────────────────────────────────────

def _parse_from_url(source_url: str) -> JobData | None:
    """Extract company + title from Comeet URL path segments."""
    match = _URL_RE.search(source_url)
    if not match:
        return None

    company_uid, company_slug, pos_uid, pos_slug = match.groups()
    company = company_slug.replace("-", " ").title() if company_slug else ""
    title = pos_slug.replace("-", " ").title() if pos_slug else "Position"

    return JobData(
        title=title,
        company=company,
        location="",
        seniority=_detect_seniority(title),
        apply_url=source_url,
        source_url=source_url,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_company_from_url(url: str) -> str:
    match = _URL_RE.search(url)
    if match:
        company_slug = match.group(2) or ""
        return company_slug.replace("-", " ").title()
    return ""


def _dig(obj: dict, *keys):
    """Safely traverse nested dicts."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _detect_seniority(title: str) -> str:
    t = title.lower()
    for kw, level in {
        "intern": "intern", "junior": "junior", "senior": "senior",
        "sr.": "senior", "lead": "lead", "staff": "senior",
        "principal": "lead", "manager": "manager", "director": "director",
    }.items():
        if kw in t:
            return level
    return ""
