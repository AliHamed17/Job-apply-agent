"""Offline discovery coverage for the canonical Greenhouse candidate contract."""

from __future__ import annotations

import json

import pytest

from jobs.extractor import extract_jobs
from jobs.parsers.greenhouse import (
    greenhouse_identity,
    greenhouse_job_id,
    is_greenhouse_page,
    parse_greenhouse,
)
from submitters.greenhouse_identity import GreenhouseCandidateRoute


def _jsonld_job(*, title: str, apply_url: str) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": "Structured role description.",
        "hiringOrganization": {"@type": "Organization", "name": "Example Labs"},
        "jobLocation": "Remote",
        "url": apply_url,
    }
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


@pytest.mark.parametrize(
    ("url", "expected_board", "expected_job", "expected_route"),
    [
        (
            "https://boards.greenhouse.io/example/jobs/12345",
            "example",
            "12345",
            GreenhouseCandidateRoute.HOSTED,
        ),
        (
            "https://job-boards.greenhouse.io/Example/jobs/23456?gh_jid=23456",
            "example",
            "23456",
            GreenhouseCandidateRoute.HOSTED,
        ),
        (
            "https://greenhouse-hosted.com/example/jobs/34567",
            "example",
            "34567",
            GreenhouseCandidateRoute.HOSTED,
        ),
        (
            "https://boards.greenhouse.io/example?gh_jid=45678&gh_src=fixture",
            "example",
            "45678",
            GreenhouseCandidateRoute.JOB_ID,
        ),
        (
            "https://job-boards.greenhouse.io/embed/job_app?for=example&token=56789&gh_src=fixture",
            "example",
            "56789",
            GreenhouseCandidateRoute.EMBEDDED,
        ),
    ],
)
def test_candidate_variants_delegate_to_canonical_identity(
    url: str,
    expected_board: str,
    expected_job: str,
    expected_route: GreenhouseCandidateRoute,
) -> None:
    candidate = greenhouse_identity(url)

    assert candidate is not None
    assert candidate.identity.board_token == expected_board
    assert candidate.identity.job_token == expected_job
    assert candidate.route is expected_route
    assert greenhouse_job_id(url) == expected_job


def test_hosted_single_job_uses_the_canonical_source_not_an_external_link() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    html = """
    <html>
      <head><meta property="og:site_name" content="Example Labs"></head>
      <body>
        <h1 class="app-title">Senior Platform Engineer</h1>
        <div class="location">Remote</div>
        <div id="content">Build reliable systems.</div>
        <a href="https://untrusted.example/apply">Apply</a>
      </body>
    </html>
    """

    jobs = parse_greenhouse(html, source)

    assert len(jobs) == 1
    assert jobs[0].title == "Senior Platform Engineer"
    assert jobs[0].company == "Example Labs"
    assert jobs[0].location == "Remote"
    assert jobs[0].seniority == "senior"
    assert jobs[0].description == "Build reliable systems."
    assert jobs[0].apply_url == source


def test_mismatched_jsonld_cannot_replace_a_canonical_greenhouse_job() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    html = f"""
    <html>
      <head>
        {
        _jsonld_job(
            title="Wrong structured role",
            apply_url="https://boards.greenhouse.io/other/jobs/99999",
        )
    }
      </head>
      <body>
        <h1 class="app-title">Reviewed Platform Engineer</h1>
        <div id="content">Canonical candidate content.</div>
      </body>
    </html>
    """

    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert len(result.jobs) == 1
    assert result.jobs[0].title == "Reviewed Platform Engineer"
    assert result.jobs[0].apply_url == source
    assert result.jobs[0].source_url == source


def test_mismatched_jsonld_cannot_replace_a_markup_free_canonical_job() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    html = _jsonld_job(
        title="Wrong structured role",
        apply_url="https://boards.greenhouse.io/other/jobs/99999",
    )

    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert result.page_type == "no_jobs"
    assert result.jobs == []


def test_same_identity_jsonld_keeps_metadata_but_not_an_alternate_target_url() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    alternate_route = "https://job-boards.greenhouse.io/example?gh_jid=12345"
    html = _jsonld_job(
        title="Structured Platform Engineer",
        apply_url=alternate_route,
    )

    result = extract_jobs(html, source)

    assert result.parser_used == "jsonld"
    assert len(result.jobs) == 1
    assert result.jobs[0].title == "Structured Platform Engineer"
    assert result.jobs[0].description == "Structured role description."
    assert result.jobs[0].apply_url == source
    assert result.jobs[0].source_url == source


