"""Strict, read-only identity resolution for SmartRecruiters candidate pages."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from bs4 import BeautifulSoup, Tag

_CANDIDATE_HOST = "jobs.smartrecruiters.com"
_COMPANY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,126}[A-Za-z0-9])?$")
_POSTING_SEGMENT_RE = re.compile(
    r"^(?P<public_id>[0-9]{6,20})"
    r"(?:-(?P<slug>[A-Za-z0-9](?:[A-Za-z0-9-]{0,198}[A-Za-z0-9])?))?$"
)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_MAX_RESOLVER_HTML_BYTES = 256 * 1024


class SmartRecruitersIdentityError(ValueError):
    """One public candidate URL or resolver observation was not exact."""


@dataclass(frozen=True, slots=True)
class SmartRecruitersCandidateIdentity:
    """Stable public identity; the numeric ID is never treated as a UUID."""

    hostname: str
    company: str
    public_id: str
    slug: str | None

    @property
    def posting_segment(self) -> str:
        return f"{self.public_id}-{self.slug}" if self.slug else self.public_id

    @property
    def job_url(self) -> str:
        return f"https://{self.hostname}/{self.company}/{self.posting_segment}"

    @property
    def apply_url(self) -> str:
        return f"{self.job_url}/apply"

    @property
    def stable_key(self) -> str:
        return f"{self.hostname}/{self.company}/{self.public_id}"


@dataclass(frozen=True, slots=True, repr=False)
class SmartRecruitersResolvedIdentity:
    """Candidate identity plus independently observed posting UUID evidence."""

    candidate: SmartRecruitersCandidateIdentity
    posting_uuid: str
    resolver_evidence_sha256: str
    resolver_source: str = "candidate_page_metadata"

    def __post_init__(self) -> None:
        try:
            parsed = UUID(self.posting_uuid)
        except (AttributeError, TypeError, ValueError) as exc:
            raise SmartRecruitersIdentityError("SMARTRECRUITERS_POSTING_UUID_INVALID") from exc
        if (
            str(parsed) != self.posting_uuid
            or parsed.int == 0
            or _UUID_RE.fullmatch(self.posting_uuid) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.resolver_evidence_sha256) is None
            or self.resolver_source != "candidate_page_metadata"
        ):
            raise SmartRecruitersIdentityError("SMARTRECRUITERS_POSTING_UUID_INVALID")

    @property
    def stable_key(self) -> str:
        return f"{self.candidate.stable_key}/{self.posting_uuid}"


def _candidate_url_parts(
    url: str,
) -> tuple[str, tuple[str, ...]]:
    try:
        parsed = urlsplit((url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_URL_INVALID") from exc
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or hostname != _CANDIDATE_HOST
        or hostname != hostname.rstrip(".")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or "\\" in parsed.path
        or not parsed.path.startswith("/")
        or parsed.path.endswith("/")
        or "//" in parsed.path
        or any(ord(character) > 127 for character in hostname)
    ):
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_URL_INVALID")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_URL_INVALID")
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    return hostname, segments


def parse_smartrecruiters_candidate_identity(
    url: str,
) -> SmartRecruitersCandidateIdentity:
    """Parse one exact public numeric posting route and optional ``/apply``."""

    hostname, segments = _candidate_url_parts(url)
    if len(segments) not in {2, 3}:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_CANDIDATE_IDENTITY_REQUIRED")
    company, posting_segment, *suffix = segments
    match = _POSTING_SEGMENT_RE.fullmatch(posting_segment)
    public_id = match.group("public_id") if match is not None else ""
    if (
        _COMPANY_RE.fullmatch(company) is None
        or match is None
        or not public_id.strip("0")
        or (suffix and suffix != ["apply"])
    ):
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_CANDIDATE_IDENTITY_REQUIRED")
    return SmartRecruitersCandidateIdentity(
        hostname=hostname,
        company=company,
        public_id=public_id,
        slug=match.group("slug"),
    )


def is_smartrecruiters_candidate_url(url: str) -> bool:
    try:
        parse_smartrecruiters_candidate_identity(url)
    except SmartRecruitersIdentityError:
        return False
    return True


def same_smartrecruiters_candidate(left: str, right: str) -> bool:
    try:
        left_identity = parse_smartrecruiters_candidate_identity(left)
        right_identity = parse_smartrecruiters_candidate_identity(right)
    except SmartRecruitersIdentityError:
        return False
    return (
        left_identity.hostname,
        left_identity.company,
        left_identity.public_id,
    ) == (
        right_identity.hostname,
        right_identity.company,
        right_identity.public_id,
    )


def resolve_smartrecruiters_posting_identity(
    html: str,
    candidate: SmartRecruitersCandidateIdentity,
) -> SmartRecruitersResolvedIdentity:
    """Resolve one UUID from exact read-only candidate-page metadata.

    The resolver never performs a request and never transforms the public
    numeric ID, company slug, or title slug into a UUID.
    """

    if len((html or "").encode("utf-8")) > _MAX_RESOLVER_HTML_BYTES:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_RESOLVER_INVALID")
    soup = BeautifulSoup(html or "", "html.parser")
    nodes = [
        node
        for node in soup.select(
            '[data-qa="posting-identity"][data-company][data-public-id]'
            "[data-posting-uuid][data-candidate-url]"
        )
        if isinstance(node, Tag)
    ]
    if len(nodes) != 1:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_RESOLVER_INVALID")
    node = nodes[0]
    observed_url = str(node.get("data-candidate-url", "")).strip()
    try:
        observed_candidate = parse_smartrecruiters_candidate_identity(observed_url)
    except SmartRecruitersIdentityError as exc:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_RESOLVER_INVALID") from exc
    if (
        observed_candidate.hostname != candidate.hostname
        or observed_candidate.company != candidate.company
        or observed_candidate.public_id != candidate.public_id
        or str(node.get("data-company", "")).strip() != candidate.company
        or str(node.get("data-public-id", "")).strip() != candidate.public_id
    ):
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_RESOLVER_INVALID")
    raw_uuid = str(node.get("data-posting-uuid", "")).strip().casefold()
    try:
        parsed_uuid = UUID(raw_uuid)
        canonical_uuid = str(parsed_uuid)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_RESOLVER_INVALID") from exc
    if canonical_uuid != raw_uuid or parsed_uuid.int == 0 or _UUID_RE.fullmatch(raw_uuid) is None:
        raise SmartRecruitersIdentityError("SMARTRECRUITERS_RESOLVER_INVALID")
    evidence = "|".join(
        (
            "smartrecruiters-readonly-resolver-v1",
            candidate.stable_key,
            raw_uuid,
            observed_candidate.job_url,
        )
    )
    return SmartRecruitersResolvedIdentity(
        candidate=candidate,
        posting_uuid=raw_uuid,
        resolver_evidence_sha256=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
    )
