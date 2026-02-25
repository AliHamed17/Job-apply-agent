"""Tests for the matching and scoring engine."""

import pytest

from jobs.models import JobData
from match.scoring import (
    SKIP_THRESHOLD,
    AUTO_APPLY_THRESHOLD,
    Action,
    ScoreBreakdown,
    decide_action,
    score_job,
)
from profile.models import (
    CoverLetterConfig,
    Links,
    Personal,
    Preferences,
    Resume,
    SalaryPreference,
    UserProfile,
)


def _make_profile(**overrides) -> UserProfile:
    """Create a test profile with sensible defaults."""
    defaults = dict(
        personal=Personal(
            name="Jane Doe",
            email="jane@example.com",
            location="San Francisco, CA",
            work_authorization="US Citizen",
        ),
        resume=Resume(text="Python FastAPI Django PostgreSQL AWS Kubernetes"),
        preferences=Preferences(
            roles=["Software Engineer", "Backend Engineer"],
            locations=["San Francisco, CA", "Remote"],
            remote_ok=True,
            hybrid_ok=True,
            keywords=["python", "fastapi", "aws", "kubernetes", "backend"],
            blacklist_companies=["SpamCorp"],
            seniority=["mid", "senior"],
            salary=SalaryPreference(min=120000, max=200000, currency="USD"),
        ),
    )
    defaults.update(overrides)
    return UserProfile(**defaults)


def _make_job(**overrides) -> JobData:
    """Create a test job with sensible defaults."""
    defaults = dict(
        title="Senior Backend Engineer",
        company="GoodCorp",
        location="San Francisco, CA",
        employment_type="full-time",
        seniority="senior",
        description="Build APIs with Python and FastAPI on AWS.",
        requirements="3+ years Python, cloud experience.",
        apply_url="https://example.com/apply",
        source_url="https://example.com/job/123",
        keywords=["python", "fastapi", "aws"],
    )
    defaults.update(overrides)
    return JobData(**defaults)


class TestScoreJob:
    """Tests for the scoring function."""

    def test_perfect_match_high_score(self):
        profile = _make_profile()
        job = _make_job()
        breakdown = score_job(job, profile)
        assert breakdown.total >= 60  # should be high

    def test_no_match_low_score(self):
        profile = _make_profile(
            preferences=Preferences(
                roles=["Data Scientist"],
                locations=["Tokyo, Japan"],
                keywords=["machine learning", "tensorflow"],
                seniority=["junior"],
            )
        )
        job = _make_job(
            title="VP of Sales",
            location="London, UK",
            description="Drive B2B enterprise sales.",
        )
        breakdown = score_job(job, profile)
        assert breakdown.total < 30

    def test_blacklisted_company(self):
        profile = _make_profile()
        job = _make_job(company="SpamCorp")
        breakdown = score_job(job, profile)
        assert breakdown.total == 0
        assert breakdown.skip_reason is not None
        assert "blacklist" in breakdown.skip_reason.lower()

    def test_remote_job_matches_remote_preference(self):
        profile = _make_profile(
            preferences=Preferences(
                roles=["Engineer"],
                locations=["Remote"],
                remote_ok=True,
                keywords=["python"],
            )
        )
        job = _make_job(location="Remote")
        breakdown = score_job(job, profile)
        assert breakdown.location_score > 0

    def test_location_mismatch_zero_location_score(self):
        profile = _make_profile(
            preferences=Preferences(
                roles=["Engineer"],
                locations=["Tokyo, Japan"],
                remote_ok=False,
                keywords=["python"],
            )
        )
        job = _make_job(location="Berlin, Germany")
        breakdown = score_job(job, profile)
        assert breakdown.location_score == 0

    def test_seniority_match(self):
        profile = _make_profile()
        job = _make_job(seniority="senior")
        breakdown = score_job(job, profile)
        assert breakdown.seniority_score > 0

    def test_seniority_mismatch_low_score(self):
        profile = _make_profile(
            preferences=Preferences(
                roles=["Engineer"],
                locations=["SF"],
                keywords=["python"],
                seniority=["intern"],
            )
        )
        job = _make_job(seniority="director")
        breakdown = score_job(job, profile)
        assert breakdown.seniority_score < 15  # less than full

    def test_keyword_overlap_contributes_to_score(self):
        profile = _make_profile()
        job = _make_job(
            description="Python FastAPI AWS Kubernetes microservices backend",
            keywords=["python", "fastapi", "aws", "kubernetes"],
        )
        breakdown = score_job(job, profile)
        assert breakdown.keyword_score > 10

    def test_no_keywords_in_profile_neutral(self):
        profile = _make_profile(
            preferences=Preferences(roles=["Engineer"], keywords=[])
        )
        job = _make_job()
        breakdown = score_job(job, profile)
        # Should get neutral score (50% of weight)
        assert breakdown.keyword_score > 0

    def test_score_is_bounded(self):
        profile = _make_profile()
        job = _make_job()
        breakdown = score_job(job, profile)
        assert 0 <= breakdown.total <= 100


class TestDecideAction:
    """Tests for the action decision logic."""

    def test_skip_below_threshold(self):
        action = decide_action(score=10, draft_only=True)
        assert action == Action.SKIP

    def test_draft_when_draft_only(self):
        action = decide_action(score=50, draft_only=True)
        assert action == Action.DRAFT

    def test_draft_default_mode(self):
        action = decide_action(score=50, draft_only=True, auto_apply_enabled=False)
        assert action == Action.DRAFT

    def test_auto_apply_high_score(self):
        action = decide_action(
            score=90, draft_only=False, auto_apply_enabled=True
        )
        assert action == Action.AUTO_APPLY

    def test_draft_when_auto_disabled(self):
        action = decide_action(
            score=90, draft_only=False, auto_apply_enabled=False
        )
        assert action == Action.DRAFT

    def test_skip_with_reason(self):
        action = decide_action(score=80, skip_reason="Blacklisted")
        assert action == Action.SKIP

    def test_draft_medium_score_auto_enabled(self):
        action = decide_action(
            score=50, draft_only=False, auto_apply_enabled=True
        )
        assert action == Action.DRAFT  # below auto-apply threshold

    def test_skip_threshold_boundary(self):
        # Exactly at threshold should NOT skip
        action = decide_action(score=SKIP_THRESHOLD, draft_only=True)
        assert action == Action.DRAFT

    def test_below_skip_threshold(self):
        action = decide_action(score=SKIP_THRESHOLD - 1, draft_only=True)
        assert action == Action.SKIP


class TestScoreBreakdown:
    """Tests for the ScoreBreakdown dataclass."""

    def test_compute_total_bounds(self):
        b = ScoreBreakdown(
            title_score=25, keyword_score=25, location_score=20,
            seniority_score=15, employment_score=10, salary_bonus=10,
        )
        total = b.compute_total()
        assert total == 100  # capped at 100

    def test_blacklist_penalty_zeroes_score(self):
        b = ScoreBreakdown(
            title_score=25, keyword_score=25, blacklist_penalty=100
        )
        total = b.compute_total()
        assert total == 0
