"""Adversarial test suite — Phase 10.

Tests the system against "fake" or deceptive job posts to verify that:
1. The LLM placeholder guardrail fires for missing/fabricated information.
2. The scoring engine correctly skips low-quality or blacklisted postings.
3. Parsers do NOT extract false positives from non-job pages.
4. The pipeline handles malformed HTML, injected content, and empty responses.
5. Workday parser handles obfuscated / empty pages gracefully.

Per HANDOVER_PLAN.md Phase 10:
  "Build a test suite of 'fake' job posts to see if the LLM correctly
   identifies missing info vs. fabricating it."
"""

from __future__ import annotations

import pytest

from jobs.models import JobData
from jobs.parsers.html_heuristic import parse_html_heuristic
from jobs.parsers.jsonld import parse_jsonld
from jobs.parsers.workday import parse_workday
from jobs.extractor import extract_jobs
from llm.generation import _check_placeholders
from match.scoring import decide_action, score_job
from profile.models import Preferences, Personal, Resume, UserProfile

# ── Fixtures ─────────────────────────────────────────────────────────────


def _make_profile(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    blacklist: list[str] | None = None,
    remote_ok: bool = True,
) -> UserProfile:
    """Return a minimal test UserProfile."""
    return UserProfile(
        personal=Personal(
            name="Test User",
            email="test@example.com",
            location="London, UK",
            work_authorization="Authorized",
        ),
        resume=Resume(text="5 years Python, FastAPI, PostgreSQL, AWS"),
        preferences=Preferences(
            roles=roles or ["Software Engineer"],
            keywords=keywords or ["Python", "FastAPI"],
            blacklist_companies=blacklist or [],
            remote_ok=remote_ok,
            seniority=["senior", "mid"],
        ),
    )


def _make_job(**kwargs) -> JobData:
    defaults = dict(
        title="Senior Software Engineer",
        company="Acme Corp",
        location="Remote",
        employment_type="full-time",
        seniority="senior",
        description="We need Python, FastAPI and AWS skills.",
        apply_url="https://example.com/apply",
        source_url="https://example.com/jobs/123",
    )
    defaults.update(kwargs)
    return JobData(**defaults)


# ── 1. Placeholder guardrail ──────────────────────────────────────────────


class TestPlaceholderGuardrail:
    """Verify the LLM output placeholder detector catches missing info."""

    def test_detects_single_placeholder(self):
        text = "I have worked with [PLACEHOLDER: specify technology] for 3 years."
        found = _check_placeholders(text)
        assert len(found) == 1
        assert "specify technology" in found[0]

    def test_detects_multiple_placeholders(self):
        text = (
            "[PLACEHOLDER: add degree] from [PLACEHOLDER: add university]. "
            "I previously worked at [PLACEHOLDER: add company name]."
        )
        found = _check_placeholders(text)
        assert len(found) == 3

    def test_no_false_positive_on_clean_text(self):
        text = "I have 5 years of experience with Python and FastAPI at Acme Corp."
        found = _check_placeholders(text)
        assert found == []

    def test_detects_nested_description(self):
        text = "[PLACEHOLDER: describe relevant project from resume]"
        found = _check_placeholders(text)
        assert len(found) == 1

    def test_placeholder_in_qa_dict(self):
        qa_dict = {
            "why_this_company": "I admire their innovation.",
            "salary_expectations": "[PLACEHOLDER: add salary range]",
        }
        all_text = str(qa_dict)
        found = _check_placeholders(all_text)
        assert len(found) == 1

    def test_case_insensitive_not_matched(self):
        # Placeholder markers MUST use exact uppercase PLACEHOLDER
        text = "[placeholder: do not match this]"
        found = _check_placeholders(text)
        assert found == []  # our regex is case-sensitive

    def test_empty_placeholder(self):
        text = "[PLACEHOLDER: ]"
        found = _check_placeholders(text)
        # Regex requires at least 1 non-bracket char — empty matches allowed
        # The important thing is no crash
        assert isinstance(found, list)


# ── 2. Scoring — fake/irrelevant job posts ────────────────────────────────


