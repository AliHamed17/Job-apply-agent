"""Tests for LinkedIn search URL builder."""

from urllib.parse import parse_qs, urlparse

from profile.models import UserProfile
from discovery.query_builder import build_search_urls


def _p():
    p = UserProfile()
    p.preferences.roles = ["RF Engineer"]
    p.preferences.locations = ["Dubai"]
    return p


def test_builds_easy_apply_urls_with_pagination():
    urls = build_search_urls(_p(), pages_per_query=2)
    assert len(urls) == 2
    q = parse_qs(urlparse(urls[0]).query)
    assert q["f_AL"] == ["true"]
    assert "RF Engineer" in q["keywords"][0]
    assert q["location"] == ["Dubai"]
    starts = sorted(int(parse_qs(urlparse(u).query)["start"][0]) for u in urls)
    assert starts == [0, 25]


def test_total_url_cap():
    p = UserProfile()
    p.preferences.roles = [f"role{i}" for i in range(20)]
    p.preferences.locations = [f"loc{j}" for j in range(20)]
    assert len(build_search_urls(p, pages_per_query=3)) <= 30
