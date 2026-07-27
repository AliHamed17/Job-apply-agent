"""Greenhouse discovery using the canonical candidate-URL identity contract."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit

import structlog
from bs4 import BeautifulSoup, Tag

from jobs.models import JobData
from submitters.greenhouse_identity import (
    GreenhouseApplicationIdentity,
    GreenhouseCandidateRoute,
    GreenhouseCandidateUrl,
    GreenhouseIdentityError,
    parse_greenhouse_candidate_url,
)

logger = structlog.get_logger(__name__)

_EMBED_SELECTORS = (
    ("iframe", "src"),
    ("form", "action"),
)

CandidateReference = tuple[GreenhouseCandidateUrl, str]


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def _candidate_reference(
    raw_url: str,
    *,
    base_url: str | None = None,
    required_route: GreenhouseCandidateRoute | None = None,
) -> CandidateReference | None:
    candidate_url = (raw_url or "").strip()
    if (
        base_url is not None
        and candidate_url.startswith("/")
        and not candidate_url.startswith("//")
    ):
        candidate_url = urljoin(base_url, candidate_url)
    try:
        candidate = parse_greenhouse_candidate_url(candidate_url)
        parsed = urlsplit(candidate_url)
    except (GreenhouseIdentityError, ValueError):
        return None
    if required_route is not None and candidate.route is not required_route:
        return None
    normalized_url = urlunsplit(
        (
            "https",
            candidate.hostname,
            parsed.path,
            parsed.query,
            "",
        )
    )
    return candidate, normalized_url


def greenhouse_identity(
    url: str,
    *,
    embedded: bool = False,
) -> GreenhouseCandidateUrl | None:
    """Compatibility wrapper over the single canonical candidate parser."""

    reference = _candidate_reference(
        url,
        required_route=GreenhouseCandidateRoute.EMBEDDED if embedded else None,
    )
    return reference[0] if reference is not None else None


def greenhouse_job_id(url: str) -> str | None:
    """Return the exact canonical Greenhouse job token for a candidate URL."""

    candidate = greenhouse_identity(url)
    return candidate.identity.job_token if candidate is not None else None


def _source_hostname(source_url: str) -> str | None:
    try:
        parsed = urlsplit((source_url or "").strip())
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or hostname != hostname.rstrip(".")
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or any(character.isspace() for character in hostname)
    ):
        return None
    return hostname


def _is_candidate_origin(hostname: str) -> bool:
    try:
        parse_greenhouse_candidate_url(f"https://{hostname}/identity/jobs/1")
    except GreenhouseIdentityError:
        return False
    return True


def _is_rejected_greenhouse_namespace(hostname: str) -> bool:
    # This is a rejection-only lookalike/control-plane check. Candidate
    # admission remains exclusively owned by parse_greenhouse_candidate_url.
    resembles_greenhouse = "greenhouse.io" in hostname or "greenhouse-hosted.com" in hostname
    return resembles_greenhouse and not _is_candidate_origin(hostname)


def _source_board_identity(
    source_url: str,
    *,
    job_token: str,
) -> GreenhouseCandidateUrl | None:
    try:
        source = urlsplit((source_url or "").strip())
    except (TypeError, ValueError):
        return None
    if source.query or source.fragment:
        return None
    probe = urlunsplit(
        (
            source.scheme,
            source.netloc,
            source.path,
            f"gh_jid={job_token}",
            "",
        )
    )
    try:
        candidate = parse_greenhouse_candidate_url(probe)
    except GreenhouseIdentityError:
        return None
    return candidate if candidate.route is GreenhouseCandidateRoute.JOB_ID else None


def _embedded_references(
    soup: BeautifulSoup,
    source_url: str,
) -> tuple[CandidateReference, ...]:
    source_hostname = _source_hostname(source_url)
    if source_hostname is None or _is_rejected_greenhouse_namespace(source_hostname):
        return ()

    references: list[CandidateReference] = []
    for tag_name, attribute in _EMBED_SELECTORS:
        for node in soup.find_all(tag_name):
            if not isinstance(node, Tag):
                continue
            raw = node.get(attribute)
            if not isinstance(raw, str):
                continue
            reference = _candidate_reference(
                raw,
                required_route=GreenhouseCandidateRoute.EMBEDDED,
            )
            if reference is not None:
                references.append(reference)

    for candidate, _ in references:
        source_board = _source_board_identity(
            source_url,
            job_token=candidate.identity.job_token,
        )
        if _is_candidate_origin(source_hostname):
            if (
                source_board is None
                or source_board.identity.board_token != candidate.identity.board_token
            ):
                return ()

    unique = {reference[1]: reference for reference in references}
    values = tuple(unique.values())
    bindings = {reference[0].application_binding for reference in values}
    return values if len(bindings) <= 1 else ()


def _listing_source_matches(
    source_url: str,
    candidate: GreenhouseCandidateUrl,
) -> bool:
    """Validate a board-listing path by converting it to a canonical job-ID route."""

    source_board = _source_board_identity(
        source_url,
        job_token=candidate.identity.job_token,
    )
    return bool(
        source_board is not None
        and source_board.hostname == candidate.hostname
        and source_board.identity == candidate.identity
    )


def _listing_references(
    soup: BeautifulSoup,
    source_url: str,
) -> tuple[tuple[Tag, CandidateReference], ...]:
    source_hostname = _source_hostname(source_url)
    if source_hostname is None:
        return ()

    references: list[tuple[Tag, CandidateReference]] = []
    for link in soup.select("div.opening a[href], li.opening a[href], [data-qa='opening'] a[href]"):
        if not isinstance(link, Tag):
            continue
        raw = link.get("href")
        if not isinstance(raw, str):
            continue
        reference = _candidate_reference(raw, base_url=source_url)
        if (
            reference is None
            or reference[0].hostname != source_hostname
            or not _listing_source_matches(source_url, reference[0])
        ):
            continue
        references.append((link, reference))

    return tuple(references)


def is_greenhouse_page(html: str, source_url: str) -> bool:
    """Recognize only canonical candidate, embedded, or same-origin listing evidence."""

    return bool(greenhouse_page_references(html, source_url))


def greenhouse_page_references(
    html: str,
    source_url: str,
) -> tuple[CandidateReference, ...]:
    """Return the canonical candidate targets proven by one page.

    The references are safe to use as an allowlist before generic metadata is
    parsed. They contain no page text or candidate data.
    """

    source_reference = _candidate_reference(source_url)
    if source_reference is not None:
        return (source_reference,)
    soup = _soup(html)
    embedded = _embedded_references(soup, source_url)
    if embedded:
        return embedded
    references: list[CandidateReference] = []
    seen: set[GreenhouseApplicationIdentity] = set()
    for _link, reference in _listing_references(soup, source_url):
        identity = reference[0].identity
        if identity in seen:
            continue
        references.append(reference)
        seen.add(identity)
    return tuple(references)


def _is_opening_container(tag: Tag) -> bool:
    class_value = tag.get("class")
    if isinstance(class_value, str):
        classes = {class_value.casefold()}
    elif class_value is None:
        classes = set()
    else:
        classes = {str(item).casefold() for item in class_value}
    return "opening" in classes or str(tag.get("data-qa", "")).casefold() == "opening"


def _opening_container(link: Tag) -> Tag | None:
    parent = link.find_parent(lambda tag: isinstance(tag, Tag) and _is_opening_container(tag))
    return parent if isinstance(parent, Tag) else None


def _listing_jobs(
    soup: BeautifulSoup,
    source_url: str,
) -> list[JobData]:
    jobs: list[JobData] = []
    seen: set[GreenhouseApplicationIdentity] = set()
    for link, reference in _listing_references(soup, source_url):
        candidate, candidate_url = reference
        title = link.get_text(" ", strip=True)
        if not title or candidate.identity in seen:
            continue
        container = _opening_container(link)
        location_node = (
            container.select_one(".location, [data-qa='location']") if container else None
        )
        jobs.append(
            JobData(
                title=title,
                company=_extract_company_greenhouse(soup, candidate),
                location=location_node.get_text(" ", strip=True) if location_node else "",
                seniority=_detect_seniority(title),
                apply_url=candidate_url,
                source_url=source_url,
            )
        )
        seen.add(candidate.identity)
    return jobs


def _single_job(
    soup: BeautifulSoup,
    source_url: str,
    reference: CandidateReference,
) -> list[JobData]:
    candidate, candidate_url = reference
    title_node = (
        soup.select_one("h1.app-title")
        or soup.select_one("[data-qa='job-title']")
        or soup.select_one(".job__title h1")
        or soup.select_one("h1")
    )
    title = title_node.get_text(" ", strip=True) if title_node else ""
    if not title:
        return []

    location_node = soup.select_one(".location, [data-qa='location']")
    description_node = (
        soup.select_one("#content")
        or soup.select_one("[data-qa='job-description']")
        or soup.select_one(".content")
    )
    return [
        JobData(
            title=title,
            company=_extract_company_greenhouse(soup, candidate),
            location=location_node.get_text(" ", strip=True) if location_node else "",
            seniority=_detect_seniority(title),
            description=(
                description_node.get_text(separator="\n", strip=True) if description_node else ""
            ),
            apply_url=candidate_url,
            source_url=source_url,
        )
    ]


def parse_greenhouse(html: str, source_url: str) -> list[JobData]:
    """Parse only jobs whose final candidate URL satisfies the execution contract."""

    soup = _soup(html)
    source_reference = _candidate_reference(source_url)
    if source_reference is not None:
        jobs = _single_job(soup, source_url, source_reference)
    else:
        embedded = _embedded_references(soup, source_url)
        if embedded:
            jobs = _single_job(soup, source_url, embedded[0])
        else:
            jobs = _listing_jobs(soup, source_url)

    if jobs:
        logger.info(
            "greenhouse_parsed",
            source_host=(_source_hostname(source_url) or "")[:255],
            count=len(jobs),
        )
    return jobs


def _extract_company_greenhouse(
    soup: BeautifulSoup,
    candidate: GreenhouseCandidateUrl,
) -> str:
    """Extract a display name while retaining the canonical board as fallback."""

    for selector in ('meta[property="og:site_name"]', 'meta[property="og:title"]'):
        meta = soup.select_one(selector)
        if meta is None:
            continue
        content = str(meta.get("content", "")).strip()
        if not content:
            continue
        if selector.endswith('og:title"]'):
            if " at " not in content:
                continue
            content = content.rsplit(" at ", 1)[-1].strip()
        if content:
            return content[:500]
    return candidate.identity.board_token.replace("-", " ").replace("_", " ").title()


def _detect_seniority(title: str) -> str:
    """Detect seniority from title."""

    title_lower = title.casefold()
    for keyword, level in {
        "intern": "intern",
        "junior": "junior",
        "senior": "senior",
        "sr.": "senior",
        "lead": "lead",
        "staff": "senior",
        "principal": "lead",
        "manager": "manager",
        "director": "director",
    }.items():
        if keyword in title_lower:
            return level
    return ""
