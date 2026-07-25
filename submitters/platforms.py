"""Deterministic ATS/platform detection shared by routing and the dashboard."""

from __future__ import annotations

from urllib.parse import urlparse

_PLATFORM_DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workday", ("myworkdayjobs.com", "myworkday.com", "workday.com")),
    ("greenhouse", ("greenhouse.io", "greenhouse-hosted.com")),
    ("lever", ("jobs.lever.co", "lever.co")),
    ("ashby", ("jobs.ashbyhq.com", "ashbyhq.com")),
    ("workable", ("apply.workable.com", "workable.com")),
    ("smartrecruiters", ("jobs.smartrecruiters.com", "smartrecruiters.com")),
    ("jobvite", ("jobs.jobvite.com", "jobvite.com")),
    ("icims", ("icims.com",)),
    ("comeet", ("comeet.com",)),
    ("linkedin", ("linkedin.com",)),
    ("indeed", ("indeed.com",)),
)


def _matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def detect_platform(url: str) -> str:
    """Return a stable platform name without performing network requests."""
    hostname = (urlparse((url or "").strip()).hostname or "").lower().rstrip(".")
    for platform, domains in _PLATFORM_DOMAINS:
        if any(_matches_domain(hostname, domain) for domain in domains):
            return platform
    return "generic_portal" if hostname else "unknown"


def supported_platforms() -> list[str]:
    return [platform for platform, _domains in _PLATFORM_DOMAINS]