def test_canonical_greenhouse_jsonld_filters_conflicting_postings() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    matching = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Matching role",
        "description": "Matching structured description.",
        "hiringOrganization": {"name": "Example Labs"},
        "jobLocation": "Remote",
        "url": source,
    }
    conflicting = {
        **matching,
        "title": "Conflicting role",
        "url": "https://boards.greenhouse.io/example/jobs/54321",
    }
    html = f'<script type="application/ld+json">{json.dumps([conflicting, matching])}</script>'

    result = extract_jobs(html, source)

    assert result.parser_used == "jsonld"
    assert [job.title for job in result.jobs] == ["Matching role"]
    assert [job.apply_url for job in result.jobs] == [source]


def test_canonical_greenhouse_rejects_jsonld_without_explicit_target() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    matching = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Matching role",
        "description": "Matching structured description.",
        "hiringOrganization": {"name": "Example Labs"},
        "jobLocation": "Remote",
        "url": source,
    }
    missing_target = {
        **matching,
        "title": "Wrong job without URL",
        "hiringOrganization": {"name": "Wrong Employer"},
    }
    missing_target.pop("url")
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
          {json.dumps([missing_target, matching])}
        </script>
      </head>
      <body>
        <h1 class="app-title">Canonical fallback title</h1>
        <div id="content">Canonical candidate content.</div>
      </body>
    </html>
    """

    result = extract_jobs(html, source)

    assert result.parser_used == "jsonld"
    assert [job.title for job in result.jobs] == ["Matching role"]
    assert [job.company for job in result.jobs] == ["Example Labs"]
    assert [job.apply_url for job in result.jobs] == [source]


def test_canonical_greenhouse_rejects_ambiguous_same_identity_metadata() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "description": "Structured description.",
        "hiringOrganization": {"name": "Example Labs"},
        "jobLocation": "Remote",
        "url": source,
    }
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
          {json.dumps([{**posting, "title": "First claim"}, {**posting, "title": "Second claim"}])}
        </script>
      </head>
      <body>
        <h1 class="app-title">Canonical fallback title</h1>
        <div id="content">Canonical candidate content.</div>
      </body>
    </html>
    """

    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert [job.title for job in result.jobs] == ["Canonical fallback title"]
    assert [job.apply_url for job in result.jobs] == [source]


def test_embedded_greenhouse_identity_rejects_mismatched_jsonld_target() -> None:
    source = "https://careers.example.test/platform-engineer"
    embed = "https://job-boards.greenhouse.io/embed/job_app?for=example&token=12345"
    html = f"""
    <html>
      <head>
        {
        _jsonld_job(
            title="Wrong structured role",
            apply_url="https://boards.greenhouse.io/other/jobs/99999",
        )
    }
      </head>
      <body>
        <h1>Reviewed embedded role</h1>
        <div id="content">Canonical embedded content.</div>
        <iframe src="{embed}"></iframe>
      </body>
    </html>
    """

    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert [job.title for job in result.jobs] == ["Reviewed embedded role"]
    assert [job.apply_url for job in result.jobs] == [embed]
    assert [job.source_url for job in result.jobs] == [source]


def test_greenhouse_listing_rejects_other_board_jsonld_target() -> None:
    source = "https://boards.greenhouse.io/example"
    html = f"""
    <html>
      <head>
        {
        _jsonld_job(
            title="Wrong structured role",
            apply_url="https://boards.greenhouse.io/other/jobs/99999",
        )
    }
      </head>
      <body>
        <div class="opening">
          <a href="/example/jobs/11111">Trusted listing role</a>
          <span class="location">Remote</span>
        </div>
      </body>
    </html>
    """

    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert [job.title for job in result.jobs] == ["Trusted listing role"]
    assert [job.apply_url for job in result.jobs] == [
        "https://boards.greenhouse.io/example/jobs/11111"
    ]


def test_non_greenhouse_jsonld_behavior_is_unchanged() -> None:
    source = "https://careers.example.test/jobs/platform-engineer"
    apply_url = "https://apply.example.test/jobs/platform-engineer"
    html = _jsonld_job(title="Generic Platform Engineer", apply_url=apply_url)

    result = extract_jobs(html, source)

    assert result.parser_used == "jsonld"
    assert len(result.jobs) == 1
    assert result.jobs[0].apply_url == apply_url
    assert result.jobs[0].source_url == source


def test_same_origin_board_listing_emits_only_canonical_candidate_urls() -> None:
    source = "https://job-boards.greenhouse.io/example"
    html = """
    <html>
      <head><meta property="og:site_name" content="Example Labs"></head>
      <body>
        <div class="opening">
          <a href="/example/jobs/11111">Data Engineer</a>
          <span class="location">Haifa</span>
        </div>
      </body>
    </html>
    """

    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert result.page_type == "single_job"
    assert len(result.jobs) == 1
    assert result.jobs[0].location == "Haifa"
    assert result.jobs[0].apply_url == ("https://job-boards.greenhouse.io/example/jobs/11111")
    assert greenhouse_identity(result.jobs[0].apply_url) is not None


