"""Parsers for the Israeli job boards: Drushim, AllJobs, JobMaster.

Same contract as the other parsers in this package —
``parse(html, source_url) -> list[JobData]``, empty list when the page isn't
recognised — so jobs/extractor.py can dispatch to them by hostname.

Two things make these boards different from the Greenhouse/Lever family:

* **They are Hebrew and RTL.** Field labels are Hebrew words, and the useful
  structure is often "label cell / value cell" pairs rather than semantic
  classes. Text must survive as UTF-8 all the way through; there was a real
  cp1252 mangling bug in this project's history, so tests assert on actual
  Hebrew strings rather than on lengths.
* **Their markup drifts.** These are ordinary commercial sites with no public
  API contract, so every selector here is a guess with a shelf life. The
  parsers therefore try several candidates per field and degrade to "" rather
  than raising — a partial job that scoring can still use beats an exception
  that loses the posting entirely.

discovery/israel_boards.py used to fabricate data on a selector miss
(title="Software Engineer", company="Drushim Employer") instead of using this
module — an unreadable page produced a confident-looking posting for a job
that does not exist, which then got scored, generated for, and applied to.
This module returns an empty list rather than inventing a job.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import structlog
from bs4 import BeautifulSoup

from jobs.models import JobData

logger = structlog.get_logger(__name__)

ISRAELI_BOARD_DOMAINS = ("drushim.co.il", "alljobs.co.il", "jobmaster.co.il", "jobs.co.il")

# Hebrew labels that introduce a requirements block. Boards vary in wording,
# and some prefix with "ה" (the) or suffix with ":".
_REQUIREMENT_LABELS = (
    "דרישות התפקיד",
    "דרישות המשרה",
    "דרישות",
    "requirements",
)
_DESCRIPTION_LABELS = (
    "תיאור התפקיד",
    "תיאור המשרה",
    "תיאור",
    "description",
)


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _clean(text: str | None) -> str:
    """Collapse whitespace without touching non-Latin characters.

    ``\\s`` is Unicode-aware here, which is what we want: Hebrew text is left
    intact while NBSPs and stray newlines from the markup are normalised.
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _first(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue  # a malformed selector must not kill the parse
        if el:
            text = _clean(el.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _labelled_section(soup: BeautifulSoup, labels: tuple[str, ...]) -> str:
    """Text that follows a Hebrew section heading.

    These boards mark up a section as a heading element carrying the label,
    followed by the content as siblings. Matching on the label text is more
    durable than matching on their class names, which change often.
    """
    for label in labels:
        for el in soup.find_all(
            string=lambda s, lbl=label: s and lbl in s  # noqa: B023 - bound via default
        ):
            container = el.find_parent()
            if container is None:
                continue
            parts: list[str] = []
            for sib in container.find_next_siblings():
                text = _clean(sib.get_text(" ", strip=True))
                if not text:
                    continue
                # Stop at the next section heading so blocks don't bleed.
                if any(
                    other in text[:40]
                    for other in _REQUIREMENT_LABELS + _DESCRIPTION_LABELS
                    if other != label
                ):
                    break
                parts.append(text)
            if parts:
                return " ".join(parts)
            # Some layouts nest the body inside the labelled container itself.
            own = _clean(container.get_text(" ", strip=True))
            if own and own != label:
                return own.replace(label, "", 1).strip(" :־-")
    return ""


def _absolute(href: str, source_url: str) -> str:
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(source_url, href)


def _board_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for domain in ISRAELI_BOARD_DOMAINS:
        if domain in host:
            return domain
    return ""


def is_israeli_board(url: str) -> bool:
    """Whether the extractor should route this URL to these parsers."""
    return bool(_board_of(url))


# ── per-board selector sets ───────────────────────────────────────────
# Ordered most- to least-specific. The generic fallbacks at the end are what
# keep a posting parseable after the board reskins.

_TITLE_SELECTORS = (
    "h1.job-title",
    "h1[itemprop='title']",
    ".job-title-h1",
    ".jobTitle",
    "[data-testid='job-title']",
    # Class-only before bare tags: on a results card the title is an h2/h3,
    # and matching the class avoids picking up a page-level heading.
    ".job-title",
    "h1",
    "h2.job-title",
    "h2",
    "h3",
)
_COMPANY_SELECTORS = (
    "[itemprop='hiringOrganization']",
    ".company-name",
    ".jobCompany",
    "[data-testid='company-name']",
    ".employer-name",
    "h2.company",
    ".company",
    ".employer",
    "[class*='company']",
)
_LOCATION_SELECTORS = (
    "[itemprop='jobLocation']",
    ".job-location",
    ".jobLocation",
    "[data-testid='job-location']",
    ".location",
    ".city",
    ".area",
    "[class*='location']",
)
_DESCRIPTION_SELECTORS = (
    "[itemprop='description']",
    ".job-description",
    ".job-desc",
    ".description",
    ".jobDescription",
    "[class*='description']",
)
_CARD_SELECTORS = (
    "div.job-item",
    "div.jobList_item",
    "article.job-card",
    "[data-testid='job-card']",
    "div.job-content-top",
)


def parse_israeli_board(html: str, source_url: str) -> list[JobData]:
    """Parse a Drushim / AllJobs / JobMaster posting or results page."""
    if not html or not html.strip():
        return []

    board = _board_of(source_url)
    soup = _soup(html)

    # A results page carries many cards; a posting carries one detail block.
    cards: list = []
    for sel in _CARD_SELECTORS:
        try:
            found = soup.select(sel)
        except Exception:
            continue
        if len(found) > 1:
            cards = found
            break

    if cards:
        jobs = _parse_cards(cards, source_url, board)
        if jobs:
            logger.info("israeli_board_listing_parsed", board=board, count=len(jobs))
            return jobs

    job = _parse_detail(soup, source_url, board)
    if job is None:
        logger.info("israeli_board_no_jobs", board=board, url=source_url)
        return []
    logger.info("israeli_board_job_parsed", board=board, title=job.title)
    return [job]


def _parse_cards(cards: list, source_url: str, board: str) -> list[JobData]:
    jobs: list[JobData] = []
    for card in cards:
        card_soup = card
        link = card_soup.select_one("a[href]")
        title = _first(card_soup, _TITLE_SELECTORS)
        if not title and link is not None:
            # The anchor text, not the whole card: card.get_text() swallows the
            # company and location too and yields a title like
            # "מהנדס DevOps אמדוקס רעננה".
            title = _clean(link.get_text(" ", strip=True))
        if not title:
            continue
        href = _absolute(link.get("href", "") if link else "", source_url)
        job = JobData(
            title=title,
            company=_first(card_soup, _COMPANY_SELECTORS),
            location=_first(card_soup, _LOCATION_SELECTORS),
            apply_url=href,
            source_url=href or source_url,
        )
        if job.is_complete:
            jobs.append(job)
    return jobs


def _parse_detail(soup: BeautifulSoup, source_url: str, board: str) -> JobData | None:
    title = _first(soup, _TITLE_SELECTORS)
    if not title:
        return None

    description = _labelled_section(soup, _DESCRIPTION_LABELS)
    requirements = _labelled_section(soup, _REQUIREMENT_LABELS)

    if not description:
        description = _first(soup, _DESCRIPTION_SELECTORS)
    if not description:
        # Last resort: the main content block, so scoring has something to
        # work with even when neither a Hebrew label nor a known class exists.
        main = soup.select_one("main") or soup.select_one("article") or soup.body
        if main is not None:
            description = _clean(main.get_text(" ", strip=True))[:5000]

    apply_link = soup.select_one("a.apply-button[href], a[href*='apply'], a[href*='Apply']")
    apply_url = _absolute(apply_link.get("href", "") if apply_link else "", source_url)

    job = JobData(
        title=title,
        company=_first(soup, _COMPANY_SELECTORS),
        location=_first(soup, _LOCATION_SELECTORS),
        description=description,
        requirements=requirements,
        apply_url=apply_url or source_url,
        source_url=source_url,
    )
    return job if job.is_complete else None
