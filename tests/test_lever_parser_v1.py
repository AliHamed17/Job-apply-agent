"""Strict Lever parser routing and identity tests."""

from __future__ import annotations

from jobs.extractor import extract_jobs
from jobs.parsers.lever import parse_lever

POSTING_A = "11111111-2222-4333-8444-555555555555"
POSTING_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_single_posting_canonicalizes_source_and_apply_routes() -> None:
    source = f"https://jobs.lever.co/sample-company/{POSTING_A}/apply/"
    html = """
    <html><body>
      <h2 class="posting-headline">Senior Fixture Engineer</h2>
      <div class="posting-categories"><span class="location">Haifa</span></div>
      <div class="posting-page"><section class="section-wrapper">Build safe systems.</section></div>
    </body></html>
    """

    jobs = parse_lever(html, source)

    assert len(jobs) == 1
    assert jobs[0].source_url == f"https://jobs.lever.co/sample-company/{POSTING_A}"
    assert jobs[0].apply_url == f"https://jobs.lever.co/sample-company/{POSTING_A}/apply"
    assert jobs[0].company == "Sample Company"


def test_listing_accepts_only_exact_same_host_and_site_postings() -> None:
    source = "https://jobs.lever.co/sample-company"
    html = f"""
    <html><body>
      <div class="posting"><a class="posting-title"
        href="https://jobs.lever.co/sample-company/{POSTING_A}"><h5>Role A</h5></a></div>
      <div class="posting"><a class="posting-title"
        href="https://jobs.lever.co/sample-company/{POSTING_B}/apply"><h5>Role B</h5></a></div>
      <div class="posting"><a class="posting-title"
        href="https://jobs.eu.lever.co/sample-company/{POSTING_A}"><h5>Wrong region</h5></a></div>
      <div class="posting"><a class="posting-title"
        href="https://jobs.lever.co/other-company/{POSTING_A}"><h5>Wrong site</h5></a></div>
      <div class="posting"><a class="posting-title"
        href="https://jobs.lever.co.evil.test/sample-company/{POSTING_A}"><h5>Evil</h5></a></div>
    </body></html>
    """

    jobs = parse_lever(html, source)

    assert [job.title for job in jobs] == ["Role A", "Role B"]
    assert all(job.company == "Sample Company" for job in jobs)
    assert all(job.apply_url.endswith("/apply") for job in jobs)


def test_extractor_never_routes_suffix_or_subdomain_laundering_to_lever() -> None:
    html = "<html><body><h2 class='posting-headline'>Fixture Engineer</h2></body></html>"

    evil = extract_jobs(
        html,
        f"https://jobs.lever.co.evil.test/sample-company/{POSTING_A}",
    )
    subdomain = extract_jobs(
        html,
        f"https://evil.jobs.lever.co/sample-company/{POSTING_A}",
    )

    assert evil.parser_used != "lever"
    assert subdomain.parser_used != "lever"
