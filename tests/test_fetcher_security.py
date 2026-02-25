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
    monkeypatch.setattr(fetcher, "_is_url_fetch_allowed", lambda _url: (True, ""))
    monkeypatch.setattr(fetcher, "_check_robots_txt", lambda _url: True)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        fetcher,
        "_do_fetch",
        lambda _url: SimpleNamespace(
            status_code=200,
            text="<html><title>ok</title></html>",
            url="https://example.com/careers",
        ),
    )

    result = fetcher.fetch_page("https://example.com/careers")
    assert result.success is True
    assert result.status_code == 200


def test_detects_google_login_auth_requirement(monkeypatch):
    monkeypatch.setattr(fetcher, "_is_url_fetch_allowed", lambda _url: (True, ""))
    monkeypatch.setattr(fetcher, "_check_robots_txt", lambda _url: True)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    class _Resp:
        status_code = 200
        url = "https://accounts.google.com/signin/v2/identifier"
        text = "Sign in with Google account"

    monkeypatch.setattr(fetcher, "_do_fetch", lambda _url: _Resp())

    result = fetcher.fetch_page("https://example.com/job/123")
    assert result.blocked is True
    assert result.auth_required is True
    assert "auth" in result.error.lower() or "login" in result.error.lower()
