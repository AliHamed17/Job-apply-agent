"""Strict, network-free identity parsing for public Lever candidate URLs."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_LEVER_HOSTS = frozenset({"jobs.lever.co", "jobs.eu.lever.co"})
_SITE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,126}[A-Za-z0-9])?$")
_POSTING_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class LeverIdentityError(ValueError):
    """Raised when a URL cannot identify one exact public Lever posting."""


@dataclass(frozen=True, slots=True)
class LeverPostingIdentity:
    """Canonical identity of one Lever candidate posting."""

    hostname: str
    site: str
    posting_id: str

    @property
    def job_url(self) -> str:
        return f"https://{self.hostname}/{self.site}/{self.posting_id}"

    @property
    def apply_url(self) -> str:
        return f"{self.job_url}/apply"

    @property
    def stable_key(self) -> str:
        return f"{self.hostname}/{self.site}/{self.posting_id}"

    def matches(self, other: LeverPostingIdentity) -> bool:
        return self == other


def _public_url_parts(url: str) -> tuple[str, tuple[str, ...]]:
    candidate = (url or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise LeverIdentityError("LEVER_URL_INVALID") from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in _LEVER_HOSTS
        or hostname != hostname.rstrip(".")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or "\\" in parsed.path
        or any(ord(character) > 127 for character in hostname)
    ):
        raise LeverIdentityError("LEVER_URL_INVALID")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise LeverIdentityError("LEVER_URL_INVALID")
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    return hostname, segments


def is_lever_public_url(url: str) -> bool:
    """Return whether a URL is on an exact public Lever candidate host."""

    try:
        hostname, segments = _public_url_parts(url)
    except LeverIdentityError:
        return False
    del hostname
    return bool(segments and _SITE_RE.fullmatch(segments[0]))


def parse_lever_posting_identity(url: str) -> LeverPostingIdentity:
    """Parse only ``/{site}/{posting UUID}`` and its canonical apply variant."""

    hostname, segments = _public_url_parts(url)
    if len(segments) not in {2, 3}:
        raise LeverIdentityError("LEVER_POSTING_IDENTITY_REQUIRED")
    site, posting_id, *suffix = segments
    if (
        not _SITE_RE.fullmatch(site)
        or not _POSTING_RE.fullmatch(posting_id)
        or (suffix and suffix != ["apply"])
    ):
        raise LeverIdentityError("LEVER_POSTING_IDENTITY_REQUIRED")
    return LeverPostingIdentity(
        hostname=hostname,
        site=site,
        posting_id=posting_id.lower(),
    )


def canonical_lever_job_url(url: str) -> str:
    return parse_lever_posting_identity(url).job_url


def canonical_lever_apply_url(url: str) -> str:
    return parse_lever_posting_identity(url).apply_url


def canonical_lever_listing_url(url: str) -> str:
    """Canonicalize one exact ``/{site}`` listing URL."""

    hostname, segments = _public_url_parts(url)
    if len(segments) != 1 or not _SITE_RE.fullmatch(segments[0]):
        raise LeverIdentityError("LEVER_LISTING_IDENTITY_REQUIRED")
    return urlunsplit(("https", hostname, f"/{segments[0]}", "", ""))


def same_lever_posting(left: str, right: str) -> bool:
    try:
        return parse_lever_posting_identity(left) == parse_lever_posting_identity(right)
    except LeverIdentityError:
        return False
