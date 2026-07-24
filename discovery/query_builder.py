"""Build LinkedIn job-search URLs from the user profile."""

from __future__ import annotations

from urllib.parse import urlencode

_BASE = "https://www.linkedin.com/jobs/search/"
_MAX_URLS = 30


def build_search_urls(profile, pages_per_query: int = 3) -> list[str]:
    """Build LinkedIn job-search URLs from profile preferences.

    Args:
        profile: UserProfile with preferences.roles and preferences.locations
        pages_per_query: Number of pages (pagination offsets) per role/location combo

    Returns:
        List of LinkedIn search URLs with Easy Apply filter, newest sort, 24h recency,
        and pagination. Capped at _MAX_URLS.
    """
    roles = profile.preferences.roles or ["Engineer"]
    locations = profile.preferences.locations or [""]
    urls: list[str] = []
    for role in roles:
        for loc in locations:
            for page in range(pages_per_query):
                params = {
                    "keywords": role,
                    "location": loc,
                    "f_AL": "true",       # Easy Apply
                    "f_TPR": "r86400",    # last 24h
                    "sortBy": "DD",       # newest
                    "start": page * 25,
                }
                urls.append(f"{_BASE}?{urlencode(params)}")
                if len(urls) >= _MAX_URLS:
                    return urls
    return urls
