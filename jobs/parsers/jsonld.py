"""JSON-LD (Schema.org JobPosting) parser — highest-fidelity extraction."""

from __future__ import annotations

import json
from typing import Any

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)

# Seniority keywords detected from titles
_SENIORITY_MAP = {
    "intern": "intern",
    "junior": "junior",
    "entry": "entry",
    "associate": "entry",
    "mid": "mid",
    "senior": "senior",
    "sr.": "senior",
    "sr ": "senior",
    "staff": "senior",
    "principal": "lead",
    "lead": "lead",
    "manager": "manager",
    "director": "director",
    "vp": "director",
    "head of": "director",
}


def _detect_seniority(title: str) -> str:
    """Infer seniority level from the job title."""
    title_lower = title.lower()
    for keyword, level in _SENIORITY_MAP.items():
        if keyword in title_lower:
            return level
    return ""


def _extract_location(loc_data: Any) -> str:
    """Extract a human-readable location string from Schema.org jobLocation."""
    if isinstance(loc_data, str):
        return loc_data

    if isinstance(loc_data, list):
        locations = [_extract_location(item) for item in loc_data]
        return " | ".join(filter(None, locations))

    if isinstance(loc_data, dict):
        loc_type = loc_data.get("@type", "")

        if loc_type == "VirtualLocation":
            return "Remote"

        address = loc_data.get("address", loc_data)
        if isinstance(address, dict):
            parts = [
                address.get("addressLocality", ""),
                address.get("addressRegion", ""),
                address.get("addressCountry", ""),
            ]
            return ", ".join(p for p in parts if p)

        return str(address) if address else ""

    return ""


def _extract_employment_type(data: dict) -> str:
    """Map Schema.org employmentType to a readable string."""
    emp = data.get("employmentType", "")
    if isinstance(emp, list):
        emp = emp[0] if emp else ""
    mapping = {
        "FULL_TIME": "full-time",
        "PART_TIME": "part-time",
        "CONTRACT": "contract",
        "TEMPORARY": "contract",
        "INTERN": "internship",
        "VOLUNTEER": "volunteer",
        "PER_DIEM": "per-diem",
        "OTHER": "other",
    }
    return mapping.get(str(emp).upper(), str(emp).lower())


def _extract_salary(data: dict) -> str:
    """Extract salary info from baseSalary or estimatedSalary."""
    for key in ("baseSalary", "estimatedSalary"):
        salary = data.get(key)
        if not salary:
            continue
        if isinstance(salary, dict):
            currency = salary.get("currency", "")
            value = salary.get("value", "")
            if isinstance(value, dict):
                low = value.get("minValue", "")
                high = value.get("maxValue", "")
                return f"{currency} {low}–{high}".strip()
            return f"{currency} {value}".strip()
        return str(salary)
    return ""


def _find_job_postings(data: Any) -> list[dict]:
    """Recursively find all JobPosting objects in parsed JSON-LD data."""
    postings: list[dict] = []

    if isinstance(data, dict):
        schema_type = data.get("@type", "")
        if isinstance(schema_type, list):
            types = schema_type
        else:
            types = [schema_type]

        if "JobPosting" in types:
            postings.append(data)

        # Check @graph array (common in multi-entity JSON-LD)
        if "@graph" in data:
            postings.extend(_find_job_postings(data["@graph"]))

    elif isinstance(data, list):
        for item in data:
            postings.extend(_find_job_postings(item))

    return postings


def _convert_posting(data: dict, source_url: str) -> JobData:
    """Convert a single Schema.org JobPosting dict to our canonical JobData."""
    title = data.get("title", data.get("name", ""))
    company = ""
    org = data.get("hiringOrganization", {})
    if isinstance(org, dict):
        company = org.get("name", "")
    elif isinstance(org, str):
        company = org

    location = _extract_location(data.get("jobLocation", ""))

    # Check for remote / telecommute
    if data.get("jobLocationType") == "TELECOMMUTE" or data.get("applicantLocationRequirements"):
        if location:
            location = f"Remote | {location}"
        else:
            location = "Remote"

    description = data.get("description", "")
    # Strip HTML from description
    if description and "<" in description:
        description = BeautifulSoup(description, "lxml").get_text(separator="\n", strip=True)

    requirements_raw = data.get("qualifications", data.get("skills", ""))
    if isinstance(requirements_raw, list):
        requirements_raw = "\n".join(str(r) for r in requirements_raw)
    requirements = str(requirements_raw)

    apply_url = data.get("url", source_url)

    keywords_raw = data.get("skills", data.get("occupationalCategory", ""))
    keywords: list[str] = []
    if isinstance(keywords_raw, list):
        keywords = [str(k) for k in keywords_raw]
    elif isinstance(keywords_raw, str) and keywords_raw:
        keywords = [k.strip() for k in keywords_raw.split(",")]

    salary = _extract_salary(data)
    if salary:
        keywords.append(f"salary:{salary}")

    return JobData(
        title=title,
        company=company,
        location=location,
        employment_type=_extract_employment_type(data),
        seniority=_detect_seniority(title),
        description=description,
        requirements=requirements,
        apply_url=apply_url,
        source_url=source_url,
        date_posted=data.get("datePosted", ""),
        keywords=keywords,
    )


def parse_jsonld(html: str, source_url: str) -> list[JobData]:
    """Parse JSON-LD script tags for Schema.org JobPosting data.

    Returns a list of JobData objects (may be empty if none found).
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    scripts = soup.find_all("script", {"type": "application/ld+json"})
    if not scripts:
        return []

    all_postings: list[dict] = []

    for script in scripts:
        text = script.get_text(strip=True)
        if not text:
            continue
        try:
            data = json.loads(text)
            found = _find_job_postings(data)
            all_postings.extend(found)
        except json.JSONDecodeError:
            logger.debug("invalid_jsonld", url=source_url)
            continue

    if not all_postings:
        return []

    jobs: list[JobData] = []
    for posting in all_postings:
        try:
            job = _convert_posting(posting, source_url)
            if job.is_complete:
                jobs.append(job)
        except Exception as exc:
            logger.warning("jsonld_conversion_error", url=source_url, error=str(exc))

    logger.info("jsonld_parsed", url=source_url, count=len(jobs))
    return jobs
