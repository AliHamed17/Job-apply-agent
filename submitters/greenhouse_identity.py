"""Canonical public Greenhouse candidate URL and application identity contract.

This module is deliberately dependency-free so discovery, platform detection,
browser admission, and final-action execution can share one parser instead of
drifting into hostname-only or path-only interpretations.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlsplit

_ALLOWED_CANDIDATE_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "greenhouse-hosted.com",
    }
)
_IDENTITY_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_JOB_TOKEN = re.compile(r"^[0-9]{1,20}$")
_HOSTED_PATH = re.compile(r"^/([A-Za-z0-9_-]{1,120})/jobs/([0-9]{1,20})/?$")


class GreenhouseIdentityError(ValueError):
    """A URL is not one exact, public Greenhouse candidate application."""


class GreenhouseCandidateRoute(StrEnum):
    """Qualified public route shapes carrying the same board and job identity."""

    HOSTED = "hosted"
    EMBEDDED = "embedded"
    JOB_ID = "job_id"


@dataclass(frozen=True, slots=True)
class GreenhouseApplicationIdentity:
    """Canonical employer board and job identity independent of route shape."""

    board_token: str
    job_token: str


@dataclass(frozen=True, slots=True)
class GreenhouseCandidateUrl:
    """Validated candidate URL with exact origin, route, and application binding."""

    hostname: str
    identity: GreenhouseApplicationIdentity
    route: GreenhouseCandidateRoute

    @property
    def application_binding(self) -> tuple[str, str, str]:
        """Exact origin plus canonical board and job identity."""

        return (
            self.hostname,
            self.identity.board_token,
            self.identity.job_token,
        )


def _bounded_query(query: str) -> dict[str, str]:
    if (
        "\\" in query
        or "%" in query
        or any(ord(character) < 32 or ord(character) == 127 for character in query)
    ):
        raise GreenhouseIdentityError("GREENHOUSE_QUERY_INVALID")
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise GreenhouseIdentityError("GREENHOUSE_QUERY_INVALID") from exc
    if len(pairs) > 4:
        raise GreenhouseIdentityError("GREENHOUSE_QUERY_INVALID")
    values: dict[str, str] = {}
    for raw_key, raw_value in pairs:
        key = raw_key.casefold()
        value = raw_value.strip()
        if (
            key not in {"for", "gh_jid", "gh_src", "token"}
            or key in values
            or not value
            or len(value) > 160
        ):
            raise GreenhouseIdentityError("GREENHOUSE_QUERY_INVALID")
        values[key] = value
    source = values.get("gh_src")
    if source is not None and _IDENTITY_TOKEN.fullmatch(source) is None:
        raise GreenhouseIdentityError("GREENHOUSE_QUERY_INVALID")
    return values


def _candidate_route(
    path: str, query: str
) -> tuple[
    GreenhouseApplicationIdentity,
    GreenhouseCandidateRoute,
]:
    raw_path = path or "/"
    low_path = raw_path.casefold()
    if (
        "\\" in raw_path
        or "%" in raw_path
        or "//" in raw_path
        or any(segment in {".", ".."} for segment in raw_path.split("/"))
    ):
        raise GreenhouseIdentityError("GREENHOUSE_PATH_INVALID")
    values = _bounded_query(query)

    hosted = _HOSTED_PATH.fullmatch(raw_path)
    if hosted is not None:
        if set(values).difference({"gh_jid", "gh_src"}):
            raise GreenhouseIdentityError("GREENHOUSE_QUERY_INVALID")
        board_token, job_token = hosted.groups()
        query_job_id = values.get("gh_jid")
        if query_job_id is not None and (
            _JOB_TOKEN.fullmatch(query_job_id) is None or query_job_id != job_token
        ):
            raise GreenhouseIdentityError("GREENHOUSE_JOB_ID_MISMATCH")
        return (
            GreenhouseApplicationIdentity(
                board_token=board_token.casefold(),
                job_token=job_token,
            ),
            GreenhouseCandidateRoute.HOSTED,
        )

    if low_path.rstrip("/") == "/embed/job_app":
        if set(values) not in (
            {"for", "token"},
            {"for", "gh_src", "token"},
        ):
            raise GreenhouseIdentityError("GREENHOUSE_QUERY_INVALID")
        if (
            _IDENTITY_TOKEN.fullmatch(values["for"]) is None
            or _JOB_TOKEN.fullmatch(values["token"]) is None
        ):
            raise GreenhouseIdentityError("GREENHOUSE_IDENTITY_INVALID")
        return (
            GreenhouseApplicationIdentity(
                board_token=values["for"].casefold(),
                job_token=values["token"],
            ),
            GreenhouseCandidateRoute.EMBEDDED,
        )

    segments = tuple(segment for segment in raw_path.split("/") if segment)
    if (
        len(segments) == 1
        and _IDENTITY_TOKEN.fullmatch(segments[0]) is not None
        and set(values) in ({"gh_jid"}, {"gh_jid", "gh_src"})
        and _JOB_TOKEN.fullmatch(values["gh_jid"]) is not None
    ):
        return (
            GreenhouseApplicationIdentity(
                board_token=segments[0].casefold(),
                job_token=values["gh_jid"],
            ),
            GreenhouseCandidateRoute.JOB_ID,
        )
    raise GreenhouseIdentityError("GREENHOUSE_PATH_INVALID")


def parse_greenhouse_candidate_url(
    url: str,
    *,
    expected_hostname: str | None = None,
    expected_identity: GreenhouseApplicationIdentity | None = None,
) -> GreenhouseCandidateUrl:
    """Parse one exact public candidate URL and optionally enforce prior bindings."""

    candidate = (url or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise GreenhouseIdentityError("GREENHOUSE_URL_INVALID") from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or hostname != hostname.rstrip(".")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
        or any(ord(character) > 127 for character in hostname)
        or hostname not in _ALLOWED_CANDIDATE_HOSTS
        or (expected_hostname is not None and hostname != expected_hostname)
    ):
        raise GreenhouseIdentityError("GREENHOUSE_ORIGIN_INVALID")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        raise GreenhouseIdentityError("GREENHOUSE_ORIGIN_INVALID")

    identity, route = _candidate_route(parsed.path, parsed.query)
    if expected_identity is not None and identity != expected_identity:
        raise GreenhouseIdentityError("GREENHOUSE_APPLICATION_IDENTITY_CHANGED")
    return GreenhouseCandidateUrl(
        hostname=hostname,
        identity=identity,
        route=route,
    )


def is_greenhouse_candidate_url(url: str) -> bool:
    """Return whether a URL satisfies the complete candidate identity contract."""

    try:
        parse_greenhouse_candidate_url(url)
    except GreenhouseIdentityError:
        return False
    return True