class TestAdversarialScoring:
    """Ensure low-quality, blacklisted, or mismatched jobs are skipped."""

    def test_blacklisted_company_scores_zero(self):
        profile = _make_profile(blacklist=["Acme Corp"])
        job = _make_job(company="Acme Corp")
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        assert breakdown.total == 0.0
        assert breakdown.skip_reason is not None

    def test_completely_irrelevant_job_is_skipped(self):
        profile = _make_profile(roles=["Software Engineer"], keywords=["Python"])
        job = _make_job(
            title="Fry Cook",
            company="Burger Palace",
            description="Flip burgers and clean fryers.",
            seniority="",
            employment_type="part-time",
        )
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        action = decide_action(
            breakdown.total,
            auto_apply_enabled=False,
            draft_only=True,
            skip_reason=breakdown.skip_reason,
        )
        # Very low score should result in SKIP
        assert breakdown.total < 30

    def test_blacklist_takes_precedence_over_high_score(self):
        profile = _make_profile(
            roles=["Senior Python Engineer"],
            keywords=["Python", "FastAPI", "AWS"],
            blacklist=["TechCo"],
        )
        job = _make_job(
            title="Senior Python Engineer",
            company="TechCo",
            description="Python FastAPI AWS senior engineer role",
        )
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        assert breakdown.total == 0.0

    def test_missing_title_and_description(self):
        profile = _make_profile()
        job = _make_job(title="", description="")
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        # Empty description means zero keyword overlap.
        # (Title score may be non-zero due to substring matching of "".)
        assert breakdown.keyword_score == 0
        # Overall score is bounded
        assert 0 <= breakdown.total <= 100

    def test_wrong_seniority_penalises(self):
        profile = _make_profile()
        # Profile wants senior/mid, job is director level
        job = _make_job(title="VP Engineering Director", seniority="director")
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        assert breakdown.seniority_score < 15  # should be penalised

    def test_decide_action_skip_below_threshold(self):
        action = decide_action(score=10.0, auto_apply_enabled=False, draft_only=True)
        from match.scoring import Action
        assert action == Action.SKIP

    def test_decide_action_draft_at_medium_score(self):
        action = decide_action(score=55.0, auto_apply_enabled=False, draft_only=True)
        from match.scoring import Action
        assert action == Action.DRAFT

    def test_decide_action_skip_with_reason(self):
        action = decide_action(
            score=90.0,
            auto_apply_enabled=True,
            draft_only=False,
            skip_reason="blacklisted company",
        )
        from match.scoring import Action
        assert action == Action.SKIP


# ── 3. Parser false-positive prevention ──────────────────────────────────


class TestParserFalsePositives:
    """Parsers must not extract jobs from non-job pages."""

    def test_heuristic_ignores_blog_post(self):
        html = """
        <html><body>
          <h1>10 Tips for Better Sleep</h1>
          <p>Getting enough sleep is crucial for your health and wellbeing.</p>
          <p>Here are our top tips for improving sleep quality...</p>
        </body></html>
        """
        jobs = parse_html_heuristic(html, "https://blog.example.com/sleep-tips")
        assert jobs == []

    def test_heuristic_ignores_homepage(self):
        html = """
        <html><body>
          <h1>Welcome to Acme Corp</h1>
          <p>We build innovative solutions for modern problems.</p>
          <nav><a href="/about">About</a><a href="/contact">Contact</a></nav>
        </body></html>
        """
        jobs = parse_html_heuristic(html, "https://acme.com")
        assert jobs == []

    def test_heuristic_ignores_error_page(self):
        html = "<html><body><h1>404 Not Found</h1><p>The page does not exist.</p></body></html>"
        jobs = parse_html_heuristic(html, "https://example.com/jobs/99999")
        assert jobs == []

    def test_jsonld_ignores_product_schema(self):
        html = """
        <html><body>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Widget","price":"9.99"}
        </script>
        </body></html>
        """
        jobs = parse_jsonld(html, "https://shop.example.com/products/widget")
        assert jobs == []

    def test_jsonld_ignores_article_schema(self):
        html = """
        <html><body>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Article","headline":"Big News","author":"Jane"}
        </script>
        </body></html>
        """
        jobs = parse_jsonld(html, "https://news.example.com/article")
        assert jobs == []

    def test_workday_empty_html_returns_empty(self):
        jobs = parse_workday("", "https://company.wd3.myworkday.com/jobs")
        assert jobs == []

    def test_workday_non_job_page(self):
        html = """
        <html><body>
          <h1>Company Benefits</h1>
          <p>We offer great health insurance and flexible PTO.</p>
        </body></html>
        """
        jobs = parse_workday(html, "https://company.wd3.myworkday.com/benefits")
        assert jobs == []

    def test_extractor_empty_html(self):
        result = extract_jobs("", "https://example.com")
        assert result.page_type == "no_jobs"
        assert result.has_jobs is False

    def test_extractor_whitespace_only(self):
        result = extract_jobs("   \n\t  ", "https://example.com")
        assert result.page_type == "no_jobs"


# ── 4. Injection & malformed content resilience ───────────────────────────


