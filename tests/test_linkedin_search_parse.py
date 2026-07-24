"""Tests for the pure LinkedIn search-results parser (discovery/linkedin_search.py).

run_discovery (the browser-driven half of that module) is deliberately not
unit-tested here — see the task brief for Task 4.3.
"""

from pathlib import Path

from discovery.linkedin_search import parse_search_results

HTML = (Path(__file__).parent / "fixtures" / "linkedin_search_results.html").read_text(encoding="utf-8")


def test_parse_extracts_jobs():
    jobs = parse_search_results(HTML)
    assert len(jobs) >= 3
    j = jobs[0]
    assert j.title and j.company
    assert "linkedin.com/jobs/view/" in j.apply_url


def test_parse_skips_cards_without_a_title():
    jobs = parse_search_results(HTML)
    titles = [j.title for j in jobs]
    assert "Ghost Corp" not in titles
    assert len(jobs) == 3


def test_parse_sets_location_and_source_url_matches_apply_url():
    jobs = parse_search_results(HTML)
    j = next(j for j in jobs if j.title == "RAN Systems Engineer")
    assert j.company == "Parallel Wireless"
    assert j.location == "Remote"
    assert j.source_url == j.apply_url
    assert j.apply_url == "https://www.linkedin.com/jobs/view/4123456782"
    assert j.keywords == []


def test_parse_empty_html_returns_empty_list():
    assert parse_search_results("") == []
    assert parse_search_results("<html><body>no jobs here</body></html>") == []
