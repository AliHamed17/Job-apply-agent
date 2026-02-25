"""Security tests for the HTTP fetcher."""

from types import SimpleNamespace

import jobs.fetcher as fetcher


def test_blocks_localhost_url():
    result = fetcher.fetch_page("http://localhost:8000/admin")
    assert result.blocked is True
    assert "not allowed" in result.error.lower()


def test_blocks_private_ip_url():
    result = fetcher.fetch_page("http://10.0.0.12/internal")
    assert result.blocked is True
    assert "private or local" in result.error.lower()


def test_blocks_non_http_scheme():
    result = fetcher.fetch_page("file:///etc/passwd")
    assert result.blocked is True
    assert "http/https" in result.error.lower()


def test_allows_public_url_and_fetches(monkeypatch):
    monkeypatch.setattr(fetcher, "_check_robots_txt", lambda _url: True)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        fetcher,
        "_do_fetch",
        lambda _url: SimpleNamespace(status_code=200, text="<html><title>ok</title></html>"),
    )

    result = fetcher.fetch_page("https://example.com/careers")
    assert result.success is True
    assert result.status_code == 200