def test_listing_rejects_other_origins_and_boards_then_deduplicates() -> None:
    source = "https://boards.greenhouse.io/example"
    html = """
    <div class="opening">
      <a href="/example/jobs/11111">Data Engineer</a>
      <span class="location">North</span>
    </div>
    <div class="opening">
      <a href="https://boards.greenhouse.io/example/jobs/22222">QA Engineer</a>
      <span class="location">South</span>
    </div>
    <div class="opening">
      <a href="/example/jobs/11111">Duplicate Data Engineer</a>
      <span class="location">Wrong</span>
    </div>
    <div class="opening">
      <a href="https://job-boards.greenhouse.io/example/jobs/33333">
        Other Origin Engineer
      </a>
    </div>
    <div class="opening">
      <a href="/other/jobs/44444">Other Board Engineer</a>
    </div>
    <div class="opening">
      <a href="https://untrusted.example/jobs/55555">Untrusted Engineer</a>
    </div>
    """

    jobs = parse_greenhouse(html, source)

    assert [(job.title, job.location) for job in jobs] == [
        ("Data Engineer", "North"),
        ("QA Engineer", "South"),
    ]


def test_listing_deduplicates_alternate_routes_by_application_identity() -> None:
    source = "https://boards.greenhouse.io/example"
    html = """
    <div class="opening">
      <a href="/example/jobs/11111">Canonical hosted route</a>
      <span class="location">North</span>
    </div>
    <div class="opening">
      <a href="/example?gh_jid=11111">Same job through job-ID route</a>
      <span class="location">Wrong duplicate</span>
    </div>
    """

    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert len(result.jobs) == 1
    assert result.jobs[0].title == "Canonical hosted route"
    assert result.jobs[0].location == "North"
    assert result.jobs[0].apply_url == "https://boards.greenhouse.io/example/jobs/11111"


@pytest.mark.parametrize("element", ["iframe", "form"])
def test_external_page_dispatches_only_through_canonical_embedded_candidate(
    element: str,
) -> None:
    source = "https://careers.example.test/platform-role?untrusted=source-value"
    attribute = "src" if element == "iframe" else "action"
    embedded = "https://job-boards.greenhouse.io/embed/job_app?for=example&token=34567"
    html = f"""
    <html>
      <body>
        <h1 data-qa="job-title">Platform Engineer</h1>
        <div data-qa="location">Tel Aviv</div>
        <div data-qa="job-description">Operate the platform.</div>
        <{element} {attribute}="{embedded}"></{element}>
      </body>
    </html>
    """

    assert is_greenhouse_page(html, source) is True
    result = extract_jobs(html, source)

    assert result.parser_used == "greenhouse"
    assert len(result.jobs) == 1
    assert result.jobs[0].apply_url == embedded
    candidate = greenhouse_identity(result.jobs[0].apply_url, embedded=True)
    assert candidate is not None
    assert candidate.route is GreenhouseCandidateRoute.EMBEDDED


@pytest.mark.parametrize(
    "html",
    [
        """
        <script
          src="https://job-boards.greenhouse.io/embed/job_board/js?for=example">
        </script>
        <div class="opening">
          <a href="https://job-boards.greenhouse.io/example/jobs/56789">
            Infrastructure Engineer
          </a>
        </div>
        """,
        """
        <h1>Platform Engineer</h1>
        <a href="https://job-boards.greenhouse.io/example/jobs/56789">Apply</a>
        """,
        """
        <h1>Platform Engineer</h1>
        <iframe
          src="//job-boards.greenhouse.io/embed/job_app?for=example&token=56789">
        </iframe>
        """,
        """
        <h1>Platform Engineer</h1>
        <iframe
          src="https://job-boards.greenhouse.io/example/jobs/56789">
        </iframe>
        """,
    ],
)
def test_external_script_anchor_relative_or_hosted_evidence_cannot_dispatch(
    html: str,
) -> None:
    source = "https://careers.example.test/openings"

    assert is_greenhouse_page(html, source) is False
    assert parse_greenhouse(html, source) == []


def test_exact_candidate_source_wins_over_unrelated_openings() -> None:
    source = "https://boards.greenhouse.io/example/jobs/12345"
    html = """
    <h1>Primary Engineer</h1>
    <div class="opening">
      <a href="/example/jobs/99999">Related Engineer</a>
      <span class="location">Elsewhere</span>
    </div>
    """

    jobs = parse_greenhouse(html, source)

    assert len(jobs) == 1
    assert jobs[0].title == "Primary Engineer"
    assert jobs[0].apply_url == source


