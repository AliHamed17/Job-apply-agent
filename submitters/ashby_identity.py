"""Canonical identity parsing for public Ashby candidate pages.

Only the documented public candidate host and the exact job/application route
shapes are accepted. Query parameters are treated as bounded tracking metadata
and never contribute authority to select another posting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

ASHBY_CANDIDATE_HOST = "jobs.ashbyhq.com"
_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_ALLOWED_QUERY_KEYS = frozenset(
    {
        "source",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
_MAX_QUERY_ITEMS = 8
_MAX_QUERY_VALUE_CHARS = 256


class AshbyIdentityError(ValueError):
    """A candidate URL is not an exact, bounded Ashby application identity."""


class AshbyCandidateRoute(StrEnum):
    JOB = "job"
    APPLICATION = "application"


@dataclass(frozen=True, slots=True)
class AshbyApplicationIdentity:
    """Stable employer board plus posting UUID."""

    board_token: str
    posting_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.board_token, str)
            or _BOARD_TOKEN.fullmatch(self.board_token) is None
            or not isinstance(self.posting_id, str)
            or _canonical_uuid(self.posting_id) != self.posting_id
        ):
            raise AshbyIdentityError("ASHBY_APPLICATION_IDENTITY_INVALID")


@dataclass(frozen=True, slots=True)
class AshbyCandidateUrl:
    """Validated public candidate URL independent of optional tracking data."""

    hostname: str
    identity: AshbyApplicationIdentity
    route: AshbyCandidateRoute

    @property
    def application_binding(self) -> tuple[str, str, str]:
        return (
            self.hostname,
            self.identity.board_token,
            self.identity.posting_id,
        )


def _canonical_uuid(raw: str) -> str:
    try:
        parsed = UUID(raw)
    except (AttributeError, ValueError) as exc:
        raise AshbyIdentityError("ASHBY_POSTING_ID_INVALID") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise AshbyIdentityError("ASHBY_POSTING_ID_NOT_CANONICAL")
    return canonical


def _validate_query(query: str) -> None:
    try:
        entries = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=_MAX_QUERY_ITEMS,
        )
    except ValueError as exc:
        raise AshbyIdentityError("ASHBY_QUERY_INVALID") from exc
    keys: set[str] = set()
    for raw_key, raw_value in entries:
        key = raw_key.strip().casefold()
        if (
            key not in _ALLOWED_QUERY_KEYS
            or key in keys
            or not raw_value.strip()
            or len(raw_value) > _MAX_QUERY_VALUE_CHARS
            or "\\" in raw_value
            or any(ord(character) < 32 or ord(character) == 127 for character in raw_value)
        ):
            raise AshbyIdentityError("ASHBY_QUERY_INVALID")
        keys.add(key)


def parse_ashby_candidate_url(
    url: str,
    *,
    expected_hostname: str | None = None,
    expected_identity: AshbyApplicationIdentity | None = None,
) -> AshbyCandidateUrl:
    """Parse one exact public Ashby job or application route."""

    candidate = (url or "").strip()
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise AshbyIdentityError("ASHBY_URL_INVALID") from exc
    if (
        parsed.scheme.casefold() != "https"
        or hostname != ASHBY_CANDIDATE_HOST
        or hostname != hostname.rstrip(".")
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise AshbyIdentityError("ASHBY_CANDIDATE_ORIGIN_INVALID")
    if expected_hostname is not None and hostname != expected_hostname.casefold():
        raise AshbyIdentityError("ASHBY_HOST_CHANGED")

    segments = parsed.path.split("/")
    if segments and segments[0] == "":
        segments = segments[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    if len(segments) not in {2, 3}:
        raise AshbyIdentityError("ASHBY_ROUTE_INVALID")
    board_token, raw_posting_id = segments[:2]
    if not _BOARD_TOKEN.fullmatch(board_token):
        raise AshbyIdentityError("ASHBY_BOARD_TOKEN_INVALID")
    if len(segments) == 3 and segments[2] != "application":
        raise AshbyIdentityError("ASHBY_ROUTE_INVALID")
    posting_id = _canonical_uuid(raw_posting_id)
    identity = AshbyApplicationIdentity(
        board_token=board_token,
        posting_id=posting_id,
    )
    if expected_identity is not None and identity != expected_identity:
        raise AshbyIdentityError("ASHBY_APPLICATION_IDENTITY_CHANGED")
    _validate_query(parsed.query)
    return AshbyCandidateUrl(
        hostname=hostname,
        identity=identity,
        route=(AshbyCandidateRoute.APPLICATION if len(segments) == 3 else AshbyCandidateRoute.JOB),
    )


def canonical_ashby_application_url(url: str) -> str:
    """Return the exact query-free application route for one candidate URL."""

    parsed = parse_ashby_candidate_url(url)
    identity = parsed.identity
    return f"https://{parsed.hostname}/{identity.board_token}/{identity.posting_id}/application"


def is_ashby_candidate_url(url: str) -> bool:
    """Return whether a URL satisfies the complete candidate identity contract."""

    try:
        parse_ashby_candidate_url(url)
    except AshbyIdentityError:
        return False
    return True
