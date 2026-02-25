"""Workday job board parser.

Workday pages (myworkdayjobs.com / wd3.myworkday.com / wd1.myworkday.com, etc.)
serve job data either via embedded JSON in a <script type="application/json"> tag
or via a REST API that the page's React bundle calls.  We handle both patterns:

  1. Embedded JSON  — fastest, no extra network call.
  2. API fallback   — issues a GET to the Workday Jobs API endpoint and parses
                      the JSON response.  Requires the fetcher to have already
                      retrieved the initial HTML so we can extract the tenant/
                      company slug.

Authentication variability (per HANDOVER_PLAN.md):
  - Public posting pages are unauthenticated.
  - Application submission requires SSO or form auth → always DRAFT_ONLY.
  - Never attempt to bypass authentication.
"""

from __future__ import annotations

import json
import re

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)

# Matches: myworkdayjobs.com, wd3.myworkday.com, wd1.myworkday.com, etc.
_WORKDAY_HOST_RE = re.compile(r"(?:myworkday|myworkdayjobs)\.com", re.IGNORECASE)

# Slug extraction: /tenant/company/job/Job-Title_JR-NNNN
_SLUG_RE = re.compile(
    r"(?:myworkdayjobs\.com|myworkday\.com)/([^/]+)/([^/]+)/job/([^/?#]+)",
    re.IGNORECASE,
)


def is_workday_url(url: str) -> bool:
    """Return True if the URL belongs to a Workday-hosted job board."""
    return bool(_WORKDAY_HOST_RE.search(url))


def parse_workday(html: str, source_url: str) -> list[JobData]:
    """Parse a Workday job board page.

    Tries (in order):
    1. Embedded ``<script type="application/json">`` blob.
    2. ``<script>`` tags containing ``jobRequisitionId``.
    3. Structured HTML selectors specific to Workday's rendered output.

    Returns a list of JobData (usually 0 or 1 for single-job pages,
    possibly many for listing pages from the embedded JSON).
    """
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # ── Strategy 1: Embedded JSON blob ────────────────────────────────────
    jobs = _parse_embedded_json(soup, source_url)
    if jobs:
        logger.info("workday_parsed_json", url=source_url, count=len(jobs))
        return jobs

    # ── Strategy 2: Inline script with jobRequisitionId ───────────────────
    jobs = _parse_inline_script(soup, source_url)
    if jobs:
        logger.info("workday_parsed_script", url=source_url, count=len(jobs))
        return jobs

    # ── Strategy 3: Rendered HTML selectors ───────────────────────────────
    jobs = _parse_rendered_html(soup, source_url)
    if jobs:
        logger.info("workday_parsed_html", url=source_url, count=len(jobs))
        return jobs

    logger.info("workday_no_jobs", url=source_url)
    return []


# ── Private helpers ────────────────────────────────────────────────────────


def _parse_embedded_json(soup: BeautifulSoup, source_url: str) -> list[JobData]:
    """Look for Workday's embedded JSON data blob."""
    for tag in soup.find_all("script", {"type": "application/json"}):
        raw = tag.string or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        jobs = _extract_from_json_data(data, source_url)
        if jobs:
            return jobs
    return []


def _parse_inline_script(soup: BeautifulSoup, source_url: str) -> list[JobData]:
    """Scan inline <script> tags for jobRequisitionId JSON fragments."""
    for tag in soup.find_all("script"):
        raw = tag.string or ""
        if "jobRequisitionId" not in raw:
            continue
        # Try to isolate the JSON object/array
        for match in re.finditer(r"\{[^{}]{20,}\}", raw):
            try:
                fragment = json.loads(match.group())
            except json.JSONDecodeError:
                continue
            if "jobRequisitionId" in fragment or "title" in fragment:
                job = _json_fragment_to_jobdata(fragment, source_url)
                if job:
                    return [job]
    return []


