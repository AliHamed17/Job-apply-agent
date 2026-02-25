"""Safe HTTP fetcher with retries, timeouts, caching, and robots.txt checking."""

from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import get_settings

logger = structlog.get_logger(__name__)

# Simple in-memory cache (swap for Redis in production)
_page_cache: dict[str, str] = {}
_robots_cache: dict[str, RobotFileParser] = {}

USER_AGENT = "JobApplyAgent/1.0 (+https://github.com/AliHamed17/Job-apply-agent)"


def _check_robots_txt(url: str) -> bool:
    """Check if we are allowed to fetch this URL per robots.txt."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        if robots_url not in _robots_cache:
            rp = RobotFileParser()
            rp.set_url(robots_url)
            try:
                rp.read()
            except Exception:
                # If we can't read robots.txt, assume allowed
                return True
            _robots_cache[robots_url] = rp

        return _robots_cache[robots_url].can_fetch(USER_AGENT, url)
    except Exception:
        return True  # fail-open


class FetchResult:
    """Result of fetching a page."""

    def __init__(
        self,
        url: str,
        html: str = "",
        status_code: int = 0,
        success: bool = False,
        error: str = "",
        blocked: bool = False,
    ):
        self.url = url
        self.html = html
        self.status_code = status_code
        self.success = success
        self.error = error
        self.blocked = blocked


@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _do_fetch(url: str, timeout: float = 15.0) -> httpx.Response:
    """Perform the actual HTTP GET with retry logic."""
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        return client.get(url)


# Common bot-detection indicators
_BLOCK_INDICATORS = [
    "captcha", "cf-challenge", "access denied", "blocked",
    "please verify you are a human", "enable javascript",
]


def fetch_page(url: str) -> FetchResult:
    """Fetch a page safely with caching, robots.txt, and rate limiting.

    Returns a FetchResult with success/failure metadata.
    """
    settings = get_settings()

    # Check cache
    if url in _page_cache:
        logger.debug("cache_hit", url=url)
        return FetchResult(url=url, html=_page_cache[url], success=True)

    # Check robots.txt
    if not _check_robots_txt(url):
        logger.warning("robots_txt_disallowed", url=url)
        return FetchResult(url=url, error="Blocked by robots.txt", blocked=True)

    # Polite crawl delay
    time.sleep(settings.polite_crawl_delay_seconds)

    try:
        resp = _do_fetch(url)

        # Check for bot protection
        if resp.status_code == 403 or resp.status_code == 429:
            return FetchResult(
                url=url,
                status_code=resp.status_code,
                error=f"HTTP {resp.status_code} — likely bot protection",
                blocked=True,
            )

        html = resp.text

        # Heuristic: detect CAPTCHA / challenge pages
        lower_html = html[:5000].lower()
        for indicator in _BLOCK_INDICATORS:
            if indicator in lower_html:
                logger.warning("bot_protection_detected", url=url, indicator=indicator)
                return FetchResult(
                    url=url,
                    html=html,
                    status_code=resp.status_code,
                    error=f"Bot protection detected: {indicator}",
                    blocked=True,
                )

        # Cache and return
        _page_cache[url] = html
        logger.info("page_fetched", url=url, status=resp.status_code, length=len(html))
        return FetchResult(url=url, html=html, status_code=resp.status_code, success=True)

    except Exception as exc:
        logger.error("fetch_failed", url=url, error=str(exc))
        return FetchResult(url=url, error=str(exc))
