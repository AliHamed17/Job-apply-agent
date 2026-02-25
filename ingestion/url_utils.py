"""URL normalization, expansion, and hashing utilities."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Tracking query parameters to strip
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referer",
    "source", "si", "igshid",
}

# Known URL shortener domains
SHORT_URL_DOMAINS = {
    "bit.ly", "t.co", "goo.gl", "tinyurl.com", "ow.ly",
    "is.gd", "buff.ly", "tiny.cc", "lnkd.in", "rb.gy",
    "cutt.ly", "shorturl.at",
}


def normalize_url(url: str) -> str:
    """Normalize a URL: lowercase host, strip tracking params, remove fragments."""
    try:
        parsed = urlparse(url)

        # Lowercase the scheme and host
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()

        # Strip trailing slash from path (but keep "/" if it's the only path)
        path = parsed.path.rstrip("/") or "/"

        # Filter out tracking query params
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            filtered = {
                k: v for k, v in qs.items()
                if k.lower() not in TRACKING_PARAMS
            }
            query = urlencode(filtered, doseq=True) if filtered else ""
        else:
            query = ""

        # Drop fragment
        return urlunparse((scheme, netloc, path, "", query, ""))

    except Exception:
        return url


def url_hash(url: str) -> str:
    """SHA-256 hash of a URL for deduplication."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def is_short_url(url: str) -> bool:
    """Check if a URL belongs to a known shortener."""
    try:
        host = urlparse(url).netloc.lower()
        return host in SHORT_URL_DOMAINS
    except Exception:
        return False


async def expand_short_url(url: str, max_redirects: int = 5) -> str:
    """Follow redirects to resolve a shortened URL.

    Uses HEAD requests to avoid downloading full page content.
    Returns the original URL on failure.
    """
    if not is_short_url(url):
        return url

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=max_redirects,
            timeout=10.0,
        ) as client:
            resp = await client.head(url)
            final_url = str(resp.url)
            logger.debug("expanded_short_url", original=url, expanded=final_url)
            return final_url
    except Exception as exc:
        logger.warning("short_url_expansion_failed", url=url, error=str(exc))
        return url


def job_signature(title: str, company: str, location: str) -> str:
    """Generate a dedup signature for a job posting."""
    raw = f"{title.lower().strip()}|{company.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
