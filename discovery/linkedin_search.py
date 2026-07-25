"""LinkedIn job-search results page parser + browser-driven discovery run.

``parse_search_results`` is pure and unit-tested (see
``tests/test_linkedin_search_parse.py``) — it takes saved LinkedIn
job-search results HTML and returns the ``JobData`` rows it can find.

``run_discovery`` is the browser-driven half: it builds search URLs from
the user's profile, loads each one in a persistent Chromium context,
scrolls to trigger LinkedIn's lazy-loaded cards, parses the resulting
HTML with ``parse_search_results``, and inserts deduped ``Job`` rows.
It is intentionally NOT unit-tested (per the Task 4.3 brief); Playwright
is imported lazily inside the function so this module — including the
pure parser above — can be imported without the optional ``browser``
extra installed.

Discovery is READ-ONLY: it never calls ``governor.record_application()``.
That counter is reserved for actual submissions (see ``submitters/``).
"""

from __future__ import annotations

import asyncio
import re

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)

# Numeric LinkedIn job id out of a /jobs/view/<id> href.
_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)", re.IGNORECASE)

# Card root selectors, tried in order — first one that matches anything wins.
_CARD_SELECTORS = (
    "div.job-card-container[data-job-id]",
    "li[data-occludable-job-id]",
    "[data-job-id]",
)

_TITLE_SELECTORS = (
    ".job-card-container__link",
    ".job-card-list__title",
    ".artdeco-entity-lockup__title",
)

_COMPANY_SELECTORS = (
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__primary-description",
)

_LOCATION_SELECTORS = (
    ".artdeco-entity-lockup__caption",
    ".job-card-container__metadata-wrapper",
    ".job-card-container__metadata-item",
)

# Strings that indicate LinkedIn threw up a challenge/CAPTCHA page —
# discovery must never try to solve these, only stop gracefully.
_CHALLENGE_MARKERS = ("checkpoint", "captcha")


def _first_text(card, selectors: tuple[str, ...]) -> str:
    """Return the text of the first selector that matches and is non-empty."""
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return ""


def _extract_job_id(card) -> str:
    """Get the numeric LinkedIn job id from a card's attributes or link href."""
    job_id = card.get("data-job-id") or card.get("data-occludable-job-id") or ""
    if job_id:
        return str(job_id).strip()

    link = card.select_one("a[href*='/jobs/view/']")
    if link:
        match = _JOB_ID_RE.search(link.get("href", ""))
        if match:
            return match.group(1)
    return ""


def parse_search_results(html: str) -> list[JobData]:
    """Parse a saved LinkedIn job-search results page into ``JobData`` rows.

    Cards missing a title are skipped. ``apply_url`` and ``source_url``
    are both built as ``https://www.linkedin.com/jobs/view/<id>`` from the
    card's job id (left as ``""`` if no id can be found). ``keywords`` is
    always ``[]`` — search-results cards don't carry structured keywords.
    """
    if not html or not html.strip():
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    cards = []
    for sel in _CARD_SELECTORS:
        cards = soup.select(sel)
        if cards:
            break

    jobs: list[JobData] = []
    for card in cards:
        title = _first_text(card, _TITLE_SELECTORS)
        if not title:
            logger.debug("linkedin_search_card_no_title")
            continue

        company = _first_text(card, _COMPANY_SELECTORS)
        location = _first_text(card, _LOCATION_SELECTORS)
        job_id = _extract_job_id(card)
        url = f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else ""

        jobs.append(
            JobData(
                title=title,
                company=company,
                location=location,
                apply_url=url,
                source_url=url,
                keywords=[],
            )
        )

    logger.info("linkedin_search_parsed", count=len(jobs))
    return jobs


async def run_discovery(db, profile, settings, governor) -> int:
    """Discover new LinkedIn jobs via search results — browser-driven, READ-ONLY.

    Builds search URLs from ``profile`` (via
    ``discovery.query_builder.build_search_urls``), then for each URL:

    1. Gate on ``governor.can_act()`` — stop the whole run if it says no
       (kill switch / cooldown / outside active hours / daily cap).
    2. Load the URL in a persistent Chromium context at
       ``settings.linkedin_browser_profile_dir`` and scroll it to trigger
       LinkedIn's lazy-loaded job cards.
    3. Bail out (without ever attempting to solve anything) if the page
       looks like a CAPTCHA/checkpoint challenge — trips
       ``governor.trip_cooldown()`` (so the apply drainer also pauses)
       and fires a best-effort WhatsApp alert via
       ``worker.alerts.notify_challenge``.
    4. Parse the page with ``parse_search_results`` and insert any new
       (deduped) ``Job`` rows, enqueuing ``score_job_task`` for each.
    5. Sleep ``governor.next_gap_seconds()`` before the next URL.

    Returns the number of ``Job`` rows inserted. Never calls
    ``governor.record_application()`` — discovery only reads search
    results, it doesn't submit anything.
    """
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from discovery.ingest import ingest_discovered_jobs
    from discovery.query_builder import build_search_urls

    urls = build_search_urls(profile, settings.discovery_pages_per_query)
    inserted = 0

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            settings.linkedin_browser_profile_dir,
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            for url in urls:
                can_act, reason = governor.can_act()
                if not can_act:
                    logger.info("discovery_stopped", reason=reason, url=url)
                    break

                try:
                    await page.goto(url, timeout=20_000)
                    await page.wait_for_timeout(1500)
                    for _ in range(3):
                        await page.mouse.wheel(0, 2000)
                        await page.wait_for_timeout(800)
                    html = await page.content()
                except Exception as exc:
                    logger.warning("discovery_page_load_failed", url=url, error=str(exc))
                    await asyncio.sleep(governor.next_gap_seconds())
                    continue

                html_lower = html.lower()
                if any(marker in html_lower for marker in _CHALLENGE_MARKERS):
                    logger.warning("discovery_challenge_detected", url=url)
                    governor.trip_cooldown()
                    from worker.alerts import notify_challenge  # noqa: PLC0415
                    await notify_challenge(settings)
                    break

                inserted += ingest_discovered_jobs(
                    db,
                    parse_search_results(html),
                    source="linkedin_search",
                    easy_apply=True,
                    tasks_always_eager=settings.tasks_always_eager,
                )

                await asyncio.sleep(governor.next_gap_seconds())
        finally:
            await ctx.close()

    logger.info("discovery_run_complete", inserted=inserted, urls_visited=len(urls))
    return inserted