@pytest.mark.parametrize(
    ("source", "html"),
    [
        (
            "https://untrusted.example/?next=boards.greenhouse.io/example/jobs/12345",
            "<h1>Safety Engineer</h1>",
        ),
        (
            "https://boards.greenhouse.io.untrusted.example/example/jobs/12345",
            "<h1>Safety Engineer</h1>",
        ),
        (
            "https://careers.example.test/role?gh_jid=12345",
            "<h1>Safety Engineer</h1>",
        ),
        (
            "https://careers.example.test/role",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io.untrusted.example/'
                'embed/job_app?for=example&token=12345"></iframe>'
            ),
        ),
        (
            "https://[",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io/embed/job_app'
                '?for=example&token=12345"></iframe>'
            ),
        ),
        (
            "file:///C:/fixtures/greenhouse.html",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io/embed/job_app'
                '?for=example&token=12345"></iframe>'
            ),
        ),
        (
            "https://user@careers.example.test/role",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io/embed/job_app'
                '?for=example&token=12345"></iframe>'
            ),
        ),
        (
            "https://status.greenhouse.io/incidents",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io/embed/job_app'
                '?for=example&token=12345"></iframe>'
            ),
        ),
        (
            "https://api.greenhouse.io/v1",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io/embed/job_app'
                '?for=example&token=12345"></iframe>'
            ),
        ),
        (
            "https://harvest.greenhouse.io/v1",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io/embed/job_app'
                '?for=example&token=12345"></iframe>'
            ),
        ),
        (
            "https://boards.greenhouse.io.untrusted.example/role",
            (
                "<h1>Safety Engineer</h1>"
                '<iframe src="https://job-boards.greenhouse.io/embed/job_app'
                '?for=example&token=12345"></iframe>'
            ),
        ),
    ],
)
def test_spoofed_unbound_or_control_plane_sources_never_select_greenhouse(
    source: str,
    html: str,
) -> None:
    assert is_greenhouse_page(html, source) is False

    result = extract_jobs(html, source)

    assert result.parser_used != "greenhouse"


def test_conflicting_embedded_application_bindings_fail_closed() -> None:
    source = "https://careers.example.test/openings"
    html = """
    <h1>Safety Engineer</h1>
    <iframe
      src="https://job-boards.greenhouse.io/embed/job_app?for=example&amp;token=12345">
    </iframe>
    <form
      action="https://job-boards.greenhouse.io/embed/job_app?for=example&amp;token=99999">
    </form>
    """

    assert is_greenhouse_page(html, source) is False
    assert parse_greenhouse(html, source) == []


def test_duplicate_elements_for_one_canonical_embed_are_safe_to_deduplicate() -> None:
    source = "https://careers.example.test/openings"
    embedded = "https://job-boards.greenhouse.io/embed/job_app?for=example&token=12345"
    html = f"""
    <h1>Safety Engineer</h1>
    <iframe src="{embedded}"></iframe>
    <form action="{embedded}"></form>
    """

    jobs = parse_greenhouse(html, source)

    assert len(jobs) == 1
    assert jobs[0].apply_url == embedded


def test_official_board_path_cannot_bind_another_boards_embedded_job() -> None:
    source = "https://boards.greenhouse.io/example"
    html = """
    <h1>Safety Engineer</h1>
    <form
      action="https://job-boards.greenhouse.io/embed/job_app?for=other&amp;token=12345">
    </form>
    """

    assert is_greenhouse_page(html, source) is False
    assert parse_greenhouse(html, source) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://boards.greenhouse.io/example/jobs/12345",
        "https://user@boards.greenhouse.io/example/jobs/12345",
        "https://boards.greenhouse.io:444/example/jobs/12345",
        "https://boards.greenhouse.io/example/jobs/12345#unreviewed",
        "https://tenant.greenhouse-hosted.com/example/jobs/12345",
        "https://status.greenhouse.io/example/jobs/12345",
        "https://api.greenhouse.io/example/jobs/12345",
        "https://harvest.greenhouse.io/example/jobs/12345",
        "https://boards.greenhouse.io/jobs/12345",
        "https://boards.greenhouse.io/example",
        "https://boards.greenhouse.io/example/jobs/not-a-job-id",
        "https://boards.greenhouse.io/example/jobs/12345-platform-engineer",
        "https://boards.greenhouse.io/example/jobs/12345?gh_jid=99999",
        "https://boards.greenhouse.io/example/jobs/12345?gh_jid=12345&gh_jid=12345",
        "https://boards.greenhouse.io/example/jobs/12345?unknown=value",
        "https://boards.greenhouse.io/example/jobs%2f12345",
        "https://boards.greenhouse.io.untrusted.example/example/jobs/12345",
        "//boards.greenhouse.io/example/jobs/12345",
    ],
)
def test_noncanonical_candidate_shapes_are_rejected(url: str) -> None:
    assert greenhouse_identity(url) is None
    assert greenhouse_job_id(url) is None
