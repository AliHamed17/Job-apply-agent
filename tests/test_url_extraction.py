"""Tests for URL extraction, normalization, and hashing."""


from ingestion.url_utils import (
    is_short_url,
    job_signature,
    normalize_url,
    url_hash,
)


class TestExtractUrls:
    """Tests for extract_urls from webhook module."""

    def test_extract_single_url(self):
        from api.routes.webhook import extract_urls
        result = extract_urls("Check out this job: https://boards.greenhouse.io/company/jobs/12345")
        assert result == ["https://boards.greenhouse.io/company/jobs/12345"]

    def test_extract_multiple_urls(self):
        from api.routes.webhook import extract_urls
        text = "Two jobs: https://example.com/job1 and https://example.com/job2"
        result = extract_urls(text)
        assert len(result) == 2
        assert "https://example.com/job1" in result
        assert "https://example.com/job2" in result

    def test_extract_no_urls(self):
        from api.routes.webhook import extract_urls
        assert extract_urls("No links here, just text") == []

    def test_extract_empty_string(self):
        from api.routes.webhook import extract_urls
        assert extract_urls("") == []

    def test_extract_none(self):
        from api.routes.webhook import extract_urls
        assert extract_urls(None) == []

    def test_extract_url_with_trailing_punctuation(self):
        from api.routes.webhook import extract_urls
        result = extract_urls("Apply here: https://example.com/job.")
        assert result == ["https://example.com/job"]

    def test_extract_url_with_parentheses(self):
        from api.routes.webhook import extract_urls
        result = extract_urls("(https://example.com/job)")
        assert result == ["https://example.com/job"]

    def test_dedup_same_url(self):
        from api.routes.webhook import extract_urls
        text = "https://example.com/job https://example.com/job"
        result = extract_urls(text)
        assert len(result) == 1

    def test_extract_url_with_query_params(self):
        from api.routes.webhook import extract_urls
        text = "https://example.com/job?id=123&role=dev"
        result = extract_urls(text)
        assert "https://example.com/job?id=123&role=dev" in result

    def test_extract_url_shorteners(self):
        from api.routes.webhook import extract_urls
        text = "Check out https://bit.ly/abc123 for the role"
        result = extract_urls(text)
        assert result == ["https://bit.ly/abc123"]


class TestNormalizeUrl:
    """Tests for URL normalization."""

    def test_lowercase_host(self):
        result = normalize_url("https://EXAMPLE.COM/Job")
        assert "example.com" in result

    def test_strip_tracking_params(self):
        result = normalize_url("https://example.com/job?utm_source=whatsapp&id=123")
        assert "utm_source" not in result
        assert "id=123" in result

    def test_strip_multiple_tracking_params(self):
        result = normalize_url(
            "https://example.com/job?utm_source=wa&utm_medium=social&fbclid=abc&role=dev"
        )
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "fbclid" not in result
        assert "role=dev" in result

    def test_strip_fragment(self):
        result = normalize_url("https://example.com/job#apply-section")
        assert "#" not in result

    def test_strip_trailing_slash(self):
        result = normalize_url("https://example.com/job/")
        assert result.endswith("/job")

    def test_keep_root_slash(self):
        result = normalize_url("https://example.com/")
        assert result == "https://example.com/"

    def test_preserve_meaningful_params(self):
        result = normalize_url("https://example.com/search?q=python+developer&page=2")
        assert "q=" in result
        assert "page=" in result

    def test_handles_malformed_url(self):
        # Should not crash
        result = normalize_url("not-a-url")
        assert result == "not-a-url"


class TestUrlHash:
    """Tests for URL hashing."""

    def test_deterministic(self):
        h1 = url_hash("https://example.com/job")
        h2 = url_hash("https://example.com/job")
        assert h1 == h2

    def test_different_urls(self):
        h1 = url_hash("https://example.com/job1")
        h2 = url_hash("https://example.com/job2")
        assert h1 != h2

    def test_hash_length(self):
        h = url_hash("https://example.com/job")
        assert len(h) == 64  # SHA-256


class TestShortUrl:
    """Tests for short URL detection."""

    def test_known_shorteners(self):
        assert is_short_url("https://bit.ly/abc123") is True
        assert is_short_url("https://t.co/xyz") is True
        assert is_short_url("https://tinyurl.com/abc") is True

    def test_regular_url_not_short(self):
        assert is_short_url("https://greenhouse.io/jobs/123") is False
        assert is_short_url("https://example.com") is False


class TestJobSignature:
    """Tests for job dedup signature."""

    def test_same_job_same_sig(self):
        s1 = job_signature("Software Engineer", "TechCorp", "San Francisco, CA")
        s2 = job_signature("Software Engineer", "TechCorp", "San Francisco, CA")
        assert s1 == s2

    def test_case_insensitive(self):
        s1 = job_signature("Software Engineer", "TechCorp", "San Francisco")
        s2 = job_signature("software engineer", "techcorp", "san francisco")
        assert s1 == s2

    def test_different_jobs(self):
        s1 = job_signature("Software Engineer", "TechCorp", "San Francisco")
        s2 = job_signature("Data Scientist", "TechCorp", "San Francisco")
        assert s1 != s2

    def test_whitespace_handling(self):
        s1 = job_signature("  Software Engineer  ", "TechCorp", "SF")
        s2 = job_signature("Software Engineer", "TechCorp", "SF")
        assert s1 == s2