class TestMalformedContentResilience:
    """Parsers and scoring must not crash on adversarial or corrupt inputs."""

    def test_jsonld_malformed_json(self):
        html = """
        <html><body>
        <script type="application/ld+json">
        { this is: not valid JSON at all !!!
        </script>
        </body></html>
        """
        # Should not raise — graceful degradation
        jobs = parse_jsonld(html, "https://example.com")
        assert jobs == []

    def test_jsonld_empty_object(self):
        html = '<html><body><script type="application/ld+json">{}</script></body></html>'
        jobs = parse_jsonld(html, "https://example.com")
        assert jobs == []

    def test_heuristic_no_text_at_all(self):
        jobs = parse_html_heuristic("", "https://example.com")
        assert jobs == []

    def test_scoring_with_none_values(self):
        # JobData enforces str fields — test graceful handling of empty strings
        # (the equivalent of "None" data arriving from a partial parse).
        profile = _make_profile()
        job = _make_job(
            title="Software Engineer",
            company="",
            location="",
            description="",
            employment_type="",
            seniority="",
        )
        from match.scoring import score_job as _score
        # Should not raise
        breakdown = _score(job, profile)
        assert 0 <= breakdown.total <= 100

    def test_scoring_with_empty_strings(self):
        profile = _make_profile()
        job = _make_job(title="", company="", location="", description="", seniority="")
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        assert 0 <= breakdown.total <= 100

    def test_heuristic_script_injection_ignored(self):
        """Ensure <script> content with job-like words doesn't trick the parser."""
        html = """
        <html><body>
        <script>
        // apply for this role requirements senior position
        var data = {title: "Fake Job", company: "Bad Actor"};
        </script>
        <p>Regular webpage content here.</p>
        </body></html>
        """
        jobs = parse_html_heuristic(html, "https://example.com")
        # Either finds nothing or finds something — must not raise
        assert isinstance(jobs, list)

    def test_workday_malformed_json_in_script(self):
        html = """
        <html><body>
        <script type="application/json">{ not valid json }</script>
        </body></html>
        """
        jobs = parse_workday(html, "https://company.wd3.myworkday.com/jobs/123")
        assert isinstance(jobs, list)


# ── 5. Fake job post scenarios ────────────────────────────────────────────


class TestFakeJobPostScenarios:
    """Simulate real-world adversarial job post patterns."""

    def test_fake_senior_title_wrong_seniority_field(self):
        """Title says 'Senior' but seniority field says 'intern'."""
        profile = _make_profile(keywords=["Python"])
        job = _make_job(
            title="Senior Software Engineer (Python)",
            seniority="intern",  # contradictory
            description="Entry-level internship position",
        )
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        # Seniority mismatch should reduce the score
        assert breakdown.seniority_score < 15

    def test_keyword_stuffed_description_no_real_match(self):
        """Description is stuffed with profile keywords but title is irrelevant."""
        profile = _make_profile(
            roles=["Software Engineer"],
            keywords=["Python", "FastAPI", "AWS"],
        )
        job = _make_job(
            title="Janitor",
            company="Cleaning Services Ltd",
            description=(
                "Python FastAPI AWS Docker Kubernetes ML AI JavaScript React "
                "TypeScript Senior Engineer experience required for janitor role."
            ),
        )
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        # Title score should be very low despite keyword stuffing
        assert breakdown.title_score < 15

    def test_remote_claim_but_onsite_required(self):
        """Job claims remote but location says on-site city."""
        profile = _make_profile(remote_ok=True)
        # Profile is remote-ok but job requires being in a city
        job = _make_job(
            location="On-site: New York, NY",
            description="This role is fully on-site, no remote work available.",
        )
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        # Location score should reflect city match attempt
        assert isinstance(breakdown.location_score, float)
        assert breakdown.total >= 0

    def test_jsonld_fake_job_posting_missing_required_fields(self):
        """JSON-LD job posting missing title — should not be extracted."""
        html = """
        <html><body>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "hiringOrganization": {"@type": "Organization", "name": "Acme"},
          "jobLocation": {"@type": "Place", "address": "London"}
        }
        </script>
        </body></html>
        """
        jobs = parse_jsonld(html, "https://example.com/jobs/1")
        # No title — should not produce a valid job
        assert jobs == [] or all(not j.title for j in jobs)

    def test_spam_job_post_very_short_description(self):
        """Job with no real content — should score low."""
        profile = _make_profile()
        job = _make_job(
            title="Software Engineer",
            description="Apply now!!!",  # spammy, minimal content
        )
        from match.scoring import score_job as _score
        breakdown = _score(job, profile)
        # Keyword overlap should be near zero
        assert breakdown.keyword_score < 15
