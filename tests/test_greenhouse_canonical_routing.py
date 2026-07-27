"""Cross-layer Greenhouse discovery and execution-routing identity contract."""

from __future__ import annotations

import pytest

from jobs.parsers.greenhouse import is_greenhouse_page, parse_greenhouse
from submitters.greenhouse_identity import (
    GreenhouseCandidateRoute,
    is_greenhouse_candidate_url,
    parse_greenhouse_candidate_url,
)
from submitters.platforms import adapter_for_url, detect_platform


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme/jobs/123",
        "https://job-boards.greenhouse.io/acme/jobs/456?gh_jid=456",
        "https://greenhouse-hosted.com/acme/jobs/789",
        "https://boards.greenhouse.io/acme?gh_jid=123&gh_src=fixture",
        "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=123",
    ],
)
def test_every_dispatched_candidate_is_accepted_by_discovery_and_execution(
    url: str,
) -> None:
    assert is_greenhouse_candidate_url(url) is True
    assert is_greenhouse_page("<h1>Safety Engineer</h1>", url) is True
    assert detect_platform(url) == "greenhouse"

    descriptor = adapter_for_url(url)
    assert descriptor is not None
    assert descriptor.platform == "greenhouse"

    jobs = parse_greenhouse("<h1>Safety Engineer</h1>", url)
    assert len(jobs) == 1
    assert jobs[0].apply_url == url
    assert is_greenhouse_candidate_url(jobs[0].apply_url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://status.greenhouse.io/acme/jobs/123",
        "https://api.greenhouse.io/acme/jobs/123",
        "https://harvest.greenhouse.io/acme/jobs/123",
        "https://boards.greenhouse.io.evil.test/acme/jobs/123",
        "https://tenant.greenhouse-hosted.com/acme/jobs/123",
        "https://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/jobs/123",
        "https://boards.greenhouse.io/embed/job_board/js?for=acme",
        "https://boards.greenhouse.io/acme/jobs/not-numeric",
        "https://boards.greenhouse.io/acme/jobs/123-role",
        "https://boards.greenhouse.io/acme/jobs/123?unknown=value",
        "https://boards.greenhouse.io/acme/jobs%2f123",
    ],
)
def test_noncandidate_greenhouse_names_or_routes_never_reach_the_adapter(
    url: str,
) -> None:
    assert is_greenhouse_candidate_url(url) is False
    assert is_greenhouse_page("<h1>Safety Engineer</h1>", url) is False
    assert detect_platform(url) != "greenhouse"
    assert adapter_for_url(url) is None


def test_external_embed_dispatches_only_the_canonical_embedded_url() -> None:
    source = "https://careers.example.test/roles/safety"
    candidate_url = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=123"
    html = f"""
    <h1>Safety Engineer</h1>
    <iframe src="{candidate_url}"></iframe>
    """

    assert detect_platform(source) == "generic_portal"
    assert adapter_for_url(source) is None
    jobs = parse_greenhouse(html, source)

    assert len(jobs) == 1
    assert jobs[0].apply_url == candidate_url
    candidate = parse_greenhouse_candidate_url(jobs[0].apply_url)
    assert candidate.route is GreenhouseCandidateRoute.EMBEDDED
    descriptor = adapter_for_url(jobs[0].apply_url)
    assert descriptor is not None
    assert descriptor.platform == "greenhouse"


@pytest.mark.parametrize(
    "source",
    [
        "https://status.greenhouse.io/incidents",
        "https://api.greenhouse.io/v1",
        "https://harvest.greenhouse.io/v1",
        "https://tenant.greenhouse-hosted.com/jobs",
        "https://boards.greenhouse.io.untrusted.example/jobs",
    ],
)
def test_control_plane_or_unqualified_greenhouse_hosts_cannot_launder_an_embed(
    source: str,
) -> None:
    html = """
    <h1>Safety Engineer</h1>
    <iframe
      src="https://job-boards.greenhouse.io/embed/job_app?for=acme&amp;token=123">
    </iframe>
    """

    assert is_greenhouse_page(html, source) is False
    assert parse_greenhouse(html, source) == []


def test_external_script_or_candidate_anchor_is_discovery_only_not_dispatch() -> None:
    source = "https://careers.example.test/jobs"
    html = """
    <script src="https://job-boards.greenhouse.io/embed/job_board/js?for=acme"></script>
    <div class="opening">
      <a href="https://job-boards.greenhouse.io/acme/jobs/123">Safety Engineer</a>
    </div>
    """

    assert is_greenhouse_page(html, source) is False
    assert parse_greenhouse(html, source) == []
