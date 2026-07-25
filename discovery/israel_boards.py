"""Parsers for Israeli job boards (Drushim.co.il & Jobs.co.il)."""

from __future__ import annotations

import re
import structlog
from bs4 import BeautifulSoup
from jobs.models import JobData

logger = structlog.get_logger(__name__)


def parse_drushim_job(html_content: str, source_url: str) -> JobData:
    """Parse job posting HTML from drushim.co.il."""
    soup = BeautifulSoup(html_content, "html.parser")

    title_elem = soup.find("h1") or soup.find(class_=re.compile(r"job-title|title", re.I))
    title = title_elem.get_text(strip=True) if title_elem else "Software Engineer"

    company_elem = soup.find(class_=re.compile(r"company|employer", re.I))
    company = company_elem.get_text(strip=True) if company_elem else "Drushim Employer"

    location_elem = soup.find(class_=re.compile(r"location|area|region", re.I))
    location = location_elem.get_text(strip=True) if location_elem else "Israel"

    desc_elem = soup.find(class_=re.compile(r"description|content|details", re.I)) or soup.body
    description = desc_elem.get_text(" ", strip=True) if desc_elem else html_content

    return JobData(
        title=title,
        company=company,
        location=location,
        description=description,
        requirements=description,
        apply_url=source_url,
        source_url=source_url,
    )


def parse_jobs_il_job(html_content: str, source_url: str) -> JobData:
    """Parse job posting HTML from jobs.co.il / jobmaster.co.il."""
    soup = BeautifulSoup(html_content, "html.parser")

    title_elem = soup.find("h1") or soup.find("h2")
    title = title_elem.get_text(strip=True) if title_elem else "Developer"

    company_elem = soup.find(class_=re.compile(r"comp|company", re.I))
    company = company_elem.get_text(strip=True) if company_elem else "JobIL Employer"

    location_elem = soup.find(class_=re.compile(r"city|location", re.I))
    location = location_elem.get_text(strip=True) if location_elem else "Israel"

    desc_elem = soup.find(class_=re.compile(r"job-desc|description", re.I)) or soup.body
    description = desc_elem.get_text(" ", strip=True) if desc_elem else html_content

    return JobData(
        title=title,
        company=company,
        location=location,
        description=description,
        requirements=description,
        apply_url=source_url,
        source_url=source_url,
    )