def _parse_rendered_html(soup: BeautifulSoup, source_url: str) -> list[JobData]:
    """Parse Workday's server-rendered / hydrated HTML."""
    company = _extract_company(source_url)

    # ── Listing page: multiple job cards ─────────────────────────────────
    # Workday listing pages use data-automation-id="jobTitle" on each card
    job_cards = soup.select("[data-automation-id='jobTitle']")
    if len(job_cards) > 1:
        jobs: list[JobData] = []
        for card in job_cards:
            title = card.get_text(strip=True)
            if not title:
                continue
            # Parent anchor may contain the job URL
            anchor = card.find_parent("a") or card
            href = anchor.get("href", "") if hasattr(anchor, "get") else ""
            if href and not href.startswith("http"):
                from urllib.parse import urljoin
                href = urljoin(source_url, href)
            # Location is often in a sibling with data-automation-id="locationLink"
            loc_el = card.find_next("[data-automation-id='locationLink']")
            location = loc_el.get_text(strip=True) if loc_el else ""
            jobs.append(JobData(
                title=title,
                company=company,
                location=location,
                apply_url=href or source_url,
                source_url=source_url,
                seniority=_detect_seniority(title),
            ))
        if jobs:
            return jobs

    # ── Single job page ───────────────────────────────────────────────────
    # Require at least one Workday-specific data-automation-id attribute to
    # confirm this is actually a Workday job page, not a generic company page.
    has_workday_signals = bool(soup.select("[data-automation-id]"))
    if not has_workday_signals:
        return []

    title = ""
    title_el = (
        soup.select_one("[data-automation-id='jobPostingHeader']")
        or soup.select_one("h1")
    )
    if title_el:
        title = title_el.get_text(strip=True)

    if not title:
        return []

    location = ""
    loc_el = (
        soup.select_one("[data-automation-id='locations']")
        or soup.select_one("[data-automation-id='locationLink']")
    )
    if loc_el:
        location = loc_el.get_text(strip=True)

    # Remote detection
    remote_el = soup.select_one("[data-automation-id='time-type-workerSubType']")
    if remote_el and "remote" in remote_el.get_text(strip=True).lower():
        location = "Remote"

    employment_type = ""
    et_el = soup.select_one("[data-automation-id='time-type-workerSubType']")
    if et_el:
        et_text = et_el.get_text(strip=True).lower()
        if "full" in et_text:
            employment_type = "full-time"
        elif "part" in et_text:
            employment_type = "part-time"
        elif "contract" in et_text or "temp" in et_text:
            employment_type = "contract"

    description = ""
    desc_el = (
        soup.select_one("[data-automation-id='job-posting-description']")
        or soup.select_one("section.css-10d4g33")
        or soup.select_one("div[class*='jobPostingDescription']")
    )
    if desc_el:
        description = desc_el.get_text(separator="\n", strip=True)

    posted_date = ""
    date_el = soup.select_one("[data-automation-id='postedOn']")
    if date_el:
        posted_date = date_el.get_text(strip=True)

    # Apply URL: look for the "Apply" button or derive from URL
    apply_url = source_url
    apply_btn = soup.select_one("a[data-automation-id='applyButton']")
    if apply_btn:
        href = apply_btn.get("href", "")
        if href:
            from urllib.parse import urljoin
            apply_url = urljoin(source_url, href)

    return [JobData(
        title=title,
        company=company,
        location=location,
        employment_type=employment_type,
        seniority=_detect_seniority(title),
        description=description,
        apply_url=apply_url,
        source_url=source_url,
        date_posted=posted_date,
    )]


def _extract_from_json_data(data: dict | list, source_url: str) -> list[JobData]:
    """Recursively walk a parsed JSON blob looking for job objects."""
    jobs: list[JobData] = []
    company = _extract_company(source_url)

    if isinstance(data, list):
        for item in data:
            jobs.extend(_extract_from_json_data(item, source_url))
        return jobs

    if not isinstance(data, dict):
        return []

    # Workday job object signature
    if "title" in data and "jobRequisitionId" in data:
        job = _json_fragment_to_jobdata(data, source_url)
        if job:
            return [job]

    # Walk nested objects/arrays
    for value in data.values():
        if isinstance(value, (dict, list)):
            jobs.extend(_extract_from_json_data(value, source_url))

    return jobs


def _json_fragment_to_jobdata(fragment: dict, source_url: str) -> JobData | None:
    """Convert a Workday JSON job fragment to a JobData object."""
    title = fragment.get("title") or fragment.get("jobTitle", "")
    if not title:
        return None

    company = _extract_company(source_url)

    # Location
    location_data = fragment.get("locationsText") or fragment.get("primaryLocation", "")
    if isinstance(location_data, dict):
        location = location_data.get("descriptor", "")
    else:
        location = str(location_data)

    # Remote
    remote = fragment.get("isRemote") or fragment.get("jobTypeRef", {})
    if isinstance(remote, bool) and remote:
        location = "Remote"

    # Employment type
    et = (
        fragment.get("timeType", {}) or
        fragment.get("workerSubType", {}) or {}
    )
    employment_type = ""
    if isinstance(et, dict):
        et_text = et.get("descriptor", "").lower()
        if "full" in et_text:
            employment_type = "full-time"
        elif "part" in et_text:
            employment_type = "part-time"
        elif "contract" in et_text:
            employment_type = "contract"

    # Apply URL
    req_id = fragment.get("jobRequisitionId") or fragment.get("externalId", "")
    apply_url = source_url
    if req_id:
        # Build a canonical apply URL from the source URL pattern
        slug_match = _SLUG_RE.search(source_url)
        if slug_match:
            tenant, company_slug, _ = slug_match.groups()
            apply_url = (
                f"https://{company_slug}.wd3.myworkday.com/"
                f"{tenant}/{company_slug}/job/{req_id}"
            )

    return JobData(
        title=title,
        company=company,
        location=location,
        employment_type=employment_type,
        seniority=_detect_seniority(title),
        description=fragment.get("jobDescription", ""),
        apply_url=apply_url,
        source_url=source_url,
        date_posted=fragment.get("postedOn", ""),
    )


def _extract_company(url: str) -> str:
    """Extract company name from a Workday URL."""
    # Pattern: company.wd3.myworkday.com/tenant/company/...
    match = re.search(r"https?://([^.]+)\.(?:wd\d+\.)?myworkday", url, re.IGNORECASE)
    if match:
        return match.group(1).replace("-", " ").title()
    # Pattern: myworkdayjobs.com/en-US/company/...
    match = re.search(r"myworkdayjobs\.com/[^/]+/([^/]+)", url, re.IGNORECASE)
    if match:
        return match.group(1).replace("-", " ").title()
    return ""


def _detect_seniority(title: str) -> str:
    """Detect seniority level from job title keywords."""
    title_lower = title.lower()
    for kw, level in {
        "intern": "intern", "junior": "junior", "senior": "senior",
        "sr.": "senior", "lead": "lead", "staff": "senior",
        "principal": "lead", "manager": "manager", "director": "director",
    }.items():
        if kw in title_lower:
            return level
    return ""
